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

import pandas as pd

from ..config import RiskConfig


@dataclass
class SizeResult:
    symbol: str
    approved: bool
    notional: float   # dollars to deploy; fractional shares let this be exact
    reason: str


@dataclass
class TrimPlan:
    symbol: str
    qty: float
    notional: float
    full_exit: bool


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


def portfolio_stop_risk(
    positions: dict,
    atrs: dict[str, float],
    risk: RiskConfig,
) -> float:
    """Dollars currently at risk if every open stop is hit, using ATR distance
    from each position's average entry (same formula as the live stop)."""
    total = 0.0
    for symbol, pos in positions.items():
        atr = atrs.get(symbol)
        entry = getattr(pos, "avg_entry_price", 0.0)
        value = getattr(pos, "market_value", 0.0)
        if atr is None or entry <= 0 or value <= 0:
            continue
        total += value * stop_distance_pct(atr, entry, risk)
    return total


def average_corr_to_holdings(
    candidate_close: pd.Series,
    holding_closes: dict[str, pd.Series],
    window: int = 60,
) -> float | None:
    """Mean pairwise return correlation of `candidate` vs current holdings
    over the last `window` overlapping sessions. None if there isn't enough
    overlap to say anything."""
    def _naive(series: pd.Series) -> pd.Series:
        out = series.dropna().copy()
        idx = pd.DatetimeIndex(out.index)
        if idx.tz is not None:
            idx = idx.tz_localize(None)
        out.index = idx.normalize()
        return out[~out.index.duplicated(keep="last")]

    if not holding_closes:
        return None
    cand = _naive(candidate_close).tail(window + 5)
    if len(cand) < 20:
        return None
    corrs: list[float] = []
    cand_ret = cand.pct_change().dropna()
    for series in holding_closes.values():
        other = _naive(series).pct_change().dropna()
        joined = pd.concat([cand_ret, other], axis=1, join="inner").dropna()
        if len(joined) < 20:
            continue
        tail = joined.tail(window)
        value = float(tail.iloc[:, 0].corr(tail.iloc[:, 1]))
        if value == value:  # not NaN
            corrs.append(value)
    if not corrs:
        return None
    return float(sum(corrs) / len(corrs))


def effective_max_exposure_pct(risk: RiskConfig, regime_label: str | None) -> float:
    table = getattr(risk, "regime_max_exposure", None) or {}
    if not regime_label or regime_label not in table:
        cap = risk.max_total_exposure_pct
    else:
        cap = float(table[regime_label])
    # Permanent cash reserve: the book is never deployed past 1 - buffer,
    # whatever the regime table allows. Partial-Kelly style — the cost of
    # forgoing the last few percent of exposure is small next to always
    # having (and being seen to have) room left.
    buffer = float(getattr(risk, "min_cash_buffer_pct", 0.0) or 0.0)
    if buffer > 0.0:
        cap = min(cap, 1.0 - buffer)
    return max(0.0, cap)


def sector_of(symbol: str, mapping: dict[str, str] | None) -> str:
    if not mapping:
        return "Unknown"
    return mapping.get(symbol, "Unknown")


def sector_cap_pct(sector: str, risk: RiskConfig) -> float:
    caps = getattr(risk, "max_sector_pct", None) or {}
    if sector in caps:
        return float(caps[sector])
    return float(getattr(risk, "default_max_sector_pct", 0.35) or 0.35)


def sector_invested(positions: dict, mapping: dict[str, str] | None) -> dict[str, float]:
    out: dict[str, float] = {}
    for symbol, pos in positions.items():
        value = getattr(pos, "market_value", 0.0) or 0.0
        if value <= 0:
            continue
        sec = sector_of(symbol, mapping)
        out[sec] = out.get(sec, 0.0) + value
    return out


def sector_room_dollars(
    symbol: str,
    equity: float,
    occupancy: dict[str, float],
    mapping: dict[str, str] | None,
    risk: RiskConfig,
) -> float:
    if equity <= 0:
        return 0.0
    sec = sector_of(symbol, mapping)
    cap = sector_cap_pct(sec, risk)
    used = occupancy.get(sec, 0.0)
    return max(0.0, equity * cap - used)


def theme_room_dollars(
    symbol: str,
    equity: float,
    positions: dict,
    risk: RiskConfig,
    *,
    reserved: dict[str, float] | None = None,
) -> float | None:
    """Remaining room for an optional cross-sector research basket.

    ``None`` means the symbol is outside the configured basket, so ordinary
    sector/position limits remain the only constraints.  Reserved notionals
    count same-cycle buys that have been sized but not filled yet.
    """
    theme = set(getattr(risk, "theme_symbols", None) or [])
    if symbol not in theme:
        return None
    if equity <= 0:
        return 0.0
    reserved = reserved or {}
    held_symbols = {
        name for name, pos in positions.items()
        if name in theme and (getattr(pos, "market_value", 0.0) or 0.0) > 0
    }
    reserved_symbols = {name for name, value in reserved.items() if name in theme and value > 0}
    max_positions = int(getattr(risk, "max_theme_positions", 1_000_000) or 0)
    if symbol not in held_symbols and symbol not in reserved_symbols:
        if len(held_symbols | reserved_symbols) >= max_positions:
            return 0.0
    used = sum(
        float(getattr(pos, "market_value", 0.0) or 0.0)
        for name, pos in positions.items() if name in theme
    ) + sum(float(value) for name, value in reserved.items() if name in theme)
    cap = float(getattr(risk, "max_theme_pct", 1.0) or 0.0)
    return max(0.0, equity * cap - used)


