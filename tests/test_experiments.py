import pandas as pd

from agentic_trading.backtest.experiments import (
    _stitch, ablations, continuous_threshold_walk_forward, format_ablations,
    format_surface, parameter_surface, walk_forward,
)
from tests.test_backtest import _trend, _with_macro


def test_stitch_chains_fold_equity_by_return():
    a = pd.Series([100.0, 110.0, 121.0], index=pd.to_datetime(["2020-01-02", "2020-06-01", "2020-12-31"]))
    b = pd.Series([50.0, 55.0], index=pd.to_datetime(["2021-01-04", "2021-12-31"]))
    out = _stitch([a, b])
    assert abs(out.iloc[0] - 100.0) < 1e-9
    # 2021 starts at 121 (end of 2020) and rises 10% → 133.1
    assert abs(out.iloc[-1] - 133.1) < 1e-6


def test_surface_ranks_a_short_window_without_crashing():
    _, strong = _trend("AAA", n=90, step=0.6)
    bars = _with_macro({"AAA": strong})
    start = strong.index[60].date().isoformat()
    end = strong.index[-1].date().isoformat()
    rows = parameter_surface(
        start, end, bars=bars, initial_cash=100_000,
        buy=(0.20, 0.35), atr=(2.5,),
    )
    assert len(rows) == 2
    text = format_surface(rows)
    assert "buy_threshold" in text
    assert rows[0].metrics.sharpe >= rows[1].metrics.sharpe


def test_walk_forward_emits_an_oos_fold():
    _, strong = _trend("AAA", n=400, step=0.5, start="2019-01-02")
    bars = _with_macro({"AAA": strong})
    folds, equity, combined = walk_forward(
        "2019-06-03", strong.index[-1].date().isoformat(),
        bars=bars, initial_cash=100_000, min_is_years=1,
        buy=(0.35,), atr=(2.5,),
    )
    assert len(folds) >= 1
    assert folds[0].chosen["buy_threshold"] == 0.35
    assert folds[0].oos_year == 2020
    assert not equity.empty
    assert combined.end_equity > 0


def test_ablations_cover_the_four_cuts():
    _, strong = _trend("AAA", n=90, step=0.6)
    _, spy = _trend("SPY", n=90, step=0.7)
    bars = _with_macro({"AAA": strong, "SPY": spy})
    start = strong.index[60].date().isoformat()
    end = strong.index[-1].date().isoformat()
    rows = ablations(start, end, bars=bars, initial_cash=100_000)
    names = [r.name for r in rows]
    assert names == ["baseline", "no_rsi", "no_macro", "stops_only_exits", "with_spy_qqq"]
    text = format_ablations(rows)
    assert "rip this piece out" in text


def test_continuous_walk_forward_emits_one_continuous_oos_result():
    _, strong = _trend("AAA", n=520, step=0.5, start="2018-01-02")
    bars = _with_macro({"AAA": strong})
    choices, result = continuous_threshold_walk_forward(
        "2018-01-02", strong.index[-1].date().isoformat(),
        bars=bars, initial_cash=100_000, min_is_years=1, buy=(0.35,),
    )
    assert choices
    assert result is not None
    assert result.equity.index.is_unique
    assert result.equity.index[0].year == choices[0].oos_year
