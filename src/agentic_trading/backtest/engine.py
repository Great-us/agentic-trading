"""Replay the live cycle engine against historical daily bars."""
from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path

import pandas as pd

from ..config import Settings, load_settings
from ..data.feed import HistoricalFeed
from ..execution.sim_broker import Fill, SimulatedBroker
from ..journal.logger import connect
from ..run import run_cycle
from .data import load_price_history
from .metrics import Metrics, buy_and_hold, compute_metrics, equal_weight_hold

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[3]
BACKTEST_DIR = ROOT / "data" / "backtest"


@dataclass
class BacktestResult:
    metrics: Metrics
    spy_metrics: Metrics | None
    equal_weight_metrics: Metrics | None
    equity: pd.Series
    spy_equity: pd.Series | None
    fills: list[Fill]
    settings: Settings


def _as_dates(idx: pd.DatetimeIndex) -> pd.DatetimeIndex:
    naive = idx.tz_localize(None) if idx.tz is not None else idx
    return pd.DatetimeIndex(naive.normalize())


def _session_dates(spy: pd.DataFrame, start: date, end: date) -> list[pd.Timestamp]:
    if spy.empty:
        return []
    idx = _as_dates(spy.index)
    start_ts = pd.Timestamp(start)
    end_ts = pd.Timestamp(end)
    return [ts for ts in idx if start_ts <= ts <= end_ts]


def run_backtest(
    start: str | date,
    end: str | date,
    *,
    settings: Settings | None = None,
    initial_cash: float = 100_000.0,
    slippage_bps: float = 5.0,
    stop_slippage_bps: float = 10.0,
    bars: dict[str, pd.DataFrame] | None = None,
    journal_path: Path | str | None = None,
    risk_overrides: dict | None = None,
    quiet: bool = True,
) -> BacktestResult:
    """Quant-only daily replay. Decisions at T close, fills at T+1 open."""
    start_d = date.fromisoformat(start) if isinstance(start, str) else start
    end_d = date.fromisoformat(end) if isinstance(end, str) else end

    settings = settings or load_settings(include_research=False)
    settings.watchlist = list(settings.core_watchlist or settings.watchlist)
    if risk_overrides:
        settings = replace(settings, risk=replace(settings.risk, **risk_overrides))

    if bars is None:
        logger.info("Downloading / loading cached price history for %d symbols + macro.", len(settings.watchlist))
        bars = load_price_history(settings.watchlist)

    spy = bars.get("SPY", pd.DataFrame())
    dates = _session_dates(spy, start_d, end_d)
    if not dates:
        # Fall back to the union of watchlist calendars if SPY is missing (unit tests).
        union: set[pd.Timestamp] = set()
        for df in bars.values():
            if df is None or df.empty:
                continue
            union.update(_session_dates(df, start_d, end_d))
        dates = sorted(union)
    if not dates:
        raise ValueError(f"No trading dates between {start_d} and {end_d}")

    feed = HistoricalFeed(bars)
    broker = SimulatedBroker(
        starting_cash=initial_cash,
        slippage_bps=slippage_bps,
        stop_slippage_bps=stop_slippage_bps,
        atr_stop_multiple=settings.risk.atr_stop_multiple,
        min_stop_pct=settings.risk.min_stop_pct,
        max_stop_pct=settings.risk.max_stop_pct,
        bars=bars,
    )
    if journal_path is None:
        journal_path = ":memory:"
    conn = connect(journal_path)

    if quiet:
        logging.getLogger("run_cycle").setLevel(logging.CRITICAL)
        logging.getLogger("agentic_trading.signals.macro").setLevel(logging.CRITICAL)
        logging.getLogger("agentic_trading.data.market_data").setLevel(logging.CRITICAL)

    logger.info("Backtest %s → %s, %d sessions, %d symbols, cash=$%.0f",
                start_d, end_d, len(dates), len(settings.watchlist), initial_cash)

    for i, session in enumerate(dates):
        broker.process_bar(session)
        run_cycle(
            skip_llm=True,
            settings=settings,
            broker=broker,
            feed=feed,
            conn=conn,
            asof=session,
        )
        if (i + 1) % 252 == 0:
            logger.info("  ... %s equity=$%.0f positions=%d",
                        session.date(), broker.equity(), len(broker.positions))

    # Fill orders submitted on the last close, if a next session exists in the data.
    spy_idx = _as_dates(spy.index) if not spy.empty else pd.DatetimeIndex([])
    later = [ts for ts in spy_idx if ts > dates[-1]]
    if later:
        broker.process_bar(later[0])
    elif dates:
        # Synthetic fixtures may not have a trailing session; still snapshot equity.
        if not broker.equity_curve or broker.equity_curve[-1][0] != dates[-1]:
            broker.equity_curve.append((dates[-1], broker.equity(), broker.cash))

    conn.close()

    metrics = compute_metrics(broker.equity_curve, broker.fills, starting_cash=initial_cash)
    equity = pd.Series(
        {pd.Timestamp(t).tz_localize(None) if pd.Timestamp(t).tzinfo else pd.Timestamp(t): e
         for t, e, _ in broker.equity_curve},
        dtype=float,
    ).sort_index()
    equity = equity[~equity.index.duplicated(keep="last")]

    spy_metrics = None
    spy_equity = None
    if not spy.empty:
        spy_close = spy["Close"].copy()
        spy_close.index = _as_dates(spy_close.index)
        spy_close = spy_close.loc[(spy_close.index >= pd.Timestamp(start_d)) & (spy_close.index <= pd.Timestamp(end_d))]
        spy_equity = buy_and_hold(spy_close, initial_cash)
        if not spy_equity.empty:
            spy_curve = [(ts, float(v), 0.0) for ts, v in spy_equity.items()]
            spy_metrics = compute_metrics(spy_curve, [], starting_cash=initial_cash)

    ew_metrics = None
    watch_bars = {s: bars[s] for s in settings.watchlist if s in bars}
    date_index = pd.DatetimeIndex(dates)
    ew = equal_weight_hold(watch_bars, initial_cash, date_index)
    if not ew.empty:
        ew_curve = [(ts, float(v), 0.0) for ts, v in ew.items()]
        ew_metrics = compute_metrics(ew_curve, [], starting_cash=initial_cash)

    return BacktestResult(
        metrics=metrics,
        spy_metrics=spy_metrics,
        equal_weight_metrics=ew_metrics,
        equity=equity,
        spy_equity=spy_equity,
        fills=broker.fills,
        settings=settings,
    )


