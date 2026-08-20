# Agentic Trading

A US-equities paper-trading agent: technical (quant) signals combined with
Kimi K3's qualitative read on news/fundamentals, run through hard risk limits,
executed autonomously on Alpaca's **paper** trading account. Long-only.

It does place orders without asking — but only ever against a paper account, on
simulated money. The code refuses to construct a live broker client at all; see
[Safety](#safety).

## How a cycle works

Once per cycle, before any symbol is looked at:

0. **Market regime** (`signals/macro.py`) — scores the tape from five
   cross-asset ratios (RSP/SPY breadth, HYG/LQD credit, IWM/SPY size, SPY/TLT,
   XLY/XLP cyclicals) plus a VIX penalty. Every per-symbol signal below is
   computed in isolation and would happily buy a strong-looking chart in a
   collapsing market; this is the check that stops that. In a risk-off tape
   position sizes are halved and the entry bar is raised.

Then for each symbol in `config/watchlist.yaml`:

1. **Quant signal** (`signals/technical.py`) — a transparent composite score in
   `[-1, 1]` from trend (price vs SMA50), SMA20/SMA50 crossover, 20-day
   momentum, MACD histogram, and RSI14. No ML, no curve-fit parameters — you can
   read the score and know exactly why it's what it is. Also flags a setup as
   *extended* (trend intact but price stretched above its 20-day mean with a hot
   RSI), which becomes a WAIT rather than a chase.
2. **Risk override check** — if the symbol is already held, the ATR stop,
   trailing stop, and optional take-profit (`config/risk.yaml`) are checked
   first and win over everything else.
3. **LLM read** (`llm/analyst.py`) — if not overridden, recent headlines +
   fundamentals + the quant signal go to an LLM analyst, which returns a
   structured stance/confidence/rationale/risk_flags. Two interchangeable
   backends (see [Analyst backends](#analyst-backends)); skipped entirely if
   neither is configured, or with `--skip-llm`.
4. **Decision** (`decision/engine.py`) — quant and LLM scores are averaged. If
   they *disagree* in direction, that's treated as no-conviction (hold/avoid)
   rather than averaged away silently. Actions are BUY / SELL / HOLD / WAIT /
   AVOID, where WAIT means "constructive, but not at this price".
5. **Risk sizing** (`risk/manager.py`) — a BUY still has to clear
   `max_position_pct`, `max_total_exposure_pct`, `max_open_positions`, and
   `max_new_orders_per_cycle`, and gets cut by the regime multiplier, before an
   order is placed.
6. **Execution** (`execution/broker.py`) — buys go to Alpaca's paper API as
   *notional* (fractional-share) orders, so a $1,800 budget buys $1,800 of a
   $779 stock rather than rounding down to two shares. No Alpaca credentials
   configured → falls back to `DryRunBroker`, which logs what it *would* have
   done against a fake $100k book instead.
7. **Journal** (`journal/logger.py`) — every symbol's signal, verdict, decision,
   and order outcome is written to `data/journal.db` (SQLite) for later review.

Then once more at the end of the cycle:

8. **Protective-stop reconciliation** — positions are re-read (so anything that
   just filled is included) and every one of them is checked against a resting
   stop order at Alpaca, which is placed or ratcheted up as needed. This is what
   protects a position between cycles.

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\pip.exe install -r requirements.txt
.\.venv\Scripts\pip.exe install -e .
copy .env.example .env
```

Then fill in `.env`:

- **Alpaca** (paper trading, free): sign up at https://alpaca.markets →
  dashboard → paper trading → generate an API key/secret. Put them in
  `ALPACA_API_KEY` / `ALPACA_SECRET_KEY`. Leave `ALPACA_BASE_URL` as the paper
  endpoint — the code refuses to run against anything else (see Safety).
- **Analyst backend**: see below — either a local agent CLI (no extra cost if
  you already subscribe to one) or a pay-per-token API key.

Without either the pipeline still runs — it just falls back to `DryRunBroker`
and/or quant-only decisions, which is the easiest way to sanity check the
pipeline before trusting it with even a paper account.

## Analyst backends

`ANALYST_PROVIDER` in `.env` picks how step 3 gets its qualitative read. Both
produce the same `AnalystVerdict`; the rest of the pipeline can't tell which ran.

### `cli` (default) — spend an existing coding-plan subscription

Drives a locally-installed agent CLI through its documented non-interactive
`-p/--prompt` flag, so the analysis is billed against a subscription you already
pay for instead of a separate per-token API key.

```ini
ANALYST_PROVIDER=cli
ANALYST_CLI_PATH=C:\Users\<you>\.kimi-code\bin\kimi.exe
ANALYST_CLI_TIMEOUT=180
```

Verified with Kimi Code (`kimi.exe`); Claude Code (`claude`) exposes the same
`-p` + `--output-format stream-json` interface and the output parser handles
both envelope shapes. Roughly 15-25s per symbol at max reasoning effort, so a
14-symbol cycle takes about 5 minutes — fine for a twice-daily run, too slow
for intraday.

#### Pinning the model and reasoning effort

`ANALYST_CLI_MODEL` is passed through as `-m`, so the analyst can't silently
ride on whatever the CLI's global `default_model` happens to be — if you change
your everyday coding model, the trading system keeps using the one it was
configured with.

Reasoning effort is more awkward. In Kimi Code the global `[thinking] effort`
in `~/.kimi-code/config.toml` outranks both a per-model `default_effort` and a
project-local `.kimi-code/local.toml`, and a built-in migration
(`migrations-effort.json`, recorded as `thinking-effort-max-to-high`) rewrites
`max` back to `high` the first time it sees a config without that marker. The
only clean way to pin `max` for this project without changing the effort of
your interactive coding sessions is a separate config home:

```powershell
# one-time setup
mkdir $HOME\.kimi-code-trading
copy $HOME\.kimi-code\config.toml           $HOME\.kimi-code-trading\
copy $HOME\.kimi-code\migrations-effort.json $HOME\.kimi-code-trading\   # stops the max->high rewrite
copy $HOME\.kimi-code\device_id             $HOME\.kimi-code-trading\
copy -r $HOME\.kimi-code\credentials        $HOME\.kimi-code-trading\
# then set effort = "max" under [thinking] in the copied config.toml
```

Point `ANALYST_CLI_HOME` at that directory. Verify it took effect by checking
`thinkingEffort` in the session wire log:

```powershell
Get-ChildItem $HOME\.kimi-code-trading\sessions -Recurse -Filter wire.jsonl |
  Select-Object -Last 1 | Get-Content | Select-String '"thinkingEffort"'
```

Because the credentials are copied, re-authenticating (`kimi login`) may
eventually require refreshing that copy. Keep this directory **outside** the
repo — it holds OAuth tokens.

Two caveats worth knowing:

- **The machine has to be on** when a cycle fires, since the CLI runs locally.
- Coding-plan subscriptions are sold for coding assistance. Personal,
  low-frequency use like this is what `-p` exists for, but heavy automated
  querying is the kind of thing rate limits and abuse detection exist to catch.
  Keep the cadence modest.

### `api` — pay-per-token

```ini
ANALYST_PROVIDER=api
MOONSHOT_API_KEY=sk-...
ANALYST_MODEL=kimi-k3
```

Uses OpenAI-style function calling, so parsing is exact rather than
best-effort — more reliable than the CLI path, at the cost of per-token
billing. The key must come from the **open developer platform**
(platform.moonshot.ai), not the Kimi Code subscription console — a Kimi Code
key returns 401 here. Any OpenAI-compatible endpoint works by editing
`DEFAULT_BASE_URL` in `llm/api_provider.py`.

## Grok sentiment gate (optional)

A second, separate check — live X/web sentiment via Grok CLI — runs right
before a BUY or SELL actually submits, in the full twice-daily cycle only
(not `--fast`; the latency doesn't fit a 20-minute loop). It's informational,
not a veto: if sentiment contradicts the trade, that's appended to the
decision's reasoning and logged at warning level, and the trade proceeds
anyway — same principle as Kimi's `risk_flags`, one layer further out.

```ini
GROK_ENABLED=false      # off by default — see the caveat below before flipping this on
GROK_CLI_PATH=C:\Users\<you>\.grok\bin\grok.exe
GROK_TIMEOUT_SECONDS=60
GROK_REASONING_EFFORT=low
```

Grok CLI (`grok.com` login) is a general coding agent by default, not a thin
search API. An unscoped call in this project directory took **3 minutes** —
it tried loading this project's own Claude Code skills (specifically
"agent-reach", a multi-platform search router) before falling back to ad-hoc
CLI discovery, with one sub-search it had to self-abort. `llm/grok_provider.py`
tells it explicitly not to touch skills or shell tools and to use its own
native web/X search directly, which cuts that to **~12-40s** with no loss in
answer quality (real prices, dated headlines, sourced X posts/handles) — but
it's still real per-call latency, and 5 confirmations in one cycle add roughly
100s total, observed live.

`--json-schema` was tried and rejected as the parsing strategy: it can still
emit two concatenated JSON objects in one response (observed directly), so
output goes through the same tolerant brace-matching extractor
(`llm/json_extract.py`, shared with the Kimi CLI provider) rather than
trusting the schema flag to guarantee clean output alone.

**This costs real USD per call (~$0.06 observed), not subscription quota
like Kimi.** At most `max_new_orders_per_cycle` BUY confirmations plus any
decision-driven SELLs per day, this is cents/day — but it's not free the way
the Kimi path is, and the call only fires after `size_position` has already
approved a trade (not on every symbol that merely clears the buy threshold),
so a symbol that gets vetoed for cash/exposure reasons never reaches Grok.

Left `false` by default: verify with a manual `--dry-run` review (see
`report.py` output for the `grok_stance`/`grok_confidence`/`grok_summary`
columns) before enabling it on the live scheduled tasks.

## Running

```powershell
# one cycle, using whatever real credentials are in .env
.\.venv\Scripts\python.exe -m agentic_trading.run

# force a dry run even with real credentials configured
.\.venv\Scripts\python.exe -m agentic_trading.run --dry-run

# quant-only, no LLM calls (fast, free, good smoke test)
.\.venv\Scripts\python.exe -m agentic_trading.run --skip-llm
```

Logs go to `logs/YYYY-MM-DD.log` and the console. Decisions and orders go to
`data/journal.db`.

### Reviewing what it did

`report.py` reads the journal so you don't need SQL:

```powershell
.\.venv\Scripts\python.exe -m agentic_trading.report              # last cycle, one line per symbol
.\.venv\Scripts\python.exe -m agentic_trading.report --last 10    # summary of recent cycles
.\.venv\Scripts\python.exe -m agentic_trading.report --symbol NVDA  # one symbol over time
```

### Scheduling

With `ANALYST_PROVIDER=cli` the machine must be awake when a cycle fires.

For unattended operation, `scheduler.py` is a long-running loop that fires a
cycle at 9:45 AM and 4:15 PM US/Eastern on weekdays:

```powershell
.\.venv\Scripts\python.exe -m agentic_trading.scheduler
```

**09:45, not pre-market.** Notional orders only fill during regular hours, so a
pre-market cycle leaves its buys sitting in the queue — and the protective-stop
reconciliation at the end of that cycle would find no position to protect,
leaving the day's new entries uncovered until the next run. Running after the
open lets a buy fill and receive its stop within the same cycle. Orders from
the afternoon run queue for the next session, which is the intended behaviour
for a swing-horizon strategy.

### Windows Task Scheduler (what's actually installed)

More robust than keeping `scheduler.py` alive: it survives reboots and can wake
a sleeping machine. The task invokes the venv's `python.exe` directly:

```powershell
$py       = "C:\Users\helow\Documents\Trading\.venv\Scripts\python.exe"
$dir      = "C:\Users\helow\Documents\Trading"
$action   = New-ScheduledTaskAction -Execute $py -Argument "-m agentic_trading.run" -WorkingDirectory $dir
$t1       = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At 9:45am
$t2       = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At 4:15pm
$settings = New-ScheduledTaskSettingsSet -WakeToRun -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 30) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName "AgenticTrading" -Action $action -Trigger $t1,$t2 -Settings $settings -Force
```

Inspect, run on demand, or remove it:

```powershell
Get-ScheduledTaskInfo -TaskName "AgenticTrading"     # last result, next run time
Start-ScheduledTask   -TaskName "AgenticTrading"     # fire one cycle now
Unregister-ScheduledTask -TaskName "AgenticTrading"  # remove entirely
```

`run_cycle.cmd` is kept as a convenience wrapper for manual runs — it forwards
its arguments, so the chain can be smoke-tested without touching the account:

```powershell
.\run_cycle.cmd --dry-run --skip-llm
```

Three things that will bite if you rebuild this setup:

- **The scheduled task runs `python.exe` directly, not through `cmd.exe`.**
  Going through a batch wrapper added a console layer that the cycle did not
  survive (see the next point).
- **The CLI analyst subprocess must be spawned with `CREATE_NO_WINDOW |
  CREATE_NEW_PROCESS_GROUP`** (handled in `llm/cli_provider.py`). Under Task
  Scheduler the parent has no visible console, and a console-attached child
  exiting there propagated a control event to the whole process group — the
  cycle was killed part-way through the watchlist with `0xC000013A`
  (`STATUS_CONTROL_C_EXIT`), leaving orders half-placed and no error in the log.
- **`run_cycle.cmd` must keep CRLF line endings** if you use it. `cmd.exe`
  misparses LF-only batch files — `REM` becomes `RE` plus an unknown command
  `M`.

The task runs with **Interactive** logon, so it needs the user to be logged in
(a locked screen is fine). That is deliberate: the CLI analyst reads OAuth
credentials from the user profile.

The cycle log is `logs\YYYY-MM-DD.log`. `logs\scheduler-task.log` only exists
if you run through `run_cycle.cmd`.

### Fast-tier scanning (`--fast`)

The twice-daily task above does a full pass — news, fundamentals, an LLM read
— over the entire universe, which takes minutes and isn't meant to run often.
For finding new opportunities faster than twice a day without either
hammering the LLM or turning this into a tick-by-tick system it can't safely
be, there's a second task, `AgenticTradingFastScan`, registered similarly but
running `python -m agentic_trading.run --fast` every 20 minutes from 9:35am to
~4:05pm ET:

```powershell
$py  = "C:\Users\helow\Documents\Trading\.venv\Scripts\python.exe"
$dir = "C:\Users\helow\Documents\Trading"
$action  = New-ScheduledTaskAction -Execute $py -Argument "-m agentic_trading.run --fast" -WorkingDirectory $dir
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At 9:35am
$trigger.Repetition = (New-ScheduledTaskTrigger -Once -At 9:35am -RepetitionInterval (New-TimeSpan -Minutes 20) -RepetitionDuration (New-TimeSpan -Hours 6 -Minutes 30)).Repetition
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 10) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName "AgenticTradingFastScan" -Action $action -Trigger $trigger -Settings $settings -Force
```

Use `-Weekly` with `-DaysOfWeek`, not `-Daily` and not a bare `-Once`. A bare
`-Once` trigger only covers the single day it was created on — the repetition
runs out that evening and the task silently never fires again, which is exactly
what happened here (registered 8/19, dead by 8/20 with a blank `NextRunTime`).

**`--fast` reuses the exact same decision/risk/execution code as a normal
cycle — it does not run a separate, less-tested pipeline.** The differences are
the data source and two gates:

**It scores today's forming bar, and the deep cycle does not.** This is the
whole reason the fast tier can see anything at all. `YFinanceFeed(drop_forming=True)`
deletes the current session's incomplete daily bar, which is *correct* for the
9:45/16:15 runs — otherwise they'd compute SMA/RSI/MACD on 15 minutes of tape —
but it meant every intraday scan re-scored yesterday's close. Measured live
mid-session on 2026-08-20 at 13:36 ET: the scan was scoring MSFT's *8/19* close
of 484.31 while the live quote was 481.61/486.00. Across all of 8/19, JNJ's
score took four distinct values (0.33–0.37), and that ±0.04 was Yahoo revising
yesterday's bar, not price action — all of it under the cooldown delta, so
nearly every scan did literally nothing, ~19 times a day.

The fix is `data/alpaca_feed.py`: Alpaca's free tier (the paper key already in
`.env`, no data subscription) returns today's in-progress daily bar and
batch-fetches the whole universe in one request — measured at ~1.1s for 8
symbols × 300 bars, versus yfinance's one-symbol-at-a-time seconds each. A
23-symbol fast scan now completes in about 1.5s. The deep cycle keeps using
yfinance with settled bars; only `--fast` switches feeds, and only when Alpaca
credentials exist (otherwise it logs a warning that the scan is blind to
intraday movement and falls back).

**Index symbols must bypass Alpaca.** Its bars endpoint is equities-only and
returns `400 invalid symbol: ^VIX`. When this feed first shipped, that quietly
stripped VIX out of the macro regime mid-session — the error was caught and
logged, the cycle completed normally, and the only visible symptom was the
regime line losing its `VIX 15.8` suffix. `_is_index_symbol()` now routes
anything starting with `^` back to yfinance. The ratio ETFs (SPY/HYG/XLY/…)
were never affected because they're ordinary equities. Worth remembering as a
class of bug: a data source that degrades instead of failing loudly can remove
a risk input without anything looking broken.

**Two gates keep that from becoming a noise generator.** Riding the forming bar
means indicators move continuously, so a symbol can cross the buy threshold at
10:00, fall back at 10:20, and cross again at 10:40 — three "fresh signals" to
a stateless scan, on one wobble. Given the backtest's 41% win rate, more
triggers is not self-evidently good:

1. **`min_intraday_confirm_scans`** (`config/risk.yaml`, default 2) — a *new
   intraday entry* must clear the bar on that many consecutive scans (~20 min
   apart) before an order is placed. Streak state lives in the journal's
   `intraday_confirmations` table and resets the moment a symbol stops
   qualifying, so two unrelated crossings never add up to an entry. **Exits are
   exempt** — a risk exit never waits for confirmation.
2. **The existing escalation cooldown** (`llm_escalations`) still applies on top,
   so a confirmed symbol whose score hasn't moved much since its last full
   analysis still skips the LLM call.

Observed working live, two scans ~20 min apart: first scan flagged 7 symbols as
qualifying and placed **0** orders (all "1/2 consecutive scans"); the second
released 3 of them, of which the cooldown suppressed 4 others, ending in 2
simulated orders rather than 7.

One thing this deliberately does *not* do: **it is not tick-by-tick.** Reacting
to an adverse move faster than 20 minutes is the broker-side protective stop's
job (see below) — no polling loop beats a resting order at the exchange.

- `config/watchlist.yaml` — core symbols to track, hand-maintained. Starts with
  12 diversified large-caps + SPY/QQQ; edit freely, no code changes needed.
- `config/research.yaml` — an Obsidian vault to mine for additional tickers
  (see [Research vault](#research-vault-as-a-symbol-source)).
- `config/risk.yaml` — every hard limit (position size, exposure, stops, order
  cap per cycle, noise floor, regime response). Tune here first before touching
  code.

### Research vault as a symbol source

The trading universe isn't only the hand-maintained watchlist — `config/research.yaml`
points at an Obsidian vault and pulls in any ticker with a real per-instrument
note, so symbols the user has actually researched show up automatically without
manual list maintenance.

Extraction (`research/obsidian_scanner.py`) trusts exactly two signals, both
deliberately narrow — a false-positive ticker here means the system could
analyze or trade the wrong instrument, which is worse than missing a real one:

1. A `ticker:` (singular) field in a note's YAML front matter.
2. A filename ending `-XXXX.md` (1-5 uppercase letters), matching how the
   vault's individual stock write-ups are actually named (`01-IQVIA-IQV.md` → `IQV`).

A `tickers:` *array* is recorded as a mention and is **not** tradeable. Seeking
Alpha Daily analysis notes stamp that field on every article; those are names
the piece talked about, not names that should go onto the paper account. A
20-day average dollar-volume floor (`min_adv_usd` in `risk.yaml`) is a second
gate for anything that still slips through.

Tag lists are **not** used as a source, even though tickers often appear in
them — a tag set like `[投资研究, CRO, 医疗数据, IQV]` mixes the real subject
(`IQV`) with acronyms that are themselves valid tickers for something
unrelated (`CRO` is Cronos Group, not "Contract Research Organization" here).

`exclude_symbols` in `research.yaml` removes tickers the scanner correctly
finds but Alpaca can't trade — currently two Swiss-exchange-only names (Lonza
`LONN`, Bachem `BANB`) with no reachable US listing. Anything scanned but not
in this list and not actually tradable will simply show up as a rejected-order
log line rather than crashing the cycle, since order submission already
degrades gracefully (see [Protective stops are broker-side](#protective-stops-are-broker-side)).

Each cycle logs which symbols came from where: `Universe: 23 symbols (14 core +
9 from research vault: BOTZ, BTSG, IBB, ...)`.

### Why these numbers

The profile is deliberately aggressive, but three of the settings were changed
because the "safe" version was actively harmful, not merely cautious:

**No fixed take-profit.** A momentum strategy earns its return from a small
number of large winners. Capping gains at +15% while letting losses run to the
stop inverts the payoff — you keep the small wins and all of the losses. Winners
are now cut by a 12% trailing stop instead, so a trend can run as far as it
wants and only gives back a fixed slice of its peak.

**Stops scale with volatility, not a fixed percentage.** At 60% annualised
volatility (MSFT, AMZN, and TSLA were all near or above that in testing) one
standard deviation is ~3.8% *per day*. A flat 5% stop sits inside ordinary noise:
it doesn't protect against a real move, it just guarantees getting shaken out of
good positions at bad prices. Distance is now `2.5 × ATR14`, clamped to 6–20%,
so a quiet stock is still cut quickly and a wild one gets room to breathe.

**A lower noise floor.** `min_quant_score_to_consider` gates on the quant score
alone. At 0.30 it was high enough that the LLM read almost never changed an
outcome — the qualitative layer was decorative. At 0.15 it still blocks
LLM-only trades (the technical signal must show *something*) without vetoing
setups the quant model rates as merely moderate.

The genuinely riskier dial is concentration: `max_position_pct` 0.18 with
`max_open_positions` 6 means up to ~6 positions carrying real weight each,
rather than a diluted basket. That is a real variance increase, and it is the
one to turn down first if the swings are uncomfortable.

Aggression is also **conditional on the regime** rather than constant: in a
risk-off tape sizes are halved and the entry bar rises by
`risk_off_score_penalty`. Exits are never gated by the regime.

### Protective stops are broker-side

The client-side exit check in step 2 only looks twice a day. On its own that
leaves a position unprotected for hours at a time, which matters more with
concentrated positions. So at the end of every cycle each open position is
reconciled against a resting **stop order held at Alpaca**, which fires whether
or not this program is running.

The level is `max(ATR stop from entry, trailing stop from the peak)` — early in
a trade the ATR level governs, and once the position runs the trailing level
overtakes it. Since the level only ever moves up, re-placing the order each
cycle reproduces a trailing stop.

It has to be reproduced that way because of two Alpaca constraints on fractional
positions, which this project uses for every buy:

| | fractional | whole shares |
|---|---|---|
| trailing stop order | not supported | supported |
| plain stop order | **supported** | supported |
| time in force | DAY only | GTC allowed |

So the resting order is a plain stop, and DAY-only means it must be re-placed
each cycle rather than left indefinitely. The pre-market run covers the session
that follows it.

Two consequences worth understanding:

- **A resting stop reserves the shares it covers.** Any exit path therefore
  cancels open orders for that symbol before selling, or the sell is rejected
  for insufficient quantity.
- **A stop is not a floor.** It becomes a market order when touched, so a
  position that gaps down overnight fills at the gapped price, not the stop
  price. No order type avoids that.

### Unfilled buys are not re-bought

Orders submitted outside regular hours sit queued until the next open, so a
cycle that runs before they fill still sees a flat book. Each cycle reads open
orders first and refuses to place a second buy for any symbol that already has
one pending — without that guard, a run in that window would double the
intended position size. If the open-orders read itself fails, the cycle skips
new entries entirely rather than guessing.

The client-side check remains as a backstop for when a stop order was rejected;
that case is logged at error level.

## Testing

```powershell
.\.venv\Scripts\pip.exe install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest
```

Tests cover the signal math, market-regime scoring, decision logic (including
conflicting quant/LLM signals and the regime's effect on the entry bar), risk
sizing, ATR/trailing exit checks, protective-stop reconciliation, the CLI
provider's output parsing, the research-vault ticker gate, and the backtest
fill/stop/look-ahead behaviour.

Not covered by unit tests: the duplicate-buy guard and the ordering inside
`run_cycle` itself, which are validated by running a real cycle. Both were
exercised live against the paper account.

## Safety

- **Paper only.** `AlpacaBroker` hard-refuses to construct with `paper=False`,
  and `Settings.is_paper` checks the base URL contains `paper`. Going live
  would require deliberately rewriting both, not just editing `.env`.
- **Long-only.** No shorting/margin logic exists; `SELL` only ever closes an
  existing long.
- **Risk limits are hard-coded to win.** Stop / trailing-stop / take-profit
  checks run before the quant/LLM decision even happens, and position sizing is
  capped regardless of how confident a signal is.
- **Every position sits behind a broker-side stop**, so protection does not
  depend on this program being awake — see below.
- **LLM failures degrade to quant-only**, they never get treated as a
  bullish/bearish signal by default — a failed API call returns `None`, not a
  guess.

### Backtesting

The live cycle is replayed against daily history: same `decide` / `size_position`
/ `check_exit` / `compute_signal` / `assess_regime`, a `SimulatedBroker` for
fills. Quant-only — LLM verdicts are not historically reproducible without
look-ahead. Decisions at day T's close, fills at T+1 open; stops use the day's
low (and fill at the open if the gap went through the stop).

```powershell
# Default universe is config/watchlist.yaml (not the research vault).
.\.venv\Scripts\python.exe -m agentic_trading.backtest --start 2019-01-01

# One-at-a-time sweep around the current risk.yaml knobs
.\.venv\Scripts\python.exe -m agentic_trading.backtest --sweep
```

Writes `data/backtest/equity.csv` and a journal. Report is vs SPY buy-and-hold
and vs equal-weight hold of the same universe.

## What's not here yet

Roughly in order of how much they'd improve the system:

- **Risk flags don't affect sizing.** The LLM emits useful warnings ("earnings
  in 3 days", "stretched valuation") that are logged and shown but have no
  mechanical effect. Feeding flag count or severity into the size multiplier is
  a small change with real value.
- Shorting, options, or anything beyond long-only equities.
- Partial exits (TRIM). Exits are currently all-or-nothing.
- Multi-day position tracking beyond what Alpaca's own position/avg-cost
  reporting gives you — the journal is an audit log, not a source of truth for
  current holdings. The one exception is `position_peaks`, which the trailing
  stop needs because the broker doesn't report a high-water mark.

## Credits

The market-regime pillar (cross-asset ratio set and the idea of tightening size
and stops when the macro read deteriorates) is adapted from
[Oft3r/agentic-trading-desk](https://github.com/Oft3r/agentic-trading-desk),
along with its "constructive but don't chase" decision state. The scoring here
is continuous rather than bucketed, and this project deliberately keeps the
autonomous execution and hard stops that design leaves to the user.
