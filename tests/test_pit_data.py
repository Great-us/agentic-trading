from dataclasses import replace

import numpy as np
import pandas as pd

from agentic_trading.backtest.benchmarks import (
    point_in_time_universe_returns,
    random_portfolio_placebo_returns,
)
from agentic_trading.backtest.pit_data import (
    _alpaca_prices,
    _asset_id,
    _attach_delisting_return,
    _normalise_prices,
    _secondary_members_asof,
)
from agentic_trading.backtest.universe import UniverseSchedule
from agentic_trading.execution.broker import Position
from agentic_trading.risk.manager import theme_room_dollars
from tests.test_risk import RISK


def _bars(values, index):
    close = np.asarray(values, dtype=float)
    return pd.DataFrame({
        "Open": close,
        "High": close + 1,
        "Low": close - 1,
        "Close": close,
        "Volume": 1_000_000,
    }, index=index)


def test_asset_id_is_stable_and_separates_reentry_episodes():
    assert _asset_id("ABC", "2020-01-01") == _asset_id("ABC", "2020-01-01")
    assert _asset_id("ABC", "2020-01-01") != _asset_id("ABC", "2024-01-01")


def test_secondary_snapshot_rewinds_additions_and_removals():
    payload = {
        "current": ["NEW", "KEEP"],
        "changes": [{"date": "2024-06-03", "added": "NEW", "removed": "OLD"}],
    }
    assert _secondary_members_asof(payload, pd.Timestamp("2024-06-03")) == {"NEW", "KEEP"}
    assert _secondary_members_asof(payload, pd.Timestamp("2024-05-31")) == {"OLD", "KEEP"}


def test_price_normalisation_prefers_adjusted_tiingo_columns():
    raw = pd.DataFrame({
        "date": ["2024-01-02T00:00:00Z"],
        "open": [50], "high": [51], "low": [49], "close": [50], "volume": [10],
        "adjOpen": [100], "adjHigh": [102], "adjLow": [98], "adjClose": [100], "adjVolume": [5],
    })
    result = _normalise_prices(raw)
    assert result.iloc[0]["Open"] == 100
    assert result.iloc[0]["Close"] == 100


def test_alpaca_prices_normalises_and_caches_adjusted_sip_bars(tmp_path, monkeypatch):
    cache = tmp_path / "LEGACY.parquet"
    monkeypatch.setattr(
        "agentic_trading.backtest.pit_data._alpaca_cache_path",
        lambda _ticker: cache,
    )
    index = pd.MultiIndex.from_tuples(
        [("LEGACY", pd.Timestamp("2020-01-02", tz="UTC"))],
        names=["symbol", "timestamp"],
    )
    response = type("Response", (), {"df": pd.DataFrame({
        "open": [10.0], "high": [11.0], "low": [9.0],
        "close": [10.5], "volume": [1_000],
    }, index=index)})()

    class Client:
        def get_stock_bars(self, request):
            assert request.feed.value == "sip"
            assert request.adjustment.value == "all"
            return response

    result = _alpaca_prices(
        "LEGACY", "2019-01-01", "2020-12-31", "key", "secret", client=Client(),
    )
    assert result.iloc[0]["Close"] == 10.5
    assert cache.exists()
    cached = _alpaca_prices("LEGACY", "2019-01-01", "2020-12-31", None, None)
    pd.testing.assert_frame_equal(result, cached)


def test_delisting_return_creates_a_terminal_business_day_bar():
    index = pd.bdate_range("2024-01-02", periods=3)
    frame = _bars([100, 101, 102], index)
    delist = pd.DataFrame([{
        "ticker": "AAA", "observed_delist_date": "2024-01-06",
        "dlret": -0.55, "dlret_confidence": "medium", "dlret_method": "test",
    }])
    result, attached = _attach_delisting_return(
        frame, "AAA", pd.Timestamp("2023-01-01"), pd.Timestamp("2024-12-31"), delist,
    )
    terminal_day = pd.Timestamp("2024-01-08")
    assert attached
    assert result.loc[terminal_day, "DelistingReturn"] == -0.55
    assert abs(result.loc[terminal_day, "Close"] - 45.9) < 1e-12


def test_point_in_time_equal_weight_lags_membership_one_session(tmp_path):
    index = pd.bdate_range("2024-01-02", periods=4)
    path = tmp_path / "membership.csv"
    pd.DataFrame({
        "symbol": ["A_ID", "B_ID"],
        "instrument_id": ["A_ID", "B_ID"],
        "ticker": ["AAA", "BBB"],
        "start_date": ["2024-01-02", "2024-01-04"],
        "end_date": [None, None],
        "sector": ["Tech", "Health"],
        "source_id": ["test", "test"],
    }).to_csv(path, index=False)
    schedule = UniverseSchedule.from_csv(path)
    bars = {
        "A_ID": _bars([100, 110, 121, 133.1], index),
        "B_ID": _bars([100, 200, 400, 800], index),
    }
    returns = point_in_time_universe_returns(bars, schedule, index)
    assert abs(returns.iloc[1] - 0.10) < 1e-12
    # BBB joins on Jan 4, so its Jan 4 return was not investable at Jan 3 close.
    assert abs(returns.iloc[2] - 0.10) < 1e-12
    assert abs(returns.iloc[3] - 0.55) < 1e-12


def test_random_placebo_is_seed_deterministic(tmp_path):
    index = pd.bdate_range("2024-01-02", periods=25)
    path = tmp_path / "membership.csv"
    tickers = [f"S{number}" for number in range(6)]
    pd.DataFrame({
        "symbol": tickers,
        "start_date": ["2024-01-01"] * 6,
        "end_date": [None] * 6,
        "sector": ["A", "A", "B", "B", "C", "C"],
        "source_id": ["test"] * 6,
    }).to_csv(path, index=False)
    schedule = UniverseSchedule.from_csv(path)
    bars = {ticker: _bars(100 + np.arange(len(index)) * (n + 1), index) for n, ticker in enumerate(tickers)}
    first = random_portfolio_placebo_returns(bars, schedule, index, samples=10, positions=3, seed=7)
    second = random_portfolio_placebo_returns(bars, schedule, index, samples=10, positions=3, seed=7)
    pd.testing.assert_frame_equal(first, second)


def test_theme_cap_counts_existing_and_reserved_positions():
    risk = replace(
        RISK,
        theme_symbols=["A", "B", "C"],
        max_theme_pct=0.25,
        max_theme_positions=2,
    )
    positions = {"A": Position("A", 1, 100, 100, 10_000)}
    assert theme_room_dollars("B", 100_000, positions, risk, reserved={"C": 5_000}) == 0
    assert theme_room_dollars("OUTSIDE", 100_000, positions, risk) is None
