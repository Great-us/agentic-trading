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
        logger.error("Analyst CLI not found at %s", cli_path)
        return None
    except Exception:
        logger.exception("Analyst CLI invocation failed for %s", signal.symbol)
        return None

    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_process_tree(proc)
        stdout, stderr = proc.communicate()
        logger.error(
            "Analyst CLI timed out after %ss for %s — process tree killed (partial stderr: %r)",
            timeout, signal.symbol, (stderr or "")[:300],
        )
        return None

    if proc.returncode != 0:
        detail = (stderr or stdout or "").strip()[:500]
        logger.error("Analyst CLI exited %s for %s: %s", proc.returncode, signal.symbol, detail)
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
        logger.error("No structured JSON object in analyst CLI output for %s: %r",
                     signal.symbol, analyst_output.strip()[:500])
        return None

    return parse_verdict(payload, signal.symbol)
