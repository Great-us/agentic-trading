"""Analyst backed by a locally-installed agent CLI (Kimi Code, Claude Code, ...)
running in its official non-interactive prompt mode.

This exists so an existing coding-plan subscription can supply the qualitative
read without a separate pay-per-token API key. The CLI is driven only through
its documented `-p/--prompt` flag; nothing here touches stored credentials.

CLIs return prose, not tool calls, so the prompt asks for raw JSON and the
parser is deliberately tolerant: it understands JSONL envelopes (stream-json),
markdown code fences, and bare JSON, and gives up (returning None) rather than
guessing if none of those yield a valid verdict.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from pathlib import Path

from ..data.market_data import NewsItem
from ..signals.technical import QuantSignal
from .json_extract import extract_json_object as _extract_json_object  # re-exported for tests
from .schema import FIELD_DOC, SYSTEM_PROMPT, AnalystVerdict, build_user_prompt, parse_verdict

logger = logging.getLogger(__name__)

_FIELDS_BLOCK = "\n".join(f'  "{k}": {v}' for k, v in FIELD_DOC.items())

JSON_INSTRUCTION = f"""

Respond with a single raw JSON object and nothing else — no prose before or
after, no markdown code fence, no tool calls, and do not read or write any
files. The object must have exactly these keys:
{{
{_FIELDS_BLOCK}
}}"""


def _extract_text(stdout: str) -> str:
    """Pulls assistant text out of stream-json JSONL if present, else returns
    the raw output. Tolerates the meta/version/resume-hint lines that agent CLIs
    interleave, and unknown envelope shapes from other CLIs."""
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
        if obj.get("role") != "assistant":
            continue
        content = obj.get("content")
        if isinstance(content, str):
            assistant_chunks.append(content)
        elif isinstance(content, list):
            # Claude-Code-style content blocks: [{"type": "text", "text": ...}]
            for block in content:
                if isinstance(block, dict) and isinstance(block.get("text"), str):
                    assistant_chunks.append(block["text"])

    if assistant_chunks:
        return "\n".join(assistant_chunks)
    if saw_envelope:
        logger.debug("stream-json envelopes found but no assistant content")
    return stdout


def analyze_via_cli(
    signal: QuantSignal,
    news: list[NewsItem],
    fundamentals: dict,
    cli_path: str,
    timeout: int = 180,
    cwd: Path | None = None,
    model: str | None = None,
    cli_home: str | None = None,
) -> AnalystVerdict | None:
    """Returns None on any failure (missing CLI, timeout, unparseable output) —
    callers must treat that as 'no LLM input', not as a directional signal.

    `model` is passed through as -m so the analyst never silently rides on
    whatever the user's global default_model happens to be. `cli_home` sets
    KIMI_CODE_HOME, pointing Kimi Code at a config dir owned by this project —
    that is the only place reasoning effort can be pinned, since the CLI's
    global [thinking] effort outranks both per-model default_effort and
    project-local local.toml.
    """
    prompt = f"{SYSTEM_PROMPT}\n\n{build_user_prompt(signal, news, fundamentals)}{JSON_INSTRUCTION}"

    # An empty scratch cwd keeps the agent away from this repo's files in the
    # unlikely event it decides to look around despite the instruction not to.
    work_dir = cwd or (Path(__file__).resolve().parents[3] / "data" / "llm_scratch")
    work_dir.mkdir(parents=True, exist_ok=True)

    argv = [cli_path, "-p", prompt, "--output-format", "stream-json"]
    if model:
        argv[1:1] = ["-m", model]

    env = None
    if cli_home:
        env = {**os.environ, "KIMI_CODE_HOME": cli_home}

    # On Windows the child is given its own console-free process group. Under
    # Task Scheduler the parent has no visible console, and a console child
    # exiting there propagates a control event to the whole group — which killed
    # the scheduled cycle mid-run with 0xC000013A (STATUS_CONTROL_C_EXIT).
    creation_flags = 0
    if sys.platform == "win32":
        creation_flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP

    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            cwd=work_dir,
            env=env,
            creationflags=creation_flags,
        )
    except FileNotFoundError:
        logger.error("Analyst CLI not found at %s", cli_path)
        return None
    except subprocess.TimeoutExpired:
        logger.error("Analyst CLI timed out after %ss for %s", timeout, signal.symbol)
        return None
    except Exception:
        logger.exception("Analyst CLI invocation failed for %s", signal.symbol)
        return None

    if proc.returncode != 0:
        logger.error(
            "Analyst CLI exited %s for %s: %s", proc.returncode, signal.symbol, (proc.stderr or "").strip()[:500]
        )
        return None

    text = _extract_text(proc.stdout or "")
    payload = _extract_json_object(text)
    if payload is None:
        logger.error("No JSON object in analyst CLI output for %s: %r", signal.symbol, text.strip()[:500])
        return None

    return parse_verdict(payload, signal.symbol)
