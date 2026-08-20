from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

from agentic_trading.data.market_data import (
    average_dollar_volume, drop_forming_bar, slice_asof, yahoo_symbol,
)


def _df(closes, start="2020-01-02"):
    idx = pd.bdate_range(start, periods=len(closes))
    close = pd.Series(closes, index=idx, dtype=float)
    return pd.DataFrame({
        "Open": close, "High": close + 1, "Low": close - 1,
        "Close": close, "Volume": 1_000_000.0,
    })


def test_yahoo_symbol_maps_share_class_dots():
    assert yahoo_symbol("BRK.B") == "BRK-B"
    assert yahoo_symbol("AAPL") == "AAPL"
    assert yahoo_symbol("^VIX") == "^VIX"


def test_slice_asof_hides_future_bars():
    df = _df([100, 101, 102, 200])
    asof = df.index[2]
    sliced = slice_asof(df, asof)
    assert len(sliced) == 3
    assert float(sliced["Close"].iloc[-1]) == 102
    assert 200 not in set(sliced["Close"])


def test_slice_asof_none_is_a_noop():
    df = _df([100, 101])
    assert slice_asof(df, None) is df or len(slice_asof(df, None)) == 2


def test_drop_forming_bar_before_the_close():
    df = _df([100, 101, 102], start="2020-01-06")  # Mon-Wed
    now = datetime(2020, 1, 8, 9, 45, tzinfo=ZoneInfo("America/New_York"))
    dropped = drop_forming_bar(df, now=now)
    assert len(dropped) == 2
    assert float(dropped["Close"].iloc[-1]) == 101


def test_drop_forming_bar_keeps_today_after_the_close():
    df = _df([100, 101, 102], start="2020-01-06")
    now = datetime(2020, 1, 8, 16, 15, tzinfo=ZoneInfo("America/New_York"))
    kept = drop_forming_bar(df, now=now)
    assert len(kept) == 3


def test_average_dollar_volume():
    df = _df([10] * 20)
    df["Volume"] = 2_000_000
    assert average_dollar_volume(df) == 20_000_000
