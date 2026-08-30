"""Walk-forward, parameter surface, and component ablations on top of run_backtest.

These answer "is 0.35 just the in-sample winner?" and "which pieces of the
machine actually contribute return?" — they do not retune live by themselves.
"""
from __future__ import annotations

import itertools
import logging
from dataclasses import dataclass
from datetime import date
from typing import Iterator

import pandas as pd

import math

from ..signals.technical import weights_without
from .data import load_price_history
from .engine import BacktestResult, run_backtest
from .metrics import Metrics, compute_metrics
from .overfit import deflated_sharpe_ratio, minimum_backtest_length, pbo_cscv

logger = logging.getLogger(__name__)

SURFACE_BUY = (0.20, 0.25, 0.35)
SURFACE_ATR = (2.0, 2.5, 3.0)


def _grid(buy=SURFACE_BUY, atr=SURFACE_ATR) -> Iterator[dict]:
    for buy_threshold, atr_stop_multiple in itertools.product(buy, atr):
        yield {
            "buy_threshold": buy_threshold,
            "atr_stop_multiple": atr_stop_multiple,
        }


def _label(overrides: dict) -> str:
    return " ".join(f"{k}={v}" for k, v in overrides.items())


def _score(metrics: Metrics) -> tuple[float, float, float]:
    """Rank key: Sharpe, then Calmar, then CAGR. Higher is better."""
    return (metrics.sharpe, metrics.calmar, metrics.cagr)


@dataclass
class SurfaceRow:
    overrides: dict
    metrics: Metrics
    # The grid run's equity curve, kept so format_surface can run the
    # multiple-testing corrections (PBO/DSR) on the returns matrix — metrics
    # alone cannot say how much of the winner's Sharpe is search luck.
    equity: pd.Series | None = None


def parameter_surface(
    start: str | date,
    end: str | date,
    *,
    bars: dict[str, pd.DataFrame] | None = None,
    initial_cash: float = 100_000.0,
    buy=SURFACE_BUY,
    atr=SURFACE_ATR,
) -> list[SurfaceRow]:
    if bars is None:
        from ..config import load_settings
        settings = load_settings(include_research=False)
        bars = load_price_history(settings.core_watchlist or settings.watchlist)
    rows: list[SurfaceRow] = []
    for overrides in _grid(buy, atr):
        logger.info("Surface %s", _label(overrides))
        result = run_backtest(
            start, end, bars=bars, initial_cash=initial_cash,
            risk_overrides=overrides, journal_path=":memory:",
        )
        rows.append(SurfaceRow(overrides=overrides, metrics=result.metrics, equity=result.equity))
    rows.sort(key=lambda r: _score(r.metrics), reverse=True)
    return rows


def surface_overfit_report(rows: list[SurfaceRow]) -> str:
    """Multiple-testing corrections across the grid cells that produced `rows`.

    The surface table ranks N in-sample winners and stops there; this block
    asks the follow-up — how much of the best cell's Sharpe survives once you
    admit that N cells were tried. Returns an empty string when any row lacks
    its equity curve, so older callers that only kept metrics are unaffected.

    Units note (the trap `overfit.py`'s docstring warns about): `deflated_
    sharpe_ratio` works in per-observation Sharpes. We hand it the annualised
    figure with `periods_per_year=252` and convert the cross-trial Sharpe
    variance the same way (annualised / sqrt(252) per cell). Pandas `.kurt()`
    is *excess* kurtosis, so 3 is added back before passing it on.
    """
    if not rows or any(row.equity is None for row in rows):
        return ""

    trials = len(rows)
    best = rows[0]
    best_returns = best.equity.pct_change().dropna()

    lines = [
        "",
        f"Multiple-testing correction across the {trials} grid cells just run:",
    ]

    # PBO needs the {date x configuration} daily-returns matrix, aligned on
    # the sessions every cell shares.
    frame = pd.concat(
        {_label(row.overrides): row.equity for row in rows}, axis=1, join="inner"
    )
    returns = frame.pct_change().dropna()
    try:
        pbo = pbo_cscv(returns)
        lines.append(
            f"  PBO (CSCV, 8 splits) {pbo:.2f}   "
            "(how often the in-sample winner lands in the OOS bottom half; ~0.50 = no skill)"
        )
    except ValueError as exc:
        lines.append(f"  PBO (CSCV, 8 splits) n/a   ({exc})")

    if len(best_returns) >= 2:
        skew = float(best_returns.skew())
        kurtosis = float(best_returns.kurt()) + 3.0  # pandas reports excess kurtosis
        if math.isnan(skew):
            skew = 0.0
        if math.isnan(kurtosis):
            kurtosis = 3.0
        per_obs_sharpes = [row.metrics.sharpe / math.sqrt(252.0) for row in rows]
        sharpe_variance = (
            float(pd.Series(per_obs_sharpes).var(ddof=1)) if trials > 1 else None
        )
        result = deflated_sharpe_ratio(
            best.metrics.sharpe,
            len(best_returns),
            trials,
            skew=skew,
            kurtosis=kurtosis,
            sharpe_variance=sharpe_variance,
            periods_per_year=252.0,
        )
        verdict = (
            "clears the 95% bar" if result.is_significant
            else "does NOT clear the 95% bar - consistent with search luck"
        )
        lines += [
            f"  Deflated Sharpe      {result.deflated:.3f}   "
            f"(best cell Sharpe {best.metrics.sharpe:.2f}, {trials} trials, "
            f"{result.observations:,} obs) -> {verdict}",
            f"  Luck benchmark       {result.benchmark_sharpe:.4f}   "
            f"(E[max] per-obs Sharpe of {trials} zero-edge trials)",
        ]
        years = minimum_backtest_length(best.metrics.sharpe, trials)
        lines.append(
            f"  Min backtest length  {years:.1f} years   "
            f"(before Sharpe {best.metrics.sharpe:.2f} after {trials} trials "
            "is distinguishable from luck)"
        )
    else:
        lines.append("  Deflated Sharpe      n/a   (best cell has fewer than 2 daily returns)")

    return "\n".join(lines)


