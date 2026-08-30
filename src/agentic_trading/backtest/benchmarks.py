"""Cash-aware and risk-matched benchmark construction."""
from __future__ import annotations

import numpy as np
import pandas as pd

from .universe import UniverseSchedule


def _normalise(series: pd.Series) -> pd.Series:
    out = series.astype(float).copy()
    idx = pd.DatetimeIndex(out.index)
    if idx.tz is not None:
        idx = idx.tz_localize(None)
    out.index = idx.normalize()
    return out[~out.index.duplicated(keep="last")].sort_index()


def daily_cash_returns_from_yield(yield_close: pd.Series, dates: pd.DatetimeIndex) -> pd.Series:
    """Convert an annual percentage yield (for example Yahoo ``^IRX``) to daily returns."""
    annual = _normalise(yield_close).reindex(pd.DatetimeIndex(dates)).ffill().fillna(0.0)
    annual = annual.clip(lower=0.0) / 100.0
    return (1.0 + annual) ** (1.0 / 252.0) - 1.0


def equity_from_returns(returns: pd.Series, starting_cash: float = 100_000.0) -> pd.Series:
    rets = _normalise(returns).fillna(0.0)
    if rets.empty:
        return pd.Series(dtype=float)
    rets.iloc[0] = 0.0
    return starting_cash * (1.0 + rets).cumprod()


def allocation_benchmark(
    risky_prices: pd.Series,
    cash_returns: pd.Series,
    dates: pd.DatetimeIndex,
    *,
    risky_weight: float | pd.Series,
    starting_cash: float = 100_000.0,
) -> pd.Series:
    """Daily-rebalanced risky/cash portfolio; dynamic weights are lagged one session."""
    index = pd.DatetimeIndex(dates)
    risky = _normalise(risky_prices).reindex(index).ffill()
    risky_rets = risky.pct_change().fillna(0.0)
    cash_rets = _normalise(cash_returns).reindex(index).fillna(0.0)

    if isinstance(risky_weight, pd.Series):
        weights = _normalise(risky_weight).reindex(index).ffill().shift(1).fillna(0.0)
    else:
        weights = pd.Series(float(risky_weight), index=index)
    weights = weights.clip(lower=0.0, upper=1.0)
    returns = weights * risky_rets + (1.0 - weights) * cash_rets
    return equity_from_returns(returns, starting_cash)


def cash_equity(
    cash_returns: pd.Series,
    dates: pd.DatetimeIndex,
    starting_cash: float = 100_000.0,
) -> pd.Series:
    returns = _normalise(cash_returns).reindex(pd.DatetimeIndex(dates)).fillna(0.0)
    return equity_from_returns(returns, starting_cash)


def point_in_time_universe_returns(
    bars: dict[str, pd.DataFrame],
    schedule: UniverseSchedule,
    dates: pd.DatetimeIndex,
    *,
    sector_neutral: bool = False,
) -> pd.Series:
    """Daily equal-weight return of the membership known at the prior close.

    The one-session membership lag is deliberate: a return from T-1 close to T
    close can only belong to a portfolio formed at T-1.  Missing constituent
    returns stay missing and are excluded rather than forward-filled.
    """
    index = pd.DatetimeIndex(dates)
    closes: dict[str, pd.Series] = {}
    for symbol in schedule.symbols:
        frame = bars.get(symbol)
        if frame is None or frame.empty or "Close" not in frame:
            continue
        closes[symbol] = _normalise(frame["Close"]).reindex(index)
    if not closes:
        return pd.Series(dtype=float)
    returns = pd.DataFrame(closes, index=index).pct_change(fill_method=None)
    out = pd.Series(0.0, index=index, dtype=float)
    for position in range(1, len(index)):
        active = schedule.active_on(index[position - 1])
        available = [symbol for symbol in active if symbol in returns.columns]
        row = returns.loc[index[position], available].dropna()
        if row.empty:
            continue
        if not sector_neutral:
            out.iloc[position] = float(row.mean())
            continue
        grouped: dict[str, list[float]] = {}
        for symbol, value in row.items():
            sector = schedule.sector_for(str(symbol)) or "Unknown"
            grouped.setdefault(sector, []).append(float(value))
        out.iloc[position] = float(np.mean([
            np.mean(values) for values in grouped.values() if values
        ]))
    return out


def random_portfolio_placebo_returns(
    bars: dict[str, pd.DataFrame],
    schedule: UniverseSchedule,
    dates: pd.DatetimeIndex,
    *,
    samples: int = 500,
    positions: int = 8,
    max_per_sector: int = 2,
    seed: int = 20260822,
) -> pd.DataFrame:
    """Monthly random-stock portfolios drawn from the historical universe.

    Each path rebalances on the first available session of a month using the
    prior session's membership.  The draw is deterministic and sector-capped;
    it is a placebo for stock selection, not a replacement for the benchmark.
    """
    index = pd.DatetimeIndex(dates)
    closes: dict[str, pd.Series] = {}
    for symbol in schedule.symbols:
        frame = bars.get(symbol)
        if frame is not None and not frame.empty and "Close" in frame:
            closes[symbol] = _normalise(frame["Close"]).reindex(index)
    if not closes or samples <= 0:
        return pd.DataFrame(index=index)
    returns = pd.DataFrame(closes, index=index).pct_change(fill_method=None)
    result = np.zeros((len(index), samples), dtype=float)
    rng = np.random.default_rng(seed)
    holdings: list[list[str]] = [[] for _ in range(samples)]
    previous_month: tuple[int, int] | None = None
    for offset in range(1, len(index)):
        formation_day = index[offset - 1]
        month = (formation_day.year, formation_day.month)
        if month != previous_month:
            candidates = [
                symbol for symbol in schedule.active_on(formation_day)
                if symbol in returns.columns
            ]
            for sample in range(samples):
                order = rng.permutation(len(candidates))
                selected: list[str] = []
                sector_counts: dict[str, int] = {}
                for candidate_offset in order:
                    symbol = candidates[int(candidate_offset)]
                    sector = schedule.sector_for(symbol) or "Unknown"
                    if sector == "Unknown":
                        # Missing metadata must not force all unknown names into
                        # one synthetic sector and accidentally cap a placebo
                        # portfolio at two positions.
                        sector = f"Unknown:{symbol}"
                    if sector_counts.get(sector, 0) >= max_per_sector:
                        continue
                    selected.append(symbol)
                    sector_counts[sector] = sector_counts.get(sector, 0) + 1
                    if len(selected) >= positions:
                        break
                holdings[sample] = selected
            previous_month = month
        row = returns.iloc[offset]
        for sample, selected in enumerate(holdings):
            values = row.reindex(selected).dropna()
            result[offset, sample] = 0.0 if values.empty else float(values.mean())
    return pd.DataFrame(
        result,
        index=index,
        columns=[f"placebo_{sample:03d}" for sample in range(samples)],
    )
