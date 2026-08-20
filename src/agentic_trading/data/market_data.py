"""Price history and news fetching via yfinance.

yfinance needs no API key, which keeps the analysis half of the pipeline
runnable even before Alpaca/Anthropic credentials are configured. Alpaca is
used only for the paper account itself (positions, orders, equity).
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[3]
CACHE_DIR = ROOT / "data" / "cache"
CACHE_TTL_SECONDS = 15 * 60
ET = ZoneInfo("America/New_York")


@dataclass
class NewsItem:
    title: str
    publisher: str
    published: str


def yahoo_symbol(symbol: str) -> str:
    """Alpaca uses BRK.B; Yahoo uses BRK-B. Same for other dotted suffixes."""
    return symbol.replace(".", "-")


def cache_path(symbol: str) -> Path:
    safe = symbol.replace("/", "_").replace("^", "")
    if symbol.startswith("^"):
        safe = f"idx_{safe}"
    return CACHE_DIR / f"{safe}.parquet"


def slice_asof(df: pd.DataFrame, asof) -> pd.DataFrame:
    """Keep bars whose session date is <= asof. Empty/None asof is a no-op."""
    if df.empty or asof is None:
        return df
    asof_ts = pd.Timestamp(asof)
    idx = df.index
    if not isinstance(idx, pd.DatetimeIndex):
        return df
    if idx.tz is not None:
        if asof_ts.tzinfo is None:
            asof_ts = asof_ts.tz_localize(idx.tz)
        else:
            asof_ts = asof_ts.tz_convert(idx.tz)
        cutoff = asof_ts.normalize()
        return df.loc[idx.normalize() <= cutoff]
    asof_naive = asof_ts.tz_localize(None) if asof_ts.tzinfo is not None else asof_ts
    return df.loc[idx.tz_localize(None).normalize() <= asof_naive.normalize()]


def drop_forming_bar(df: pd.DataFrame, now: datetime | None = None) -> pd.DataFrame:
    """Drop today's still-open daily bar so 9:45 doesn't score on 15 minutes of tape.

    After 16:00 ET the regular session is over and today's bar is treated as complete.
    """
    if df.empty:
        return df
    now = now or datetime.now(ET)
    if now.tzinfo is None:
        now = now.replace(tzinfo=ET)
    else:
        now = now.astimezone(ET)
    if (now.hour, now.minute) >= (16, 0):
        return df
    last = pd.Timestamp(df.index[-1])
    if last.tzinfo is not None:
        last_date = last.tz_convert(ET).date()
    else:
        last_date = last.date()
    if last_date == now.date():
        return df.iloc[:-1]
    return df


def average_dollar_volume(df: pd.DataFrame, window: int = 20) -> float:
    if df.empty or "Volume" not in df.columns or "Close" not in df.columns:
        return 0.0
    tail = df.tail(window)
    return float((tail["Close"] * tail["Volume"]).mean())


def _read_cache(symbol: str) -> pd.DataFrame | None:
    path = cache_path(symbol)
    if not path.exists():
        return None
    age = time.time() - path.stat().st_mtime
    if age > CACHE_TTL_SECONDS:
        return None
    try:
        return pd.read_parquet(path)
    except Exception:
        logger.warning("Failed to read price cache for %s", symbol, exc_info=True)
        return None


def _write_cache(symbol: str, df: pd.DataFrame) -> None:
    if df.empty:
        return
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        df.to_parquet(cache_path(symbol))
    except Exception:
        logger.debug("Could not write price cache for %s", symbol, exc_info=True)


def fetch_price_history(
    symbol: str,
    period: str = "6mo",
    interval: str = "1d",
    *,
    use_cache: bool = True,
) -> pd.DataFrame:
    """OHLCV history for one symbol. Empty DataFrame on failure (never raises)."""
    if use_cache and interval == "1d":
        cached = _read_cache(symbol)
        if cached is not None and not cached.empty:
            return cached
    try:
        df = yf.Ticker(yahoo_symbol(symbol)).history(period=period, interval=interval, auto_adjust=True)
        if df.empty:
            logger.warning("No price history returned for %s", symbol)
            return df
        if use_cache and interval == "1d":
            _write_cache(symbol, df)
        return df
    except Exception:
        logger.exception("Failed to fetch price history for %s", symbol)
        return pd.DataFrame()


def fetch_recent_news(symbol: str, limit: int = 5) -> list[NewsItem]:
    """Most recent headlines for a symbol. Empty list on failure (never raises)."""
    try:
        raw = yf.Ticker(yahoo_symbol(symbol)).news or []
    except Exception:
        logger.exception("Failed to fetch news for %s", symbol)
        return []

    items: list[NewsItem] = []
    for entry in raw[:limit]:
        content = entry.get("content", entry)  # yfinance schema has shifted before
        title = content.get("title") or entry.get("title")
        if not title:
            continue
        publisher = (
            content.get("provider", {}).get("displayName")
            if isinstance(content.get("provider"), dict)
            else entry.get("publisher")
        ) or "unknown"
        published = content.get("pubDate") or entry.get("providerPublishTime") or ""
        items.append(NewsItem(title=title, publisher=str(publisher), published=str(published)))
    return items


def fetch_fundamentals_summary(symbol: str) -> dict:
    """A trimmed subset of yfinance's `info` blob — just enough context for the LLM."""
    keys = [
        "shortName", "sector", "industry", "marketCap", "trailingPE", "forwardPE",
        "priceToBook", "profitMargins", "revenueGrowth", "earningsGrowth",
        "debtToEquity", "returnOnEquity", "fiftyTwoWeekHigh", "fiftyTwoWeekLow",
        "recommendationKey", "targetMeanPrice",
    ]
    try:
        info = yf.Ticker(yahoo_symbol(symbol)).info or {}
    except Exception:
        logger.exception("Failed to fetch fundamentals for %s", symbol)
        return {}
    return {k: info.get(k) for k in keys if info.get(k) is not None}