def format_surface(rows: list[SurfaceRow]) -> str:
    lines = [
        f"{'buy':>6} {'ATR':>5} {'CAGR':>8} {'Sharpe':>8} {'MaxDD':>8} {'Calmar':>7} {'end $':>12} {'buys':>6}",
        "-" * 70,
    ]
    for row in rows:
        m = row.metrics
        lines.append(
            f"{row.overrides['buy_threshold']:6.2f} {row.overrides['atr_stop_multiple']:5.1f} "
            f"{m.cagr:+8.1%} {m.sharpe:8.2f} {m.max_drawdown:8.1%} {m.calmar:7.2f} "
            f"{m.end_equity:12,.0f} {m.n_buys:6d}"
        )
    if rows:
        best = rows[0]
        lines.append("")
        lines.append(
            f"Best on this window (Sharpe): buy_threshold={best.overrides['buy_threshold']} "
            f"atr_stop_multiple={best.overrides['atr_stop_multiple']}"
        )
        report = surface_overfit_report(rows)
        if report:
            lines.append(report)
        lines.append("This is in-sample ranking, not a licence to promote the winner to live.")
    return "\n".join(lines)


@dataclass
class WalkForwardFold:
    oos_year: int
    is_start: date
    is_end: date
    oos_start: date
    oos_end: date
    chosen: dict
    is_sharpe: float
    oos: BacktestResult


@dataclass
class ContinuousFoldChoice:
    oos_year: int
    is_start: date
    is_end: date
    buy_threshold: float
    is_sharpe: float


