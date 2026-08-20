"""The whole point of AlpacaFeed is that the fast tier can see today's
in-progress bar — the yfinance path deliberately cannot. These pin down that
distinction and the batch/failure behaviour, without hitting the network."""
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from agentic_trading.data.alpaca_feed import AlpacaFeed


def _multiindex_frame(symbols, closes, start="2020-01-06"):
    """Mimics the (symbol, timestamp) MultiIndex frame alpaca-py returns."""
    idx = pd.bdate_range(start, periods=len(closes), tz="UTC")
    rows = []
    for symbol in symbols:
        for ts, close in zip(idx, closes):
            rows.append({
                "symbol": symbol, "timestamp": ts,
                "open": close, "high": close + 1, "low": close - 1,
                "close": close, "volume": 1_000_000.0,
            })
    return pd.DataFrame(rows).set_index(["symbol", "timestamp"])


class _FakeResponse:
    def __init__(self, df):
        self.df = df


def _feed_with(monkeypatch, df, **kwargs):
    feed = AlpacaFeed("key", "secret", **kwargs)

    class _FakeClient:
        def __init__(self):
            self.calls = 0

        def get_stock_bars(self, request):
            self.calls += 1
            return _FakeResponse(df)

    client = _FakeClient()
    monkeypatch.setattr(feed, "_get_client", lambda: client)
    return feed, client


def test_renames_alpaca_columns_to_pipeline_names(monkeypatch):
    df = _multiindex_frame(["MSFT"], [100, 101, 102])
    feed, _ = _feed_with(monkeypatch, df)
    out = feed.price_history("MSFT")
    assert list(out.columns) == ["Open", "High", "Low", "Close", "Volume"]
    assert float(out["Close"].iloc[-1]) == 102


def test_batch_prefetch_is_one_call_for_many_symbols(monkeypatch):
    df = _multiindex_frame(["MSFT", "NVDA", "JPM"], [100, 101])
    feed, client = _feed_with(monkeypatch, df)
    feed.prefetch(["MSFT", "NVDA", "JPM"])
    assert client.calls == 1
    # All three are memoized, so reading them costs no further calls.
    for symbol in ("MSFT", "NVDA", "JPM"):
        assert not feed.price_history(symbol).empty
    assert client.calls == 1


def test_already_memoized_symbols_are_not_refetched(monkeypatch):
    df = _multiindex_frame(["MSFT"], [100, 101])
    feed, client = _feed_with(monkeypatch, df)
    feed.prefetch(["MSFT"])
    feed.prefetch(["MSFT"])
    assert client.calls == 1


def test_keeps_todays_forming_bar_when_drop_forming_is_false(monkeypatch):
    et = ZoneInfo("America/New_York")
    today = datetime.now(et).date()
    idx = pd.DatetimeIndex([pd.Timestamp(today, tz="UTC")])
    df = pd.DataFrame(
        [{"symbol": "MSFT", "timestamp": idx[0], "open": 10.0, "high": 11.0,
          "low": 9.0, "close": 10.5, "volume": 1_000.0}]
    ).set_index(["symbol", "timestamp"])

    feed, _ = _feed_with(monkeypatch, df, drop_forming=False)
    out = feed.price_history("MSFT")
    assert len(out) == 1  # today's bar survives — this is the fast tier's whole point


def test_unknown_symbol_returns_empty_not_an_error(monkeypatch):
    df = _multiindex_frame(["MSFT"], [100, 101])
    feed, _ = _feed_with(monkeypatch, df)
    assert feed.price_history("NOPE").empty


def test_api_failure_degrades_to_empty(monkeypatch):
    feed = AlpacaFeed("key", "secret")

    class _Broken:
        def get_stock_bars(self, request):
            raise RuntimeError("data API down")

    monkeypatch.setattr(feed, "_get_client", lambda: _Broken())
    # compute_signal already treats an empty frame as "skip this symbol", so a
    # data outage must not raise out of the cycle.
    assert feed.price_history("MSFT").empty


def test_empty_response_degrades_to_empty(monkeypatch):
    feed, _ = _feed_with(monkeypatch, pd.DataFrame())
    assert feed.price_history("MSFT").empty


def test_index_symbols_bypass_alpaca_entirely(monkeypatch):
    """Regression: Alpaca's stock-bars endpoint 400s on '^VIX' ('invalid
    symbol'), and when this feed first shipped that silently stripped VIX out
    of the macro regime mid-session. Index symbols must route to yfinance and
    must never be included in an Alpaca batch request."""
    df = _multiindex_frame(["MSFT"], [100, 101])
    feed, client = _feed_with(monkeypatch, df)

    called = {}

    def _fake_yf(symbol, period="6mo"):
        called["symbol"] = symbol
        idx = pd.bdate_range("2020-01-06", periods=3)
        close = pd.Series([15.0, 16.0, 17.0], index=idx)
        return pd.DataFrame({
            "Open": close, "High": close, "Low": close,
            "Close": close, "Volume": 0.0,
        })

    monkeypatch.setattr("agentic_trading.data.alpaca_feed.fetch_price_history", _fake_yf)

    out = feed.price_history("^VIX")
    assert called["symbol"] == "^VIX"
    assert float(out["Close"].iloc[-1]) == 17.0
    assert client.calls == 0  # never hit the equities endpoint


def test_prefetch_excludes_index_symbols_from_the_batch(monkeypatch):
    df = _multiindex_frame(["SPY"], [100, 101])
    feed, client = _feed_with(monkeypatch, df)
    feed.prefetch(["SPY", "^VIX"])
    assert client.calls == 1
    assert "^VIX" not in feed._bars  # not memoized from the equities batch


def test_asof_slicing_still_hides_future_bars(monkeypatch):
    df = _multiindex_frame(["MSFT"], [100, 101, 102, 200])
    feed, _ = _feed_with(monkeypatch, df)
    full = feed.price_history("MSFT")
    asof = full.index[2]
    sliced = feed.price_history("MSFT", asof=asof)
    assert len(sliced) == 3
    assert 200 not in set(sliced["Close"])
