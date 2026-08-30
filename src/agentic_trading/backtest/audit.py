"""Run a reproducible, risk-adjusted audit of the quant/risk backtest."""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import platform
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import yaml

from ..config import Settings, load_settings
from ..signals.technical import weights_without
from .benchmarks import (
    allocation_benchmark, cash_equity, daily_cash_returns_from_yield,
    equity_from_returns, point_in_time_universe_returns,
    random_portfolio_placebo_returns,
)
from .data import _max_path, load_price_history
from .engine import BACKTEST_DIR, BacktestResult, run_backtest
from .experiments import continuous_threshold_walk_forward
from .metrics import compute_metrics
from .overfit import deflated_sharpe_ratio, minimum_backtest_length, pbo_cscv
from .precompute import CachedCorrelationModel, CachedRegimeModel, CachedSignalModel
from .statistics import alpha_beta, block_bootstrap_comparison
from .trades import TradeRecord, build_trade_records
from .universe import UniverseSchedule

logger = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CONFIG = ROOT / "config" / "backtest_audit.yaml"


@dataclass(frozen=True)
class RunSpec:
    name: str
    symbols: tuple[str, ...]
    start: str
    end: str
    slippage_bps: float = 5.0
    stop_slippage_bps: float = 10.0
    risk_overrides: tuple[tuple[str, Any], ...] = ()
    dropped_component: str | None = None
    disable_macro: bool = False
    signal_mode: str = "composite"
    use_cash: bool = True


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git_value(*args: str) -> str:
    try:
        return subprocess.check_output(
            ["git", *args], cwd=ROOT, stderr=subprocess.DEVNULL,
        ).decode("utf-8", errors="replace").strip()
    except Exception:
        return "unavailable"


def _package_version(name: str) -> str:
    try:
        from importlib.metadata import version

        return version(name)
    except Exception:
        return "unavailable"


def _build_manifest(
    config: dict,
    config_path: Path,
    base_settings: Settings,
    bars: dict[str, pd.DataFrame],
    full_start: str,
) -> dict[str, Any]:
    source_paths = sorted((ROOT / "src" / "agentic_trading").rglob("*.py"))
    source_paths.extend(
        path for path in (
            ROOT / "config" / "risk.yaml",
            ROOT / "config" / "watchlist.yaml",
            DEFAULT_CONFIG,
        ) if path.exists()
    )
    consumed_data = {
        symbol: {
            "path": str(path.relative_to(ROOT)),
            "sha256": _sha256(path),
            "size": path.stat().st_size,
        }
        for symbol in sorted(bars)
        if (path := _max_path(symbol)).exists()
    }
    pit_metadata = {}
    for path in (
        ROOT / "data" / "backtest" / "point_in_time" / "build_manifest.json",
        ROOT / "data" / "backtest" / "point_in_time" / "data_quality.json",
        ROOT / "data" / "backtest" / "point_in_time" / "membership.csv",
        ROOT / "data" / "backtest" / "point_in_time" / "instrument_master.csv",
        ROOT / "data" / "backtest" / "point_in_time" / "delist_events.csv",
        ROOT / "data" / "backtest" / "point_in_time" / "price_manifest.json",
    ):
        if path.exists():
            pit_metadata[str(path.relative_to(ROOT))] = {
                "sha256": _sha256(path), "size": path.stat().st_size,
            }
    return {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "git_commit": _git_value("rev-parse", "HEAD"),
        "git_diff_sha256": hashlib.sha256(_git_value("diff", "--binary").encode("utf-8")).hexdigest(),
        "source_files": {
            str(path.relative_to(ROOT)): _sha256(path)
            for path in source_paths
        },
        "environment": {
            "python": platform.python_version(),
            "pandas": _package_version("pandas"),
            "numpy": _package_version("numpy"),
            "yfinance": _package_version("yfinance"),
        },
        "config": config,
        "config_sha256": _sha256(config_path),
        "resolved_full_macro_start": full_start,
        "risk_config": asdict(base_settings.risk),
        "risk_config_sha256": _sha256(ROOT / "config" / "risk.yaml"),
        "data_files": consumed_data,
        "point_in_time_metadata_files": pit_metadata,
        "universe_drift": _universe_drift(config),
        "random_seed": 20260822,
    }


def _metrics_dict(metrics) -> dict[str, Any]:
    return {
        "start_equity": metrics.start_equity,
        "end_equity": metrics.end_equity,
        "cagr": metrics.cagr,
        "volatility": metrics.volatility,
        "sharpe": metrics.sharpe,
        "max_drawdown": metrics.max_drawdown,
        "calmar": metrics.calmar,
        "n_buys": metrics.n_buys,
        "n_sells": metrics.n_sells,
        "win_rate_realized_only": metrics.win_rate,
        "profit_factor_realized_only": metrics.profit_factor,
        "avg_exposure": metrics.avg_exposure,
        "years": {str(key): value for key, value in metrics.years.items()},
    }


def _metrics_from_equity(equity: pd.Series, starting_cash: float):
    curve = [(timestamp, float(value), 0.0) for timestamp, value in equity.items()]
    return compute_metrics(curve, [], starting_cash=starting_cash)


