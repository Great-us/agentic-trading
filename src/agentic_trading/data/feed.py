"""Price/news/fundamentals providers that the cycle engine talks to.

Live trading uses YFinanceFeed (in-process memo + on-disk TTL cache).
Backtests use HistoricalFeed, which slices a preloaded OHLCV dict to an as-of date
so a day-T decision cannot see day-T+1.
"""
from __future__ import annotations

from typing import Protocol

import pandas as pd

from .market_data import (
    NewsItem,
    drop_forming_bar,
    fetch_fundamentals_summary,
    fetch_price_history,
    fetch_recent_news,
    slice_asof,
)


class DataFeed(Protocol):
    def price_history(self, symbol: str, asof=None, period: str = "6mo") -> pd.DataFrame: ...
    def news(self, symbol: str, limit: int = 5) -> list[NewsItem]: ...
    def fundamentals(self, symbol: str) -> dict: ...


class YFinanceFeed:
    """One fetch per (symbol, period) per process; optional drop of today's forming bar."""

    def __init__(self, drop_forming: bool = True):
        self.drop_forming = drop_forming
        self._mem: dict[tuple[str, str], pd.DataFrame] = {}

    def price_history(self, symbol: str, asof=None, period: str = "6mo") -> pd.DataFrame:
        key = (symbol, period)
        if key not in self._mem:
            self._mem[key] = fetch_price_history(symbol, period=period)
        df = slice_asof(self._mem[key], asof)
        if self.drop_forming and asof is None:
            df = drop_forming_bar(df)
        return df

    def news(self, symbol: str, limit: int = 5) -> list[NewsItem]:
        return fetch_recent_news(symbol, limit=limit)

    def fundamentals(self, symbol: str) -> dict:
        return fetch_fundamentals_summary(symbol)


class HistoricalFeed:
    """Preloaded bars only. News and fundamentals are empty — quant-only backtests."""

    def __init__(self, bars: dict[str, pd.DataFrame]):
        self.bars = bars

    def price_history(self, symbol: str, asof=None, period: str = "6mo") -> pd.DataFrame:
        df = self.bars.get(symbol)
        if df is None or df.empty:
            return pd.DataFrame()
        return slice_asof(df, asof)

    def news(self, symbol: str, limit: int = 5) -> list[NewsItem]:
        return []

    def fundamentals(self, symbol: str) -> dict:
        return {}