def continuous_threshold_walk_forward(
    start: str | date,
    end: str | date,
    *,
    bars: dict[str, pd.DataFrame],
    initial_cash: float = 100_000.0,
    min_is_years: int = 3,
    buy=SURFACE_BUY,
    signal_model=None,
    regime_model=None,
    correlation_model=None,
    cash_returns: pd.Series | None = None,
) -> tuple[list[ContinuousFoldChoice], BacktestResult | None]:
    """Choose only the entry bar annually, then replay OOS without resetting the book."""
    start_d = date.fromisoformat(start) if isinstance(start, str) else start
    end_d = date.fromisoformat(end) if isinstance(end, str) else end
    first_oos_year = start_d.year + min_is_years
    # A fixed-parameter run is prefix-invariant: its equity through an IS cutoff
    # is identical whether the replay stops there or continues. Run each
    # candidate once, then score its expanding prefixes instead of needlessly
    # replaying the same early years for every fold.
    candidate_equities: dict[float, pd.Series] = {}
    for threshold in buy:
        candidate = run_backtest(
            start_d, end_d,
            bars=bars,
            initial_cash=initial_cash,
            risk_overrides={"buy_threshold": threshold},
            journal_path=":memory:",
            signal_model=signal_model,
            regime_model=regime_model,
            correlation_model=correlation_model,
            cash_returns=cash_returns,
        )
        candidate_equities[float(threshold)] = candidate.equity

    choices: list[ContinuousFoldChoice] = []
    for year in range(first_oos_year, end_d.year + 1):
        is_end = date(year - 1, 12, 31)
        scored: list[tuple[tuple[float, float, float], float]] = []
        for threshold, equity in candidate_equities.items():
            prefix = equity.loc[equity.index <= pd.Timestamp(is_end)]
            curve = [(timestamp, float(value), 0.0) for timestamp, value in prefix.items()]
            metrics = compute_metrics(curve, [], starting_cash=initial_cash)
            scored.append((_score(metrics), threshold))
        best_score, best_threshold = max(scored, key=lambda item: item[0])
        choices.append(ContinuousFoldChoice(
            oos_year=year,
            is_start=start_d,
            is_end=is_end,
            buy_threshold=best_threshold,
            is_sharpe=best_score[0],
        ))
    if not choices:
        return choices, None
    thresholds = {choice.oos_year: choice.buy_threshold for choice in choices}
    oos_start = date(first_oos_year, 1, 1)
    result = run_backtest(
        oos_start, end_d,
        bars=bars,
        initial_cash=initial_cash,
        journal_path=":memory:",
        signal_model=signal_model,
        regime_model=regime_model,
        correlation_model=correlation_model,
        cash_returns=cash_returns,
        risk_schedule=lambda day: {"buy_threshold": thresholds[day.year]},
    )
    return choices, result


def walk_forward(
    start: str | date,
    end: str | date,
    *,
    bars: dict[str, pd.DataFrame] | None = None,
    initial_cash: float = 100_000.0,
    min_is_years: int = 2,
    buy=SURFACE_BUY,
    atr=SURFACE_ATR,
) -> tuple[list[WalkForwardFold], pd.Series, Metrics]:
    start_d = date.fromisoformat(start) if isinstance(start, str) else start
    end_d = date.fromisoformat(end) if isinstance(end, str) else end
    if bars is None:
        from ..config import load_settings
        settings = load_settings(include_research=False)
        bars = load_price_history(settings.core_watchlist or settings.watchlist)

    folds: list[WalkForwardFold] = []
    year = start_d.year + min_is_years
    while year <= end_d.year:
        is_start = start_d
        is_end = date(year - 1, 12, 31)
        oos_start = date(year, 1, 1)
        oos_end = min(date(year, 12, 31), end_d)
        if oos_start > oos_end:
            break
        logger.info("Walk-forward OOS %s  IS %s → %s", year, is_start, is_end)
        best_score: tuple[float, float, float] | None = None
        best_overrides: dict | None = None
        for overrides in _grid(buy, atr):
            is_result = run_backtest(
                is_start, is_end, bars=bars, initial_cash=initial_cash,
                risk_overrides=overrides, journal_path=":memory:",
            )
            scored = _score(is_result.metrics)
            if best_score is None or scored > best_score:
                best_score = scored
                best_overrides = overrides
        assert best_overrides is not None and best_score is not None
        oos = run_backtest(
            oos_start, oos_end, bars=bars, initial_cash=initial_cash,
            risk_overrides=best_overrides, journal_path=":memory:",
        )
        folds.append(WalkForwardFold(
            oos_year=year, is_start=is_start, is_end=is_end,
            oos_start=oos_start, oos_end=oos_end, chosen=best_overrides,
            is_sharpe=best_score[0], oos=oos,
        ))
        year += 1

    stitched = _stitch([f.oos.equity for f in folds])
    if stitched.empty:
        combined = Metrics(
            start_equity=initial_cash, end_equity=initial_cash, cagr=0.0, volatility=0.0,
            sharpe=0.0, max_drawdown=0.0, calmar=0.0, n_buys=0, n_sells=0,
            win_rate=None, profit_factor=None, avg_exposure=0.0, years={},
        )
    else:
        curve = [(ts, float(v), 0.0) for ts, v in stitched.items()]
        combined = compute_metrics(curve, [], starting_cash=float(stitched.iloc[0]))
        combined.n_buys = sum(f.oos.metrics.n_buys for f in folds)
        combined.n_sells = sum(f.oos.metrics.n_sells for f in folds)
    return folds, stitched, combined


