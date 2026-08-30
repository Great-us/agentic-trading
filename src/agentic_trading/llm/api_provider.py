"""Analyst backed by an OpenAI-compatible HTTP API (Moonshot/Kimi by default,
but any OpenAI-compatible base URL works). Uses function calling for structured
output, so parsing is exact rather than best-effort.

Requires a pay-per-token developer API key. If you'd rather spend an existing
coding-plan subscription, use the CLI provider instead — see cli_provider.py.
"""
from __future__ import annotations

import json
import logging

from ..data.market_data import NewsItem
from ..signals.technical import QuantSignal
from .schema import FIELD_DOC, SYSTEM_PROMPT, AnalystVerdict, build_user_prompt, parse_verdict

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.moonshot.ai/v1"
DEFAULT_MODEL = "kimi-k3"

_TOOL = {
    "type": "function",
    "function": {
        "name": "submit_analysis",
        "description": "Submit a structured qualitative assessment of the stock.",
        "parameters": {
            "type": "object",
            "properties": {
                "stance": {"type": "string", "enum": ["bullish", "neutral", "bearish"], "description": FIELD_DOC["stance"]},
                "confidence": {"type": "number", "description": FIELD_DOC["confidence"]},
                "rationale": {"type": "string", "description": FIELD_DOC["rationale"]},
                "risk_flags": {"type": "array", "items": {"type": "string"}, "description": FIELD_DOC["risk_flags"]},
                "evidence_quality": {
                    "type": "string",
                    "enum": ["high", "medium", "low", "none"],
                    "description": FIELD_DOC["evidence_quality"],
                },
            },
            "required": ["stance", "confidence", "rationale", "risk_flags"],
        },
    },
}


def analyze_via_api(
    signal: QuantSignal,
    news: list[NewsItem],
    fundamentals: dict,
    api_key: str,
    model: str = DEFAULT_MODEL,
    base_url: str = DEFAULT_BASE_URL,
) -> AnalystVerdict | None:
    """Returns None on any API failure or malformed response — callers must
    treat that as 'no LLM input', not as a directional signal."""
    from openai import OpenAI

    client = OpenAI(api_key=api_key, base_url=base_url)
    try:
        response = client.chat.completions.create(
            model=model,
            tools=[_TOOL],
            tool_choice="required",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": build_user_prompt(signal, news, fundamentals) + "\n\nCall submit_analysis with your assessment."},
            ],
            # A hung request must not eat the whole cycle window, and a rambling
            # response cannot help — a verdict is a small JSON object.
            timeout=120,
            max_tokens=1024,
        )
        message = response.choices[0].message
        calls = list(message.tool_calls or [])
    except Exception:
        # Everything from HTTP failure to a malformed envelope degrades to
        # 'no LLM input' instead of aborting the caller's cycle.
        logger.exception("Analyst API call failed for %s", signal.symbol)
        return None

    for call in calls:
        if call.function.name != "submit_analysis":
            continue
        try:
            payload = json.loads(call.function.arguments)
        except json.JSONDecodeError:
            logger.exception("Unparseable tool arguments for %s: %r", signal.symbol, call.function.arguments)
            return None
        return parse_verdict(payload, signal.symbol)

    logger.error("No submit_analysis tool call in response for %s (content=%r)", signal.symbol, message.content)
    return None
