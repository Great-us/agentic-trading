"""Market-regime pillar: is the whole market risk-on or risk-off right now?

Every per-symbol signal in this project is computed in isolation, which leaves a
blind spot — a stock can look technically strong while the market it trades in
is falling apart. This module scores the environment from cross-asset ratios,
so position sizing and entry thresholds can tighten in a deteriorating tape and
loosen in a healthy one.

Idea adapted from the three-pillar framework in Oft3r/agentic-trading-desk; the
ratio set is the same, the scoring here is continuous rather than bucketed to
match the rest of this codebase.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

import pandas as pd

from ..data.market_data import fetch_price_history

logger = logging.getLogger(__name__)

# Each entry: (label, numerator, denominator, what a RISING ratio means)
RATIOS: list[tuple[str, str, str, str]] = [
    ("breadth", "RSP", "SPY", "equal-weight leading cap-weight: broad participation"),
    ("credit", "HYG", "LQD", "high-yield leading investment-grade: credit risk appetite"),
    ("size", "IWM", "SPY", "small caps leading: risk appetite"),
    ("equity_vs_bonds", "SPY", "TLT", "equities preferred over treasuries"),
    ("cyclicals", "XLY", "XLP", "discretionary leading staples: cyclical strength"),
]

VIX_TICKER = "^VIX"
VIX_CALM = 18.0      # at or below this, volatility is not a constraint (sub-20 is a normal tape)
VIX_STRESSED = 30.0  # at or above this, treat the tape as hostile


@dataclass
class MacroRegime:
    score: float                      # [-1, 1], positive = risk-on
    label: str                        # risk_on | neutral | risk_off | unknown
    components: dict[str, float] = field(default_factory=dict)
    vix: float | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def is_risk_off(self) -> bool:
        return self.label == "risk_off"

    @property
    def is_unknown(self) -> bool:
        return self.label == "unknown"


def _clip(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return float(max(lo, min(hi, x)))


def _ratio_score(numerator: pd.DataFrame, denominator: pd.DataFrame) -> float | None:
    """Scores one cross-asset ratio by how far it sits above/below its own 50-day
    average, blended with its recent rate of change. Self-referencing like this
    keeps the score comparable across ratios with wildly different levels."""
    if numerator.empty or denominator.empty:
        return None

    ratio = (numerator["Close"] / denominator["Close"]).dropna()
    if len(ratio) < 60:
        return None

    sma50 = ratio.rolling(50).mean()
    level = float(ratio.iloc[-1] / sma50.iloc[-1] - 1)          # position vs trend
    change = float(ratio.iloc[-1] / ratio.iloc[-21] - 1)         # 20-day slope

    # x5 / x8 put a typical 2-3% divergence near the middle of the range rather
    # than pinned at the extremes.
    return _clip(0.6 * _clip(level * 8) + 0.4 * _clip(change * 5))


def _vix_adjustment(vix: float) -> tuple[float, str]:
    """Volatility acts as a damper, never as a bullish signal on its own: calm
    contributes nothing, stress subtracts."""
    if vix <= VIX_CALM:
        return 0.0, f"VIX {vix:.1f} calm"
    if vix >= VIX_STRESSED:
        return -0.5, f"VIX {vix:.1f} stressed — regime penalised"
    fraction = (vix - VIX_CALM) / (VIX_STRESSED - VIX_CALM)
    return -0.5 * fraction, f"VIX {vix:.1f} elevated"


def assess_regime(period: str = "1y", *, feed=None, asof=None) -> MacroRegime:
    """Never raises: any ratio that fails to load is dropped, and if too few
    survive the regime is reported as neutral so callers fall back to their
    normal behaviour instead of acting on a half-computed score."""
    def _history(ticker: str, hist_period: str) -> pd.DataFrame:
        if feed is not None:
            return feed.price_history(ticker, asof=asof, period=hist_period)
        return fetch_price_history(ticker, period=hist_period)

    history: dict[str, pd.DataFrame] = {}
    for _, num, den, _ in RATIOS:
        for ticker in (num, den):
            if ticker not in history:
                history[ticker] = _history(ticker, period)

    components: dict[str, float] = {}
    notes: list[str] = []
    for label, num, den, meaning in RATIOS:
        score = _ratio_score(history[num], history[den])
        if score is None:
            logger.warning("Macro ratio %s (%s/%s) unavailable", label, num, den)
            continue
        components[label] = score
        direction = "supportive" if score > 0.1 else ("adverse" if score < -0.1 else "flat")
        notes.append(f"{label} {score:+.2f} ({direction}: {meaning})")

    if len(components) < 3:
        logger.error("Only %d/%d macro ratios available — reporting UNKNOWN regime (no new entries)",
                     len(components), len(RATIOS))
        return MacroRegime(score=0.0, label="unknown", components=components,
                           notes=notes + ["insufficient macro data; fail-closed, no new entries"])

    base = sum(components.values()) / len(components)

    vix_value: float | None = None
    vix_df = _history(VIX_TICKER, "3mo")
    if not vix_df.empty:
        vix_value = float(vix_df["Close"].iloc[-1])
        adjustment, note = _vix_adjustment(vix_value)
        base = _clip(base + adjustment)
        notes.append(note)

    if base >= 0.20:
        label = "risk_on"
    elif base <= -0.20:
        label = "risk_off"
    else:
        label = "neutral"

    return MacroRegime(score=_clip(base), label=label, components=components, vix=vix_value, notes=notes)
