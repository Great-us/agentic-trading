"""Analyst backed by a locally installed agent CLI (Claude Code, Kimi Code).

The CLI runs in its documented non-interactive prompt mode. Every backend must
produce the same AnalystVerdict contract, and any failure returns None so the
caller can safely fall back to quant-only behavior.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from ..data.market_data import NewsItem
from ..signals.technical import QuantSignal
from .json_extract import extract_json_object as _extract_json_object  # re-exported for tests
from .schema import FIELD_DOC, AnalystVerdict, build_user_prompt, parse_verdict

logger = logging.getLogger(__name__)

# Environment variables matching this are stripped before spawning any analyst
# subprocess. A coding CLI can read its environment (and pass it on to whatever
# it spawns), so the trading account's keys must not be visible to it — the only
# thing the analyst needs from this project is the prompt text.
SECRET_ENV_RE = re.compile(r"ALPACA|APCA|MOONSHOT|TIINGO|SECRET|PASSWORD|TOKEN|API_KEY", re.IGNORECASE)


def sanitized_child_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """A copy of the environment minus anything that looks like a credential."""
    env = {k: v for k, v in os.environ.items() if not SECRET_ENV_RE.search(k)}
    if extra:
        env.update(extra)
    return env


def kill_process_tree(proc: subprocess.Popen) -> None:
    """Terminate a timed-out child and everything it spawned.

    `ANALYST_CLI_PATH` is usually a `.cmd` shim, so the real worker is a
    grandchild (cmd.exe -> node.exe). Popen.kill() only terminates the direct
    child and leaves that grandchild running as an orphan, still burning
    subscription quota. On Windows taskkill /T walks the whole tree."""
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            capture_output=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    else:  # pragma: no cover - non-Windows development fallback
        proc.kill()


# --- failure diagnostics -----------------------------------------------------
#
# A CLI that exits non-zero used to be logged as its first 500 characters of
# stderr, which for Codex is the harmless "Reading additional input from
# stdin..." preamble — the real cause ("You've hit your usage limit ... try
# again at Sep 20th") sits at the END of stderr and never reached the log
# (2026-09-16/17: three deep cycles fail-closed to quant-only and nobody could
# tell why). Failures are now classified and the last one is kept on the module
# so run.py can put "why the LLM is unavailable" into the heartbeat/progress.

FAILURE_CATEGORIES = ("quota", "auth", "timeout", "parse", "not_found", "unknown")

# Matched case-insensitively against stderr + stdout of a failed invocation;
# quota is checked before auth because some CLIs phrase a spent plan as a 403.
_QUOTA_RE = re.compile(
    r"usage limit|rate[ _-]?limit|quota|too many requests|\b429\b|insufficient[ _]credits|"
    r"out of credits|billing", re.IGNORECASE,
)
_AUTH_RE = re.compile(
    r"unauthori[sz]ed|\b401\b|\b403\b|not logged in|please (?:log|sign) in|login required|"
    r"authenticat|invalid (?:api[ _-]?key|token|credential)|expired (?:token|session)|"
    r"permission denied", re.IGNORECASE,
)
# "try again at Sep 20th, 2026 4:00 PM" / "resets in 3 hours" — kept verbatim
# in the detail so the operator sees when the analyst is expected back.
_RETRY_HINT_RE = re.compile(
    r"((?:try again|retry|resets?|available again)[^.\n]{0,80})", re.IGNORECASE,
)

# Anything resembling a credential is masked before it reaches a log line or
# the progress card. Long opaque tokens (32+ url-safe chars) are almost never
# meaningful in an error message and almost always a key.
_REDACT_RES = (
    re.compile(r"(?i)\bbearer\s+\S+"),
    re.compile(r"(?i)((?:api[_-]?key|secret(?:[_-]?key)?|token|password|authorization)\s*[:=]\s*)\S+"),
    re.compile(r"\b(?:sk|pk|xoxb|ghp|gho)[-_][A-Za-z0-9_\-]{8,}\b"),
    re.compile(r"\b[A-Za-z0-9_\-]{32,}\b"),
)

HEAD_CHARS = 200
TAIL_CHARS = 600


@dataclass
class CliFailure:
    """One failed analyst invocation, already redacted and truncated."""

    category: str
    symbol: str
    exit_code: int | None
    elapsed_s: float
    detail: str
    retry_hint: str | None = None

    def summary(self) -> str:
        """One line for the operator: the category, then the most useful bit
        (the retry hint when there is one, else the last line of the error)."""
        tail = self.retry_hint
        if not tail and self.detail.strip():
            tail = self.detail.strip().splitlines()[-1][:160]
        return f"{self.category}: {tail}" if tail else self.category

    def to_dict(self) -> dict:
        return asdict(self)


# The most recent failure (None after any success). run.py reads this after
# analyze() returns None so the heartbeat/progress can say WHY, not just that
# the circuit opened. Module-level on purpose: analyze_via_cli's return
# contract (verdict | None) is shared with the API provider and stays as is.
last_failure: CliFailure | None = None


def redact_secrets(text: str) -> str:
    for pattern in _REDACT_RES:
        text = pattern.sub(
            lambda m: (m.group(1) + "[REDACTED]") if m.lastindex else "[REDACTED]", text,
        )
    return text


def head_tail(text: str, head: int = HEAD_CHARS, tail: int = TAIL_CHARS) -> str:
    """First `head` + last `tail` characters — the error is almost always at
    the end, the context at the start. Short text is returned whole."""
    text = text.strip()
    if len(text) <= head + tail:
        return text
    return f"{text[:head]} …[{len(text) - head - tail} chars omitted]… {text[-tail:]}"


def classify_failure(stderr: str, stdout: str = "", *, timed_out: bool = False) -> str:
    """Bucket a failed invocation. Quota/auth are matched on message text
    because every CLI exits 1 for everything; timeout/parse/not_found come
    from the caller, who knows what actually happened."""
    if timed_out:
        return "timeout"
    text = f"{stderr or ''}\n{stdout or ''}"
    if _QUOTA_RE.search(text):
        return "quota"
    if _AUTH_RE.search(text):
        return "auth"
    return "unknown"


def _retry_hint(text: str) -> str | None:
    match = _RETRY_HINT_RE.search(text or "")
    return match.group(1).strip() if match else None


def _record_failure(category: str, symbol: str, *, exit_code: int | None,
                    elapsed_s: float, raw: str) -> CliFailure:
    global last_failure
    clean = redact_secrets(raw or "")
    failure = CliFailure(
        category=category if category in FAILURE_CATEGORIES else "unknown",
        symbol=symbol,
        exit_code=exit_code,
        elapsed_s=round(elapsed_s, 1),
        detail=head_tail(clean),
        retry_hint=_retry_hint(clean),
    )
    last_failure = failure
    return failure


def _clear_failure() -> None:
    global last_failure
    last_failure = None


_FIELDS_BLOCK = "\n".join(f'  "{key}": {doc}' for key, doc in FIELD_DOC.items())

_CLAUDE_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "stance": {"type": "string", "enum": ["bullish", "neutral", "bearish"]},
        "confidence": {"type": "number"},
        "rationale": {"type": "string"},
        "risk_flags": {"type": "array", "items": {"type": "string"}},
        "evidence_quality": {"type": "string", "enum": ["high", "medium", "low", "none"]},
    },
    "required": ["stance", "confidence", "rationale", "risk_flags", "evidence_quality"],
    "additionalProperties": False,
}

JSON_INSTRUCTION = f"""

