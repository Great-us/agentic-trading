"""Multiple-testing corrections.

These are the numbers that decide whether a searched result is believed, so
they are checked against analytic values and known limiting behaviour rather
than against whatever the implementation happens to produce.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from agentic_trading.backtest.overfit import (
    deflated_sharpe_ratio,
    expected_max_sharpe,
    minimum_backtest_length,
    normal_cdf,
    normal_ppf,
    pbo_cscv,
    probabilistic_sharpe_ratio,
)


# --- the stdlib replacements for scipy ---------------------------------------

@pytest.mark.parametrize("x, expected", [
    (0.0, 0.5), (1.0, 0.8413447461), (-1.0, 0.1586552539),
    (1.959963985, 0.975), (-2.5758293, 0.005),
])
def test_normal_cdf_matches_published_values(x, expected):
    assert normal_cdf(x) == pytest.approx(expected, abs=1e-9)


@pytest.mark.parametrize("p, expected", [
    (0.5, 0.0), (0.975, 1.959963985), (0.025, -1.959963985),
    (0.99, 2.326347874), (0.001, -3.090232306),
])
def test_normal_ppf_matches_published_values(p, expected):
    assert normal_ppf(p) == pytest.approx(expected, abs=1e-6)


def test_ppf_and_cdf_are_inverses():
    for p in (0.01, 0.1, 0.3, 0.5, 0.7, 0.9, 0.99):
        assert normal_cdf(normal_ppf(p)) == pytest.approx(p, abs=1e-8)


@pytest.mark.parametrize("p", [0.0, 1.0, -0.1, 1.1])
def test_ppf_rejects_out_of_range(p):
    with pytest.raises(ValueError):
        normal_ppf(p)


# --- expected maximum Sharpe under the null ----------------------------------

def test_a_single_trial_has_no_selection_bias():
    assert expected_max_sharpe(1) == 0.0


def test_more_trials_raise_the_luck_bar():
    bars = [expected_max_sharpe(n) for n in (2, 5, 20, 100, 1000)]
    assert all(b > 0 for b in bars)
    assert bars == sorted(bars), "searching more configurations must raise the bar"


def test_the_luck_bar_scales_with_trial_dispersion():
    # Twice the Sharpe standard deviation across trials ⇒ twice the bar.
    assert expected_max_sharpe(50, sharpe_variance=4.0) == pytest.approx(
        2.0 * expected_max_sharpe(50, sharpe_variance=1.0)
    )


def test_trial_variance_defaults_to_the_null_sampling_variance():
    # The benchmark must land in the same per-observation units as the Sharpe
    # it is compared against; an annualised-units variance would reject
    # everything. Default is 1/observations.
    result = deflated_sharpe_ratio(0.05, 2500, trials=30)
    assert result.benchmark_sharpe == pytest.approx(
        expected_max_sharpe(30, 1.0 / 2500)
    )
    assert result.benchmark_sharpe < 0.1, "luck bar must be on the same scale as the Sharpe"


def test_expected_max_sharpe_rejects_zero_trials():
    with pytest.raises(ValueError):
        expected_max_sharpe(0)


# --- probabilistic Sharpe ----------------------------------------------------

def test_psr_of_a_zero_sharpe_is_a_coin_flip():
    assert probabilistic_sharpe_ratio(0.0, 1000) == pytest.approx(0.5)


def test_psr_rises_with_sample_size():
    short = probabilistic_sharpe_ratio(0.05, 100)
    long = probabilistic_sharpe_ratio(0.05, 10_000)
    assert 0.5 < short < long < 1.0


def test_negative_skew_and_fat_tails_reduce_confidence():
    plain = probabilistic_sharpe_ratio(0.08, 2000)
    skewed = probabilistic_sharpe_ratio(0.08, 2000, skew=-1.5)
    fat = probabilistic_sharpe_ratio(0.08, 2000, kurtosis=9.0)
    assert skewed < plain
    assert fat < plain


def test_psr_needs_two_observations():
    with pytest.raises(ValueError):
        probabilistic_sharpe_ratio(0.1, 1)


# --- deflated Sharpe ---------------------------------------------------------

def test_one_trial_deflates_to_the_plain_significance_test():
    # With no search, the deflated Sharpe must reduce to the PSR against zero.
    result = deflated_sharpe_ratio(0.05, 2000, trials=1)
    assert result.benchmark_sharpe == 0.0
    assert result.deflated == pytest.approx(probabilistic_sharpe_ratio(0.05, 2000))


def test_searching_harder_deflates_the_same_result():
    few = deflated_sharpe_ratio(0.06, 2000, trials=2)
    many = deflated_sharpe_ratio(0.06, 2000, trials=500)
    assert many.deflated < few.deflated
    assert many.benchmark_sharpe > few.benchmark_sharpe


def test_annualised_input_is_converted_not_taken_raw():
    # Handing in an annualised Sharpe as if it were per-observation would
    # overstate significance by a factor of sqrt(252) — the whole point of the
    # periods_per_year argument.
    annual = deflated_sharpe_ratio(1.16, 4873, trials=24, periods_per_year=252.0)
    assert annual.observed_sharpe == pytest.approx(1.16 / math.sqrt(252.0))
    raw = deflated_sharpe_ratio(1.16, 4873, trials=24)
    assert raw.observed_sharpe == pytest.approx(1.16)
    assert annual.deflated < raw.deflated


def test_a_strong_long_result_survives_a_modest_search():
    result = deflated_sharpe_ratio(1.16, 4873, trials=24, periods_per_year=252.0)
    assert result.is_significant


def test_a_weak_result_does_not_survive_a_wide_search():
    result = deflated_sharpe_ratio(0.35, 500, trials=400, periods_per_year=252.0)
    assert not result.is_significant


# --- minimum backtest length -------------------------------------------------

def test_minimum_length_grows_with_the_number_of_trials():
    assert (minimum_backtest_length(1.0, 100)
            > minimum_backtest_length(1.0, 10)
            > minimum_backtest_length(1.0, 2))


def test_a_higher_sharpe_needs_less_history():
    assert minimum_backtest_length(2.0, 50) < minimum_backtest_length(0.5, 50)


def test_a_non_positive_sharpe_can_never_be_established():
    assert minimum_backtest_length(0.0, 10) == float("inf")
    assert minimum_backtest_length(-0.5, 10) == float("inf")


# --- PBO / CSCV --------------------------------------------------------------

def _noise_matrix(configs=12, periods=400, seed=1):
    rng = np.random.default_rng(seed)
    return pd.DataFrame(rng.normal(scale=0.01, size=(periods, configs)))


def _permutation_null(configs=12, periods=400, seed=1):
    """Every configuration is the SAME return stream in a different order.

    This is the true null for CSCV: identical full-sample mean and volatility
    everywhere, so an in-sample advantage can only come from which periods
    happened to land in the in-sample half — the definition of noise-mining.
    (Independent noise columns are *not* this null: over 400 periods one column
    genuinely realises a persistent edge, keeps it out of sample, and correctly
    scores a low PBO.)
    """
    rng = np.random.default_rng(seed)
    base = rng.normal(scale=0.01, size=periods)
    return pd.DataFrame({i: rng.permutation(base) for i in range(configs)})


def test_selecting_on_noise_is_caught():
    # The in-sample winner spent its good periods in-sample, so it is left
    # below median out of sample — the purest overfitting signature.
    assert pbo_cscv(_permutation_null(), splits=8) >= 0.5


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_the_noise_verdict_does_not_depend_on_the_draw(seed):
    assert pbo_cscv(_permutation_null(seed=seed), splits=8) >= 0.5


def test_a_persistent_realised_edge_is_not_flagged():
    # Independent columns: whichever one realises the best full-sample Sharpe
    # keeps it across the split, which is not overfitting and must score low.
    assert pbo_cscv(_noise_matrix(), splits=8) < 0.3


def test_a_genuinely_better_configuration_scores_low():
    frame = _noise_matrix()
    # One column with a real, persistent edge: the in-sample winner should keep
    # winning out of sample, so PBO collapses toward zero.
    frame[0] = frame[0] + 0.02
    assert pbo_cscv(frame, splits=8) < 0.1


def test_pbo_needs_something_to_choose_between():
    with pytest.raises(ValueError, match="at least 2 configurations"):
        pbo_cscv(_noise_matrix(configs=1), splits=8)


@pytest.mark.parametrize("splits", [3, 5, 2])
def test_pbo_requires_an_even_split_count_of_at_least_four(splits):
    with pytest.raises(ValueError, match="even number"):
        pbo_cscv(_noise_matrix(), splits=splits)


def test_pbo_refuses_a_matrix_too_short_to_split():
    with pytest.raises(ValueError, match="at least"):
        pbo_cscv(_noise_matrix(periods=10), splits=8)


def test_pbo_is_deterministic():
    frame = _noise_matrix()
    assert pbo_cscv(frame, splits=8) == pbo_cscv(frame, splits=8)