def _stitch(series_list: list[pd.Series]) -> pd.Series:
    pieces: list[pd.Series] = []
    level: float | None = None
    for series in series_list:
        s = series.dropna()
        if s.empty:
            continue
        if level is None:
            pieces.append(s)
            level = float(s.iloc[-1])
            continue
        start = float(s.iloc[0])
        if start <= 0:
            continue
        scaled = s / start * level
        pieces.append(scaled.iloc[1:] if len(scaled) > 1 else scaled)
        level = float(scaled.iloc[-1])
    if not pieces:
        return pd.Series(dtype=float)
    out = pd.concat(pieces)
    return out[~out.index.duplicated(keep="last")].sort_index()


def format_walk_forward(folds: list[WalkForwardFold], combined: Metrics) -> str:
    lines = [
        f"{'OOS':>6} {'IS Sharpe':>10} {'chosen buy':>11} {'chosen ATR':>11} "
        f"{'OOS CAGR':>9} {'OOS Sharpe':>11} {'OOS MaxDD':>10}",
        "-" * 80,
    ]
    for fold in folds:
        lines.append(
            f"{fold.oos_year:6d} {fold.is_sharpe:10.2f} "
            f"{fold.chosen['buy_threshold']:11.2f} {fold.chosen['atr_stop_multiple']:11.1f} "
            f"{fold.oos.metrics.cagr:+9.1%} {fold.oos.metrics.sharpe:11.2f} "
            f"{fold.oos.metrics.max_drawdown:10.1%}"
        )
    lines += [
        "",
        "Concatenated OOS (each year uses the params that won the prior IS window):",
        f"  CAGR {combined.cagr:+.1%}   Sharpe {combined.sharpe:.2f}   "
        f"MaxDD {combined.max_drawdown:.1%}   end ${combined.end_equity:,.0f}",
        "If this is much worse than the single-window 0.35 result, that result was in-sample luck.",
    ]
    return "\n".join(lines)


@dataclass
class AblationRow:
    name: str
    metrics: Metrics
    note: str


def _ensure_symbols(bars: dict[str, pd.DataFrame], symbols: list[str]) -> dict[str, pd.DataFrame]:
    missing = [
        s for s in symbols
        if s not in bars or bars[s] is None or getattr(bars[s], "empty", True)
    ]
    if not missing:
        return bars
    extra = load_price_history(missing)
    merged = dict(bars)
    merged.update(extra)
    return merged


def ablations(
    start: str | date,
    end: str | date,
    *,
    bars: dict[str, pd.DataFrame] | None = None,
    initial_cash: float = 100_000.0,
) -> list[AblationRow]:
    if bars is None:
        from ..config import load_settings
        settings = load_settings(include_research=False)
        bars = load_price_history(settings.core_watchlist or settings.watchlist)

    specs: list[tuple[str, dict, dict, str]] = [
        ("baseline", {}, {}, "current risk.yaml + core watchlist"),
        ("no_rsi", {}, {"signal_weights": weights_without("rsi")},
         "drop RSI from the composite; renormalise the other four"),
        ("no_macro", {}, {"disable_macro": True},
         "force neutral regime — no size cut, no raised entry bar"),
        ("stops_only_exits", {"sell_threshold": -1.01}, {},
         "no score-based SELL; ATR + trailing stops only"),
        ("with_spy_qqq", {}, {"extra_symbols": ["SPY", "QQQ"]},
         "let the agent buy the regime ETFs (currently context-only)"),
    ]
    rows: list[AblationRow] = []
    for name, risk_over, kwargs, note in specs:
        logger.info("Ablation %s", name)
        run_bars = bars
        extra = kwargs.get("extra_symbols")
        if extra:
            run_bars = _ensure_symbols(bars, list(extra))
        result = run_backtest(
            start, end, bars=run_bars, initial_cash=initial_cash,
            risk_overrides=risk_over or None, journal_path=":memory:",
            **kwargs,
        )
        rows.append(AblationRow(name=name, metrics=result.metrics, note=note))
    return rows


def format_ablations(rows: list[AblationRow]) -> str:
    lines = [
        f"{'name':<18} {'CAGR':>8} {'Sharpe':>8} {'MaxDD':>8} {'end $':>12} {'buys':>6}  note",
        "-" * 90,
    ]
    for row in rows:
        m = row.metrics
        lines.append(
            f"{row.name:<18} {m.cagr:+8.1%} {m.sharpe:8.2f} {m.max_drawdown:8.1%} "
            f"{m.end_equity:12,.0f} {m.n_buys:6d}  {row.note}"
        )
    lines.append("")
    lines.append("Read as 'what happens if we rip this piece out', not as a new default.")
    return "\n".join(lines)