def _trade_summary(
    records: list[TradeRecord],
    display_symbols: dict[str, str] | None = None,
) -> dict[str, Any]:
    if not records:
        return {"count": 0}
    pnls = pd.Series([record.pnl for record in records], dtype=float)
    winners = pnls[pnls > 0].sort_values(ascending=False)
    gross_profit = float(winners.sum())
    top_five = float(winners.head(5).sum() / gross_profit) if gross_profit > 0 else None
    top_n = max(1, int(len(winners) * 0.10)) if len(winners) else 0
    top_ten_pct = float(winners.head(top_n).sum() / gross_profit) if gross_profit > 0 and top_n else None
    by_symbol: dict[str, float] = {}
    for record in records:
        symbol = (display_symbols or {}).get(record.symbol, record.symbol)
        by_symbol[symbol] = by_symbol.get(symbol, 0.0) + record.pnl
    return {
        "count": len(records),
        "open_count": sum(record.is_open for record in records),
        "win_rate_including_open_mtm": float((pnls > 0).mean()),
        "median_return": float(pd.Series([record.return_pct for record in records]).median()),
        "median_holding_days": float(pd.Series([record.holding_days for record in records]).median()),
        "top_5_share_of_gross_profit": top_five,
        "top_10pct_share_of_gross_profit": top_ten_pct,
        "pnl_by_symbol": dict(sorted(by_symbol.items(), key=lambda item: item[1], reverse=True)),
    }


def _benchmarks(
    result: BacktestResult,
    bars: dict[str, pd.DataFrame],
    cash_returns: pd.Series,
    initial_cash: float,
    universe_schedule: UniverseSchedule | None = None,
) -> dict[str, pd.Series]:
    dates = result.equity.index
    spy = bars["SPY"]["Close"]
    curves = {
        "cash": cash_equity(cash_returns, dates, initial_cash),
        "spy_60_cash_40": allocation_benchmark(
            spy, cash_returns, dates, risky_weight=0.60, starting_cash=initial_cash,
        ),
        "exposure_matched_spy": allocation_benchmark(
            spy, cash_returns, dates, risky_weight=result.exposure, starting_cash=initial_cash,
        ),
    }
    if universe_schedule is not None:
        equal_returns = point_in_time_universe_returns(
            bars, universe_schedule, dates, sector_neutral=False,
        )
        if not equal_returns.empty:
            equal_prices = equity_from_returns(equal_returns, 1.0)
            curves["pit_equal_weight"] = equity_from_returns(equal_returns, initial_cash)
            curves["exposure_matched_pit_equal_weight"] = allocation_benchmark(
                equal_prices, cash_returns, dates,
                risky_weight=result.exposure, starting_cash=initial_cash,
            )
        known_sector_share = (
            sum(
                1 for symbol in universe_schedule.symbols
                if universe_schedule.sector_for(symbol) not in (None, "", "Unknown")
            ) / len(universe_schedule.symbols)
            if universe_schedule.symbols else 0.0
        )
        sector_returns = (
            point_in_time_universe_returns(
                bars, universe_schedule, dates, sector_neutral=True,
            )
            if known_sector_share >= 0.90 else pd.Series(dtype=float)
        )
        if not sector_returns.empty:
            sector_prices = equity_from_returns(sector_returns, 1.0)
            curves["pit_sector_neutral"] = equity_from_returns(sector_returns, initial_cash)
            curves["exposure_matched_pit_sector_neutral"] = allocation_benchmark(
                sector_prices, cash_returns, dates,
                risky_weight=result.exposure, starting_cash=initial_cash,
            )
    return curves


def _placebo_summary(
    result: BacktestResult,
    bars: dict[str, pd.DataFrame],
    schedule: UniverseSchedule,
    cash_returns: pd.Series,
    *,
    samples: int,
    seed: int = 20260822,
) -> dict[str, Any]:
    dates = result.equity.index
    placebo = random_portfolio_placebo_returns(
        bars, schedule, dates, samples=samples, seed=seed,
    )
    if placebo.empty:
        return {"samples": 0, "status": "unavailable"}
    cash = cash_returns.reindex(dates).fillna(0.0)
    weights = result.exposure.reindex(dates).ffill().shift(1).fillna(0.0).clip(0.0, 1.0)
    sharpes: list[float] = []
    cagrs: list[float] = []
    for column in placebo:
        returns = weights * placebo[column] + (1.0 - weights) * cash
        equity = equity_from_returns(returns, float(result.equity.iloc[0]))
        metrics = _metrics_from_equity(equity, float(result.equity.iloc[0]))
        sharpes.append(metrics.sharpe)
        cagrs.append(metrics.cagr)
    sharpe_series = pd.Series(sharpes, dtype=float)
    cagr_series = pd.Series(cagrs, dtype=float)
    return {
        "status": "completed",
        "samples": len(sharpe_series),
        "seed": seed,
        "portfolio_positions": 8,
        "monthly_rebalance": True,
        "max_names_per_sector": 2,
        "strategy_sharpe_percentile": float((sharpe_series < result.metrics.sharpe).mean()),
        "strategy_cagr_percentile": float((cagr_series < result.metrics.cagr).mean()),
        "placebo_sharpe_quantiles": {
            str(key): float(value)
            for key, value in sharpe_series.quantile([0.05, 0.50, 0.95]).items()
        },
        "placebo_cagr_quantiles": {
            str(key): float(value)
            for key, value in cagr_series.quantile([0.05, 0.50, 0.95]).items()
        },
    }


