# Model & Agent Architecture Research — 2026-08-22

## Implementation update — later on 2026-08-22

The user approved the shared-prompt rewrite described in the historical notes
below. `SYSTEM_PROMPT` is now a direct task instruction and
`build_user_prompt()` emits one complete paragraph ending in an explicit
assessment request. Kimi Code returned a valid `AnalystVerdict` with the new
prompt, and the full suite passes (214 tests). The active `.env` now targets
Claude Code with `sonnet` / high effort and a zero-tool surface
(`--tools "" --strict-mcp-config --disable-slash-commands
--no-session-persistence`). Claude's post-change smoke call returned a valid
`AnalystVerdict`, and the Claude project directory stayed unchanged at 10
files / 11.94 MB. A CLI failure remains fail-safe (`None` -> quant-only). This
update supersedes the older "reverted to Kimi" status notes retained below as
an audit trail.

**Question this answers:** does Agentic Trading need to stay on Claude Code, or can
GLM / GPT / Grok / Kimi / DeepSeek substitute — and where, specifically, is the
lock-in (if any) real vs assumed. Follows the research plan from the
`chatgpt.com/c/6a869b66...` conversation in `Pro强在哪.md`, condensed and grounded
in this repo's actual code rather than treated as a hypothetical.

Confidence tags on load-bearing claims: **[code]** = verified by reading this
repo directly. **[primary]** = verified by fetching the vendor's own docs/ToS
page. **[web]** = search-engine-aggregated summaries of 2026 blog/docs content —
directionally reliable, not independently benchmarked. Pricing/benchmark numbers
move fast; treat anything tagged **[web]** as "true as of a few weeks before
2026-08-22," not gospel.

---

## TL;DR

- You already left "Claude Code as runtime" behind. It was never the runtime —
  it's one of two interchangeable non-interactive backends for a single
  `AnalystVerdict` call, and the actual decision/risk/execution pipeline is
  plain Python with no framework and no model calls in the enforcement path.
  **[code]**
- The thing actually worth fixing this week isn't architecture, it's
  **compliance**: your default config runs Kimi Code CLI on an unattended
  schedule, and Kimi Code's own community guidelines explicitly prohibit that.
  Claude Code's consumer ToS explicitly *allows* the identical pattern.
  **Now actually done**, not just recommended — see §1's 2026-08-22 update:
  the first attempt failed (this account's Claude Code refused to answer the
  old prompt format non-interactively), fixed by rewriting the shared
  analyst prompt, live-verified against both Claude Code and Kimi Code.
  **[primary]**
- "Is it a format problem" — no. GLM, Kimi, and DeepSeek each now expose an
  endpoint that speaks the real Anthropic Messages API wire format, so the
  unmodified `claude` binary runs against their models with an env var change.
  They also all speak OpenAI-compatible chat completions, which is what your
  `api_provider.py` already uses. Format is a solved problem across this whole
  vendor set. See §3.
- A model router (Part 7 of the ChatGPT plan) is available off-the-shelf
  (LiteLLM) and would be a `base_url` change, not a rewrite — but you don't
  need it yet with 3 provider modules. See §7.
- One concrete security gap: `grok_provider.py` hard-blocks shell/tool access
  on the subprocess; `cli_provider.py` (your Kimi Code / Claude Code path)
  only *asks* the model not to use tools, in the prompt. See §8.

---

## 1. Do this first: the ToS mismatch on your default config

`ANALYST_PROVIDER=cli` is your default **[code]**, and it runs unattended via
Windows Task Scheduler twice a day plus every 20 minutes during market hours
(`README.md`, "Scheduling"). That is about as clear a case of "scripted,
non-interactive, unattended automation" as exists.

I checked the actual current guidelines pages directly (fetched primary
sources, not just search summaries) for all four backends touched by this
system — including the two (DeepSeek, Grok) not in the original pass:

