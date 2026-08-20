"""Price history from Alpaca's market-data API, for the intraday fast tier.

Why this exists: `YFinanceFeed` drops today's still-forming daily bar
(`drop_forming_bar`), which is correct for the twice-daily deep cycle — a 9:45
run must not compute SMA/RSI/MACD on 15 minutes of tape — but it means an
intraday scan re-scores yesterday's close every time. Verified live mid-session
2026-08-20 13:36 ET: the fast scan was scoring MSFT's 2026-08-19 close of
484.31 while the live quote was 481.61/486.00. Every 20-minute scan that day
was arithmetically identical to the last.

Alpaca's free tier (same paper key already in .env — no data subscription)
returns today's in-progress daily bar, and batch-fetches the whole watchlist in
one request: 8 symbols x 300 daily bars measured at ~1.1s, versus yfinance's
one-symbol-at-a-time seconds each.

Only price history comes from here. News and fundamentals still come from
yfinance, since Alpaca's free tier has no fundamentals — so the deep cycle's
qualitative inputs are unchanged.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import pandas as pd

from .market_data import (
    NewsItem,
    drop_forming_bar,
    fetch_fundamentals_summary,
    fetch_price_history,
    fetch_recent_news,
    slice_asof,
)

logger = logging.getLogger(__name__)

# Enough daily bars for a 50-day SMA plus warmup, with slack for holidays.
DEFAULT_LOOKBACK_DAYS = 400


def _is_index_symbol(symbol: str) -> bool:
    """Alpaca's stock-bars endpoint is equities-only and 400s on index symbols
    ('invalid symbol: ^VIX'). The macro regime pillar reads ^VIX, so those have
    to keep coming from yfinance or the regime silently loses its volatility
    input — observed live: VIX vanished from the regime line the moment this
    feed took over, while the ratio ETFs (SPY/HYG/XLY/...) kept working because
    they are ordinary equities."""
    return symbol.startswith("^")


class AlpacaFeed:
    """Batch-fetched daily bars, memoized per process.

    `drop_forming=True` reproduces YFinanceFeed's behaviour (settled bars only)
    so the deep cycle can use this feed too; the fast tier passes False to
    actually see the current session.
    """

    def __init__(
        self,
        api_key: str,
        secret_key: str,
        *,
        drop_forming: bool = False,
        lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    ):
        self.api_key = api_key
        self.secret_key = secret_key
        self.drop_forming = drop_forming
        self.lookback_days = lookback_days
        self._bars: dict[str, pd.DataFrame] = {}
        self._client = None

    def _get_client(self):
        if self._client is None:
            from alpaca.data.historical import StockHistoricalDataClient

            self._client = StockHistoricalDataClient(self.api_key, self.secret_key)
        return self._client

    def prefetch(self, symbols: list[str]) -> None:
        """One request for the whole universe. Failure is non-fatal: symbols
        stay unmemoized and `price_history` returns empty for them, which
        `compute_signal` already treats as 'skip this symbol'."""
        wanted = [s for s in symbols if s not in self._bars and not _is_index_symbol(s)]
        if not wanted:
            return

        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame

        start = datetime.now(timezone.utc) - timedelta(days=self.lookback_days)
        try:
            response = self._get_client().get_stock_bars(
                StockBarsRequest(symbol_or_symbols=wanted, timeframe=TimeFrame.Day, start=start)
            )
        except Exception:
            logger.exception("Alpaca batch bar fetch failed for %d symbols", len(wanted))
            return

        frame = getattr(response, "df", None)
        if frame is None or frame.empty:
            logger.warning("Alpaca returned no bars for %s", ", ".join(wanted))
            return

        # MultiIndex (symbol, timestamp) -> one OHLCV frame per symbol, with the
        # column names the rest of the pipeline expects from yfinance.
        for symbol in wanted:
            try:
                per_symbol = frame.xs(symbol, level="symbol")
            except KeyError:
                logger.warning("Alpaca returned no bars for %s", symbol)
                continue
            self._bars[symbol] = per_symbol.rename(columns={
                "open": "Open", "high": "High", "low": "Low",
                "close": "Close", "volume": "Volume",
            })

    def price_history(self, symbol: str, asof=None, period: str = "6mo") -> pd.DataFrame:
        if _is_index_symbol(symbol):
            # Indices aren't on Alpaca's equities endpoint; fall back rather
            # than let the macro regime lose them. These are read once per
            # cycle, so the slower path costs little.
            df = fetch_price_history(symbol, period=period)
            if df.empty:
                return df
            df = slice_asof(df, asof)
            return drop_forming_bar(df) if (self.drop_forming and asof is None) else df

        if symbol not in self._bars:
            self.prefetch([symbol])
        df = self._bars.get(symbol)
        if df is None or df.empty:
            return pd.DataFrame()
        df = slice_asof(df, asof)
        if self.drop_forming and asof is None:
            df = drop_forming_bar(df)
        return df

    def news(self, symbol: str, limit: int = 5) -> list[NewsItem]:
        return fetch_recent_news(symbol, limit=limit)

    def fundamentals(self, symbol: str) -> dict:
        return fetch_fundamentals_summary(symbol)
