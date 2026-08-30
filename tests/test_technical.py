import numpy as np
import pandas as pd

from agentic_trading.signals.technical import compute_signal, weights_without


def _make_price_df(closes: list[float]) -> pd.DataFrame:
    n = len(closes)
    close = pd.Series(closes)
    return pd.DataFrame({
        "Open": close,
        "High": close * 1.01,
        "Low": close * 0.99,
        "Close": close,
        "Volume": pd.Series([1_000_000] * n),
    })


def test_insufficient_history_returns_none():
    df = _make_price_df([100.0] * 10)
    assert compute_signal("TEST", df) is None


def test_steady_uptrend_scores_bullish():
    closes = list(np.linspace(100, 160, 80))  # strong, steady rise
    df = _make_price_df(closes)
    signal = compute_signal("UP", df)
    assert signal is not None
    assert signal.score > 0.3
    assert signal.sma20 > signal.sma50


def test_steady_downtrend_scores_bearish():
    closes = list(np.linspace(160, 100, 80))  # strong, steady decline
    df = _make_price_df(closes)
    signal = compute_signal("DOWN", df)
    assert signal is not None
    assert signal.score < -0.3
    assert signal.sma20 < signal.sma50


def test_flat_price_scores_near_zero():
    closes = [100.0] * 80
    df = _make_price_df(closes)
    signal = compute_signal("FLAT", df)
    assert signal is not None
    assert abs(signal.score) < 0.05


def test_weights_without_rsi_renormalises():
    weights = weights_without("rsi")
    assert weights["rsi"] == 0.0
    assert abs(sum(weights.values()) - 1.0) < 1e-9


def test_dropping_rsi_changes_an_overbought_uptrend_score():
    closes = list(np.linspace(100, 160, 80))
    df = _make_price_df(closes)
    full = compute_signal("UP", df)
    no_rsi = compute_signal("UP", df, weights=weights_without("rsi"))
    assert full is not None and no_rsi is not None
    # Relentless uptrend: RSI is hot, so it was subtracting; dropping it
    # should raise (or at least change) the composite.
    assert no_rsi.score != full.score


def test_score_always_within_bounds():
    # A price series with an extreme spike shouldn't push the composite score
    # outside [-1, 1] even though individual components can be large.
    closes = list(np.linspace(100, 100, 70)) + [500.0] * 10
    df = _make_price_df(closes)
    signal = compute_signal("SPIKE", df)
    assert signal is not None
    assert -1.0 <= signal.score <= 1.0