def _settings_for(base: Settings, symbols: tuple[str, ...], extra_sectors: dict[str, str]) -> Settings:
    sectors = dict(base.sectors)
    sectors.update(extra_sectors)
    return replace(
        base,
        watchlist=list(symbols),
        core_watchlist=list(symbols),
        research_symbols=[],
        sectors=sectors,
    )


def _run_spec(
    spec: RunSpec,
    *,
    base_settings: Settings,
    extra_sectors: dict[str, str],
    bars: dict[str, pd.DataFrame],
    signal_models: dict[str, CachedSignalModel],
    regime_model: CachedRegimeModel,
    correlation_model: CachedCorrelationModel,
    cash_returns: pd.Series,
    initial_cash: float,
    bootstrap_samples: int,
    universe_schedule: UniverseSchedule | None = None,
) -> tuple[dict[str, Any], pd.Series, list[TradeRecord], BacktestResult]:
    logger.info("Audit run %s (%s to %s, %d symbols)", spec.name, spec.start, spec.end, len(spec.symbols))
    weights = weights_without(spec.dropped_component) if spec.dropped_component else None
    result = run_backtest(
        spec.start,
        spec.end,
        settings=_settings_for(base_settings, spec.symbols, extra_sectors),
        initial_cash=initial_cash,
        slippage_bps=spec.slippage_bps,
        stop_slippage_bps=spec.stop_slippage_bps,
        bars=bars,
        journal_path=":memory:",
        risk_overrides=dict(spec.risk_overrides) or None,
        signal_weights=weights,
        disable_macro=spec.disable_macro,
        cash_returns=cash_returns if spec.use_cash else None,
        signal_model=signal_models[spec.signal_mode],
        regime_model=regime_model,
        correlation_model=correlation_model,
        universe_schedule=universe_schedule,
    )
    aligned_cash = cash_returns.reindex(result.equity.index).fillna(0.0) if spec.use_cash else pd.Series(0.0, index=result.equity.index)
    benchmark_curves = _benchmarks(
        result, bars, aligned_cash, initial_cash, universe_schedule=universe_schedule,
    )
    exposure_benchmark = benchmark_curves["exposure_matched_spy"]
    records = build_trade_records(
        result.fills,
        bars,
        result.equity.index[-1],
        atr_stop_multiple=result.settings.risk.atr_stop_multiple,
        min_stop_pct=result.settings.risk.min_stop_pct,
        max_stop_pct=result.settings.risk.max_stop_pct,
    )
    row = {
        "name": spec.name,
        "start": result.equity.index[0].date().isoformat(),
        "end": result.equity.index[-1].date().isoformat(),
        "symbols": list(spec.symbols),
        "assumptions": asdict(spec),
        "strategy": _metrics_dict(result.metrics),
        "spy": None if result.spy_metrics is None else _metrics_dict(result.spy_metrics),
        "benchmarks": {
            name: _metrics_dict(_metrics_from_equity(curve, initial_cash))
            for name, curve in benchmark_curves.items()
        },
        "alpha_vs_spy": alpha_beta(result.equity, result.spy_equity, aligned_cash) if result.spy_equity is not None else None,
        "alpha_vs_exposure_matched": alpha_beta(result.equity, exposure_benchmark, aligned_cash),
        "bootstrap_vs_exposure_matched": block_bootstrap_comparison(
            result.equity,
            exposure_benchmark,
            aligned_cash,
            samples=bootstrap_samples,
        ),
        "trades": _trade_summary(
            records, universe_schedule.tickers if universe_schedule is not None else None,
        ),
    }
    pit_equal = benchmark_curves.get("exposure_matched_pit_equal_weight")
    if pit_equal is not None:
        row["alpha_vs_exposure_matched_pit_equal_weight"] = alpha_beta(
            result.equity, pit_equal, aligned_cash,
        )
    return row, result.equity.rename(spec.name), records, result


