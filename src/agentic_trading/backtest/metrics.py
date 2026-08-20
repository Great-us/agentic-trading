"""Performance stats from an equity curve and a fill list."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from ..execution.sim_broker import Fill


@dataclass
class Metrics:
    start_equity: float
    end_equity: float
    cagr: float
    volatility: float
    sharpe: float
    max_drawdown: float
    calmar: float
    n_buys: int
    n_sells: int
    win_rate: float | None
    profit_factor: float | None
    avg_exposure: float
    years: dict[int, float]


def _equity_series(curve: list[tuple[pd.Timestamp, float, float]]) -> pd.Series:
    idx = [pd.Timestamp(t).tz_localize(None) if pd.Timestamp(t).tzinfo else pd.Timestamp(t) for t, _, _ in curve]
    vals = [e for _, e, _ in curve]
    s = pd.Series(vals, index=pd.DatetimeIndex(idx))
    return s[~s.index.duplicated(keep="last")].sort_index()


def max_drawdown(equity: pd.Series) -> float:
    if equity.empty:
        return 0.0
    peak = equity.cummax()
    dd = equity / peak.replace(0, np.nan) - 1.0
    return float(dd.min()) if len(dd) else 0.0


def cagr(equity: pd.Series) -> float:
    if len(equity) < 2 or equity.iloc[0] <= 0:
        return 0.0
    days = (equity.index[-1] - equity.index[0]).days
    years = days / 365.25
    if years <= 0:
        return 0.0
    return float((equity.iloc[-1] / equity.iloc[0]) ** (1 / years) - 1)


def yearly_returns(equity: pd.Series) -> dict[int, float]:
    if equity.empty:
        return {}
    out: dict[int, float] = {}
    grouped = equity.groupby(equity.index.year)
    for year, series in grouped:
        if len(series) < 2 or series.iloc[0] <= 0:
            continue
        out[int(year)] = float(series.iloc[-1] / series.iloc[0] - 1)
    return out


def _round_trip_pnls(fills: list[Fill]) -> list[float]:
    """Match buys to later sells FIFO per symbol; leftover longs are ignored."""
    lots: dict[str, list[tuple[float, float]]] = {}
    pnls: list[float] = []
    for fill in fills:
        if fill.side == "buy":
            lots.setdefault(fill.symbol, []).append((fill.qty, fill.price))
            continue
        remaining = fill.qty
        queue = lots.setdefault(fill.symbol, [])
        while remaining > 1e-9 and queue:
            qty, entry = queue[0]
            taken = min(qty, remaining)
            pnls.append((fill.price - entry) * taken)
            remaining -= taken
            if taken + 1e-9 >= qty:
                queue.pop(0)
            else:
                queue[0] = (qty - taken, entry)
    return pnls


def compute_metrics(
    curve: list[tuple[pd.Timestamp, float, float]],
    fills: list[Fill],
    *,
    starting_cash: float,
) -> Metrics:
    equity = _equity_series(curve)
    if equity.empty:
        return Metrics(
            start_equity=starting_cash, end_equity=starting_cash, cagr=0.0, volatility=0.0,
            sharpe=0.0, max_drawdown=0.0, calmar=0.0, n_buys=0, n_sells=0,
            win_rate=None, profit_factor=None, avg_exposure=0.0, years={},
        )
    rets = equity.pct_change().dropna()
    vol = float(rets.std() * np.sqrt(252)) if len(rets) else 0.0
    sharpe = float(rets.mean() / rets.std() * np.sqrt(252)) if len(rets) and rets.std() > 0 else 0.0
    dd = max_drawdown(equity)
    cagr_val = cagr(equity)
    calmar = float(cagr_val / abs(dd)) if dd < 0 else 0.0

    cash = pd.Series([c for _, _, c in curve], index=equity.index[:len(curve)])
    cash = cash[~cash.index.duplicated(keep="last")]
    aligned = cash.reindex(equity.index).ffill()
    exposure = 1.0 - (aligned / equity.replace(0, np.nan))
    avg_exposure = float(exposure.clip(lower=0).mean()) if len(exposure) else 0.0

    pnls = _round_trip_pnls(fills)
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    win_rate = (len(wins) / len(pnls)) if pnls else None
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    profit_factor = (gross_win / gross_loss) if gross_loss > 0 else None

    n_buys = sum(1 for f in fills if f.side == "buy")
    n_sells = sum(1 for f in fills if f.side == "sell")

    return Metrics(
        start_equity=float(equity.iloc[0]),
        end_equity=float(equity.iloc[-1]),
        cagr=cagr_val,
        volatility=vol,
        sharpe=sharpe,
        max_drawdown=dd,
        calmar=calmar,
        n_buys=n_buys,
        n_sells=n_sells,
        win_rate=win_rate,
        profit_factor=profit_factor,
        avg_exposure=avg_exposure,
        years=yearly_returns(equity),
    )


def buy_and_hold(prices: pd.Series, starting_cash: float) -> pd.Series:
    """Fully invested in one series from the first valid close."""
    p = prices.dropna()
    if p.empty or p.iloc[0] <= 0:
        return pd.Series(dtype=float)
    shares = starting_cash / float(p.iloc[0])
    return shares * p


def equal_weight_hold(bars: dict[str, pd.DataFrame], starting_cash: float, dates: pd.DatetimeIndex) -> pd.Series:
    """Buy 1/N of cash in each symbol on the first date they all (that exist) have a close."""
    closes = {}
    for symbol, df in bars.items():
        if df is None or df.empty or "Close" not in df.columns:
            continue
        s = df["Close"].copy()
        s.index = pd.DatetimeIndex(s.index).tz_localize(None)
        s.index = s.index.normalize()
        closes[symbol] = s
    if not closes:
        return pd.Series(dtype=float)
    panel = pd.DataFrame(closes).reindex(dates).ffill()
    first = panel.iloc[0].dropna()
    if first.empty:
        return pd.Series(dtype=float)
    per = starting_cash / len(first)
    qty = per / first
    value = panel[first.index].mul(qty, axis=1).sum(axis=1)
    return value
