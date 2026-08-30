"""Risk-adjusted comparison statistics for audit reports."""
from __future__ import annotations

import numpy as np
import pandas as pd


def _aligned_returns(
    strategy_equity: pd.Series,
    benchmark_equity: pd.Series,
    cash_returns: pd.Series | None = None,
) -> pd.DataFrame:
    frame = pd.concat(
        [strategy_equity.rename("strategy"), benchmark_equity.rename("benchmark")],
        axis=1,
        join="inner",
    ).dropna()
    returns = frame.pct_change().dropna()
    if cash_returns is None:
        returns["cash"] = 0.0
    else:
        cash = cash_returns.astype(float).copy()
        cash.index = pd.DatetimeIndex(cash.index).tz_localize(None).normalize()
        returns["cash"] = cash.reindex(returns.index).fillna(0.0)
    return returns


def _alpha_beta_from_arrays(strategy: np.ndarray, benchmark: np.ndarray) -> tuple[float, float]:
    design = np.column_stack([np.ones(len(benchmark)), benchmark])
    coefficients, *_ = np.linalg.lstsq(design, strategy, rcond=None)
    return float(coefficients[0] * 252.0), float(coefficients[1])


def alpha_beta(
    strategy_equity: pd.Series,
    benchmark_equity: pd.Series,
    cash_returns: pd.Series | None = None,
) -> dict[str, float | int | None]:
    returns = _aligned_returns(strategy_equity, benchmark_equity, cash_returns)
    if len(returns) < 20:
        return {"alpha_annual": None, "beta": None, "observations": len(returns)}
    strategy = (returns["strategy"] - returns["cash"]).to_numpy()
    benchmark = (returns["benchmark"] - returns["cash"]).to_numpy()
    alpha, beta = _alpha_beta_from_arrays(strategy, benchmark)
    return {"alpha_annual": alpha, "beta": beta, "observations": len(returns)}


def _sharpe(values: np.ndarray) -> float:
    std = float(np.std(values, ddof=1))
    return 0.0 if std <= 0 else float(np.mean(values) / std * np.sqrt(252.0))


def block_bootstrap_comparison(
    strategy_equity: pd.Series,
    benchmark_equity: pd.Series,
    cash_returns: pd.Series | None = None,
    *,
    block_size: int = 21,
    samples: int = 1_000,
    seed: int = 20260822,
) -> dict[str, float | int | list[float] | None]:
    returns = _aligned_returns(strategy_equity, benchmark_equity, cash_returns)
    n = len(returns)
    if n < max(40, block_size * 2):
        return {
            "observations": n,
            "samples": 0,
            "alpha_95": None,
            "sharpe_diff_95": None,
        }

    strategy = (returns["strategy"] - returns["cash"]).to_numpy()
    benchmark = (returns["benchmark"] - returns["cash"]).to_numpy()
    rng = np.random.default_rng(seed)
    alphas = np.empty(samples)
    sharpe_diffs = np.empty(samples)
    blocks_needed = int(np.ceil(n / block_size))
    for sample in range(samples):
        starts = rng.integers(0, n, size=blocks_needed)
        indices = np.concatenate(
            [(np.arange(start, start + block_size) % n) for start in starts]
        )[:n]
        sampled_strategy = strategy[indices]
        sampled_benchmark = benchmark[indices]
        alphas[sample], _ = _alpha_beta_from_arrays(sampled_strategy, sampled_benchmark)
        sharpe_diffs[sample] = _sharpe(sampled_strategy) - _sharpe(sampled_benchmark)

    return {
        "observations": n,
        "samples": samples,
        "alpha_95": [float(x) for x in np.quantile(alphas, [0.025, 0.975])],
        "sharpe_diff_95": [float(x) for x in np.quantile(sharpe_diffs, [0.025, 0.975])],
    }