def _build_specs(config: dict, full_start: str) -> list[RunSpec]:
    current = tuple(config["current_universe"])
    stress = tuple(dict.fromkeys([*current, *config["stress_additions"]]))
    end = str(config["end"])
    costs = config["cost_scenarios"]
    base = costs["base"]
    specs = [
        RunSpec("current_full", current, full_start, end, **base),
        RunSpec("current_degraded_macro_start", current, str(config["start"]), end, **base),
        RunSpec("stress_full", stress, full_start, end, **base),
        RunSpec("current_preselection_2007_2018", current, full_start, "2018-12-31", **base),
        RunSpec("current_train_2019_2022", current, "2019-01-01", "2022-12-31", **base),
        RunSpec("current_holdout_2023_2026", current, "2023-01-01", end, **base),
        RunSpec("current_zero_cash", current, full_start, end, **base, use_cash=False),
        RunSpec("cost_medium", current, full_start, end, **costs["medium"]),
        RunSpec("cost_severe", current, full_start, end, **costs["severe"]),
        RunSpec("ablate_no_rsi", current, full_start, end, **base, dropped_component="rsi"),
        RunSpec("ablate_no_macd", current, full_start, end, **base, dropped_component="macd"),
        RunSpec("ablate_no_macro", current, full_start, end, **base, disable_macro=True),
        RunSpec("ablate_no_signal_sell", current, full_start, end, **base,
                risk_overrides=(("sell_threshold", -1.01),)),
        RunSpec("ablate_no_trailing", current, full_start, end, **base,
                risk_overrides=(("trailing_stop_pct", 1.0),)),
        RunSpec("baseline_simple_trend", current, full_start, end, **base, signal_mode="simple_trend"),
    ]
    specs.extend(
        RunSpec(f"leave_out_{symbol}", tuple(item for item in current if item != symbol), full_start, end, **base)
        for symbol in current
    )
    return specs


def _point_in_time_status(config: dict) -> dict[str, Any]:
    section = config.get("point_in_time", {})
    membership = ROOT / section.get("membership_csv", "")
    bars_dir = ROOT / section.get("bars_dir", "")
    quality_path = ROOT / section.get("quality_json", "data/backtest/point_in_time/data_quality.json")
    missing = [str(path) for path in (membership, bars_dir) if not path.exists()]
    if missing:
        return {
            "status": "waiting_for_data",
            "missing": missing,
            "required_membership_columns": ["symbol", "instrument_id", "ticker", "start_date", "end_date", "sector", "source_id"],
            "required_price_columns": ["Open", "High", "Low", "Close", "Volume"],
            "optional_price_columns": ["DelistingReturn"],
        }
    try:
        schedule = UniverseSchedule.from_csv(membership)
    except Exception as exc:
        return {"status": "invalid_membership", "error": str(exc)}
    missing_bars = [symbol for symbol in schedule.symbols if not (bars_dir / f"{symbol}.parquet").exists()]
    quality = None
    if quality_path.exists():
        try:
            quality = json.loads(quality_path.read_text(encoding="utf-8"))
        except Exception as exc:
            return {"status": "invalid_quality_report", "error": str(exc)}
    status = "ready"
    if missing_bars:
        status = "missing_price_files"
    elif quality is None:
        status = "missing_quality_report"
    elif not quality.get("ready", False):
        status = "data_quality_failed"
    return {
        "status": status,
        "memberships": len(schedule.intervals),
        "symbols": len(schedule.symbols),
        "missing_price_files": missing_bars,
        "quality_report": None if quality is None else {
            "path": str(quality_path),
            "ready": quality.get("ready"),
            "failed_required_gates": quality.get("failed_required_gates", []),
            "prices": quality.get("prices", {}),
            "cross_source": quality.get("cross_source", {}),
            "sectors": quality.get("sectors", {}),
        },
    }


def _load_point_in_time_bundle(
    config: dict,
    context_bars: dict[str, pd.DataFrame],
) -> tuple[UniverseSchedule, dict[str, pd.DataFrame]]:
    section = config["point_in_time"]
    membership = ROOT / section["membership_csv"]
    bars_dir = ROOT / section["bars_dir"]
    schedule = UniverseSchedule.from_csv(membership)
    bars = dict(context_bars)
    required = {"Open", "High", "Low", "Close", "Volume"}
    for symbol in schedule.symbols:
        path = bars_dir / f"{symbol}.parquet"
        if not path.exists():
            raise FileNotFoundError(f"Point-in-time price file missing: {path}")
        frame = pd.read_parquet(path)
        missing = required - set(frame.columns)
        if missing:
            raise ValueError(f"{path} missing columns: {', '.join(sorted(missing))}")
        if not isinstance(frame.index, pd.DatetimeIndex):
            raise ValueError(f"{path} must use a DatetimeIndex")
        bars[symbol] = frame
    return schedule, bars


