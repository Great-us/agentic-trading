"""Build and validate the free, approximate point-in-time S&P 500 bundle.

This module deliberately separates membership truth from price availability.
An incomplete price pull never shrinks the universe: the missing securities are
reported and the Stage-B readiness gate stays closed.

Usage examples::

    python -m agentic_trading.backtest.pit_data build
    python -m agentic_trading.backtest.pit_data prices --download --providers yahoo,alpaca,tiingo
    python -m agentic_trading.backtest.pit_data validate
    python -m agentic_trading.backtest.pit_data all --download
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import logging
import os
import subprocess
import sys
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import pandas as pd
import yfinance as yf
from dotenv import load_dotenv

from ..data.market_data import yahoo_symbol
from .data import MAX_CACHE_DIR, YFINANCE_CACHE_DIR, _max_path
from .universe import UniverseSchedule

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[3]
PIT_DIR = ROOT / "data" / "backtest" / "point_in_time"
RAW_DIR = PIT_DIR / "raw"
BARS_DIR = PIT_DIR / "bars"
PROVIDER_CACHE_DIR = PIT_DIR / "provider_cache"
MEMBERSHIP_PATH = PIT_DIR / "membership.csv"
INSTRUMENT_MASTER_PATH = PIT_DIR / "instrument_master.csv"
DELIST_PATH = PIT_DIR / "delist_events.csv"
CONFLICT_PATH = PIT_DIR / "source_conflicts.csv"
QUALITY_PATH = PIT_DIR / "data_quality.json"
QUALITY_REPORT_PATH = PIT_DIR / "STAGE-B-STATUS.md"
BUILD_MANIFEST_PATH = PIT_DIR / "build_manifest.json"
PRICE_GAPS_PATH = PIT_DIR / "price_gaps.csv"
PRICE_MANIFEST_PATH = PIT_DIR / "price_manifest.json"
ALPHA_LISTING_PATH = RAW_DIR / "alpha_vantage" / "listing_status.csv"

DEFAULT_START = "2007-04-11"
DEFAULT_END = "2026-08-21"
REQUIRED_OHLCV = ("Open", "High", "Low", "Close", "Volume")


@dataclass(frozen=True)
class GithubSource:
    source_id: str
    repository: str
    commit: str
    path: str
    license: str
    role: str


# Frozen commits make a rebuild deterministic.  Updating a source is an
# explicit code/config change rather than an invisible moving-branch read.
SOURCES: tuple[GithubSource, ...] = (
    GithubSource(
        "fja_intervals",
        "fja05680/sp500",
        "c31ac3cc56f28cf9a02b4e694eff7ceab596a0ff",
        "sp500_ticker_start_end.csv",
        "MIT",
        "primary membership intervals",
    ),
    GithubSource(
        "fja_current_metadata",
        "fja05680/sp500",
        "c31ac3cc56f28cf9a02b4e694eff7ceab596a0ff",
        "sp500.csv",
        "MIT",
        "current CIK and GICS metadata only",
    ),
    GithubSource(
        "joey_membership",
        "joeyfife/point-in-time-sp500",
        "212cf6354a0fad30965cad6047b335b935562cfc",
        "data/membership.json",
        "CC BY 4.0 (derived from Wikipedia CC BY-SA 4.0)",
        "independent membership cross-check; pre-2016 is unvalidated by its author",
    ),
    GithubSource(
        "royelee_dlret",
        "royelee/delist-detection",
        "b60aa8765ab88e6d85a3fd291cc427d9efd5158d",
        "output/dlret.csv",
        "MIT",
        "SEC-EDGAR-derived terminal delisting returns",
    ),
)


def _source_path(source: GithubSource) -> Path:
    safe_repo = source.repository.replace("/", "__")
    return RAW_DIR / safe_repo / source.commit / Path(source.path).name


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _github_payload(source: GithubSource) -> bytes:
    escaped = "/".join(urllib.parse.quote(part, safe="") for part in source.path.split("/"))
    endpoint = f"repos/{source.repository}/contents/{escaped}?ref={source.commit}"
    # gh is preferred here because it applies the user's authenticated GitHub
    # transport and handles repository API behavior consistently.
    try:
        completed = subprocess.run(
            [
                "gh", "api", endpoint,
                "-H", "Accept: application/vnd.github.raw+json",
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        return completed.stdout
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        logger.warning("gh download failed for %s; trying pinned raw URL: %s", source.source_id, exc)
    raw_path = urllib.parse.quote(source.path, safe="/")
    url = f"https://raw.githubusercontent.com/{source.repository}/{source.commit}/{raw_path}"
    request = urllib.request.Request(url, headers={"User-Agent": "agentic-trading-pit-builder/1.0"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read()


def materialize_sources(*, refresh: bool = False) -> dict[str, dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    for source in SOURCES:
        target = _source_path(source)
        if refresh or not target.exists():
            payload = _github_payload(source)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
        records[source.source_id] = {
            **asdict(source),
            "local_path": str(target.relative_to(ROOT)),
            "sha256": _sha256_file(target),
            "bytes": target.stat().st_size,
            "source_url": f"https://github.com/{source.repository}/blob/{source.commit}/{source.path}",
        }
    return records


def _read_source_csv(source_id: str, **kwargs) -> pd.DataFrame:
    source = next(item for item in SOURCES if item.source_id == source_id)
    return pd.read_csv(_source_path(source), **kwargs)


def _clean_ticker(value: Any) -> str:
    return str(value).strip().upper()


def _asset_id(ticker: str, start: str) -> str:
    safe = "".join(char if char.isalnum() else "_" for char in ticker.upper()).strip("_")
    suffix = hashlib.sha256(f"{ticker}|{start}".encode("utf-8")).hexdigest()[:8].upper()
    return f"SP500__{safe}__{start.replace('-', '')}__{suffix}"


def _current_metadata() -> dict[str, dict[str, Any]]:
    frame = _read_source_csv("fja_current_metadata", dtype={"Symbol": str, "CIK": str})
    out: dict[str, dict[str, Any]] = {}
    for _, row in frame.iterrows():
        ticker = _clean_ticker(row.get("Symbol", ""))
        if not ticker:
            continue
        cik_raw = str(row.get("CIK", "")).split(".")[0].strip()
        cik = cik_raw.zfill(10) if cik_raw and cik_raw.lower() != "nan" else ""
        out[ticker] = {
            "cik": cik,
            "security_name": str(row.get("Security", "")).strip(),
            "sector": str(row.get("GICS Sector", "")).strip(),
            "sub_industry": str(row.get("GICS Sub-Industry", "")).strip(),
        }
    return out


def _load_sector_overrides() -> dict[str, str]:
    path = PIT_DIR / "sector_overrides.csv"
    if not path.exists():
        return {}
    frame = pd.read_csv(path, dtype=str)
    if not {"ticker", "sector"} <= set(frame.columns):
        raise ValueError(f"{path} must contain ticker,sector")
    return {
        _clean_ticker(row["ticker"]): str(row["sector"]).strip()
        for _, row in frame.iterrows() if str(row["sector"]).strip()
    }


def _primary_membership(start: str, end: str) -> pd.DataFrame:
    raw = _read_source_csv("fja_intervals", dtype=str)
    raw = raw.rename(columns={"ticker": "ticker"})
    metadata = _current_metadata()
    sector_overrides = _load_sector_overrides()
    start_ts, end_ts = pd.Timestamp(start), pd.Timestamp(end)
    rows: list[dict[str, Any]] = []
    source = next(item for item in SOURCES if item.source_id == "fja_intervals")
    source_ref = f"{source.repository}@{source.commit}:{source.path}"
    for _, row in raw.iterrows():
        ticker = _clean_ticker(row.get("ticker", ""))
        valid_from = pd.to_datetime(row.get("start_date"), errors="coerce")
        valid_to = pd.to_datetime(row.get("end_date"), errors="coerce")
        if not ticker or pd.isna(valid_from):
            continue
        if valid_from > end_ts or (not pd.isna(valid_to) and valid_to < start_ts):
            continue
        start_value = valid_from.date().isoformat()
        end_value = "" if pd.isna(valid_to) else valid_to.date().isoformat()
        meta = metadata.get(ticker, {})
        cik = str(meta.get("cik", ""))
        sector = sector_overrides.get(ticker) or str(meta.get("sector", "")).strip() or "Unknown"
        instrument_id = _asset_id(ticker, start_value)
        rows.append({
            "symbol": instrument_id,
            "instrument_id": instrument_id,
            "ticker": ticker,
            "start_date": start_value,
            "end_date": end_value,
            "sector": sector,
            "source_id": source_ref,
            "cik": cik,
            "security_name": str(meta.get("security_name", "")),
            "exchange": "",
            "security_type": "Common Stock / Share Class",
            "identity_status": "membership_episode_with_current_cik" if cik else "membership_episode",
        })
    frame = pd.DataFrame(rows).sort_values(["ticker", "start_date"]).reset_index(drop=True)
    if frame.empty:
        raise ValueError("Primary source produced no membership intervals")
    if frame["instrument_id"].duplicated().any():
        raise ValueError("Generated instrument IDs are not unique")
    return frame


def _secondary_members_asof(payload: dict[str, Any], value: pd.Timestamp) -> set[str]:
    members = {_clean_ticker(item) for item in payload["current"] if str(item).strip()}
    target = value.normalize()
    for event in payload["changes"]:
        event_date = pd.Timestamp(event["date"])
        if event_date <= target:
            continue
        added = _clean_ticker(event.get("added", ""))
        removed = _clean_ticker(event.get("removed", ""))
        if added:
            members.discard(added)
        if removed:
            members.add(removed)
    return members


def _membership_conflicts(
    membership: pd.DataFrame,
    start: str,
    end: str,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    source = next(item for item in SOURCES if item.source_id == "joey_membership")
    payload = json.loads(_source_path(source).read_text(encoding="utf-8"))
    intervals = [
        (
            row.ticker,
            pd.Timestamp(row.start_date),
            pd.Timestamp(end) if pd.isna(row.end_date) or not str(row.end_date).strip() else pd.Timestamp(row.end_date),
        )
        for row in membership.itertuples()
    ]
    rows: list[dict[str, Any]] = []
    primary_days = 0
    conflict_days = 0
    absolute_count_difference = 0
    validated_primary_days = 0
    validated_conflict_days = 0
    validated_absolute_count_difference = 0
    dates = pd.bdate_range(start, end)
    for day in dates:
        primary = {ticker for ticker, first, last in intervals if first <= day <= last}
        secondary = _secondary_members_asof(payload, day)
        only_primary = sorted(primary - secondary)
        only_secondary = sorted(secondary - primary)
        difference = len(only_primary) + len(only_secondary)
        primary_days += len(primary)
        conflict_days += difference
        absolute_count_difference += abs(len(primary) - len(secondary))
        if day >= pd.Timestamp("2016-01-01"):
            validated_primary_days += len(primary)
            validated_conflict_days += difference
            validated_absolute_count_difference += abs(len(primary) - len(secondary))
        if difference:
            rows.append({
                "date": day.date().isoformat(),
                "primary_count": len(primary),
                "secondary_count": len(secondary),
                "only_primary": ",".join(only_primary),
                "only_secondary": ",".join(only_secondary),
                "difference_count": difference,
                "secondary_validation_era": "validated" if day >= pd.Timestamp("2016-01-01") else "unvalidated",
            })
    summary = {
        "business_days_compared": len(dates),
        "all_period_member_day_disagreement": conflict_days / primary_days if primary_days else None,
        "post_2016_member_day_disagreement": (
            validated_conflict_days / validated_primary_days if validated_primary_days else None
        ),
        "all_period_member_count_disagreement": (
            absolute_count_difference / primary_days if primary_days else None
        ),
        "post_2016_member_count_disagreement": (
            validated_absolute_count_difference / validated_primary_days
            if validated_primary_days else None
        ),
        "ticker_label_caveat": (
            "The secondary source retroactively uses many current ticker names. Symbol-level symmetric "
            "differences therefore combine real constituent conflicts with ticker-renaming differences; "
            "the strict cross-source gate uses absolute member-count disagreement and both measures are retained."
        ),
        "secondary_caveat": "The secondary source explicitly says pre-2016 events were not price-validated.",
    }
    return pd.DataFrame(rows), summary


def _delist_events(start: str, end: str) -> pd.DataFrame:
    frame = _read_source_csv("royelee_dlret", dtype={"ticker": str})
    frame["ticker"] = frame["ticker"].map(_clean_ticker)
    frame["observed_delist_date"] = pd.to_datetime(frame["observed_delist_date"], errors="coerce")
    # A pre-start event can never terminate an episode in the requested test.
    frame = frame.loc[
        frame["observed_delist_date"].notna()
        & (frame["observed_delist_date"] >= pd.Timestamp(start) - pd.Timedelta(days=550))
        & (frame["observed_delist_date"] <= pd.Timestamp(end))
    ].copy()
    frame["observed_delist_date"] = frame["observed_delist_date"].dt.date.astype(str)
    source = next(item for item in SOURCES if item.source_id == "royelee_dlret")
    frame["source_id"] = f"{source.repository}@{source.commit}:{source.path}"
    return frame.sort_values(["ticker", "observed_delist_date"]).reset_index(drop=True)


def build_open_data(
    *,
    start: str = DEFAULT_START,
    end: str = DEFAULT_END,
    refresh_sources: bool = False,
) -> dict[str, Any]:
    PIT_DIR.mkdir(parents=True, exist_ok=True)
    source_records = materialize_sources(refresh=refresh_sources)
    membership = _primary_membership(start, end)
    conflicts, conflict_summary = _membership_conflicts(membership, start, end)
    delist = _delist_events(start, end)

    membership.to_csv(MEMBERSHIP_PATH, index=False)
    membership.rename(columns={"start_date": "valid_from", "end_date": "valid_to"}).to_csv(
        INSTRUMENT_MASTER_PATH, index=False,
    )
    delist.to_csv(DELIST_PATH, index=False)
    conflicts.to_csv(CONFLICT_PATH, index=False)
    manifest = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "requested_start": start,
        "requested_end": end,
        "method": "licensed free approximation; primary intervals plus independent cross-check",
        "identity_model": (
            "A stable membership-episode instrument_id isolates ticker reuse. Current CIKs are auxiliary; "
            "the free sources do not provide a complete historical permanent-identifier master."
        ),
        "sources": source_records,
        "membership_rows": len(membership),
        "unique_tickers": int(membership["ticker"].nunique()),
        "unique_instruments": int(membership["instrument_id"].nunique()),
        "membership_conflicts": conflict_summary,
        "delist_events": len(delist),
        "generated_files": {},
    }
    for path in (MEMBERSHIP_PATH, INSTRUMENT_MASTER_PATH, DELIST_PATH, CONFLICT_PATH):
        manifest["generated_files"][str(path.relative_to(ROOT))] = {
            "sha256": _sha256_file(path), "bytes": path.stat().st_size,
        }
    BUILD_MANIFEST_PATH.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


def _normalise_prices(frame: pd.DataFrame) -> pd.DataFrame:
    if frame is None or frame.empty:
        return pd.DataFrame(columns=REQUIRED_OHLCV)
    work = frame.copy()
    inputs = {
        "Open": ("adjOpen", "open"),
        "High": ("adjHigh", "high"),
        "Low": ("adjLow", "low"),
        "Close": ("adjClose", "close"),
        "Volume": ("adjVolume", "volume"),
    }
    for canonical, candidates in inputs.items():
        if canonical in work.columns:
            continue
        for candidate in candidates:
            if candidate in work.columns:
                work[canonical] = work[candidate]
                break
    if "date" in work.columns:
        work.index = pd.to_datetime(work.pop("date"), utc=True, errors="coerce")
    index = pd.DatetimeIndex(work.index)
    if index.tz is not None:
        index = index.tz_localize(None)
    work.index = index.normalize()
    work = work.loc[~work.index.isna()]
    work = work[~work.index.duplicated(keep="last")].sort_index()
    for column in REQUIRED_OHLCV:
        if column not in work:
            work[column] = 0.0 if column == "Volume" else pd.NA
        work[column] = pd.to_numeric(work[column], errors="coerce")
    prior_repairs = (
        work["OHLCEnvelopeRepaired"].fillna(False).astype(bool)
        if "OHLCEnvelopeRepaired" in work.columns
        else pd.Series(False, index=work.index)
    )
    complete = work[["Open", "High", "Low", "Close"]].notna().all(axis=1)
    bad_high = complete & (work["High"] < work[["Open", "Close"]].max(axis=1))
    bad_low = complete & (work["Low"] > work[["Open", "Close"]].min(axis=1))
    work.loc[bad_high, "High"] = work.loc[bad_high, ["Open", "High", "Close"]].max(axis=1)
    work.loc[bad_low, "Low"] = work.loc[bad_low, ["Open", "Low", "Close"]].min(axis=1)
    work["OHLCEnvelopeRepaired"] = prior_repairs | bad_high | bad_low
    return work[list(REQUIRED_OHLCV) + [
        column for column in work.columns if column not in REQUIRED_OHLCV
    ]]


def _yahoo_prices(ticker: str, *, download: bool) -> tuple[pd.DataFrame, str | None]:
    path = _max_path(ticker)
    if path.exists():
        try:
            return _normalise_prices(pd.read_parquet(path)), "yahoo_cache"
        except Exception:
            logger.warning("Unreadable Yahoo cache for %s", ticker)
    if not download:
        return pd.DataFrame(), None
    MAX_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    YFINANCE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    yf.set_tz_cache_location(str(YFINANCE_CACHE_DIR))
    try:
        frame = yf.Ticker(yahoo_symbol(ticker)).history(period="max", auto_adjust=True, actions=False)
    except Exception:
        logger.exception("Yahoo download failed for %s", ticker)
        return pd.DataFrame(), None
    frame = _normalise_prices(frame)
    if not frame.empty:
        frame.to_parquet(path)
        return frame, "yahoo"
    return frame, None


def _download_yahoo_batches(tickers: list[str], *, batch_size: int = 50) -> set[str]:
    """Populate the ordinary max-history cache with bounded Yahoo batch calls."""
    if not tickers:
        return set()
    MAX_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    YFINANCE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    yf.set_tz_cache_location(str(YFINANCE_CACHE_DIR))
    downloaded: set[str] = set()
    for offset in range(0, len(tickers), batch_size):
        batch = tickers[offset:offset + batch_size]
        yahoo_to_internal = {yahoo_symbol(ticker): ticker for ticker in batch}
        logger.info(
            "Yahoo price batch %d-%d of %d",
            offset + 1, min(offset + len(batch), len(tickers)), len(tickers),
        )
        try:
            raw = yf.download(
                list(yahoo_to_internal),
                period="max",
                auto_adjust=True,
                actions=False,
                group_by="ticker",
                threads=True,
                progress=False,
            )
        except Exception:
            logger.exception("Yahoo batch failed")
            continue
        for yahoo_name, ticker in yahoo_to_internal.items():
            try:
                if isinstance(raw.columns, pd.MultiIndex):
                    if yahoo_name not in raw.columns.get_level_values(0):
                        continue
                    frame = raw[yahoo_name].dropna(how="all")
                elif len(batch) == 1:
                    frame = raw.dropna(how="all")
                else:
                    continue
                frame = _normalise_prices(frame)
                if frame.empty:
                    continue
                frame.to_parquet(_max_path(ticker))
                downloaded.add(ticker)
            except Exception:
                logger.warning("Could not extract Yahoo batch result for %s", ticker, exc_info=True)
    return downloaded


def _tiingo_cache_path(ticker: str) -> Path:
    safe = "".join(char if char.isalnum() else "_" for char in ticker.upper())
    return PROVIDER_CACHE_DIR / "tiingo" / f"{safe}.parquet"


def _alpaca_cache_path(ticker: str) -> Path:
    safe = "".join(char if char.isalnum() else "_" for char in ticker.upper())
    return PROVIDER_CACHE_DIR / "alpaca" / f"{safe}.parquet"


def _alpaca_prices(
    ticker: str,
    start: str,
    end: str,
    api_key: str | None,
    secret_key: str | None,
    *,
    client: Any | None = None,
) -> pd.DataFrame:
    """Read or download adjusted SIP bars without exposing credentials.

    The cache is consulted before credentials, so a frozen PIT bundle remains
    reproducible after an API key is removed. One symbol is requested at a
    time so a bad legacy ticker cannot invalidate a batch of delisted names.
    """
    path = _alpaca_cache_path(ticker)
    if path.exists():
        try:
            return _normalise_prices(pd.read_parquet(path))
        except Exception:
            logger.warning("Unreadable Alpaca cache for %s", ticker)
    if not api_key or not secret_key:
        return pd.DataFrame()

    from alpaca.data.enums import Adjustment, DataFeed
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    active_client = client or StockHistoricalDataClient(api_key, secret_key)
    try:
        response = active_client.get_stock_bars(StockBarsRequest(
            symbol_or_symbols=ticker,
            timeframe=TimeFrame.Day,
            start=pd.Timestamp(start, tz="UTC").to_pydatetime(),
            # Alpaca's end boundary is exclusive; advance one day so the
            # bundle's configured final session remains eligible.
            end=(pd.Timestamp(end, tz="UTC") + pd.Timedelta(days=1)).to_pydatetime(),
            adjustment=Adjustment.ALL,
            feed=DataFeed.SIP,
        ))
        frame = getattr(response, "df", None)
        if frame is None or frame.empty:
            return pd.DataFrame()
        if isinstance(frame.index, pd.MultiIndex):
            try:
                frame = frame.xs(ticker, level="symbol")
            except KeyError:
                return pd.DataFrame()
        frame = _normalise_prices(frame)
    except Exception as exc:
        logger.warning("Alpaca download failed for %s: %s", ticker, exc)
        return pd.DataFrame()
    if not frame.empty:
        path.parent.mkdir(parents=True, exist_ok=True)
        frame.to_parquet(path)
    return frame


def _tiingo_prices(ticker: str, start: str, end: str, api_key: str | None) -> pd.DataFrame:
    path = _tiingo_cache_path(ticker)
    if path.exists():
        try:
            return _normalise_prices(pd.read_parquet(path))
        except Exception:
            logger.warning("Unreadable Tiingo cache for %s", ticker)
    if not api_key:
        return pd.DataFrame()
    query = urllib.parse.urlencode({"startDate": start, "endDate": end, "resampleFreq": "daily"})
    url = f"https://api.tiingo.com/tiingo/daily/{urllib.parse.quote(ticker)}/prices?{query}"
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Token {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "agentic-trading-pit-builder/1.0",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception:
        logger.exception("Tiingo download failed for %s", ticker)
        return pd.DataFrame()
    frame = _normalise_prices(pd.DataFrame(payload))
    if not frame.empty:
        path.parent.mkdir(parents=True, exist_ok=True)
        frame.to_parquet(path)
    return frame


def fetch_alpha_listing_status(api_key: str | None = None) -> dict[str, Any]:
    """Fetch Alpha Vantage active + delisted listing status in two bulk calls."""
    key = api_key or os.getenv("ALPHAVANTAGE_API_KEY") or os.getenv("ALPHA_VANTAGE_API_KEY")
    if not key:
        return {"status": "missing_api_key", "environment_names": ["ALPHAVANTAGE_API_KEY", "ALPHA_VANTAGE_API_KEY"]}
    frames: list[pd.DataFrame] = []
    for state in ("active", "delisted"):
        query = urllib.parse.urlencode({"function": "LISTING_STATUS", "state": state, "apikey": key})
        request = urllib.request.Request(
            f"https://www.alphavantage.co/query?{query}",
            headers={"User-Agent": "agentic-trading-pit-builder/1.0"},
        )
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = response.read()
        frame = pd.read_csv(io.BytesIO(payload), dtype=str)
        if "symbol" not in frame.columns:
            raise RuntimeError(f"Alpha Vantage LISTING_STATUS returned an unexpected {state} response")
        frame["requested_state"] = state
        frames.append(frame)
    combined = pd.concat(frames, ignore_index=True)
    ALPHA_LISTING_PATH.parent.mkdir(parents=True, exist_ok=True)
    combined.to_csv(ALPHA_LISTING_PATH, index=False)
    matches = 0
    if INSTRUMENT_MASTER_PATH.exists():
        master = pd.read_csv(INSTRUMENT_MASTER_PATH, dtype=str).fillna("")
        by_ticker = {
            _clean_ticker(ticker): group.copy()
            for ticker, group in combined.groupby("symbol")
        }
        master["alpha_listing_match"] = False
        for offset, row in master.iterrows():
            candidates = by_ticker.get(_clean_ticker(row["ticker"]))
            if candidates is None or candidates.empty:
                continue
            valid_from = pd.Timestamp(row["valid_from"])
            valid_to = pd.Timestamp(row["valid_to"]) if row["valid_to"] else pd.Timestamp.max.normalize()
            ipo = pd.to_datetime(candidates.get("ipoDate"), errors="coerce")
            delisted = pd.to_datetime(candidates.get("delistingDate"), errors="coerce")
            eligible = candidates.loc[
                (ipo.isna() | (ipo <= valid_to))
                & (delisted.isna() | (delisted >= valid_from))
            ]
            if eligible.empty:
                continue
            selected = eligible.iloc[-1]
            master.at[offset, "exchange"] = str(selected.get("exchange", ""))
            master.at[offset, "security_type"] = str(selected.get("assetType", row["security_type"]))
            master.at[offset, "alpha_listing_match"] = True
            matches += 1
        master.to_csv(INSTRUMENT_MASTER_PATH, index=False)
    return {
        "status": "downloaded",
        "rows": len(combined),
        "instrument_matches": matches,
        "sha256": _sha256_file(ALPHA_LISTING_PATH),
    }


def _attach_delisting_return(
    frame: pd.DataFrame,
    ticker: str,
    first: pd.Timestamp,
    last: pd.Timestamp,
    delist: pd.DataFrame,
) -> tuple[pd.DataFrame, bool]:
    candidates = delist.loc[
        (delist["ticker"] == ticker)
        & (pd.to_datetime(delist["observed_delist_date"]) >= first)
        & (pd.to_datetime(delist["observed_delist_date"]) <= last)
    ]
    if candidates.empty or frame.empty:
        return frame, False
    event = candidates.sort_values("observed_delist_date").iloc[0]
    event_day = pd.offsets.BDay().rollforward(pd.Timestamp(event["observed_delist_date"])).normalize()
    prior = frame.loc[frame.index < event_day, "Close"].dropna()
    if prior.empty:
        return frame, False
    dlret = float(event["dlret"])
    terminal = max(0.0, float(prior.iloc[-1]) * (1.0 + dlret))
    if event_day not in frame.index:
        frame.loc[event_day, list(REQUIRED_OHLCV)] = [terminal, terminal, terminal, terminal, 0.0]
    frame.loc[event_day, "DelistingReturn"] = dlret
    frame.loc[event_day, "DelistingConfidence"] = str(event.get("dlret_confidence", ""))
    frame.loc[event_day, "DelistingMethod"] = str(event.get("dlret_method", ""))
    return frame.sort_index(), True


def populate_prices(
    *,
    start: str = DEFAULT_START,
    end: str = DEFAULT_END,
    providers: Iterable[str] = ("yahoo", "alpaca", "tiingo"),
    download: bool = False,
    max_downloads: int | None = None,
    alpaca_limit: int = 1_000,
    tiingo_limit: int = 500,
) -> dict[str, Any]:
    if not MEMBERSHIP_PATH.exists():
        build_open_data(start=start, end=end)
    membership = pd.read_csv(MEMBERSHIP_PATH, dtype=str).fillna("")
    delist = pd.read_csv(DELIST_PATH, dtype={"ticker": str}) if DELIST_PATH.exists() else pd.DataFrame()
    providers = tuple(str(item).strip().lower() for item in providers if str(item).strip())
    unknown = set(providers) - {"yahoo", "alpaca", "tiingo"}
    if unknown:
        raise ValueError(f"Unknown price providers: {', '.join(sorted(unknown))}")
    tiingo_key = os.getenv("TIINGO_API_KEY")
    alpaca_key = os.getenv("ALPACA_API_KEY")
    alpaca_secret = os.getenv("ALPACA_SECRET_KEY")
    BARS_DIR.mkdir(parents=True, exist_ok=True)

    by_ticker = membership.groupby("ticker", sort=True)
    unique_tickers = list(by_ticker.groups)
    # Retry only tickers with at least one missing membership episode. Existing
    # episode files remain untouched unless another episode of that ticker must
    # be reconstructed from a fuller provider history.
    needed_tickers = {
        ticker
        for ticker, group in by_ticker
        if any(
            not (BARS_DIR / f"{instrument_id}.parquet").exists()
            for instrument_id in group["instrument_id"]
        )
    }
    yahoo_downloaded: set[str] = set()
    if download and "yahoo" in providers:
        missing_yahoo = [
            ticker for ticker in unique_tickers
            if ticker in needed_tickers and not _max_path(ticker).exists()
        ]
        if max_downloads is not None:
            missing_yahoo = missing_yahoo[:max_downloads]
        yahoo_downloaded = _download_yahoo_batches(missing_yahoo)
    downloads = len(yahoo_downloaded)
    alpaca_calls = 0
    tiingo_calls = 0
    alpaca_client = None
    if download and alpaca_key and alpaca_secret and "alpaca" in providers:
        from alpaca.data.historical import StockHistoricalDataClient

        alpaca_client = StockHistoricalDataClient(alpaca_key, alpaca_secret)
    ticker_frames: dict[str, tuple[pd.DataFrame, str | None]] = {}
    provider_counts: dict[str, int] = {}
    for ticker, _ in by_ticker:
        if ticker not in needed_tickers:
            ticker_frames[ticker] = (pd.DataFrame(), None)
            continue
        allow_download = download and (max_downloads is None or downloads < max_downloads)
        frame = pd.DataFrame()
        provider: str | None = None
        if "yahoo" in providers:
            frame, provider = _yahoo_prices(ticker, download=False)
            if ticker in yahoo_downloaded:
                provider = "yahoo"
        if frame.empty and "alpaca" in providers:
            alpaca_path = _alpaca_cache_path(ticker)
            can_call = bool(
                alpaca_key and alpaca_secret and allow_download
                and alpaca_calls < alpaca_limit and not alpaca_path.exists()
            )
            frame = _alpaca_prices(
                ticker,
                (pd.Timestamp(start) - pd.Timedelta(days=550)).date().isoformat(),
                end,
                alpaca_key if can_call else None,
                alpaca_secret if can_call else None,
                client=alpaca_client if can_call else None,
            )
            if can_call:
                alpaca_calls += 1
                downloads += 1
                if alpaca_calls % 20 == 0:
                    logger.info("Alpaca price requests: %d", alpaca_calls)
            provider = "alpaca" if not frame.empty else None
        if frame.empty and "tiingo" in providers:
            can_call = bool(
                tiingo_key and allow_download and tiingo_calls < tiingo_limit
                and not _tiingo_cache_path(ticker).exists()
            )
            frame = _tiingo_prices(
                ticker,
                (pd.Timestamp(start) - pd.Timedelta(days=550)).date().isoformat(),
                end,
                tiingo_key if can_call else None,
            )
            if can_call:
                tiingo_calls += 1
                downloads += 1
            provider = "tiingo" if not frame.empty else None
        ticker_frames[ticker] = (frame, provider)
        if provider:
            provider_counts[provider] = provider_counts.get(provider, 0) + 1

    written = 0
    attached_delists = 0
    for ticker, group in by_ticker:
        full, provider = ticker_frames[ticker]
        ordered = group.sort_values("start_date").reset_index(drop=True)
        for offset, row in ordered.iterrows():
            target = BARS_DIR / f"{row['instrument_id']}.parquet"
            first = max(pd.Timestamp(start) - pd.Timedelta(days=550), pd.Timestamp(row["start_date"]) - pd.Timedelta(days=550))
            if offset > 0:
                prior_end = pd.to_datetime(ordered.iloc[offset - 1]["end_date"], errors="coerce")
                if not pd.isna(prior_end):
                    first = max(first, prior_end + pd.Timedelta(days=1))
            last = pd.Timestamp(end)
            if offset + 1 < len(ordered):
                last = min(last, pd.Timestamp(ordered.iloc[offset + 1]["start_date"]) - pd.Timedelta(days=1))
            episode = full.loc[(full.index >= first) & (full.index <= last)].copy() if not full.empty else pd.DataFrame()
            if not episode.empty and not delist.empty:
                episode, attached = _attach_delisting_return(episode, ticker, first, last, delist)
                attached_delists += int(attached)
            if episode.empty:
                written += int(target.exists())
                continue
            episode["Ticker"] = ticker
            episode["InstrumentID"] = row["instrument_id"]
            episode["PriceSource"] = provider or "unknown_cache"
            episode.to_parquet(target)
            written += 1
    result = {
        "status": "complete" if written == len(membership) else "incomplete",
        "membership_rows": len(membership),
        "price_files_written": written,
        "downloaded_tickers": downloads,
        "alpaca_calls": alpaca_calls,
        "alpaca_credentials_present": bool(alpaca_key and alpaca_secret),
        "tiingo_calls": tiingo_calls,
        "tiingo_key_present": bool(tiingo_key),
        "provider_counts": provider_counts,
        "attached_delisting_events": attached_delists,
    }
    missing_rows = membership.loc[
        ~membership["instrument_id"].map(lambda value: (BARS_DIR / f"{value}.parquet").exists())
    ].copy()
    if not missing_rows.empty:
        missing_rows["reason"] = "unavailable_from_configured_free_price_providers"
        missing_rows["tiingo_attempted"] = bool(tiingo_key and "tiingo" in providers and download)
    missing_rows.to_csv(PRICE_GAPS_PATH, index=False)
    file_records: dict[str, Any] = {}
    ticker_by_id = dict(zip(membership["instrument_id"], membership["ticker"]))
    for path in sorted(BARS_DIR.glob("*.parquet")):
        instrument_id = path.stem
        file_records[instrument_id] = {
            "ticker": ticker_by_id.get(instrument_id, ""),
            "path": str(path.relative_to(ROOT)),
            "sha256": _sha256_file(path),
            "bytes": path.stat().st_size,
        }
    price_manifest = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "providers_requested": list(providers),
        "download_enabled": download,
        "files": file_records,
        "missing_episode_count": len(missing_rows),
    }
    PRICE_MANIFEST_PATH.write_text(json.dumps(price_manifest, indent=2), encoding="utf-8")
    result["price_manifest"] = str(PRICE_MANIFEST_PATH.relative_to(ROOT))
    return result


def _reference_dates(start: str, end: str) -> pd.DatetimeIndex:
    spy_path = _max_path("SPY")
    if spy_path.exists():
        try:
            spy = _normalise_prices(pd.read_parquet(spy_path))
            dates = spy.index[(spy.index >= pd.Timestamp(start)) & (spy.index <= pd.Timestamp(end))]
            if len(dates):
                return pd.DatetimeIndex(dates)
        except Exception:
            logger.warning("Could not use cached SPY calendar", exc_info=True)
    return pd.bdate_range(start, end)


def validate_bundle(*, start: str = DEFAULT_START, end: str = DEFAULT_END) -> dict[str, Any]:
    if not MEMBERSHIP_PATH.exists():
        result = {"status": "missing_membership", "ready": False, "missing": [str(MEMBERSHIP_PATH)]}
        QUALITY_PATH.parent.mkdir(parents=True, exist_ok=True)
        QUALITY_PATH.write_text(json.dumps(result, indent=2), encoding="utf-8")
        return result
    schedule = UniverseSchedule.from_csv(MEMBERSHIP_PATH)
    dates = _reference_dates(start, end)
    counts = pd.Series([len(schedule.active_on(day)) for day in dates], index=dates)
    count_gate = bool(not counts.empty and counts.between(490, 515).all())

    missing_files: list[str] = []
    invalid_files: dict[str, list[str]] = {}
    bars_by_symbol: dict[str, pd.DataFrame] = {}
    # Price-quality anomaly counters feeding the gates below. Envelope checks
    # catch structurally impossible bars; these catch *plausible-looking* bad
    # data — a 10x print, dead volume, or a frozen quote — that would otherwise
    # flow straight into signals and fills.
    checked_rows = 0
    spike_rows = 0
    zero_volume_rows = 0
    volume_rows = 0
    stale_rows = 0
    worst_spikes: list[tuple[float, str, str]] = []
    for symbol in schedule.symbols:
        path = BARS_DIR / f"{symbol}.parquet"
        if not path.exists():
            missing_files.append(symbol)
            continue
        try:
            frame = _normalise_prices(pd.read_parquet(path))
        except Exception as exc:
            invalid_files[symbol] = [f"read error: {exc}"]
            continue
        problems: list[str] = []
        missing_columns = [column for column in REQUIRED_OHLCV if column not in frame.columns]
        if missing_columns:
            problems.append(f"missing columns: {','.join(missing_columns)}")
        normal = frame.loc[frame.get("DelistingReturn", pd.Series(index=frame.index, dtype=float)).isna()]
        if not normal.empty:
            if normal[list(REQUIRED_OHLCV[:-1])].isna().any().any():
                problems.append("missing OHLC values")
            scale = normal[["Open", "High", "Low", "Close"]].abs().max(axis=1).clip(lower=1.0)
            tolerance = scale * 1e-10
            if ((normal["High"] + tolerance < normal[["Open", "Close"]].max(axis=1))
                    | (normal["Low"] - tolerance > normal[["Open", "Close"]].min(axis=1))).any():
                problems.append("invalid high/low envelope")
            closes = normal["Close"].astype(float)
            if len(closes) >= 2:
                rets = closes.pct_change().abs()
                spikes = rets[rets > 0.40]
                spike_rows += len(spikes)
                worst_spikes.extend(
                    (float(value), symbol, str(index.date()))
                    for index, value in spikes.items()
                )
                unchanged = closes.diff().eq(0.0)
                run = 0
                for flag in unchanged.tolist():
                    run = run + 1 if flag else 0
                    if run >= 5:
                        stale_rows += 1
                checked_rows += len(closes) - 1  # pct_change/diff drop the first row
            if "Volume" in normal.columns:
                volume = pd.to_numeric(normal["Volume"], errors="coerce").fillna(0.0)
                zero_volume_rows += int((volume <= 0).sum())
                volume_rows += len(normal)
        if problems:
            invalid_files[symbol] = problems
        bars_by_symbol[symbol] = frame

    active_member_sessions = 0
    covered_member_sessions = 0
    known_sector_member_sessions = 0
    terminal_rows = 0
    repair_rows = 0
    total_price_rows = 0
    yearly_expected: dict[int, int] = {}
    yearly_covered: dict[int, int] = {}
    for interval in schedule.intervals:
        last = pd.Timestamp(end) if interval.end_date is None else min(pd.Timestamp(end), pd.Timestamp(interval.end_date))
        first = max(pd.Timestamp(start), pd.Timestamp(interval.start_date))
        active_dates = dates[(dates >= first) & (dates <= last)]
        active_member_sessions += len(active_dates)
        for year, count in pd.Series(active_dates.year).value_counts().items():
            yearly_expected[int(year)] = yearly_expected.get(int(year), 0) + int(count)
        if interval.sector and interval.sector != "Unknown":
            known_sector_member_sessions += len(active_dates)
        frame = bars_by_symbol.get(interval.symbol)
        if frame is not None:
            covered_dates = active_dates.intersection(frame.index)
            covered_member_sessions += len(covered_dates)
            for year, count in pd.Series(covered_dates.year).value_counts().items():
                yearly_covered[int(year)] = yearly_covered.get(int(year), 0) + int(count)
            total_price_rows += len(frame)
            if "OHLCEnvelopeRepaired" in frame:
                repair_rows += int(frame["OHLCEnvelopeRepaired"].fillna(False).astype(bool).sum())
            if "DelistingReturn" in frame:
                terminal_rows += int(frame["DelistingReturn"].notna().sum())
    coverage = covered_member_sessions / active_member_sessions if active_member_sessions else 0.0
    sector_coverage = known_sector_member_sessions / active_member_sessions if active_member_sessions else 0.0
    repair_rate = repair_rows / total_price_rows if total_price_rows else 0.0
    spike_rate = spike_rows / checked_rows if checked_rows else 0.0
    zero_volume_rate = zero_volume_rows / volume_rows if volume_rows else 0.0
    stale_rate = stale_rows / checked_rows if checked_rows else 0.0

    conflict_summary: dict[str, Any] = {}
    if BUILD_MANIFEST_PATH.exists():
        conflict_summary = json.loads(BUILD_MANIFEST_PATH.read_text(encoding="utf-8")).get("membership_conflicts", {})
    post_2016_conflict = conflict_summary.get("post_2016_member_count_disagreement")
    conflict_gate = post_2016_conflict is not None and float(post_2016_conflict) <= 0.005
    gates = {
        "member_count_490_to_515": count_gate,
        "post_2016_cross_source_member_count_disagreement_le_0_5pct": conflict_gate,
        "all_price_files_present": not missing_files,
        "active_member_session_coverage_ge_99pct": coverage >= 0.99,
        "ohlcv_files_valid": not invalid_files,
        "ohlc_envelope_repair_rate_le_0_1pct": repair_rate <= 0.001,
        "single_day_abs_return_gt_40pct_le_0_05pct_of_rows": spike_rate <= 0.0005,
        "zero_volume_rows_le_0_5pct": zero_volume_rate <= 0.005,
        "stale_close_runs_ge_5d_le_0_2pct_of_rows": stale_rate <= 0.002,
        "sector_neutral_benchmark_coverage_ge_90pct": sector_coverage >= 0.90,
    }
    required_for_stage_b = [key for key in gates if not key.startswith("sector_neutral")]
    ready = all(gates[key] for key in required_for_stage_b)
    result = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": "ready" if ready else "incomplete",
        "ready": ready,
        "period": {"start": start, "end": end, "sessions": len(dates)},
        "membership": {
            "intervals": len(schedule.intervals),
            "instruments": len(schedule.symbols),
            "min_daily_count": None if counts.empty else int(counts.min()),
            "max_daily_count": None if counts.empty else int(counts.max()),
            "median_daily_count": None if counts.empty else float(counts.median()),
        },
        "cross_source": conflict_summary,
        "prices": {
            "files_present": len(bars_by_symbol),
            "files_expected": len(schedule.symbols),
            "missing_file_count": len(missing_files),
            "missing_instruments": missing_files,
            "missing_tickers": sorted({schedule.ticker_for(symbol) for symbol in missing_files}),
            "invalid_files": invalid_files,
            "active_member_sessions": active_member_sessions,
            "covered_member_sessions": covered_member_sessions,
            "coverage": coverage,
            "terminal_delisting_rows": terminal_rows,
            "ohlc_envelope_repairs": repair_rows,
            "ohlc_envelope_repair_rate": repair_rate,
            "price_quality": {
                "rows_checked_for_spikes_and_staleness": checked_rows,
                "single_day_abs_return_gt_40pct": spike_rows,
                "spike_rate": spike_rate,
                "zero_volume_rows": zero_volume_rows,
                "zero_volume_rate": zero_volume_rate,
                "stale_close_runs_ge_5d_rows": stale_rows,
                "stale_rate": stale_rate,
                "worst_spikes": [
                    {"ret": f"{value:.2f}", "symbol": symbol, "date": day}
                    for value, symbol, day in sorted(worst_spikes, reverse=True)[:15]
                ],
            },
            "coverage_by_year": {
                str(year): (
                    yearly_covered.get(year, 0) / expected if expected else None
                )
                for year, expected in sorted(yearly_expected.items())
            },
        },
        "sectors": {"member_session_coverage": sector_coverage},
        "gates": gates,
        "failed_required_gates": [key for key in required_for_stage_b if not gates[key]],
        "credential_status": {
            "alpaca": "present" if (os.getenv("ALPACA_API_KEY") and os.getenv("ALPACA_SECRET_KEY")) else "missing ALPACA_API_KEY/ALPACA_SECRET_KEY",
            "tiingo": "present" if os.getenv("TIINGO_API_KEY") else "missing TIINGO_API_KEY",
            "alpha_vantage": "present" if (os.getenv("ALPHAVANTAGE_API_KEY") or os.getenv("ALPHA_VANTAGE_API_KEY")) else "missing ALPHAVANTAGE_API_KEY",
        },
        "verdict": (
            "Stage B data gate passed."
            if ready else
            "Stage B remains inconclusive; no missing security was silently removed from the universe."
        ),
    }
    QUALITY_PATH.write_text(json.dumps(result, indent=2), encoding="utf-8")
    price = result["prices"]
    gate_lines = [
        f"- [{'x' if passed else ' '}] {name}"
        for name, passed in result["gates"].items()
    ]
    missing_tickers = price.get("missing_tickers", [])
    lines = [
        "# Stage B point-in-time data status",
        "",
        f"Status: **{result['status']}** — {result['verdict']}",
        "",
        "## Coverage",
        "",
        f"- Membership intervals: {result['membership']['intervals']}",
        f"- Daily member count: {result['membership']['min_daily_count']} to {result['membership']['max_daily_count']}",
        f"- Price files: {price['files_present']}/{price['files_expected']}",
        f"- Active member-session coverage: {price['coverage']:.2%}",
        f"- Missing tickers: {len(missing_tickers)}",
        f"- Terminal delisting rows attached: {price['terminal_delisting_rows']}",
        "",
        "## Gates",
        "",
        *gate_lines,
        "",
        "## Credentials",
        "",
        f"- Alpaca: {result['credential_status']['alpaca']}",
        f"- Tiingo: {result['credential_status']['tiingo']}",
        f"- Alpha Vantage: {result['credential_status']['alpha_vantage']}",
        "",
        "## Missing ticker sample",
        "",
        ", ".join(missing_tickers[:100]) or "None",
        "",
        "The complete episode-level gap list is in `price_gaps.csv`; no missing name is removed from membership.",
    ]
    QUALITY_REPORT_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("build", "validate", "prices", "all"):
        child = sub.add_parser(name)
        child.add_argument("--start", default=DEFAULT_START)
        child.add_argument("--end", default=DEFAULT_END)
        if name in {"build", "all"}:
            child.add_argument("--refresh-sources", action="store_true")
        if name in {"prices", "all"}:
            child.add_argument("--providers", default="yahoo,alpaca,tiingo")
            child.add_argument("--download", action="store_true")
            child.add_argument("--max-downloads", type=int)
            child.add_argument("--alpaca-limit", type=int, default=1_000)
            child.add_argument("--tiingo-limit", type=int, default=500)
            child.add_argument("--alpha-listing-status", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    load_dotenv(ROOT / ".env")
    args = _parser().parse_args(argv)
    payload: dict[str, Any] = {}
    if args.command in {"build", "all"}:
        payload["build"] = build_open_data(
            start=args.start, end=args.end, refresh_sources=args.refresh_sources,
        )
    if args.command in {"prices", "all"}:
        if args.alpha_listing_status:
            payload["alpha_vantage"] = fetch_alpha_listing_status()
        payload["prices"] = populate_prices(
            start=args.start,
            end=args.end,
            providers=args.providers.split(","),
            download=args.download,
            max_downloads=args.max_downloads,
            alpaca_limit=args.alpaca_limit,
            tiingo_limit=args.tiingo_limit,
        )
    if args.command in {"build", "validate", "prices", "all"}:
        payload["quality"] = validate_bundle(start=args.start, end=args.end)
    print(json.dumps(payload, indent=2, default=str))
    return 0 if payload["quality"].get("ready") else 2


if __name__ == "__main__":
    raise SystemExit(main())