def plan_trims(
    positions: dict,
    scores: dict[str, float],
    equity: float,
    invested_value: float,
    prices: dict[str, float],
    risk: RiskConfig,
    target_exposure_pct: float,
    skip: set[str] | None = None,
) -> list[TrimPlan]:
    """Weakest-first partial (or full) sells to bring invested value down to
    `target_exposure_pct * equity`. Names in `skip` (already full-exiting this
    cycle) are left alone. Missing scores are treated as weakest."""
    if equity <= 0 or target_exposure_pct < 0:
        return []
    excess = invested_value - equity * target_exposure_pct
    if excess <= 1.0:
        return []
    blocked = skip or set()
    min_pos = equity * risk.min_position_pct
    ranked = sorted(
        (s for s in positions if s not in blocked),
        key=lambda s: scores.get(s, float("-inf")),
    )
    plans: list[TrimPlan] = []
    for symbol in ranked:
        if excess <= 1.0:
            break
        pos = positions[symbol]
        value = getattr(pos, "market_value", 0.0) or 0.0
        qty_full = getattr(pos, "qty", 0.0) or 0.0
        price = prices.get(symbol) or getattr(pos, "current_price", 0.0) or 0.0
        if value <= 0 or qty_full <= 0 or price <= 0:
            continue
        take_value = min(value, excess)
        remaining = value - take_value
        full_exit = remaining < min_pos
        if full_exit:
            take_qty = qty_full
            take_value = value
        else:
            take_qty = take_value / price
            if take_qty >= qty_full - 1e-9:
                full_exit = True
                take_qty = qty_full
                take_value = value
        plans.append(TrimPlan(
            symbol=symbol, qty=take_qty, notional=round(take_value, 2), full_exit=full_exit,
        ))
        excess -= take_value
    return plans


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
    existing_stop_risk: float = 0.0,
    corr_multiplier: float = 1.0,
    sector_room: float | None = None,
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

    # Every cap is tracked by name so a veto can say which one actually bound.
    # Reporting only the surviving number reads as "out of cash" whatever the
    # real constraint was, and a $0 that means "exposure cap already breached"
    # has been misread as a broker-cash bug before.
    capped = min(target, equity * risk.max_position_pct)
    exposure_budget = max(0.0, equity * risk.max_total_exposure_pct - invested_value)
    limits: list[tuple[float, str]] = [
        (capped, f"max_position_pct {risk.max_position_pct:.0%}"),
        (exposure_budget,
         f"the exposure cap — invested {invested_value / equity:.1%} "
         f"of a {risk.max_total_exposure_pct:.0%} ceiling"),
        (cash, "cash"),
    ]

    port_cap = getattr(risk, "max_portfolio_stop_risk_pct", 0.0) or 0.0
    if port_cap > 0 and stop_pct > 0:
        remaining_risk = max(0.0, equity * port_cap - existing_stop_risk)
        limits.append((remaining_risk / stop_pct,
                       f"the book stop-risk budget — {existing_stop_risk / equity:.2%} "
                       f"of {port_cap:.0%} already committed"))

    notional, binding = min(limits, key=lambda item: item[0])

    if corr_multiplier < 1.0:
        notional *= corr_multiplier
        binding += f" then the {corr_multiplier:.0%} correlation haircut"

    if sector_room is not None and max(0.0, sector_room) < notional:
        notional = max(0.0, sector_room)
        binding = "sector cap"

    notional = round(max(notional, 0.0), 2)

    if notional < equity * risk.min_position_pct:
        return SizeResult(
            symbol, False, 0.0,
            f"only ${notional:,.0f} available (limited by {binding}), "
            f"below the {risk.min_position_pct:.0%} minimum position",
        )

    detail = f"${notional:,.0f} ({notional / equity:.0%} of equity, {stop_pct:.1%} stop ≈ ${notional * stop_pct:,.0f} at risk)"
    detail += f"; set by {binding}"
    if capped < target:
        detail += "; capped by max_position_pct"
    if port_cap > 0 and existing_stop_risk > 0:
        detail += f"; book stop-risk {existing_stop_risk / equity:.1%}/{port_cap:.0%}"
    if corr_multiplier < 1.0:
        detail += f"; cut to {corr_multiplier:.0%} for correlation vs holdings"
    if sector_room is not None and sector_room < target:
        detail += "; capped by sector limit"
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
