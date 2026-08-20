"""Download and cache long OHLCV history for the backtest universe."""
from __future__ import annotations

import logging
import time
from pathlib import Path

import pandas as pd
import yfinance as yf

from ..data.market_data import CACHE_DIR, yahoo_symbol
from ..signals.macro import RATIOS, VIX_TICKER

logger = logging.getLogger(__name__)

MAX_CACHE_DIR = CACHE_DIR / "max"
REFRESH_AFTER_SECONDS = 20 * 60 * 60  # ~one trading day


def macro_tickers() -> list[str]:
    tickers = {VIX_TICKER}
    for _, num, den, _ in RATIOS:
        tickers.add(num)
        tickers.add(den)
    return sorted(tickers)


def _max_path(symbol: str) -> Path:
    safe = symbol.replace("/", "_").replace("^", "idx_")
    return MAX_CACHE_DIR / f"{safe}.parquet"


def load_price_history(symbols: list[str], *, force: bool = False) -> dict[str, pd.DataFrame]:
    """Return {symbol: OHLCV} with as much daily history as Yahoo will give.

    Cached under data/cache/max/. A cache file newer than ~20h is reused as-is.
    """
    MAX_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    wanted = list(dict.fromkeys([*symbols, *macro_tickers()]))
    out: dict[str, pd.DataFrame] = {}
    for symbol in wanted:
        path = _max_path(symbol)
        if not force and path.exists() and (time.time() - path.stat().st_mtime) < REFRESH_AFTER_SECONDS:
            try:
                out[symbol] = pd.read_parquet(path)
                continue
            except Exception:
                logger.warning("Bad cache for %s, re-downloading", symbol)
        try:
            df = yf.Ticker(yahoo_symbol(symbol)).history(period="max", auto_adjust=True)
        except Exception:
            logger.exception("Failed to download %s", symbol)
            df = pd.DataFrame()
        if df.empty:
            logger.warning("No max history for %s", symbol)
            out[symbol] = df
            continue
        try:
            df.to_parquet(path)
        except Exception:
            logger.debug("Could not write max cache for %s", symbol, exc_info=True)
        out[symbol] = df
    return out
