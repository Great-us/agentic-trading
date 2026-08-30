"""Cross-sectional factor diagnostics for the signals this system trades on.

Everything else in `backtest/` answers "how did the strategy do?". This module
answers the question underneath it, which nothing here has ever asked: **does
the score rank stocks?** A composite of five hand-weighted components can post
a fine backtest on a hand-picked universe purely through position sizing, stop
placement and a bull tape, while carrying no cross-sectional information at
all. Rank information correlation (IC) and decile spreads separate the two.

Two factors are supported:

- ``quant``   — `signals/technical.py`'s composite, the live entry signal.
- ``rs``      — P2's rotation score, `0.5·pct(excess 20d) + 0.5·pct(excess 60d)`
                measured against SPY, exactly as `p2_roster.py:compute_rs`.

Read the output as a *diagnostic*, not as a tuning input. A weak IC does not
by itself say "change the weights"; it says the entry edge is not where you
assumed it was, and the `HOLDOUT-LEDGER.md` discipline still applies to any
parameter you touch afterwards.

Nothing here is imported by the live path — `run.py`, `decision/` and `risk/`
never see this module. It only reads cached daily bars.

    python -m agentic_trading.backtest.factor_ic --factor quant
    python -m agentic_trading.backtest.factor_ic --factor rs --horizon 20

Implementation lag: a score computed from date *t*'s close is scored against
the return from *t+1*'s close onward, never from *t*'s. Ranking on a close and
then claiming that close as your fill is the oldest way to manufacture an edge
that isn't there; the live book queues to the next session's entry window for
exactly the same reason.
"""
from __future__ import annotations

import argparse
import csv
import logging
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from ..signals.technical import DEFAULT_WEIGHTS
from .data import load_price_history
from .precompute import CachedSignalModel

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[3]
POOL_CSV = ROOT / "research" / "core-candidate-pool-2026-08-22.csv"
BENCHMARK = "SPY"

# P2's rotation windows (p2_roster.py:WINDOWS) — kept here so the diagnostic
# measures the roster's actual formula rather than a lookalike.
RS_WINDOWS = (20, 60)
DEFAULT_HORIZONS = (5, 20)
DEFAULT_BUCKETS = 10
# Below this many names a "decile" is meaningless — the spread is then just
# two noisy singletons differenced.
MIN_CROSS_SECTION = 20

# Block-bootstrap settings for the mean-IC interval. The seed matches the one
# `statistics.py` and `audit.py` already use so every resampled diagnostic in
# the project is reproducible from the same constant. 21 sessions ~= the
# longest forward horizon, which is also roughly the autocorrelation length an
# overlapping-horizon IC series carries — plain IID bootstrap would pretend
# the daily ICs are independent and understate the interval's width.
BOOTSTRAP_SEED = 20260822
BOOTSTRAP_BLOCK = 21
BOOTSTRAP_SAMPLES = 1_000


@dataclass
class FactorReport:
    factor: str
    horizon: int
    observations: int
    sessions: int
    mean_ic: float
    ic_std: float
    ic_ir: float
    hit_rate: float
    bucket_returns: list[float]
    bucket_counts: list[int]
    long_short_spread: float
    monotonicity: float
    # Block-bootstrap 95% interval for the mean rank IC; None when the IC
    # series is too short to resample. A diagnostic, not proof of edge.
    mean_ic_95: tuple[float, float] | None = None

    @property
    def top_bucket(self) -> float:
        return self.bucket_returns[-1]

    @property
    def bottom_bucket(self) -> float:
        return self.bucket_returns[0]


