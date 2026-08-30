"""Factor diagnostics: IC sign/magnitude, bucketing, and the look-ahead guard.

The whole point of this module is to tell the truth about whether a score ranks
stocks, so a bug that leaks the future would be worse than having no diagnostic
at all — it would manufacture the exact confidence it exists to test.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from agentic_trading.backtest.factor_ic import (
    MIN_CROSS_SECTION,
    evaluate,
    forward_returns,
    mean_ic_block_bootstrap,
    pool_symbols,
    quant_scores,
    rs_scores,
)

SESSIONS = 260
N = 40


def _dates(n=SESSIONS):
    return pd.bdate_range("2024-01-01", periods=n)


def _panel(values, columns=None, n=SESSIONS):
    columns = columns or [f"S{i:02d}" for i in range(np.shape(values)[1])]
    return pd.DataFrame(values, index=_dates(n), columns=columns)


# --- forward returns must never pay the bar they were computed from ----------

def test_forward_return_starts_the_session_after_the_signal():
    # A single name that jumps only on day 2. A score read on day 0 must not be
    # credited with that jump unless it is genuinely inside its forward window.
    closes = _panel(np.array([[100.0], [100.0], [110.0], [110.0], [110.0]]), ["A"], n=5)
    fwd = forward_returns(closes, horizon=1)
    # Day 0's score buys at day 1's close (100) and is marked at day 2 (110).
    assert fwd["A"].iloc[0] == pytest.approx(0.10)
    # Day 1's score buys at day 2's close (110) — the jump already happened and
    # is not paid again.
    assert fwd["A"].iloc[1] == pytest.approx(0.0)


def test_forward_return_never_reads_earlier_than_its_own_entry():
    closes = _panel(np.cumprod(1 + np.zeros((10, 1)) + 0.01, axis=0) * 100.0, ["A"], n=10)
    fwd = forward_returns(closes, horizon=3)
    entry = closes["A"].shift(-1)
    expected = entry.shift(-3) / entry - 1.0
    pd.testing.assert_series_equal(fwd["A"], expected, check_names=False)


def test_tail_rows_have_no_forward_return():
    closes = _panel(np.ones((10, 1)) * 100.0, ["A"], n=10)
    fwd = forward_returns(closes, horizon=3)
    # Last 4 sessions cannot have a 3-day forward return measured from t+1.
    assert fwd["A"].iloc[-4:].isna().all()


# --- IC sign and magnitude on a constructed panel ----------------------------

def _rigged_panel(sign: float, rng_seed: int = 0):
    """Scores that (anti)predict the next move by construction."""
    rng = np.random.default_rng(rng_seed)
    scores = rng.normal(size=(SESSIONS, N))
    # Forward move is the score (or its negation) plus a little noise, so the
    # rank relationship is strong but not degenerate.
    steps = sign * scores * 0.01 + rng.normal(scale=0.0005, size=(SESSIONS, N))
    closes = np.empty((SESSIONS + 2, N))
    closes[0] = 100.0
    closes[1] = 100.0
    for t in range(SESSIONS):
        closes[t + 2] = closes[t + 1] * (1.0 + steps[t])
    cols = [f"S{i:02d}" for i in range(N)]
    return (
        pd.DataFrame(scores, index=_dates(), columns=cols),
        pd.DataFrame(closes[: SESSIONS], index=_dates(), columns=cols),
    )


def test_a_predictive_score_shows_positive_ic_and_a_rising_ladder():
    scores, closes = _rigged_panel(sign=+1.0)
    report = evaluate(scores, closes, factor="rigged", horizon=1, buckets=5)
    assert report.mean_ic > 0.5
    assert report.hit_rate > 0.9
    assert report.long_short_spread > 0
    assert report.monotonicity == pytest.approx(1.0)
    assert report.top_bucket > report.bottom_bucket


def test_an_inverted_score_shows_negative_ic_and_a_falling_ladder():
    scores, closes = _rigged_panel(sign=-1.0)
    report = evaluate(scores, closes, factor="rigged", horizon=1, buckets=5)
    assert report.mean_ic < -0.5
    assert report.long_short_spread < 0
    assert report.monotonicity == pytest.approx(0.0)


def test_a_random_score_shows_no_edge():
    rng = np.random.default_rng(7)
    cols = [f"S{i:02d}" for i in range(N)]
    scores = pd.DataFrame(rng.normal(size=(SESSIONS, N)), index=_dates(), columns=cols)
    walk = 100.0 * np.cumprod(1 + rng.normal(scale=0.01, size=(SESSIONS, N)), axis=0)
    closes = pd.DataFrame(walk, index=_dates(), columns=cols)
    report = evaluate(scores, closes, factor="noise", horizon=1, buckets=5)
    assert abs(report.mean_ic) < 0.1
    assert 0.3 < report.hit_rate < 0.7


# --- mean-IC block bootstrap -------------------------------------------------

def test_bootstrap_is_deterministic_with_the_fixed_seed():
    rng = np.random.default_rng(5)
    ics = pd.Series(rng.normal(0.02, 0.15, size=250))
    assert mean_ic_block_bootstrap(ics) == mean_ic_block_bootstrap(ics)


def test_an_all_zero_ic_series_has_a_degenerate_zero_interval():
    low, high = mean_ic_block_bootstrap(pd.Series(np.zeros(120)))
    assert low == 0.0 and high == 0.0


def test_a_clearly_positive_ic_series_excludes_zero():
    rng = np.random.default_rng(9)
    ics = pd.Series(0.05 + rng.normal(scale=0.01, size=250))
    low, high = mean_ic_block_bootstrap(ics)
    assert low > 0.0
    assert low <= 0.05 <= high


def test_bootstrap_returns_none_for_an_empty_series():
    assert mean_ic_block_bootstrap(pd.Series(dtype=float)) is None


def test_evaluate_carries_the_interval_and_the_report_prints_it():
    from agentic_trading.backtest.factor_ic import format_report
    scores, closes = _rigged_panel(sign=+1.0)
    report = evaluate(scores, closes, factor="rigged", horizon=1, buckets=5)
    assert report.mean_ic_95 is not None
    low, high = report.mean_ic_95
    assert low > 0.0  # this rigged panel is strongly predictive
    assert low <= report.mean_ic <= high
    text = format_report(report)
    assert "mean IC 95% CI" in text
    assert "exploratory diagnostic" in text
    assert all(ord(ch) < 128 for ch in text)


# --- bucketing and guards ----------------------------------------------------

def test_every_bucket_is_populated_and_counts_add_up():
    scores, closes = _rigged_panel(sign=+1.0)
    report = evaluate(scores, closes, factor="rigged", horizon=1, buckets=5)
    assert all(c > 0 for c in report.bucket_counts)
    assert sum(report.bucket_counts) == report.observations


def test_a_thin_cross_section_is_refused_rather_than_reported():
    # Two names cannot make a decile; a silent tiny-n answer is worse than none.
    rng = np.random.default_rng(3)
    cols = ["A", "B"]
    scores = pd.DataFrame(rng.normal(size=(SESSIONS, 2)), index=_dates(), columns=cols)
    closes = pd.DataFrame(
        100.0 * np.cumprod(1 + rng.normal(scale=0.01, size=(SESSIONS, 2)), axis=0),
        index=_dates(), columns=cols,
    )
    with pytest.raises(ValueError, match="names with both"):
        evaluate(scores, closes, factor="thin", horizon=1, buckets=5)


def test_min_cross_section_is_large_enough_for_deciles():
    assert MIN_CROSS_SECTION >= 20


# --- the two real factors ----------------------------------------------------

def _trending_bars(n=300):
    """Two names: one steadily up, one steadily down."""
    up = 100.0 * np.cumprod(np.full(n, 1.004))
    down = 100.0 * np.cumprod(np.full(n, 0.996))
    out = {}
    for symbol, closes in (("UP", up), ("DOWN", down)):
        out[symbol] = pd.DataFrame(
            {
                "Open": closes, "High": closes * 1.01, "Low": closes * 0.99,
                "Close": closes, "Volume": np.full(n, 1_000_000.0),
            },
            index=_dates(n),
        )
    return out


def test_quant_scores_rank_an_uptrend_above_a_downtrend():
    bars = _trending_bars()
    scores = quant_scores(bars, ["UP", "DOWN"])
    latest = scores.dropna().iloc[-1]
    assert latest["UP"] > 0 > latest["DOWN"]


def test_quant_scores_are_blank_during_the_warmup_window():
    # The live signal refuses to score without SMA50/ATR; the panel must agree
    # instead of quietly emitting a partial-window number.
    bars = _trending_bars()
    scores = quant_scores(bars, ["UP", "DOWN"])
    assert scores.iloc[:40].isna().all().all()


def test_rs_scores_reproduce_the_roster_formula():
    n = 200
    cols = ["FAST", "SLOW"]
    fast = 100.0 * np.cumprod(np.full(n, 1.005))
    slow = 100.0 * np.cumprod(np.full(n, 1.0005))
    bench = pd.Series(100.0 * np.cumprod(np.full(n, 1.001)), index=_dates(n))
    closes = pd.DataFrame({"FAST": fast, "SLOW": slow}, index=_dates(n))
    rs = rs_scores(closes, bench)
    latest = rs.dropna().iloc[-1]
    # Percentile ranks across a 2-name cross-section are 50/100.
    assert latest["FAST"] == pytest.approx(100.0)
    assert latest["SLOW"] == pytest.approx(50.0)


def test_rs_needs_the_longer_window_before_it_reports():
    n = 200
    closes = pd.DataFrame(
        {"A": np.full(n, 100.0), "B": np.full(n, 100.0)}, index=_dates(n)
    )
    bench = pd.Series(np.full(n, 100.0), index=_dates(n))
    rs = rs_scores(closes, bench)
    assert rs.iloc[:59].isna().all().all()
    assert rs.iloc[60:].notna().all().all()


# --- the pool the diagnostic runs on -----------------------------------------

def test_pool_symbols_reads_only_tradable_rows(tmp_path):
    csv_path = tmp_path / "pool.csv"
    csv_path.write_text(
        "ticker,alpaca_tradable\nAAA,True\nBBB,False\nccc,True\n,True\n",
        encoding="utf-8",
    )
    assert pool_symbols(csv_path) == ["AAA", "CCC"]


def test_missing_pool_is_a_clear_error(tmp_path):
    with pytest.raises(FileNotFoundError):
        pool_symbols(tmp_path / "nope.csv")