def overfit_block(equities: dict[str, pd.Series], trials: int) -> dict[str, Any]:
    """Multiple-testing corrections across the audit's equity curves.

    The audit runs many specs, but they are repeated *looks* at one strategy —
    time splits, cost scenarios, ablations, leave-one-out — not the full search
    that chose the live parameters. `trials` therefore understates the real
    search count and the PBO/DSR below are a floor on the true correction.

    PBO needs curves that share a date range: the longest curve sets the
    window and only curves covering all of it are kept (a 2007-2018 split and
    a 2023-2026 split share no sessions, and an empty intersection would
    silently void the computation). Curves with under 16 daily returns (the
    minimum `pbo_cscv` needs for 8 splits) are dropped as well.

    This helper never raises — an audit report missing one diagnostic beats no
    audit report.
    """
    note = (
        "audit specs are repeated looks at one strategy, not the full parameter "
        "search, so the trial count understates the real multiple-testing burden"
    )
    block: dict[str, Any] = {"status": "skipped", "trials": trials, "note": note}
    if not equities:
        block["reason"] = "no equity curves"
        return block
    try:
        cleaned: dict[str, pd.Series] = {}
        for name, equity in equities.items():
            series = equity.dropna()
            series = series[~series.index.duplicated(keep="last")].sort_index()
            if len(series) >= 17:  # 16 daily returns minimum for 8 CSCV splits
                cleaned[name] = series

        selected: dict[str, pd.Series] = {}
        common: pd.DatetimeIndex | None = None
        for name, series in sorted(cleaned.items(), key=lambda item: -len(item[1])):
            candidate = series.index if common is None else common.intersection(series.index)
            if len(candidate) >= 17 and (common is None or len(candidate) == len(common)):
                selected[name] = series
                common = candidate

        if len(selected) >= 2 and common is not None:
            returns = pd.concat(selected, axis=1, join="inner").pct_change().dropna()
            try:
                block["pbo"] = pbo_cscv(returns)
            except ValueError as exc:
                block["pbo"] = None
                block["pbo_error"] = str(exc)
            block["curves_used"] = list(selected)
            block["common_window"] = {
                "start": str(common[0].date()),
                "end": str(common[-1].date()),
                "observations": len(returns),
            }
            block["status"] = "completed"
        else:
            block["reason"] = (
                f"fewer than 2 curves share a common date range of 17+ sessions "
                f"({len(selected)} usable)"
            )

        # Deflated Sharpe for the headline spec (the first curve handed in —
        # run_audit passes them in spec order, so that is current_full).
        headline_name = next(iter(equities))
        headline = equities[headline_name].dropna()
        headline_returns = headline.pct_change().dropna()
        block["headline"] = headline_name
        if len(headline_returns) >= 2:
            skew = float(headline_returns.skew())
            # pandas .kurt() is excess kurtosis; DSR expects the raw figure.
            kurtosis = float(headline_returns.kurt()) + 3.0
            if math.isnan(skew):
                skew = 0.0
            if math.isnan(kurtosis):
                kurtosis = 3.0
            # Per-observation Sharpe of each usable curve (mean/std of daily
            # returns) — the variance across them calibrates the luck benchmark.
            # overfit.py's docstring is explicit that this must stay in
            # per-observation units, never annualised.
            per_obs = []
            for series in cleaned.values():
                rets = series.pct_change().dropna()
                std = float(rets.std(ddof=1))
                if len(rets) >= 2 and std > 0:
                    per_obs.append(float(rets.mean()) / std)
            sharpe_variance = (
                float(pd.Series(per_obs).var(ddof=1)) if len(per_obs) > 1 else None
            )
            annualised = float(headline_returns.mean() / headline_returns.std(ddof=1) * math.sqrt(252.0)) \
                if float(headline_returns.std(ddof=1)) > 0 else 0.0
            result = deflated_sharpe_ratio(
                annualised,
                len(headline_returns),
                trials,
                skew=skew,
                kurtosis=kurtosis,
                sharpe_variance=sharpe_variance,
                periods_per_year=252.0,
            )
            block["dsr"] = {
                "sharpe_annual": annualised,
                "observations": result.observations,
                "skew": skew,
                "kurtosis": kurtosis,
                "benchmark_sharpe": result.benchmark_sharpe,
                "deflated": result.deflated,
                "is_significant": result.is_significant,
            }
            block["min_backtest_years"] = minimum_backtest_length(annualised, trials)
        else:
            block["dsr"] = None
            block["min_backtest_years"] = None
    except Exception as exc:  # never let a diagnostic kill the audit
        logger.warning("overfit_block failed: %s", exc)
        block["status"] = "error"
        block["reason"] = str(exc)
    return block


def _format_overfitting(block: dict[str, Any]) -> list[str]:
    """Render the payload's overfitting block as report lines.

    report.md is UTF-8 markdown, but the correction values also get skimmed
    on a GBK console, so the wording stays ASCII (`->`, `~`, `+-`).
    """
    lines = ["## Multiple-testing correction", ""]
    lines.append(f"Note: {block.get('note', '')}.")
    if "pbo" in block:
        pbo = block.get("pbo")
        window = block.get("common_window", {})
        pbo_text = "n/a" if pbo is None else f"{pbo:.2f}"
        lines.append(
            f"PBO (CSCV, 8 splits) across {len(block.get('curves_used', []))} curves "
            f"sharing {window.get('start', '?')} -> {window.get('end', '?')}: {pbo_text} "
            "(~0.50 = picking the in-sample winner says nothing out-of-sample)."
        )
    elif block.get("reason"):
        lines.append(f"PBO skipped: {block['reason']}.")
    dsr = block.get("dsr")
    if dsr:
        verdict = (
            "clears the 95% bar" if dsr["is_significant"]
            else "does NOT clear the 95% bar - consistent with search luck"
        )
        lines.append(
            f"Deflated Sharpe for {block.get('headline', 'headline spec')} "
            f"(Sharpe {dsr['sharpe_annual']:.2f}, {block.get('trials', '?')} looks, "
            f"{dsr['observations']:,} obs): {dsr['deflated']:.3f} -> {verdict}."
        )
    years = block.get("min_backtest_years")
    if years is not None:
        years_text = "inf" if years == float("inf") else f"{years:.1f}"
        lines.append(
            f"Minimum backtest length before that Sharpe is distinguishable from "
            f"luck: {years_text} years."
        )
    return lines


