"""Point-in-time universe membership for survivorship-aware backtests."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pandas as pd


REQUIRED_COLUMNS = ("symbol", "start_date", "end_date", "sector", "source_id")


@dataclass(frozen=True)
class MembershipInterval:
    # ``symbol`` is the internal, collision-safe asset key used by the replay.
    # For legacy manifests it is also the exchange ticker.  New point-in-time
    # manifests keep the historically displayed ticker separately so that a
    # reused ticker cannot splice two unrelated securities together.
    symbol: str
    start_date: date
    end_date: date | None = None
    sector: str | None = None
    source_id: str | None = None
    ticker: str | None = None
    instrument_id: str | None = None
    identity_status: str | None = None

    def contains(self, value: date) -> bool:
        return self.start_date <= value and (self.end_date is None or value <= self.end_date)


@dataclass(frozen=True)
class UniverseSchedule:
    """Inclusive membership intervals known at each historical session."""

    intervals: tuple[MembershipInterval, ...]

    @classmethod
    def from_csv(cls, path: str | Path) -> "UniverseSchedule":
        frame = pd.read_csv(
            path,
            dtype={
                "symbol": str,
                "instrument_id": str,
                "ticker": str,
                "sector": str,
                "source_id": str,
                "identity_status": str,
            },
        )
        missing = [column for column in REQUIRED_COLUMNS if column not in frame.columns]
        if missing:
            raise ValueError(f"Universe manifest missing columns: {', '.join(missing)}")
        if frame.empty:
            raise ValueError("Universe manifest is empty")

        intervals: list[MembershipInterval] = []
        for row_number, row in frame.iterrows():
            raw_instrument = row.get("instrument_id")
            instrument_id = (
                None
                if raw_instrument is None or pd.isna(raw_instrument)
                else str(raw_instrument).strip().upper() or None
            )
            symbol = instrument_id or str(row["symbol"]).strip().upper()
            if not symbol or symbol == "NAN":
                raise ValueError(f"Universe row {row_number + 2} has no symbol")
            raw_ticker = row.get("ticker")
            ticker = (
                str(row["symbol"]).strip().upper()
                if raw_ticker is None or pd.isna(raw_ticker)
                else str(raw_ticker).strip().upper()
            )
            if not ticker or ticker == "NAN":
                raise ValueError(f"Universe row {row_number + 2} has no ticker")
            start = pd.to_datetime(row["start_date"], errors="coerce")
            end = pd.to_datetime(row["end_date"], errors="coerce")
            if pd.isna(start):
                raise ValueError(f"Universe row {row_number + 2} has invalid start_date")
            end_date = None if pd.isna(end) else end.date()
            if end_date is not None and end_date < start.date():
                raise ValueError(f"Universe row {row_number + 2} ends before it starts")
            sector = None if pd.isna(row["sector"]) else str(row["sector"]).strip() or None
            source_id = None if pd.isna(row["source_id"]) else str(row["source_id"]).strip() or None
            raw_status = row.get("identity_status")
            identity_status = (
                None
                if raw_status is None or pd.isna(raw_status)
                else str(raw_status).strip() or None
            )
            intervals.append(MembershipInterval(
                symbol, start.date(), end_date, sector, source_id,
                ticker=ticker, instrument_id=instrument_id or symbol,
                identity_status=identity_status,
            ))

        schedule = cls(tuple(intervals))
        schedule._validate_non_overlapping_keys()
        return schedule

    @classmethod
    def fixed(
        cls,
        symbols: list[str],
        *,
        start_date: str | date = date.min,
        end_date: str | date | None = None,
    ) -> "UniverseSchedule":
        start = date.fromisoformat(start_date) if isinstance(start_date, str) else start_date
        end = date.fromisoformat(end_date) if isinstance(end_date, str) else end_date
        return cls(tuple(
            MembershipInterval(
                str(symbol).upper(), start, end,
                ticker=str(symbol).upper(), instrument_id=str(symbol).upper(),
            )
            for symbol in symbols
        ))

    def _validate_non_overlapping_keys(self) -> None:
        by_symbol: dict[str, list[MembershipInterval]] = {}
        for interval in self.intervals:
            by_symbol.setdefault(interval.symbol, []).append(interval)
        for symbol, items in by_symbol.items():
            ordered = sorted(items, key=lambda item: item.start_date)
            for left, right in zip(ordered, ordered[1:]):
                if left.end_date is None or right.start_date <= left.end_date:
                    raise ValueError(f"Universe key {symbol} has overlapping membership intervals")

    @property
    def symbols(self) -> list[str]:
        return list(dict.fromkeys(interval.symbol for interval in self.intervals))

    @property
    def sectors(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for interval in self.intervals:
            if interval.sector:
                out[interval.symbol] = interval.sector
        return out

    @property
    def tickers(self) -> dict[str, str]:
        """Internal asset key -> historically displayed exchange ticker."""
        return {
            interval.symbol: interval.ticker or interval.symbol
            for interval in self.intervals
        }

    def ticker_for(self, symbol: str) -> str:
        return self.tickers.get(symbol, symbol)

    def sector_for(self, symbol: str) -> str | None:
        return self.sectors.get(symbol)

    def filtered(self, *, exclude_tickers: set[str]) -> "UniverseSchedule":
        excluded = {str(item).upper() for item in exclude_tickers}
        return UniverseSchedule(tuple(
            interval for interval in self.intervals
            if (interval.ticker or interval.symbol).upper() not in excluded
        ))

    def active_on(self, value: str | date | pd.Timestamp) -> list[str]:
        if isinstance(value, str):
            day = date.fromisoformat(value)
        elif isinstance(value, pd.Timestamp):
            day = value.date()
        else:
            day = value
        return list(dict.fromkeys(interval.symbol for interval in self.intervals if interval.contains(day)))