def format_report(result: BacktestResult) -> str:
    m = result.metrics
    lines = [
        f"Backtest  {result.equity.index[0].date() if len(result.equity) else '?'} → "
        f"{result.equity.index[-1].date() if len(result.equity) else '?'}",
        f"Universe  {len(result.settings.watchlist)} symbols: {', '.join(result.settings.watchlist)}",
        "",
        f"{'':16} {'Strategy':>12} {'SPY':>12} {'Eq-weight':>12}",
        f"{'CAGR':16} {_pct(m.cagr):>12} {_pct(_opt(result.spy_metrics, 'cagr')):>12} {_pct(_opt(result.equal_weight_metrics, 'cagr')):>12}",
        f"{'Volatility':16} {_pct(m.volatility):>12} {_pct(_opt(result.spy_metrics, 'volatility')):>12} {_pct(_opt(result.equal_weight_metrics, 'volatility')):>12}",
        f"{'Sharpe':16} {_num(m.sharpe):>12} {_num(_opt(result.spy_metrics, 'sharpe')):>12} {_num(_opt(result.equal_weight_metrics, 'sharpe')):>12}",
        f"{'Max drawdown':16} {_pct(m.max_drawdown):>12} {_pct(_opt(result.spy_metrics, 'max_drawdown')):>12} {_pct(_opt(result.equal_weight_metrics, 'max_drawdown')):>12}",
        f"{'Calmar':16} {_num(m.calmar):>12} {_num(_opt(result.spy_metrics, 'calmar')):>12} {_num(_opt(result.equal_weight_metrics, 'calmar')):>12}",
        f"{'End equity':16} {_usd(m.end_equity):>12} {_usd(_opt(result.spy_metrics, 'end_equity')):>12} {_usd(_opt(result.equal_weight_metrics, 'end_equity')):>12}",
        "",
        _trades_line(m),
        f"Avg exposure: {m.avg_exposure:.0%}"
        + (f"   profit factor {m.profit_factor:.2f}" if m.profit_factor is not None else ""),
        "",
        "Calendar years (strategy):",
    ]
    for year, ret in sorted(m.years.items()):
        spy_y = result.spy_metrics.years.get(year) if result.spy_metrics else None
        extra = f"   SPY {_pct(spy_y)}" if spy_y is not None else ""
        lines.append(f"  {year}  {_pct(ret)}{extra}")
    return "\n".join(lines)


def _trades_line(m: Metrics) -> str:
    line = f"Trades: {m.n_buys} buys / {m.n_sells} sells"
    if m.win_rate is not None:
        line += f"   win rate {m.win_rate:.0%}"
    return line


def _opt(metrics: Metrics | None, attr: str):
    return getattr(metrics, attr) if metrics is not None else None


def _pct(value: float | None) -> str:
    return "         n/a" if value is None else f"{value:+.1%}"


def _num(value: float | None) -> str:
    return "         n/a" if value is None else f"{value: .2f}"


def _usd(value: float | None) -> str:
    return "         n/a" if value is None else f"${value:,.0f}"
