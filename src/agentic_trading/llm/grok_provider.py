"""Live X/web sentiment via Grok CLI (grok.com login, not a pay-per-token key),
used as a final confirmation gate right before a BUY/SELL actually executes —
not a per-symbol analyst like Kimi. See llm/analyst.py for that split.

Grok CLI is a general coding agent by default: an unscoped `-p` call in this
project directory took 3 minutes because it tried loading this project's own
Claude Code skills (specifically "agent-reach", a multi-platform search
router) before falling back to ad-hoc CLI discovery (Exa, OpenCLI), with one
sub-search it had to self-abort. Telling it explicitly not to touch skills/
shell tools and to use its own native web/X search directly cut that to ~12s
with no loss in answer quality (real prices, dated headlines, sourced X posts).

`--json-schema` was tried and rejected: it can still emit two concatenated
JSON objects in one response (observed directly), so parsing goes through the
same tolerant extractor as the Kimi CLI provider rather than trusting the
schema flag to guarantee clean output on its own.

Real USD cost per call (~$0.06 observed), not subscription quota like Kimi —
worth knowing before raising call frequency.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .json_extract import extract_json_object

logger = logging.getLogger(__name__)

VALID_STANCES = {"bullish", "neutral", "bearish"}

NO_SKILLS_INSTRUCTION = (
    "Do NOT use any skills, plugins, or shell/CLI tools (no agent-reach, no "
    "OpenCLI, no bash, no external commands). Use ONLY your own built-in "
    "native web search / X search directly. Do not read or write any files."
)

JSON_INSTRUCTION = """

Respond with a single raw JSON object and nothing else — no prose before or
after, no markdown code fence. The object must have exactly these keys:
{
  "stance": "bullish" | "neutral" | "bearish" — X/web sentiment specifically, not your own opinion,
  "confidence": number 0.0-1.0,
  "summary": "1-3 sentences citing specific recent posts/headlines, not generic commentary"
}"""


@dataclass
class SentimentVerdict:
    symbol: str
    stance: str  # bullish | neutral | bearish
    confidence: float
    summary: str

    @property
    def directional_score(self) -> float:
        sign = {"bullish": 1, "neutral": 0, "bearish": -1}[self.stance]
        return sign * self.confidence


def _build_prompt(symbol: str, pending_action: str, existing_reasoning: str) -> str:
    return (
        f"{NO_SKILLS_INSTRUCTION}\n\n"
        f"A trading system is about to {pending_action} {symbol}, based on this "
        f"reasoning: {existing_reasoning}\n\n"
        f"Search the last 24 hours of X posts and web news on {symbol}. Does "
        f"current sentiment support or contradict this {pending_action.upper()}? "
        f"Cite specific posts/headlines with sources.{JSON_INSTRUCTION}"
    )


def _parse_verdict(payload: dict, symbol: str) -> SentimentVerdict | None:
    try:
        stance = str(payload["stance"]).strip().lower()
        if stance not in VALID_STANCES:
            logger.error("Invalid stance %r from Grok for %s", stance, symbol)
            return None
        confidence = float(payload["confidence"])
        if not 0.0 <= confidence <= 1.0:
            logger.error("Confidence %r out of range from Grok for %s", confidence, symbol)
            return None
        return SentimentVerdict(
            symbol=symbol, stance=stance, confidence=confidence, summary=str(payload["summary"]),
        )
    except (KeyError, ValueError, TypeError):
        logger.exception("Malformed Grok payload for %s: %r", symbol, payload)
        return None


def check_sentiment(
    symbol: str,
    pending_action: str,  # "buy" | "sell"
    existing_reasoning: str,
    grok_path: str,
    timeout: int = 90,
    reasoning_effort: str = "low",
    cwd: Path | None = None,
) -> SentimentVerdict | None:
    """Returns None on any failure (missing CLI, timeout, unparseable output) —
    callers must treat that as 'no sentiment read', never as a directional
    signal, and must never let it block an order on its own (see run.py: this
    is informational, folded into reasoning/risk_flags, not a veto)."""
    prompt = _build_prompt(symbol, pending_action, existing_reasoning)

    work_dir = cwd or (Path(__file__).resolve().parents[3] / "data" / "llm_scratch")
    work_dir.mkdir(parents=True, exist_ok=True)

    argv = [
        grok_path, "-p", prompt,
        "--reasoning-effort", reasoning_effort,
        "--disallowed-tools", "bash,shell,execute_command",
        "--output-format", "json",
    ]

    # Same Windows console-isolation fix as cli_provider.py: under Task
    # Scheduler the parent has no console, and a console-attached child exiting
    # there propagates a control event to the whole process group, killing the
    # scheduled cycle mid-run (0xC000013A).
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
            env=os.environ.copy(),
            creationflags=creation_flags,
        )
    except FileNotFoundError:
        logger.error("Grok CLI not found at %s", grok_path)
        return None
    except subprocess.TimeoutExpired:
        logger.warning("Grok sentiment check timed out after %ss for %s — proceeding without it.", timeout, symbol)
        return None
    except Exception:
        logger.exception("Grok CLI invocation failed for %s", symbol)
        return None

    if proc.returncode != 0:
        logger.error("Grok CLI exited %s for %s: %s", proc.returncode, symbol, (proc.stderr or "").strip()[:500])
        return None

    # --output-format json wraps the answer in one envelope object with a
    # "text" field holding the model's (possibly still-messy) response.
    envelope = extract_json_object(proc.stdout or "")
    inner_text = envelope.get("text") if envelope else (proc.stdout or "")
    payload = extract_json_object(inner_text) if isinstance(inner_text, str) else None
    if payload is None:
        logger.error("No JSON object in Grok output for %s: %r", symbol, (proc.stdout or "").strip()[:500])
        return None

    return _parse_verdict(payload, symbol)