def _write_audit_report(path: Path, payload: dict[str, Any]) -> None:
    """Write a concise UTF-8 report for both Stage A and the gated Stage B."""
    runs = {row["name"]: row for row in payload["runs"]}
    core = runs["current_full"]
    stress = runs["stress_full"]
    pit = payload["point_in_time"]
    interval = core["bootstrap_vs_exposure_matched"].get("alpha_95")
    alpha = core["alpha_vs_exposure_matched"].get("alpha_annual")
    interval_text = "n/a" if interval is None else f"{interval[0]:.2%} to {interval[1]:.2%}"
    lines = [
        "# Backtest audit report",
        "",
        "## Stage A — current/preselected universes",
        "",
        "| Run | CAGR | Sharpe | Max drawdown | Avg exposure |",
        "|---|---:|---:|---:|---:|",
        f"| Current 12 | {core['strategy']['cagr']:.1%} | {core['strategy']['sharpe']:.2f} | {core['strategy']['max_drawdown']:.1%} | {core['strategy']['avg_exposure']:.1%} |",
        f"| Stress 22 | {stress['strategy']['cagr']:.1%} | {stress['strategy']['sharpe']:.2f} | {stress['strategy']['max_drawdown']:.1%} | {stress['strategy']['avg_exposure']:.1%} |",
        "",
        f"Exposure-matched SPY/cash alpha: {alpha:.2%}; 95% block-bootstrap interval: {interval_text}.",
        "These results are robustness diagnostics, not survivorship-free evidence.",
        "",
        "## Stage B — point-in-time historical S&P 500",
        "",
    ]
    if pit.get("status") != "completed":
        quality = pit.get("quality_report") or {}
        prices = quality.get("prices") or {}
        lines.extend([
            f"Status: **{pit.get('status', 'unknown')}**.",
            "",
            f"Price files: {prices.get('files_present', 0)}/{prices.get('files_expected', '?')}; "
            f"active member-session coverage: {prices.get('coverage', 0.0):.2%}.",
            f"Failed required gates: {', '.join(quality.get('failed_required_gates', [])) or 'not reported'}.",
            "",
            "Verdict: **inconclusive**. Missing securities remain in the membership denominator and are not silently dropped.",
        ])
    else:
        results = pit["results"]
        lines.extend([
            "| Run | CAGR | Sharpe | Max drawdown |",
            "|---|---:|---:|---:|",
        ])
        for name in (
            "point_in_time_full", "point_in_time_bigtech_cap", "point_in_time_ex_bigtech",
            "point_in_time_2007_2018", "point_in_time_2019_2022", "point_in_time_2023_2026",
        ):
            row = results.get(name)
            if row is None:
                continue
            metrics = row["strategy"]
            lines.append(
                f"| {name} | {metrics['cagr']:.1%} | {metrics['sharpe']:.2f} | {metrics['max_drawdown']:.1%} |"
            )
        main = results["point_in_time_full"]
        placebo = main.get("random_portfolio_placebo", {})
        equal_alpha = main.get("alpha_vs_exposure_matched_pit_equal_weight", {}).get("alpha_annual")
        lines.extend([
            "",
            "### Selection diagnostics",
            "",
            f"Alpha vs exposure-matched PIT equal-weight: {'n/a' if equal_alpha is None else f'{equal_alpha:.2%}'}.",
            f"Random-placebo Sharpe percentile: {placebo.get('strategy_sharpe_percentile', 0.0):.1%} "
            f"across {placebo.get('samples', 0)} seeded paths.",
            "",
            "A positive Stage-B result still remains an approximate-free-data finding, not licensed institutional-data proof.",
        ])
    overfitting = payload.get("overfitting")
    if overfitting:
        lines.extend(["", *_format_overfitting(overfitting)])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _universe_drift(config: dict) -> dict[str, list[str]]:
    """Compares the audit's `current_universe` with the live watchlist.

    The 'current_*' specs claim to describe the book the paper account holds.
    When someone swaps config/watchlist.yaml without updating
    backtest_audit.yaml, those specs silently audit a portfolio that no longer
    exists — so the drift is measured and surfaced rather than ignored."""
    live: list[str] = []
    path = ROOT / "config" / "watchlist.yaml"
    if path.exists():
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        context = {str(s) for s in (raw.get("context_symbols") or [])}
        live = [str(s) for s in (raw.get("symbols") or []) if str(s) not in context]
    audited = [str(s) for s in config.get("current_universe", [])]
    return {
        "in_watchlist_not_audited": sorted(set(live) - set(audited)),
        "in_audit_not_watchlist": sorted(set(audited) - set(live)),
    }


