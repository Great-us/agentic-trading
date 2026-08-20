"""Hard risk limits: position sizing and exit overrides.

These run independently of the quant/LLM decision engine and can force a SELL
(stop-loss, trailing stop, take-profit) or veto a BUY (exposure/position caps)
regardless of what the signals say. Risk limits always win.

Two things here are deliberately adaptive rather than fixed percentages:

- **Stops scale with ATR.** A flat 5% stop is roughly one day's range on a
  60%-volatility name — normal noise takes you out at the worst moment — while
  being far too loose on a quiet one. Distance is ATR-derived and clamped.
- **Size scales with the market regime.** In a risk-off tape every position is
  cut by `risk_off_size_multiplier`, so aggression is conditional on the
  environment instead of constant.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..config import RiskConfig


@dataclass
class SizeResult:
    symbol: str
    approved: bool
    notional: float   # dollars to deploy; fractional shares let this be exact
    reason: str


@dataclass
class ExitSignal:
    trigger: str      # stop_loss | trailing_stop | take_profit
    detail: str


def stop_distance_pct(atr14: float, last_price: float, risk: RiskConfig) -> float:
    """ATR-derived stop distance as a fraction of price, clamped to the config
    bounds. Falls back to the minimum when ATR is unusable."""
    if last_price <= 0 or atr14 <= 0:
        return risk.min_stop_pct
    raw = (atr14 * risk.atr_stop_multiple) / last_price
    return max(risk.min_stop_pct, min(risk.max_stop_pct, raw))


def protective_stop_price(
    entry_price: float,
    high_water_mark: float,
    atr14: float,
    risk: RiskConfig,
) -> float:
    """Where the broker-side stop should sit right now.

    Two levels compete and the higher (tighter) one wins: the ATR stop measured
    from entry, and the trailing stop measured from the peak. Early in a trade
    the ATR level governs; once the position runs, the trailing level overtakes
    it and ratchets upward with each new high. Because this only ever moves up,
    re-placing the order each cycle reproduces a trailing stop using the plain
    stop orders that Alpaca allows on fractional positions.
    """
    atr_level = entry_price * (1 - stop_distance_pct(atr14, entry_price, risk))
    peak = max(high_water_mark, entry_price)
    trail_level = peak * (1 - risk.trailing_stop_pct)
    return round(max(atr_level, trail_level), 2)


def size_position(
    symbol: str,
    last_price: float,
    equity: float,
    cash: float,
    invested_value: float,
    open_position_count: int,
    risk: RiskConfig,
    stop_pct: float,
    regime_multiplier: float = 1.0,
) -> SizeResult:
    """Equal-risk sizing: the position is scaled so that hitting its stop costs
    about `risk_per_trade_pct` of equity, whatever the stock's volatility.

    A flat percentage of equity would put three times more at stake on a
    20%-stop name than on a 6%-stop one while calling both the same "size".
    """
    if last_price <= 0:
        return SizeResult(symbol, False, 0.0, "invalid price")
    if open_position_count >= risk.max_open_positions:
        return SizeResult(symbol, False, 0.0, f"at max_open_positions ({risk.max_open_positions})")
    if stop_pct <= 0:
        return SizeResult(symbol, False, 0.0, "invalid stop distance")

    risk_budget = equity * risk.risk_per_trade_pct * regime_multiplier
    target = risk_budget / stop_pct

    capped = min(target, equity * risk.max_position_pct)
    exposure_budget = max(0.0, equity * risk.max_total_exposure_pct - invested_value)
    notional = round(min(capped, exposure_budget, cash), 2)

    if notional < equity * risk.min_position_pct:
        return SizeResult(
            symbol, False, 0.0,
            f"only ${notional:,.0f} available, below the {risk.min_position_pct:.0%} minimum position",
        )

    detail = f"${notional:,.0f} ({notional / equity:.0%} of equity, {stop_pct:.1%} stop ≈ ${notional * stop_pct:,.0f} at risk)"
    if capped < target:
        detail += "; capped by max_position_pct"
    if regime_multiplier < 1.0:
        detail += f"; cut to {regime_multiplier:.0%} for risk-off regime"
    return SizeResult(symbol, True, notional, f"sized to {detail}")


def check_exit(
    entry_price: float,
    last_price: float,
    high_water_mark: float,
    atr14: float,
    risk: RiskConfig,
) -> ExitSignal | None:
    """Exit checks, most protective first. `high_water_mark` is the highest price
    seen since entry (tracked in the journal), which is what makes the trailing
    stop let winners run instead of capping them at a fixed target."""
    if entry_price <= 0 or last_price <= 0:
        return None

    # Measured against the ENTRY price, not the current one: ATR is a dollar
    # amount, so dividing it by a falling price would widen the stop as the
    # position moved against us and the exit would never catch up.
    stop_pct = stop_distance_pct(atr14, entry_price, risk)
    change = (last_price - entry_price) / entry_price
    if change <= -stop_pct:
        return ExitSignal(
            "stop_loss",
            f"down {change:.1%} from entry {entry_price:.2f}, ATR stop was {stop_pct:.1%}",
        )

    peak = max(high_water_mark, entry_price)
    drawdown = (last_price - peak) / peak
    if drawdown <= -risk.trailing_stop_pct and peak > entry_price:
        return ExitSignal(
            "trailing_stop",
            f"down {drawdown:.1%} from peak {peak:.2f} (entry {entry_price:.2f}, still {change:+.1%} on the trade)",
        )

    if risk.take_profit_pct is not None and change >= risk.take_profit_pct:
        return ExitSignal("take_profit", f"up {change:.1%} from entry {entry_price:.2f}")

    return None