def pool_symbols(path: Path = POOL_CSV) -> list[str]:
    """The 252 Alpaca-tradable names from the quarterly screen (P2's universe)."""
    if not path.exists():
        raise FileNotFoundError(f"Candidate pool not found: {path}")
    symbols: set[str] = set()
    with open(path, newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            if str(row.get("alpaca_tradable", "")).strip().lower() not in {"true", "1", "yes"}:
                continue
            ticker = str(row.get("ticker", "")).strip().upper()
            if ticker:
                symbols.add(ticker)
    return sorted(symbols)


def _close_panel(bars: dict[str, pd.DataFrame], symbols: list[str]) -> pd.DataFrame:
    """{date × symbol} of closes, tz-naive and normalised to midnight."""
    series: dict[str, pd.Series] = {}
    for symbol in symbols:
        frame = bars.get(symbol)
        if frame is None or frame.empty or "Close" not in frame:
            continue
        close = frame["Close"].astype(float).copy()
        index = pd.DatetimeIndex(close.index)
        if index.tz is not None:
            index = index.tz_localize(None)
        close.index = index.normalize()
        series[symbol] = close[~close.index.duplicated(keep="last")]
    if not series:
        raise ValueError("No usable price history for any requested symbol.")
    return pd.DataFrame(series).sort_index()


def quant_scores(bars: dict[str, pd.DataFrame], symbols: list[str]) -> pd.DataFrame:
    """{date × symbol} composite scores, vectorised over the cached tables.

    Reuses `CachedSignalModel`, whose component columns are the same causal
    rolling windows the live `compute_signal` uses, and applies the identical
    weighting (`precompute.py` line ~133) across the whole series at once.
    """
    model = CachedSignalModel(bars, mode="composite")
    out: dict[str, pd.Series] = {}
    for symbol in symbols:
        table = model.tables.get(symbol)
        if table is None or table.empty:
            continue
        score = sum(
            weight * table[f"{key}_component"].astype(float)
            for key, weight in DEFAULT_WEIGHTS.items()
        ).clip(-1.0, 1.0)
        # The live signal refuses to score without SMA50/ATR; mirror that so a
        # warm-up window never enters the sample as a real reading.
        score = score.where(table["sma50"].notna() & table["atr"].notna())
        out[symbol] = score
    if not out:
        raise ValueError("No symbol produced a usable score table.")
    return pd.DataFrame(out).sort_index()


def rs_scores(closes: pd.DataFrame, benchmark: pd.Series) -> pd.DataFrame:
    """{date × symbol} of P2's relative-strength score.

    `RS = 0.5·percentile(excess 20d) + 0.5·percentile(excess 60d)`, percentiles
    taken across the cross-section on each date — `p2_roster.py:compute_rs`.
    """
    parts = []
    for window in RS_WINDOWS:
        excess = (closes / closes.shift(window) - 1.0).sub(
            benchmark / benchmark.shift(window) - 1.0, axis=0
        )
        parts.append(excess.rank(axis=1, pct=True) * 100.0)
    return sum(parts) / float(len(parts))


def mean_ic_block_bootstrap(
    ics: pd.Series,
    *,
    block_size: int = BOOTSTRAP_BLOCK,
    samples: int = BOOTSTRAP_SAMPLES,
    seed: int = BOOTSTRAP_SEED,
) -> tuple[float, float] | None:
    """Block-bootstrap 95% interval for the mean of a daily IC series.

    Sessions are resampled in contiguous `block_size` blocks (drawn with
    replacement, wrapped around the end as in `statistics.block_bootstrap_
    comparison`) so the interval respects the autocorrelation that overlapping
    forward horizons guarantee. Returns the 2.5%/97.5% percentiles of the
    resampled means, or None when there is nothing to resample. Seeded, so the
    number is reproducible run to run.
    """
    values = np.asarray(ics, dtype=float)
    values = values[~np.isnan(values)]
    n = len(values)
    if n == 0:
        return None
    rng = np.random.default_rng(seed)
    blocks_needed = int(np.ceil(n / block_size))
    means = np.empty(samples)
    for sample in range(samples):
        starts = rng.integers(0, n, size=blocks_needed)
        indices = np.concatenate(
            [(np.arange(start, start + block_size) % n) for start in starts]
        )[:n]
        means[sample] = values[indices].mean()
    low, high = np.quantile(means, [0.025, 0.975])
    return float(low), float(high)


def _spearman(left: pd.Series, right: pd.Series) -> float:
    """Rank correlation, by its definition: Pearson on ranks.

    `Series.corr(method="spearman")` delegates to scipy, which this project
    deliberately does not depend on. `.rank()` averages ties, which is the
    same tie handling Spearman specifies, so this is not an approximation.
    """
    return left.rank().corr(right.rank())


def forward_returns(closes: pd.DataFrame, horizon: int) -> pd.DataFrame:
    """Return earned from the session *after* the signal date, over `horizon`.

    Shifted by one session so a score read off date t's close is never paid the
    move that produced it.
    """
    entry = closes.shift(-1)
    return entry.shift(-horizon) / entry - 1.0


def evaluate(
    scores: pd.DataFrame,
    closes: pd.DataFrame,
    *,
    factor: str,
    horizon: int,
    buckets: int = DEFAULT_BUCKETS,
    min_cross_section: int = MIN_CROSS_SECTION,
) -> FactorReport:
    """Rank IC and bucketed forward returns for one factor at one horizon."""
    forward = forward_returns(closes, horizon)
    common = scores.columns.intersection(forward.columns)
    scores, forward = scores[common], forward[common]
    dates = scores.index.intersection(forward.index)

    ics: list[float] = []
    bucket_sums = np.zeros(buckets)
    bucket_counts = np.zeros(buckets, dtype=int)
    observations = 0

    for date in dates:
        row_score = scores.loc[date]
        row_forward = forward.loc[date]
        valid = row_score.notna() & row_forward.notna()
        if int(valid.sum()) < min_cross_section:
            continue
        row_score, row_forward = row_score[valid], row_forward[valid]

        ic = _spearman(row_score, row_forward)
        if pd.notna(ic):
            ics.append(float(ic))

        # qcut on ranks so ties (very common in a percentile factor) cannot
        # collapse the bucket edges.
        try:
            labels = pd.qcut(row_score.rank(method="first"), buckets, labels=False)
        except ValueError:
            continue
        for bucket in range(buckets):
            members = row_forward[labels == bucket]
            if members.empty:
                continue
            bucket_sums[bucket] += float(members.sum())
            bucket_counts[bucket] += int(members.size)
        observations += int(row_forward.size)

    if not ics:
        raise ValueError(
            f"No session had {min_cross_section}+ names with both a {factor} score "
            f"and a {horizon}-day forward return. Widen the pool or the history."
        )

    ic_series = pd.Series(ics)
    mean_ic = float(ic_series.mean())
    ic_std = float(ic_series.std(ddof=1)) if len(ic_series) > 1 else 0.0
    with np.errstate(invalid="ignore", divide="ignore"):
        ic_ir = float(mean_ic / ic_std) if ic_std > 0 else 0.0

    means = [
        float(bucket_sums[i] / bucket_counts[i]) if bucket_counts[i] else float("nan")
        for i in range(buckets)
    ]
    clean = [m for m in means if not np.isnan(m)]
    # Fraction of adjacent steps that go the right way: 1.0 is a perfectly
    # monotonic ladder, ~0.5 is noise. This is what separates "the top decile
    # happened to run" from "the score orders the cross-section".
    steps = [1.0 if clean[i + 1] >= clean[i] else 0.0 for i in range(len(clean) - 1)]
    monotonicity = float(np.mean(steps)) if steps else float("nan")

    return FactorReport(
        factor=factor,
        horizon=horizon,
        observations=observations,
        sessions=len(ics),
        mean_ic=mean_ic,
        ic_std=ic_std,
        ic_ir=ic_ir,
        hit_rate=float((ic_series > 0).mean()),
        bucket_returns=means,
        bucket_counts=[int(c) for c in bucket_counts],
        long_short_spread=(clean[-1] - clean[0]) if len(clean) >= 2 else float("nan"),
        monotonicity=monotonicity,
        mean_ic_95=mean_ic_block_bootstrap(ic_series),
    )


def format_report(report: FactorReport) -> str:
    pct = lambda x: "n/a" if np.isnan(x) else f"{x * 100:+.2f}%"  # noqa: E731
    if report.mean_ic_95 is None:
        ci_text = "n/a (too few sessions to resample)"
    else:
        ci_text = f"[{report.mean_ic_95[0]:+.4f}, {report.mean_ic_95[1]:+.4f}]"
    lines = [
        f"factor={report.factor}  horizon={report.horizon}d  "
        f"sessions={report.sessions}  observations={report.observations}",
        "",
        f"  mean rank IC     {report.mean_ic:+.4f}",
        f"  mean IC 95% CI   {ci_text}   "
        f"(block bootstrap, {BOOTSTRAP_BLOCK}d blocks, B={BOOTSTRAP_SAMPLES})",
        f"  IC std           {report.ic_std:.4f}",
        f"  IC IR            {report.ic_ir:+.3f}",
        f"  IC hit rate      {report.hit_rate * 100:.1f}%   (share of sessions with IC > 0)",
        "",
        "  bucket (low->high score)  mean fwd return      n",
    ]
    for i, (mean, count) in enumerate(zip(report.bucket_returns, report.bucket_counts)):
        lines.append(f"    {i + 1:>2}                     {pct(mean):>12}   {count:>8,}")
    lines += [
        "",
        f"  top - bottom     {pct(report.long_short_spread)}",
        f"  monotonicity     {report.monotonicity:.2f}   "
        "(1.00 = every step up the ladder pays more; ~0.50 = noise)",
        "",
        "  A long-only book harvests the top bucket only. If most of the spread",
        "  sits in a depressed bottom bucket rather than an elevated top one,",
        "  the factor's information is on the side this account cannot trade.",
        "  The CI above is an exploratory diagnostic of sampling noise, not",
        "  proof of edge - HOLDOUT-LEDGER.md still governs any retuning idea.",
    ]
    return "\n".join(lines)


def run(
    factor: str = "quant",
    horizons: tuple[int, ...] = DEFAULT_HORIZONS,
    *,
    symbols: list[str] | None = None,
    buckets: int = DEFAULT_BUCKETS,
    allow_stale: bool = True,
) -> list[FactorReport]:
    symbols = symbols if symbols is not None else pool_symbols()
    logger.info("Loading history for %d symbols (+ benchmark)…", len(symbols))
    bars = load_price_history([*symbols, BENCHMARK], allow_stale=allow_stale)
    closes = _close_panel(bars, [*symbols, BENCHMARK])
    if BENCHMARK not in closes.columns:
        raise ValueError(f"No history for the {BENCHMARK} benchmark; RS needs it.")
    benchmark = closes[BENCHMARK]
    universe = [s for s in closes.columns if s != BENCHMARK]

    if factor == "quant":
        scores = quant_scores(bars, universe)
    elif factor == "rs":
        scores = rs_scores(closes[universe], benchmark)
    else:
        raise ValueError(f"Unknown factor: {factor!r} (expected 'quant' or 'rs')")

    return [
        evaluate(scores, closes[universe], factor=factor, horizon=h, buckets=buckets)
        for h in horizons
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--factor", choices=["quant", "rs"], default="quant")
    parser.add_argument("--horizon", type=int, action="append", dest="horizons",
                        help="Forward-return horizon in sessions; repeatable "
                             f"(default {list(DEFAULT_HORIZONS)}).")
    parser.add_argument("--buckets", type=int, default=DEFAULT_BUCKETS)
    parser.add_argument("--refresh", action="store_true",
                        help="Re-download history instead of freezing the cache.")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    reports = run(
        factor=args.factor,
        horizons=tuple(args.horizons or DEFAULT_HORIZONS),
        buckets=args.buckets,
        allow_stale=not args.refresh,
    )
    for report in reports:
        print(format_report(report))
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