def run_audit(config_path: Path, output_root: Path | None = None) -> Path:
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    drift = _universe_drift(config)
    if any(drift.values()):
        logger.warning(
            "backtest_audit.yaml current_universe has DRIFTED from config/watchlist.yaml — "
            "the 'current_*' specs audit a book the paper account no longer holds "
            "(audited-only: %s; live-only: %s). Update one of the two files before trusting "
            "this audit as evidence about the current strategy.",
            ", ".join(drift["in_audit_not_watchlist"]) or "none",
            ", ".join(drift["in_watchlist_not_audited"]) or "none",
        )
    initial_cash = float(config.get("initial_cash", 100_000.0))
    symbols = list(dict.fromkeys([
        *config["current_universe"],
        *config["stress_additions"],
        "SPY", "^IRX",
    ]))
    # Freeze all readable inputs already present on disk. Missing symbols may still
    # be downloaded, and every consumed file is hashed into the manifest below.
    bars = load_price_history(symbols, allow_stale=True)
    if bars.get("SPY") is None or bars["SPY"].empty:
        raise RuntimeError("SPY history is required for audit dates and benchmarks")
    if bars.get("^IRX") is None or bars["^IRX"].empty:
        logger.warning("^IRX unavailable; cash return sensitivity will use zero")
        cash_returns = pd.Series(0.0, index=pd.DatetimeIndex(bars["SPY"].index).tz_localize(None).normalize())
    else:
        spy_dates = pd.DatetimeIndex(bars["SPY"].index)
        if spy_dates.tz is not None:
            spy_dates = spy_dates.tz_localize(None)
        cash_returns = daily_cash_returns_from_yield(bars["^IRX"]["Close"], spy_dates.normalize())

    signal_models = {
        "composite": CachedSignalModel(bars, mode="composite"),
        "simple_trend": CachedSignalModel(bars, mode="simple_trend"),
    }
    regime_model = CachedRegimeModel(bars)
    correlation_model = CachedCorrelationModel(bars)
    full_date = regime_model.first_full_date()
    if full_date is None:
        raise RuntimeError("The five macro ratios never become simultaneously valid")
    requested_start = pd.Timestamp(config["start"])
    full_start = max(requested_start, full_date).date().isoformat()

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output = output_root or (BACKTEST_DIR / "audit" / timestamp)
    output.mkdir(parents=True, exist_ok=False)
    base_settings = load_settings(include_research=False)
    specs = _build_specs(config, full_start)
    rows: list[dict[str, Any]] = []
    equities: list[pd.Series] = []
    trade_rows: list[dict[str, Any]] = []
    jobs = max(1, int(config.get("jobs", 2)))
    worker_args = dict(
        base_settings=base_settings,
        extra_sectors=dict(config.get("stress_sectors", {})),
        bars=bars,
        signal_models=signal_models,
        regime_model=regime_model,
        correlation_model=correlation_model,
        cash_returns=cash_returns,
        initial_cash=initial_cash,
        bootstrap_samples=int(config.get("bootstrap_samples", 1_000)),
    )
    with ThreadPoolExecutor(max_workers=jobs) as executor:
        futures = {executor.submit(_run_spec, spec, **worker_args): spec for spec in specs}
        for future in as_completed(futures):
            spec = futures[future]
            row, equity, records, _ = future.result()
            rows.append(row)
            equities.append(equity)
            trade_rows.extend({"run": spec.name, **record.to_dict()} for record in records)
            logger.info("Completed %s", spec.name)
    order = {spec.name: index for index, spec in enumerate(specs)}
    rows.sort(key=lambda row: order[row["name"]])

    logger.info("Continuous expanding threshold walk-forward")
    choices, walk_result = continuous_threshold_walk_forward(
        full_start,
        str(config["end"]),
        bars=bars,
        initial_cash=initial_cash,
        signal_model=signal_models["composite"],
        regime_model=regime_model,
        correlation_model=correlation_model,
        cash_returns=cash_returns,
    )
    walk = {
        "choices": [asdict(choice) for choice in choices],
        "result": None if walk_result is None else _metrics_dict(walk_result.metrics),
    }
    if walk_result is not None:
        equities.append(walk_result.equity.rename("continuous_walk_forward"))

    pit_status = _point_in_time_status(config)
    if pit_status["status"] == "ready":
        schedule, pit_bars = _load_point_in_time_bundle(config, bars)
        pit_signals = {
            "composite": CachedSignalModel(pit_bars, mode="composite"),
            "simple_trend": CachedSignalModel(pit_bars, mode="simple_trend"),
        }
        pit_regime = CachedRegimeModel(pit_bars)
        pit_correlations = CachedCorrelationModel(pit_bars)
        pit_full = pit_regime.first_full_date()
        if pit_full is None:
            raise RuntimeError("Point-in-time bundle lacks a complete five-ratio macro window")
        pit_start = max(pd.Timestamp(config["start"]), pit_full).date().isoformat()
        pit_section = config.get("point_in_time", {})
        theme_tickers = {
            str(item).upper() for item in pit_section.get(
                "theme_tickers",
                ["AAPL", "MSFT", "NVDA", "GOOG", "GOOGL", "AMZN", "META", "TSLA"],
            )
        }
        theme_keys = tuple(
            symbol for symbol, ticker in schedule.tickers.items()
            if ticker.upper() in theme_tickers
        )
        ex_theme = schedule.filtered(exclude_tickers=theme_tickers)
        base_cost = config["cost_scenarios"]["base"]
        pit_runs: list[tuple[RunSpec, UniverseSchedule]] = [
            (
                RunSpec(
                    "point_in_time_full", tuple(schedule.symbols), pit_start,
                    str(config["end"]), **base_cost,
                ),
                schedule,
            ),
            (
                RunSpec(
                    "point_in_time_bigtech_cap", tuple(schedule.symbols), pit_start,
                    str(config["end"]), **base_cost,
                    risk_overrides=(
                        ("theme_symbols", theme_keys),
                        ("max_theme_pct", float(pit_section.get("theme_max_pct", 0.25))),
                        ("max_theme_positions", int(pit_section.get("theme_max_positions", 2))),
                    ),
                ),
                schedule,
            ),
            (
                RunSpec(
                    "point_in_time_ex_bigtech", tuple(ex_theme.symbols), pit_start,
                    str(config["end"]), **base_cost,
                ),
                ex_theme,
            ),
        ]
        for name, split_start, split_end in (
            ("point_in_time_2007_2018", pit_start, "2018-12-31"),
            ("point_in_time_2019_2022", "2019-01-01", "2022-12-31"),
            ("point_in_time_2023_2026", "2023-01-01", str(config["end"])),
        ):
            if pd.Timestamp(split_start) <= pd.Timestamp(split_end):
                pit_runs.append((
                    RunSpec(name, tuple(schedule.symbols), split_start, split_end, **base_cost),
                    schedule,
                ))

        pit_results: dict[str, dict[str, Any]] = {}
        for pit_spec, run_schedule in pit_runs:
            pit_row, pit_equity, pit_records, pit_backtest_result = _run_spec(
                pit_spec,
                base_settings=base_settings,
                extra_sectors={**dict(config.get("stress_sectors", {})), **run_schedule.sectors},
                bars=pit_bars,
                signal_models=pit_signals,
                regime_model=pit_regime,
                correlation_model=pit_correlations,
                cash_returns=cash_returns,
                initial_cash=initial_cash,
                bootstrap_samples=int(config.get("bootstrap_samples", 1_000)),
                universe_schedule=run_schedule,
            )
            if pit_spec.name == "point_in_time_full":
                pit_row["random_portfolio_placebo"] = _placebo_summary(
                    pit_backtest_result,
                    pit_bars,
                    run_schedule,
                    cash_returns,
                    samples=int(pit_section.get("placebo_samples", 500)),
                )
            pit_results[pit_spec.name] = pit_row
            equities.append(pit_equity)
            ticker_map = run_schedule.tickers
            trade_rows.extend({
                "run": pit_spec.name,
                "ticker": ticker_map.get(record.symbol, record.symbol),
                **record.to_dict(),
            } for record in pit_records)
        pit_status = {
            **pit_status,
            "status": "completed",
            "theme_tickers": sorted(theme_tickers),
            "result": pit_results["point_in_time_full"],
            "results": pit_results,
        }
    manifest = _build_manifest(config, config_path, base_settings, bars, full_start)
    # Equities arrived in completion order; the overfitting block wants spec
    # order so the headline curve is current_full.
    ordered_equities = sorted(equities, key=lambda s: order.get(s.name, len(order)))
    overfitting = overfit_block(
        {series.name: series for series in ordered_equities}, trials=len(specs),
    )
    payload = {
        "manifest": manifest,
        "point_in_time": pit_status,
        "runs": rows,
        "continuous_walk_forward": walk,
        "overfitting": overfitting,
    }
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")
    (output / "summary.json").write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    pd.concat(equities, axis=1, sort=True).to_csv(output / "equity.csv", index_label="date")
    pd.DataFrame(trade_rows).to_csv(output / "trades.csv", index=False)
    yearly_rows = [
        {"run": row["name"], "year": year, "return": value}
        for row in rows for year, value in row["strategy"]["years"].items()
    ]
    pd.DataFrame(yearly_rows).to_csv(output / "yearly.csv", index=False)
    (output / "point_in_time_status.json").write_text(json.dumps(pit_status, indent=2), encoding="utf-8")
    _write_audit_report(output / "report.md", payload)
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--jobs", type=int, help="Override config worker count")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if args.jobs is not None:
        raw = yaml.safe_load(args.config.read_text(encoding="utf-8"))
        raw["jobs"] = args.jobs
        temp = BACKTEST_DIR / "audit" / "_runtime_config.yaml"
        temp.parent.mkdir(parents=True, exist_ok=True)
        temp.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
        config_path = temp
    else:
        config_path = args.config
    output = run_audit(config_path, args.output)
    print(f"Audit written to {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
