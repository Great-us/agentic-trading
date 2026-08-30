"""Multiple-testing corrections for a backtest that has been searched.

`backtest/` already carries an unusually complete robustness kit — block
bootstrap confidence intervals, a 500-path random-portfolio placebo, cost
scenarios, ablations, leave-one-symbol-out, walk-forward. All of it answers
"is this result fragile?" None of it answers the different question that
matters once you have *searched* for a configuration:

    given that N configurations were tried, how much of the best one's Sharpe
    is just the maximum of N draws from noise?

That gap is not hypothetical here. `experiments.py:parameter_surface` sweeps a
`buy_threshold × atr_stop_multiple` grid, `ablations()` runs five variants,
`audit.py` adds one leave-one-out run per symbol plus three cost scenarios and
three time splits — dozens of configurations, with no correction applied. The
live `buy_threshold` of 0.35 was chosen against exactly that kind of sweep
(`HOLDOUT-LEDGER.md` records the 0.35-vs-0.25 comparison).

Three standard tools, none of which needs scipy:

- `deflated_sharpe_ratio` — Bailey & López de Prado (2014). The probability the
  true Sharpe is above zero once you account for how many trials were run and
  for non-normal returns (skew and kurtosis both matter: negative skew and fat
  tails make a given Sharpe less impressive).
- `pbo_cscv` — Bailey et al.'s Combinatorially Symmetric Cross-Validation
  probability of backtest overfitting: how often the configuration that looked
  best in-sample lands in the bottom half out-of-sample. ~0.5 means your
  selection procedure has no skill at all.
- `minimum_backtest_length` — how many years you need before a given Sharpe,
  found after N trials, means anything.

This module only reports. It never edits a config, and nothing in `run.py`,
`decision/` or `risk/` imports it — a number produced here is an input to a
review, not to a live parameter, and `HOLDOUT-LEDGER.md` still governs whether
a window may be looked at again.

    python -m agentic_trading.backtest.overfit --sharpe 1.16 --trials 24 --observations 4873
"""
from __future__ import annotations

import argparse
import itertools
import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

# Euler–Mascheroni, in the expected-maximum-of-N-Gaussians term.
EULER_MASCHERONI = 0.5772156649015329


