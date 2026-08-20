"""Shared prompt/verdict contract for every analyst provider.

Both the HTTP-API provider and the local-CLI provider produce the same
AnalystVerdict from the same prompt, so the decision engine never has to care
which one ran.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from ..data.market_data import NewsItem
from ..signals.technical import QuantSignal

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are a disciplined equity research analyst supporting a systematic \
paper-trading agent. You are given a quant technical signal, recent headlines, and \
fundamentals for one stock. Judge how the qualitative picture (news, fundamentals) \
should confirm, contradict, or temper the quant signal. Be skeptical of thin or stale \
news, and say so via risk_flags rather than inventing a strong opinion. You are not \
placing the trade yourself — a separate decision engine combines your read with the \
quant score and hard risk limits."""

# Field contract, shared by the API provider's JSON schema and the CLI provider's
# plain-text instruction so both paths stay in sync.
FIELD_DOC = {
    "stance": "One of: bullish, neutral, bearish. Overall qualitative read given the news, fundamentals, and quant context.",
    "confidence": "Number from 0.0 (no conviction) to 1.0 (high conviction).",
    "rationale": "1-3 sentences explaining the stance. Cite specific facts given, not generic commentary.",
    "risk_flags": "Array of short strings for anything that should temper position size, e.g. 'earnings in 3 days', 'stretched valuation', 'no recent news'.",
}

VALID_STANCES = {"bullish", "neutral", "bearish"}


@dataclass
class AnalystVerdict:
    symbol: str
    stance: str  # bullish | neutral | bearish
    confidence: float
    rationale: str
    risk_flags: list[str]

    @property
    def directional_score(self) -> float:
        """Maps stance+confidence to [-1, 1] so it composes with the quant score."""
        sign = {"bullish": 1, "neutral": 0, "bearish": -1}[self.stance]
        return sign * self.confidence


def build_user_prompt(signal: QuantSignal, news: list[NewsItem], fundamentals: dict) -> str:
    news_block = (
        "\n".join(f"- ({n.publisher}) {n.title}" for n in news) if news else "- No recent headlines found."
    )
    fund_block = (
        "\n".join(f"- {k}: {v}" for k, v in fundamentals.items()) if fundamentals else "- No fundamentals data available."
    )
    return f"""Symbol: {signal.symbol}

Quant technical signal (composite score {signal.score:+.2f}, range -1 bearish to +1 bullish):
- last_price: {signal.last_price:.2f}
- sma20 vs sma50: {signal.sma20:.2f} vs {signal.sma50:.2f}
- rsi14: {signal.rsi14:.1f}
- momentum_20d_pct: {signal.momentum_20d_pct:+.1f}%
- volatility_annualized_pct: {signal.volatility_annualized_pct:.1f}%

Recent headlines:
{news_block}

Fundamentals snapshot:
{fund_block}"""


def parse_verdict(payload: dict, symbol: str) -> AnalystVerdict | None:
    """Validates a decoded payload into a verdict. Returns None (never a guess)
    if anything is missing or out of range — callers treat that as 'no LLM input'."""
    try:
        stance = str(payload["stance"]).strip().lower()
        if stance not in VALID_STANCES:
            logger.error("Invalid stance %r for %s", stance, symbol)
            return None
        confidence = float(payload["confidence"])
        if not 0.0 <= confidence <= 1.0:
            logger.error("Confidence %r out of range for %s", confidence, symbol)
            return None
        risk_flags = payload.get("risk_flags") or []
        if not isinstance(risk_flags, list):
            logger.error("risk_flags is not a list for %s: %r", symbol, risk_flags)
            return None
        return AnalystVerdict(
            symbol=symbol,
            stance=stance,
            confidence=confidence,
            rationale=str(payload["rationale"]),
            risk_flags=[str(f) for f in risk_flags],
        )
    except (KeyError, ValueError, TypeError):
        logger.exception("Malformed analyst payload for %s: %r", symbol, payload)
        return None