Analyze the complete snapshot above now. Your entire response must be one JSON
object and nothing else: no report, headings, bullets, prose, markdown fence,
or follow-up questions. Use exactly these five keys and no others. The CLI may
use its built-in structured-output mechanism to enforce this schema:
{{"stance":"bullish|neutral|bearish","confidence":0.0,"rationale":"1-3 sentences citing the supplied facts","risk_flags":["short warning"],"evidence_quality":"high|medium|low|none"}}
Replace the example values with your assessment. Even when evidence is sparse,
return the JSON object with lower confidence and suitable risk_flags. Do not
read or write any files.

Field definitions:
{_FIELDS_BLOCK}"""


def _extract_text(stdout: str) -> str:
    """Pull assistant text from known stream-json JSONL envelope shapes.

    Kimi Code and older Claude Code use a flat ``role``/``content`` object.
    Current Claude Code wraps that object under ``message``. Unknown envelope
    shapes fall through to the tolerant raw-JSON extractor.
    """
    assistant_chunks: list[str] = []
    saw_envelope = False

    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        saw_envelope = True
        message = obj.get("message") if obj.get("type") == "assistant" else obj
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        content = message.get("content")
        if isinstance(content, str):
            assistant_chunks.append(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and isinstance(block.get("text"), str):
                    assistant_chunks.append(block["text"])

    if assistant_chunks:
        return "\n".join(assistant_chunks)
    if saw_envelope:
        logger.debug("stream-json envelopes found but no assistant content")
    return stdout


def _extract_payload(stdout: str) -> dict | None:
    """Extract a verdict payload from Claude JSON or generic CLI output."""
    envelopes = []
    try:
        parsed = json.loads(stdout.strip())
        envelopes = parsed if isinstance(parsed, list) else [parsed]
    except (json.JSONDecodeError, AttributeError):
        for line in stdout.splitlines():
            try:
                envelopes.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    for envelope in envelopes:
        if not isinstance(envelope, dict):
            continue
        structured = envelope.get("structured_output")
        if isinstance(structured, dict):
            return structured
        result = envelope.get("result")
        if isinstance(result, str):
            try:
                payload = json.loads(result)
            except json.JSONDecodeError:
                payload = None
            if isinstance(payload, dict):
                return payload
        message = envelope.get("message")
        if isinstance(message, dict):
            for block in message.get("content", []):
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use" and isinstance(block.get("input"), dict):
                    return block["input"]

    return _extract_json_object(_extract_text(stdout))


def analyze_via_cli(
    signal: QuantSignal,
    news: list[NewsItem],
    fundamentals: dict,
    cli_path: str,
    timeout: int = 180,
    cwd: Path | None = None,
    model: str | None = None,
    cli_home: str | None = None,
    extra_args: str | None = None,
) -> AnalystVerdict | None:
    """Run one analyst call, returning None on any invocation/parse failure.

    ``model`` uses the long ``--model`` form accepted by both Claude Code and
    Kimi Code. ``cli_home`` sets KIMI_CODE_HOME for an isolated Kimi config and
    is harmless for other CLIs. ``extra_args`` carries CLI-specific flags; the
    recommended Claude configuration uses it to pin effort and expose zero
    built-in tools, MCP servers, skills, or slash commands.
    """
    is_claude = Path(cli_path).stem.lower() == "claude"
    is_codex = Path(cli_path).stem.lower() == "codex"
    # The CLI already supplies its own system prompt. Sending another persona
    # prompt here makes Claude Code treat the following data as a missing
    # follow-up payload; the complete direct user task is sufficient and keeps
    # this path compatible with Kimi Code as well. The API provider still uses
    # SYSTEM_PROMPT as its actual system message.
    prompt = build_user_prompt(signal, news, fundamentals)
    if not is_claude:
        prompt += JSON_INSTRUCTION

    # Keep the agent outside the Git tree so coding CLIs cannot auto-discover
    # project instructions or memory even if they ignore the no-tools policy.
    work_dir = cwd or (Path(tempfile.gettempdir()) / "agentic_trading_llm")
    work_dir.mkdir(parents=True, exist_ok=True)

    argv = [cli_path]
    last_message_file: Path | None = None
    if is_codex:
        # codex exec: the prompt is POSITIONAL — `-p` is `--profile` there —
        # and the final agent message is captured to a file rather than parsed
        # out of the event stream. Read-only sandbox + ephemeral session: the
        # analyst must judge the supplied snapshot, not touch the machine or
        # leave session state behind in the temp work dir.
        argv.extend(["exec", "--skip-git-repo-check", "--ephemeral", "--sandbox", "read-only"])
        if model:
            argv.extend(["-m", model])
        if extra_args:
            argv.extend(shlex.split(extra_args))
        last_message_file = work_dir / f"codex_last_message_{signal.symbol}.txt"
        argv.extend(["-o", str(last_message_file)])
        argv.append(prompt)
    else:
        if model:
            argv.extend(["--model", model])
        argv.extend(["-p", prompt])
        if extra_args:
            argv.extend(shlex.split(extra_args))
        if Path(cli_path).stem.lower() == "kimi" and "--skills-dir" not in argv:
            # Kimi exposes no --tools switch. Pointing --skills-dir at an empty
            # directory at least stops it from auto-loading the user's or this
            # project's skills (the agent-reach detour README documents).
            empty_skills = work_dir / "no_skills"
            empty_skills.mkdir(exist_ok=True)
            argv.extend(["--skills-dir", str(empty_skills)])
        # Keep Claude's structured-output flags last. Claude Code 2.1.241 can
        # silently fall back to prose when --json-schema precedes later flags.
        if is_claude:
            argv.extend([
                "--output-format", "json",
                "--json-schema", json.dumps(_CLAUDE_JSON_SCHEMA, separators=(",", ":")),
            ])
        else:
            argv.extend(["--output-format", "stream-json"])

    env = sanitized_child_env({"KIMI_CODE_HOME": cli_home} if cli_home else None)

    # Under Windows Task Scheduler, isolate the console-free child process so
    # its exit cannot propagate a console control event to the scheduled cycle.
    creation_flags = 0
    if sys.platform == "win32":
        creation_flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP

    started = time.perf_counter()
    try:
        # DEVNULL stdin is load-bearing for Codex (and harmless for Claude/Kimi):
        # `codex exec` takes the prompt as a positional argument, but 0.153.x
        # still waits on an inherited stdin for "additional input" and then
        # exits 1. Under Task Scheduler that hang ate the 09:45 deep cycle
        # (2026-09-04) and held cycle.lock through the morning fast scans.
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=work_dir,
            env=env,
            creationflags=creation_flags,
        )
    except FileNotFoundError:
        _record_failure("not_found", signal.symbol, exit_code=None, elapsed_s=0.0,
                        raw=f"Analyst CLI not found at {cli_path}")
        logger.error("Analyst CLI not found at %s", cli_path)
        return None
    except Exception as exc:
        _record_failure("unknown", signal.symbol, exit_code=None, elapsed_s=0.0,
                        raw=f"{type(exc).__name__}: {exc}")
        logger.exception("Analyst CLI invocation failed for %s", signal.symbol)
        return None

    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_process_tree(proc)
        stdout, stderr = proc.communicate()
        elapsed = time.perf_counter() - started
        failure = _record_failure("timeout", signal.symbol, exit_code=proc.returncode,
                                  elapsed_s=elapsed, raw=stderr or stdout or "")
        logger.error(
            "Analyst CLI timed out after %ss for %s — process tree killed "
            "(category=%s, elapsed=%.1fs, stderr head/tail: %s)",
            timeout, signal.symbol, failure.category, elapsed, failure.detail,
        )
        return None

    elapsed = time.perf_counter() - started
    if proc.returncode != 0:
        # Head AND tail of stderr: Codex prints a harmless stdin notice first
        # and the real error ("You've hit your usage limit ... try again at
        # ...") last, so a head-only excerpt hid the cause for two days.
        category = classify_failure(stderr or "", stdout or "")
        failure = _record_failure(category, signal.symbol, exit_code=proc.returncode,
                                  elapsed_s=elapsed, raw=(stderr or "") + ("\n" + stdout if stdout else ""))
        logger.error(
            "Analyst CLI exited %s for %s (category=%s, elapsed=%.1fs%s): %s",
            proc.returncode, signal.symbol, category, elapsed,
            f", {failure.retry_hint}" if failure.retry_hint else "", failure.detail,
        )
        return None

    analyst_output = stdout or ""
    if last_message_file is not None and last_message_file.exists():
        try:
            analyst_output = last_message_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            logger.exception("Could not read the codex last-message file for %s; falling back to stdout.",
                             signal.symbol)
    payload = _extract_payload(analyst_output)
    if payload is None:
        failure = _record_failure("parse", signal.symbol, exit_code=proc.returncode,
                                  elapsed_s=elapsed, raw=analyst_output)
        logger.error("No structured JSON object in analyst CLI output for %s (elapsed=%.1fs): %s",
                     signal.symbol, elapsed, failure.detail)
        return None

    verdict = parse_verdict(payload, signal.symbol)
    if verdict is None:
        _record_failure("parse", signal.symbol, exit_code=proc.returncode,
                        elapsed_s=elapsed, raw=json.dumps(payload)[:800])
        return None
    _clear_failure()
    return verdict