def normal_cdf(x: float) -> float:
    """Φ(x) via the error function — stdlib only."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def normal_ppf(p: float) -> float:
    """Φ⁻¹(p), Acklam's rational approximation (|error| < 1.15e-9).

    Present because the deflated-Sharpe threshold needs a probit and this
    project deliberately carries no scipy.
    """
    if not 0.0 < p < 1.0:
        raise ValueError(f"normal_ppf needs 0 < p < 1, got {p}")
    a = (-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00)
    b = (-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01)
    c = (-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00)
    d = (7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00)
    p_low, p_high = 0.02425, 1.0 - 0.02425
    if p < p_low:
        q = math.sqrt(-2.0 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
               ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0)
    if p > p_high:
        q = math.sqrt(-2.0 * math.log(1.0 - p))
        return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
                ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0)
    q = p - 0.5
    r = q * q
    return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / \
           (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1.0)


def expected_max_sharpe(trials: int, sharpe_variance: float = 1.0) -> float:
    """E[max Sharpe] across `trials` strategies that all truly have zero edge.

    This is the bar a searched result has to clear before it means anything:
    run enough variants and one of them looks good for free.
    """
    if trials < 1:
        raise ValueError("trials must be >= 1")
    if trials == 1:
        return 0.0
    scale = math.sqrt(max(sharpe_variance, 0.0))
    gamma = EULER_MASCHERONI
    return scale * (
        (1.0 - gamma) * normal_ppf(1.0 - 1.0 / trials)
        + gamma * normal_ppf(1.0 - 1.0 / (trials * math.e))
    )


@dataclass
class DeflatedSharpe:
    observed_sharpe: float
    benchmark_sharpe: float
    deflated: float
    trials: int
    observations: int

    @property
    def is_significant(self) -> bool:
        """Conventional 95% reading — a threshold, not a verdict."""
        return self.deflated >= 0.95


def probabilistic_sharpe_ratio(
    sharpe: float,
    observations: int,
    *,
    benchmark: float = 0.0,
    skew: float = 0.0,
    kurtosis: float = 3.0,
) -> float:
    """P(true Sharpe > `benchmark`), adjusted for skew and fat tails.

    All Sharpes here are per-observation, not annualised — see
    `deflated_sharpe_ratio` for the conversion note.
    """
    if observations < 2:
        raise ValueError("observations must be >= 2")
    denominator = math.sqrt(
        max(1.0 - skew * sharpe + ((kurtosis - 1.0) / 4.0) * sharpe ** 2, 1e-12)
    )
    return normal_cdf(
        (sharpe - benchmark) * math.sqrt(observations - 1) / denominator
    )


def deflated_sharpe_ratio(
    sharpe: float,
    observations: int,
    trials: int,
    *,
    skew: float = 0.0,
    kurtosis: float = 3.0,
    sharpe_variance: float | None = None,
    periods_per_year: float | None = None,
) -> DeflatedSharpe:
    """Bailey & López de Prado's deflated Sharpe ratio.

    `sharpe` is per-observation. Pass `periods_per_year` (e.g. 252) to hand in
    an annualised figure instead — annualised Sharpes are what the audit report
    prints, and feeding one in raw would overstate significance enormously.

    `sharpe_variance` is the variance of the Sharpes *across the trials that
    were run*, and must be in the same per-observation units as `sharpe`. When
    it is not supplied it defaults to `1 / observations`, the sampling variance
    of a per-observation Sharpe estimate under the null — the honest "I don't
    have the trial set" choice. Passing a variance in annualised units here
    would inflate the luck benchmark by ~252x and reject everything.
    """
    per_obs = sharpe / math.sqrt(periods_per_year) if periods_per_year else sharpe
    if sharpe_variance is None:
        sharpe_variance = 1.0 / observations
    benchmark = expected_max_sharpe(trials, sharpe_variance)
    return DeflatedSharpe(
        observed_sharpe=per_obs,
        benchmark_sharpe=benchmark,
        deflated=probabilistic_sharpe_ratio(
            per_obs, observations, benchmark=benchmark, skew=skew, kurtosis=kurtosis
        ),
        trials=trials,
        observations=observations,
    )


def minimum_backtest_length(sharpe: float, trials: int, *, periods_per_year: float = 252.0) -> float:
    """Years of data needed before a searched Sharpe is distinguishable from luck.

    `sharpe` annualised. Returns years.
    """
    if sharpe <= 0:
        return float("inf")
    benchmark = expected_max_sharpe(trials)
    if benchmark <= 0:
        return 0.0
    per_obs = sharpe / math.sqrt(periods_per_year)
    return ((benchmark / per_obs) ** 2 + 1.0) / periods_per_year


def pbo_cscv(returns: pd.DataFrame, splits: int = 8) -> float:
    """Probability of Backtest Overfitting via combinatorially symmetric CV.

    `returns` is {period × configuration} — one column per configuration tried,
    one row per period. The matrix is cut into `splits` contiguous blocks; for
    every way of choosing half the blocks as in-sample, the configuration with
    the best in-sample Sharpe is looked up in the complementary out-of-sample
    half and its *relative rank* recorded. PBO is how often that rank lands in
    the bottom half.

    ~0.5 means selecting on in-sample performance tells you nothing about
    out-of-sample performance. Low is good.
    """
    if returns.shape[1] < 2:
        raise ValueError("PBO needs at least 2 configurations to choose between")
    if splits % 2 or splits < 4:
        raise ValueError("splits must be an even number >= 4")
    if len(returns) < splits * 2:
        raise ValueError(f"need at least {splits * 2} periods for {splits} splits")

    blocks = np.array_split(np.arange(len(returns)), splits)
    values = returns.to_numpy(dtype=float)
    n_config = values.shape[1]
    logits: list[float] = []

    for chosen in itertools.combinations(range(splits), splits // 2):
        is_rows = np.concatenate([blocks[i] for i in chosen])
        oos_rows = np.concatenate([blocks[i] for i in range(splits) if i not in chosen])
        is_sharpe = _sharpe_columns(values[is_rows])
        oos_sharpe = _sharpe_columns(values[oos_rows])
        if np.all(np.isnan(is_sharpe)) or np.all(np.isnan(oos_sharpe)):
            continue
        best = int(np.nanargmax(is_sharpe))
        # Relative rank of the in-sample winner within the OOS results.
        order = pd.Series(oos_sharpe).rank(method="average").to_numpy()
        relative = order[best] / (n_config + 1.0)
        relative = min(max(relative, 1e-9), 1 - 1e-9)
        logits.append(math.log(relative / (1.0 - relative)))

    if not logits:
        raise ValueError("No usable in/out-of-sample split produced a Sharpe")
    # PBO = share of splits whose winner landed in the bottom half (logit < 0).
    return float(np.mean([1.0 if v <= 0.0 else 0.0 for v in logits]))


def _sharpe_columns(block: np.ndarray) -> np.ndarray:
    """Per-observation Sharpe of each column; NaN where it is undefined."""
    with np.errstate(invalid="ignore", divide="ignore"):
        std = block.std(axis=0, ddof=1)
        sharpe = np.where(std > 0, block.mean(axis=0) / std, np.nan)
    return sharpe


def format_deflated(result: DeflatedSharpe, *, label: str = "") -> str:
    head = f"Deflated Sharpe{' — ' + label if label else ''}"
    verdict = (
        "clears the 95% bar" if result.is_significant
        else "does NOT clear the 95% bar — consistent with search luck"
    )
    return "\n".join([
        head,
        f"  trials searched      {result.trials}",
        f"  observations         {result.observations:,}",
        f"  observed Sharpe      {result.observed_sharpe:.4f}  (per observation)",
        f"  luck benchmark       {result.benchmark_sharpe:.4f}  "
        f"(E[max] of {result.trials} zero-edge trials)",
        f"  deflated Sharpe      {result.deflated:.4f}  → {verdict}",
    ])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--sharpe", type=float, required=True,
                        help="Annualised Sharpe unless --per-observation is set.")
    parser.add_argument("--trials", type=int, required=True,
                        help="How many configurations were tried before picking this one.")
    parser.add_argument("--observations", type=int, required=True,
                        help="Number of return observations (sessions).")
    parser.add_argument("--skew", type=float, default=0.0)
    parser.add_argument("--kurtosis", type=float, default=3.0)
    parser.add_argument("--sharpe-variance", type=float, default=None,
                        help="Variance of the per-observation Sharpes across the trials, "
                             "if known. Defaults to 1/observations.")
    parser.add_argument("--per-observation", action="store_true",
                        help="Treat --sharpe as already per-observation.")
    parser.add_argument("--periods-per-year", type=float, default=252.0)
    args = parser.parse_args(argv)

    result = deflated_sharpe_ratio(
        args.sharpe, args.observations, args.trials,
        skew=args.skew, kurtosis=args.kurtosis,
        sharpe_variance=args.sharpe_variance,
        periods_per_year=None if args.per_observation else args.periods_per_year,
    )
    print(format_deflated(result))
    if not args.per_observation:
        years = minimum_backtest_length(
            args.sharpe, args.trials, periods_per_year=args.periods_per_year
        )
        print(f"\n  minimum backtest length  {years:.1f} years "
              f"(for Sharpe {args.sharpe:.2f} after {args.trials} trials)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