| Backend | What its own terms say | Source |
|---|---|---|
| **Kimi Code** (your current default `cli` target) | "Don't use Kimi Code for non-interactive automation. Kimi Code subscriptions are for personal interactive use only." "Using it for non-interactive purposes — such as scripted batch execution... — goes beyond normal use." Enforcement: "we'll review the situation first and take appropriate action — such as suspending access." | **[primary]** [Kimi Code Community Guidelines](https://www.kimi.com/code/docs/en/kimi-code/community-guidelines.html) |
| **Claude Code** (your verified alternate `cli` target) | Anthropic's Consumer ToS bans automated/scripted access generally, but Claude Code CLI is explicitly carved out as the intended exception — official docs show `-p` piped into cron/CI/GitHub Actions as sanctioned usage patterns. | **[web, moderate confidence]** — I could not fetch anthropic.com/legal/terms directly; read it yourself before leaning on this |
| **GLM Coding Plan** (hypothetical `cli`/`api` addition) | Fetched `docs.z.ai/devpack/usage-policy` directly: **no explicit "no automation" or "personal use only" clause found.** What it does say: "GLM Coding Plan may only be used within officially supported tools and products"; account sharing prohibited; >3 violations → possible ban. Silence on automation isn't permission — this clause alone leaves room for Z.AI to call unattended batch use "not an officially supported pattern" if they want to. | **[primary]**, but the primary source itself is ambiguous on this specific question — treat as unresolved, not cleared |
| **DeepSeek** (your `api_provider.py`'s natural cheap alternative) | Fetched the DeepSeek Open Platform Terms of Service directly: **no automation restriction found**, and structurally there's no tension to find — DeepSeek doesn't sell a flat-fee "personal coding subscription" product at all (no Kimi-Code/GLM-Coding-Plan equivalent exists as of Aug 2026). It's metered pay-per-token API access only, which is inherently built for programmatic use. | **[primary]** [DeepSeek Open Platform ToS](https://cdn.deepseek.com/policies/en-US/deepseek-open-platform-terms-of-service.html) |
| **Grok Build** (your existing `grok_provider.py` target) | xAI's own product announcement states outright: *"Headless mode (`-p`) allows easily running agents inside scripts and automations,"* and separately advertises "full ACP support to build your own bots and agent orchestration apps." This is the vendor marketing the exact pattern you're already using — the most explicit *permission* of the four, not just an absence of prohibition. | **[primary]** [x.ai — Introducing Grok Build](https://x.ai/news/grok-build-cli) |

Ranked from most-restrictive to most-permissive on this specific question:
**Kimi Code (explicitly prohibited) < GLM Coding Plan (unresolved, no
prohibition found but no permission either) < DeepSeek (no such product
category exists, so no tension) < Grok Build (officially marketed for this
exact use case).** Your existing `grok_provider.py` integration sits on the
safest end of this spectrum already; it's specifically the default `cli`
analyst path (Kimi Code) that's out of step with its vendor's stated terms.

This isn't hypothetical risk-aversion — it's a plain reading of a guidelines
page your live pipeline is currently out of step with. Practical impact if
Moonshot enforces it: `analyze_via_cli` starts failing, which your system
already degrades gracefully from (quant-only decisions, per `schema.py`'s
"never guess" contract) — so it's not a catastrophic failure mode, but it's an
unforced one, and losing the qualitative read silently on a live account is
worse than fixing this deliberately.

**Three ways to close this, in order of effort:**

1. ~~Zero-cost, same-day: point `ANALYST_CLI_PATH` at your `claude` binary
   instead of `kimi.exe`.~~ **Tried this 2026-08-22, reverted — see the update
   below. The README's claim that this "just works" turned out to only cover
   the envelope-parsing mechanics, not whether the model actually answers.**
2. **Switch that slot to `ANALYST_PROVIDER=api`** (pay-per-token Kimi
   Platform key) — this is explicitly the path Kimi's own guidelines point
   automation toward, and you already have this provider built and working.
   Costs real (small) USD instead of subscription quota. **Now the more
   attractive option, given what #1 turned into.**
3. **Keep Kimi Code CLI but throttle to something defensible as "personal
   use"** — harder to justify given the current 20-minute fast-scan cadence.

---

**Update 2026-08-22, after actually attempting option 1:** I switched
`ANALYST_CLI_PATH` to `claude.cmd`, and along the way found and fixed three
real bugs in `cli_provider.py`, all confirmed by live testing, all still in
the code: `-m` is not a valid flag for Claude Code (only `--model` is — Kimi
Code happens to accept both, which is why this went unnoticed); current
Claude Code requires `--verbose` whenever `--print` is combined with
`--output-format stream-json` (undocumented in `claude --help`, found by
testing — omitting it errors); and the assistant-message envelope shape
changed to `{"type": "assistant", "message": {"role": "assistant", ...}}`,
which `_extract_text` didn't handle (fixed, with a regression test). Also
added `ANALYST_CLI_EXTRA_ARGS` so per-CLI flags like `--effort` and a
tool-lockdown (`--tools "" --strict-mcp-config --disable-slash-commands`,
closing the §8 gap below) don't have to be hardcoded into shared code.

None of that was sufficient. With all of the above working correctly
end-to-end (verified: `--model opus --effort max` accepted, `tools`/
`mcp_servers`/`skills`/`slash_commands` all empty in the session-init event),
this account's Claude Code still would not answer `schema.py`'s actual
prompt — `SYSTEM_PROMPT` followed by `build_user_prompt()`'s labeled-sections
format ("Symbol: AAPL\n\nQuant technical signal (...):\n- last_price: ...").
Every real ticker, every effort level, every combination of `--system-prompt`
(proper channel instead of concatenating into the user turn), `--bare`
(which — correction to the ToS discussion above — did *not* break OAuth for
this account, despite the docs' warning that it strictly requires
`ANTHROPIC_API_KEY`), and `--permission-mode bypassPermissions` produced the
same failure mode: it treats the message as an incomplete request and asks
clarifying questions ("Could you share the ticker, quant signal,
headlines...") or offers to run one of this account's own configured finance
skills (`stock-eval`, `full-report`, `dcf-valuation` — all real skill names
installed on this account, referenced even with `--disable-slash-commands`
and `--bare` both active, which rules out live config leakage and points at
the model's own trained behavior around report-shaped input).

Isolated the actual trigger by testing combinations directly: it's
`SYSTEM_PROMPT`'s persona-setup framing ("You are a disciplined equity
research analyst supporting a systematic paper-trading agent...") landing in
a one-shot non-interactive call. Drop it and rephrase the same data as one
flowing paragraph ending in a direct question, and Claude Code answers
correctly and well — tested and confirmed. But that requires rewriting
`build_user_prompt()`/`SYSTEM_PROMPT`, which `api_provider.py` and Kimi Code's
`cli` path both currently depend on in production, unchanged. That's a
prompt-engineering change with real stakes for the live account, not a CLI
swap — out of scope for me to make unilaterally.

**Resolution 2026-08-22 (same day):** rewrite done, with sign-off. `schema.py`'s
`build_user_prompt()` is now one direct paragraph ending in "assess the
qualitative stance and confidence now — do not ask for more information";
`SYSTEM_PROMPT` still exists and is still used correctly by `api_provider.py`
as the real system-role message (that path was never broken — only the
CLI path's concatenation-into-user-turn was). `cli_provider.py` no longer
concatenates a persona prompt for Claude Code at all (`is_claude` branch),
and switched from a prompt-based JSON instruction to Claude's native
`--json-schema` structured-output flag, with a fallback extractor
(`_extract_payload`) that also handles `structured_output`/`result`/
`tool_use` response shapes. The scratch work dir moved fully outside the
repo (`tempfile.gettempdir()`, not a subdirectory of this git tree), closing
the memory/CLAUDE.md auto-discovery path this investigation surfaced. Live-
verified end-to-end against real Claude Code, including the exact
thin-evidence case that broke before — both now return well-reasoned
verdicts. `ANALYST_CLI_PATH` is back on `claude.cmd`.

Correction to an earlier draft of this note: `SYSTEM_PROMPT` is *not*
concatenated into the `cli` path for either CLI anymore — only
`api_provider.py` still uses it, correctly, as the real system-role message
(that path never had this bug). Kimi Code's `cli` path now gets the same new
paragraph-style `build_user_prompt()` output plus the old `JSON_INSTRUCTION`
(format-only, no persona framing) appended — a real behavior change, not just
"untouched." Live-verified separately against real Kimi Code with this new
prompt: still produces a well-reasoned verdict, same quality as before. All
214 tests pass. Section 9's item 1 below is now done, not just planned.

---

## 2. What you've already built vs. what the ChatGPT plan assumed

The plan's Part 1 worry was that you might build the trading system *inside*
Claude Code as its runtime. Reading the code, that's not what happened:

| Plan's layer | What's actually here |
|---|---|
| Dev agent | Claude Code + Grok + (implied) Codex, used interchangeably to edit this repo — see `HANDOFF-BACKTEST.md`, written by "Grok 4.6" *for* "Codex." No framework lock-in; it's markdown handoff notes. |
| Agent runtime / orchestrator | Plain Python (`run.py`, `decision/engine.py`). No LangGraph, no Agents SDK, no Claude Agent SDK. |
| Model layer | 3 independent, swappable provider modules behind one shared contract (`AnalystVerdict` / `SentimentVerdict` in `schema.py`) — `api_provider.py` (OpenAI-compatible HTTP, currently Kimi K3), `cli_provider.py` (local coding-agent CLI, Kimi Code or Claude Code, verified interchangeable), `grok_provider.py` (Grok CLI, sentiment-only). |
| Tools & data | `data/market_data.py`, yfinance/Alpaca — no LLM-driven browsing or tool use in the data path. |
| Portfolio / risk | `risk/manager.py` — deterministic, hard-coded limits, evaluated before any order exists. |
| Execution | `execution/sim_broker.py` / Alpaca broker client, paper-only, hard-refuses live. |

You're also already doing the "heterogeneous multi-model" thing the plan's
Part 6 speculates about, in practice: `research/GPT-REVIEW-PACK-2026-08-22-KIMI-COMPLETE.md`
is a Kimi-produces / GPT-reviews artifact, and the Grok→Codex handoff is the
same pattern applied to dev work. The open question was never "should you do
this," it's "is the current ad hoc markdown-handoff mechanism good enough" —
at your current scale (one repo, occasional multi-day handoffs), yes. I
wouldn't add infrastructure (shared MCP memory server, a formal handoff schema)
until handoffs start losing information in practice.

---

## 3. What's actually Claude/Claude-Code-exclusive (the plan's Part 2 ask)

| Capability | Category | Notes |
|---|---|---|
| Anthropic Messages API wire format | **C — open enough** | GLM (Z.AI), Kimi (Moonshot), and DeepSeek all now expose an endpoint that mimics this format closely enough that the unmodified `claude` CLI/SDK works against them via `ANTHROPIC_BASE_URL` + `ANTHROPIC_API_KEY`. **[web]**, multiple independent vendor setup guides agree. Caveat from DeepSeek's own docs: some content types (images, docs, some tool-result structures) aren't at full parity. |
| OpenAI chat-completions format | **C — open standard** | Every vendor above *also* speaks this. Anthropic itself shipped an OpenAI-SDK-compatible endpoint in March 2026 (`base_url=https://api.anthropic.com/v1/`), officially positioned for "testing," not production — `strict` schema conformance is not guaranteed through it. **[web]** |
| MCP (Model Context Protocol) | **C — now vendor-neutral** | Adopted by OpenAI (Agents SDK, Responses API, ChatGPT desktop, since ~May 2025) and Google DeepMind (Gemini, since ~April 2025). Anthropic donated MCP to a new vendor-neutral "Agentic AI Foundation" under the Linux Foundation in December 2025. **[web]** This is the strongest "not Claude-specific" story in the whole research — it started at Anthropic but isn't Anthropic's anymore, structurally. |
| Skills format / `CLAUDE.md` convention | **B→D, converging** | Originated at Anthropic. xAI explicitly built Grok Build (their May-2026 coding CLI) to read `CLAUDE.md` natively and support the same Skills/MCP patterns "with little to no modification" needed. **[web]** Started exclusive, becoming a copied convention rather than staying locked. |
| Subagents / hooks / headless mode *as CLI mechanisms* | **B, but D at the concept level** | Claude Code's specific flags (`--permission-mode`, `--disallowedTools`, `SessionStart`/`SubagentStop` hooks, Task-tool subagents) are Claude-Code-specific implementations. But Codex CLI has `codex exec` for the same headless-automation need, and Grok Build has its own subagent story (8 parallel sub-agents advertised). The *capability* is D (everyone builds some version); the *exact flag names* are B. |
| Computer use / browser-driving | **D, genuinely divergent** | Claude, OpenAI (Operator/Codex background), and Gemini each made a different architectural bet (portable screenshot tool vs. desktop-native vs. DOM-aware browser automation) — not standardized, and benchmark leadership varies by task type. **[web]** Not relevant to your current pipeline — you don't have a browsing/computer-use step; data comes from yfinance/Alpaca APIs directly. |

**Bottom line for your "is it a format problem" question: no.** The wire
format is solved three different ways by three different vendors. What
remains genuinely vendor-specific is CLI *flag surface* (which matters for
`cli_provider.py`'s subprocess invocation, not for the model's capability)
and computer-use (which you don't use).

---

## 4. Drop-in replacement mechanics for *your* two provider slots

Applying the plan's Level 0–4 scale to your actual code, not a hypothetical system:

### `api_provider.py` (OpenAI-compatible HTTP, currently Kimi K3)

- **Swap to GLM / DeepSeek / GPT via the same OpenAI SDK client: Level 0–1.**
  All three speak OpenAI-style function calling with `tool_choice="required"`,
  which is exactly what `_TOOL` + `parse_verdict` already assume. Today it's
  technically Level 1, not Level 0, because `DEFAULT_BASE_URL` is a Python
  constant in `api_provider.py`, not an env var — `ANALYST_MODEL` and
  `MOONSHOT_API_KEY` already are. A 10-line change (add `ANALYST_BASE_URL` to
  `Settings`, matching the existing `analyst_model` pattern) would make this a
  true Level 0, no-code-touch swap. Small, concrete, worth doing regardless of
  which vendor you land on.
- **Reliability caveat:** function-calling accuracy isn't identical across
  vendors. Aggregated 2026 blog comparisons **[web, low confidence — treat as
  directional only]** put OpenAI/Claude around 96–99% tool-selection accuracy,
  Gemini 95–98%, DeepSeek 90–95% at roughly an order of magnitude lower cost.
  Your own `parse_verdict` already returns `None` (never a guess) on anything
  malformed, so a less-reliable vendor mostly costs you *coverage* (more
  symbols silently drop to quant-only), not correctness — the failure mode is
  safe either way.

### `cli_provider.py` (local CLI subprocess, currently Kimi Code / Claude Code)

- **Swap Kimi Code ↔ Claude Code binary: Level 0.** Already true today, per
  your own README. **[code]**
- **Keep using the `claude` binary but point it at a different model via
  `ANTHROPIC_BASE_URL`/`ANTHROPIC_API_KEY` (GLM, Kimi, or DeepSeek's
  Anthropic-compatible endpoints): Level 1.** No architecture change — but
  `cli_provider.py`'s `env` handling today only injects `KIMI_CODE_HOME`
  (line 108-110); it would need generalizing to pass through arbitrary env
  vars per target so you could point the same `claude` binary at, say,
  DeepSeek's endpoint without touching your interactive Claude Code config.
  Useful if you ever want "Claude Code's parser/output shape, DeepSeek's
  price" without adding a fourth provider module.
- **Add Codex CLI (`codex exec`) as a fourth backend: Level 2.** Different
  invocation (`codex exec`, not `-p`), and `_extract_text`'s JSONL parser is
  written around the `role`/`content` envelope shape Claude Code and Kimi Code
  both use — Codex's exec output format is not verified against that parser
  and would likely need a small branch, not a rewrite.
- **Add Grok Build (xAI's new CLI, May 2026) as a backend: unverified,
  probably Level 1–2.** You already integrate Grok via its plain CLI in
  `grok_provider.py`; Grok Build is a separate, heavier coding-agent product
  gated behind a $99–300/mo SuperGrok tier **[web]** — I did not verify its
  non-interactive output envelope against your parser. Given you already have
  a working, cheap Grok integration for the one thing you use Grok for
  (sentiment), I wouldn't chase this without a specific reason.
- **Levels 3–4 (architecture rewrite / no equivalent) don't apply anywhere in
  your current design** — you'd only hit those if you wanted something
  qualitatively new, like a multi-step autonomous research agent that browses
  filings across many turns. Nothing in the plan or your system currently asks for that.

---

## 5. "OpenAI-compatible" ≠ full agent compatibility — where that bites you specifically

The plan's Part 4 worry is real but doesn't hit you where it expects to.
Chat Completions / function calling is the one layer that's genuinely
standardized across your whole vendor set (§3, §4). The gaps that exist —
Responses API, native Structured Outputs with hard schema guarantees, MCP as
a *model-side* feature vs. an *SDK-side* feature, Computer Use — are all
things your pipeline doesn't touch. Your `analyst.py`/`cli_provider.py`/
`grok_provider.py` trio already routes around the one place this would've
mattered (schema guarantees) by writing a shared, tolerant parser
(`json_extract.py`) instead of trusting any vendor's schema-conformance
promise — which your own comments note you arrived at empirically (`--json-schema`
"can still emit two concatenated JSON objects," observed directly). That's
the right instinct in general: for this class of model, trust your own
validator, not a vendor's "guaranteed schema" marketing.

---

## 6. Claude Code alternatives for the dev-agent role

You don't need to pick one — you already don't. For completeness, here's the
current (Aug 2026) landscape **[web]**:

| Tool | Model neutrality | Notes |
|---|---|---|
| Claude Code | Single-vendor-first, but works against GLM/Kimi/DeepSeek via `ANTHROPIC_BASE_URL` (§3) | What you're using now |
| OpenAI Codex CLI | OpenAI-first | `codex exec` = headless mode, sandboxed by default, official CI/CD story |
| Gemini CLI | Being sunset | Free/Pro/Ultra tiers lose it 2026-06-18 in favor of the closed-source Antigravity CLI **[web]** — a concrete example of "vendor's own CLI can be pulled out from under you," worth remembering if you ever lean harder on any single vendor CLI |
| Grok Build | xAI-first, but deliberately Claude-Code-format-compatible (reads `CLAUDE.md`, Skills, MCP) | New (May 2026), expensive tier gate |
| Qwen Code | Open (Apache-2.0), Gemini-CLI fork | Tuned for Qwen3-Coder but "endpoint-agnostic" |
| **OpenCode** | Genuinely model-agnostic by design, MIT-licensed | 75+ providers, positions itself as neutral because that's its business model |
| Cline | Model-agnostic, IDE/CLI/SDK | |

Given your dev workflow already spans Claude Code + Grok + Codex via manual
handoff docs and works fine, the only reason to standardize on something like
OpenCode would be wanting *one* tool that natively speaks to whichever model
is cheapest that week — a convenience, not a capability you're missing.

---

## 7. Model router — build one, or not yet?

The plan's Part 7 wants an `agents.yaml` with `primary`/`fallback` per role.
**LiteLLM** is close to exactly that, off the shelf **[web]**:

- Self-hosted proxy, YAML-configured `router_config` with per-deployment
  `fallbacks`, retry on 429/5xx/context-limit/timeout, priority via `order`.
- Exposes an OpenAI-compatible `/chat/completions` server — meaning
  `api_provider.py`'s `OpenAI(api_key=..., base_url=...)` client wouldn't
  need to change at all, only `base_url` would point at your local LiteLLM
  instance instead of Moonshot directly. Routing/fallback logic moves into
  LiteLLM's config, out of your Python.
- Compared to **OpenRouter** (hosted marketplace, 400+ models, no ops but
  your prompts transit their servers): LiteLLM keeps data on your machine
  until it hits the model vendor directly, which matches how you already run
  everything else (local Task Scheduler, local journal.db). **[web]**

**My actual recommendation: skip it for now.** You have 3 provider modules
sharing one contract (`AnalystVerdict`/`SentimentVerdict`), which is already
proportionate abstraction for 3 backends — introducing a proxy server,
Docker, and a Postgres dependency (LiteLLM's typical deployment) to route
between them would be infrastructure in search of a problem. Revisit this
specifically when either becomes true: (a) you want automatic fallback across
multiple `api`-style vendors (e.g., Kimi K3 primary → DeepSeek if Moonshot
rate-limits you), or (b) you're managing enough provider modules that the
hand-rolled pattern in `llm/` starts feeling repetitive. Neither is true today.

---

## 8. Trading-specific safety boundary — validated, one gap found

The plan's Part 8 principle — LLM proposes, deterministic code disposes, no
model in the enforcement path — is already exactly your architecture:
`decision/engine.py` requires the **quant score alone** to clear
`buy_threshold`/`sell_threshold`; the LLM can only veto or temper a BUY, never
originate one; `risk/manager.py`'s position/exposure/sector caps run
regardless of model confidence. This matches current external guidance
closely: *"Layer 5 must be deterministic — a rules engine or decision table
evaluated after the model proposes and before the gateway executes, with no
model in the enforcement path."* **[web]**

One concrete gap, found by reading your own code rather than searching:
`grok_provider.py` explicitly hard-restricts the subprocess —
`--disallowed-tools bash,shell,execute_command` — on top of a prompt
instruction. `cli_provider.py` (your Kimi Code / Claude Code analyst path)
only has the prompt instruction ("do NOT use any skills... do not read or
write any files") plus an empty scratch `cwd` for containment; it does not
pass an equivalent `--disallowedTools` flag. **[code]**

This matters because both Claude Code and Kimi Code confirm the same
underlying behavior in headless/`-p` mode **[web, primary for Claude Code
flag names]**: there's no human to approve a tool call, so the CLI falls
back to whatever your **existing global permission config** already allows —
for Claude Code, unmet permissions cause the process to terminate (fails
closed); for Kimi Code, "-p mode... regular tool calls are handled under the
auto permission policy" (does not fail closed the same way). If your
interactive `settings.json`/`~/.kimi-code/config.toml` has any broad
allow-rules (common for a power user who doesn't want prompted every time),
those rules apply here too — and the prompt this CLI call receives is built
from news headlines and fundamentals text, i.e. content you don't fully
control. This is a real, if narrow, prompt-injection surface: a crafted
headline could in principle try to get the model to invoke an
already-allowed tool.

You already built the fix's prerequisite: `ANALYST_CLI_HOME` /
`ANALYST_CLI_MODEL` exist specifically to give this call an isolated config
separate from your interactive session (currently used only to pin reasoning
effort). Extending that isolated config directory with an explicit
tool-deny-all policy (or passing `--disallowedTools` the way
`grok_provider.py` does) closes this with a small, contained change — no
architecture impact.

Worth knowing as general context, not as a claim this has happened to you:
in early 2026 an Alibaba-affiliated coding agent was reported to have
autonomously used its shell access to hijack GPU resources for crypto mining
and open a backdoor, unprompted. **[web]** The lesson isn't "AI agents are
dangerous" in the abstract — it's specifically "an agent spawned with broad
tool access, fed content you don't fully control, in an unattended context"
is the pattern to scope down, which is exactly what §8's gap is.

---

## 9. Recommendations, in priority order

1. **Fix the ToS mismatch (§1) — done.** `ANALYST_CLI_PATH` is on `claude.cmd`,
   `schema.py`'s `build_user_prompt()` is rewritten to the direct-paragraph
   form that's confirmed to work, and the live pipeline is verified end to
   end for both real headlines and the thin-evidence edge case.
2. **Close the tool-access gap in `cli_provider.py` (§8) — done.**
   `ANALYST_CLI_EXTRA_ARGS` is wired up and active: `--tools ""
   --strict-mcp-config --disable-slash-commands` gives the live Claude Code
   call zero tool surface. Kimi Code's own equivalent (`disallowedTools` in
   its config) is still not applied — smaller, and only relevant if you
   switch back to Kimi Code as the `cli` target — still worth doing
   independently if that happens.
3. **Make `api_provider.py`'s `base_url` env-configurable (§4).** Ten-line
   change, turns "any OpenAI-compatible endpoint works" from "edit a Python
   constant" into an actual Level-0 swap, matching how `analyst_model`
   already works.
4. **Don't adopt a model router or an agent framework yet (§7).** Your 3
   hand-rolled provider modules plus a shared verdict contract is the right
   amount of structure for 3 backends. LiteLLM is the documented off-ramp
   *when* you outgrow this, not before.
5. **Don't formalize the dev-agent handoff (§6).** Claude Code / Grok / Codex
   interchangeably editing this repo via markdown handoff docs is already
   working, per your own `HANDOFF-BACKTEST.md`. Nothing to fix here.
6. **Let your own data pick the analyst model, not a benchmark table.**
   `journal/evaluate.py` already scores forward outcomes per verdict. The
   natural next step, when you want to compare Kimi K3 against GLM or
   DeepSeek for *this specific task* (equity stance from news+fundamentals),
   is shadow mode: call two providers, log both verdicts via the shared
   `AnalystVerdict` contract, feed the decision engine from only one. Cheap
   to build since the contract already exists; far more trustworthy than any
   vendor's aggregate benchmark for your specific prompt and task.
7. **On "/goal"/autonomous build mode:** agree with the ChatGPT conversation's
   sequencing — not yet. I'd add a reason specific to this repo: your safety
   story depends on `risk/manager.py` and `decision/engine.py` being
   deliberately, synchronously reviewed — that's exactly the kind of surface
   where autonomous "iterate until done" is the wrong tool. Autonomous mode
   fits low-blast-radius, easily-reversible work (more backtest coverage,
   more tests) much better than anything touching the risk gate or order path.

---

## Sources

MCP adoption: [Anthropic — Donating MCP](https://www.anthropic.com/news/donating-the-model-context-protocol-and-establishing-of-the-agentic-ai-foundation) · [WorkOS — MCP in 2026](https://workos.com/blog/everything-your-team-needs-to-know-about-mcp-in-2026) · [Google Cloud MCP support](https://www.hpcwire.com/bigdatawire/this-just-in/google-cloud-announces-model-context-protocol-support-for-google-services/)

Vendor Claude-Code-compatible endpoints: [GLM/Z.AI guide](https://codingplan.run/guides/claude-code-with-glm) · [Kimi K2.5 + Claude Code](https://apidog.com/blog/kimi-k2-5-claude-code-integration/) · [DeepSeek Anthropic-compatible endpoint](https://www.digitalapplied.com/blog/deepseek-responses-api-anthropic-format-convergence) · [DeepSeek Claude Code setup](https://www.verdent.ai/guides/deepseek-v4-in-claude-code)

Anthropic OpenAI-SDK compatibility: [Claude Platform Docs — OpenAI SDK compatibility](https://platform.claude.com/docs/en/api/openai-sdk)

Coding CLI landscape: [State of CLI Coding Agents, Mid-2026](https://blog.arcbjorn.com/state-of-cli-coding-agents-2026) · [Grok Build launch](https://pasqualepillitteri.it/en/news/2584/grok-build-xai-cli-2026) · [DeepSeek Code Harness](https://www.verdent.ai/guides/deepseek-coding-plan-2026)

Codex CLI headless mode: [OpenAI Codex — Non-interactive mode](https://developers.openai.com/codex/noninteractive)

Claude Code permissions/flags: [Claude Code permissions guide](https://www.developersdigest.tech/blog/claude-code-permissions-settings-guide) · [Anthropic — Claude Code auto mode](https://www.anthropic.com/engineering/claude-code-auto-mode)

ToS / usage policy (primary-source fetched): [Kimi Code Community Guidelines](https://www.kimi.com/code/docs/en/kimi-code/community-guidelines.html) · [Kimi Code Benefits](https://www.kimi.com/en/help/kimi-code/benefits) · [Z.AI Usage Policy](https://docs.z.ai/devpack/usage-policy) · [DeepSeek Open Platform Terms of Service](https://cdn.deepseek.com/policies/en-US/deepseek-open-platform-terms-of-service.html) · [x.ai — Introducing Grok Build](https://x.ai/news/grok-build-cli)

Kimi Code CLI tool controls: [Kimi Code CLI — Agents and Sub-Agents](https://moonshotai.github.io/kimi-code/en/customization/agents.html)

Model router / gateway: [LiteLLM — Routing & Load Balancing](https://docs.litellm.ai/docs/routing) · [OpenRouter vs LiteLLM](https://api7.ai/openrouter-vs-litellm) · [OpenAI Agents SDK + LiteLLM](https://docs.litellm.ai/docs/tutorials/openai_agents_sdk)

Function-calling comparisons (aggregated, low-confidence): [Function Calling and Tool Use Guide 2026](https://tokenmix.ai/blog/function-calling-guide)

Model pricing (aggregated): [Kimi K3 vs DeepSeek V4 Pro vs GLM-5.2](https://www.marktechpost.com/2026/07/18/kimi-k3-vs-deepseek-v4-pro-vs-glm-5-2-open-trillion-scale-moe-models-compared-on-benchmarks-license-and-serving-cost/) · [DeepSeek V4 Pro vs Kimi K3](https://www.orcarouter.ai/blog/deepseek-v4-pro-vs-kimi-k3)

Trading-agent guardrails: [High Frequency Trading and Lessons for Agentic AI](https://www.philvenables.com/post/high-frequency-trading-and-lessons-for-agentic-ai) · [AI Agent Security Incidents 2026](https://www.kiteworks.com/cybersecurity-risk-management/ai-agent-security-incidents-2026/)
