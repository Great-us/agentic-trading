"""Combines the quant score, the LLM verdict, and the market regime into one
action per symbol.

Long-only by design for this MVP (no shorting/margin) — 'sell' only ever means
closing an existing long.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from ..llm.analyst import AnalystVerdict
from ..signals.macro import MacroRegime
from ..signals.technical import QuantSignal

BUY_THRESHOLD = 0.25
SELL_THRESHOLD = -0.25
QUANT_WEIGHT = 0.5
LLM_WEIGHT = 0.5
CONFLICT_MIN_MAGNITUDE = 0.3  # both scores need at least this magnitude to count as a real conflict


class Action(str, Enum):
    BUY = "buy"
    SELL = "sell"
    HOLD = "hold"
    WAIT = "wait"    # constructive, but this is not an entry point
    AVOID = "avoid"


@dataclass
class Decision:
    symbol: str
    action: Action
    combined_score: float
    quant_score: float
    llm_score: float | None
    conflicting_signals: bool
    reasoning: str


def decide(
    signal: QuantSignal,
    verdict: AnalystVerdict | None,
    has_open_position: bool,
    min_quant_score_to_consider: float,
    regime: MacroRegime | None = None,
    risk_off_score_penalty: float = 0.0,
    buy_threshold: float = BUY_THRESHOLD,
    sell_threshold: float = SELL_THRESHOLD,
) -> Decision:
    quant_score = signal.score
    llm_score = verdict.directional_score if verdict else None

    if llm_score is None:
        combined_score = quant_score
        basis = "quant-only (LLM unavailable)"
    else:
        combined_score = QUANT_WEIGHT * quant_score + LLM_WEIGHT * llm_score
        basis = "quant + LLM"

    # A hostile tape raises the bar for new entries but never blocks an exit.
    regime_note = ""
    if regime is not None and regime.is_risk_off:
        buy_threshold = buy_threshold + risk_off_score_penalty
        regime_note = f" Risk-off regime ({regime.score:+.2f}) raised the entry bar to {buy_threshold:.2f}."

    conflicting = (
        llm_score is not None
        and abs(quant_score) >= CONFLICT_MIN_MAGNITUDE
        and abs(llm_score) >= CONFLICT_MIN_MAGNITUDE
        and (quant_score > 0) != (llm_score > 0)
    )

    if abs(quant_score) < min_quant_score_to_consider and not has_open_position:
        action = Action.AVOID
        reasoning = f"Quant score {quant_score:+.2f} is below the noise floor ({min_quant_score_to_consider}); not enough signal to act."
    elif conflicting:
        action = Action.HOLD if has_open_position else Action.AVOID
        reasoning = (
            f"Quant ({quant_score:+.2f}) and LLM ({llm_score:+.2f}) disagree in direction — "
            f"treating as no-conviction and staying put."
        )
    elif combined_score >= buy_threshold and not has_open_position:
        if signal.extended:
            action = Action.WAIT
            reasoning = (
                f"Combined score {combined_score:+.2f} ({basis}) clears the bar, but price is "
                f"{signal.last_price / signal.sma20 - 1:+.1%} above its 20-day mean with RSI "
                f"{signal.rsi14:.0f} — constructive setup, poor entry. Waiting for a pullback."
            )
        else:
            action = Action.BUY
            reasoning = f"Combined score {combined_score:+.2f} ({basis}) clears buy threshold {buy_threshold:.2f}.{regime_note}"
    elif combined_score <= sell_threshold and has_open_position:
        action = Action.SELL
        reasoning = f"Combined score {combined_score:+.2f} ({basis}) fell below sell threshold {sell_threshold}."
    elif has_open_position:
        action = Action.HOLD
        reasoning = f"Combined score {combined_score:+.2f} ({basis}) doesn't clear the sell threshold; holding existing position."
    else:
        action = Action.AVOID
        reasoning = f"Combined score {combined_score:+.2f} ({basis}) doesn't clear the buy threshold {buy_threshold:.2f}.{regime_note}"

    if verdict and verdict.risk_flags:
        reasoning += f" Risk flags: {', '.join(verdict.risk_flags)}."

    return Decision(
        symbol=signal.symbol,
        action=action,
        combined_score=combined_score,
        quant_score=quant_score,
        llm_score=llm_score,
        conflicting_signals=conflicting,
        reasoning=reasoning,
    )
