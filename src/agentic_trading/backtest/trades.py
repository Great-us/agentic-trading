"""Episode-level trade reconstruction and path attribution."""
from __future__ import annotations

from dataclasses import asdict, dataclass

import pandas as pd

from ..execution.sim_broker import Fill


@dataclass
class TradeRecord:
    symbol: str
    entry_date: str
    exit_date: str
    entry_price: float
    exit_price: float
    quantity: float
    pnl: float
    return_pct: float
    holding_days: int
    initial_risk: float | None
    r_multiple: float | None
    mfe: float | None
    mae: float | None
    exit_reason: str
    is_open: bool

    def to_dict(self) -> dict:
        return asdict(self)


def _normalise_frame(frame: pd.DataFrame) -> pd.DataFrame:
    work = frame.copy()
    idx = pd.DatetimeIndex(work.index)
    if idx.tz is not None:
        idx = idx.tz_localize(None)
    work.index = idx.normalize()
    return work[~work.index.duplicated(keep="last")].sort_index()


def _path_stats(
    bars: pd.DataFrame,
    entry: pd.Timestamp,
    exit_: pd.Timestamp,
    entry_price: float,
) -> tuple[float | None, float | None]:
    if bars is None or bars.empty or entry_price <= 0:
        return None, None
    work = _normalise_frame(bars).loc[entry:exit_]
    if work.empty:
        return None, None
    high = work["High"] if "High" in work else work["Close"]
    low = work["Low"] if "Low" in work else work["Close"]
    return float(high.max() / entry_price - 1.0), float(low.min() / entry_price - 1.0)


def build_trade_records(
    fills: list[Fill],
    bars: dict[str, pd.DataFrame],
    end_date: str | pd.Timestamp,
    *,
    atr_stop_multiple: float = 2.5,
    min_stop_pct: float = 0.06,
    max_stop_pct: float = 0.20,
) -> list[TradeRecord]:
    """Build one row per position episode, including marked-to-market open episodes."""
    end = pd.Timestamp(end_date).tz_localize(None).normalize()
    active: dict[str, dict] = {}
    records: list[TradeRecord] = []

    def finish(symbol: str, exit_date: pd.Timestamp, exit_price: float, reason: str, is_open: bool) -> None:
        episode = active.pop(symbol)
        qty = float(episode["entry_qty"])
        proceeds = float(episode["proceeds"]) + float(episode["remaining_qty"]) * exit_price
        cost = qty * float(episode["entry_price"])
        pnl = proceeds - cost
        risk = episode["initial_risk"]
        mfe, mae = _path_stats(
            bars.get(symbol, pd.DataFrame()), episode["entry_date"], exit_date, episode["entry_price"]
        )
        sold_qty = qty - float(episode["remaining_qty"])
        exit_value = float(episode["proceeds"]) + float(episode["remaining_qty"]) * exit_price
        average_exit = exit_value / qty if qty > 0 else exit_price
        records.append(TradeRecord(
            symbol=symbol,
            entry_date=episode["entry_date"].date().isoformat(),
            exit_date=exit_date.date().isoformat(),
            entry_price=float(episode["entry_price"]),
            exit_price=average_exit,
            quantity=qty,
            pnl=pnl,
            return_pct=(pnl / cost) if cost > 0 else 0.0,
            holding_days=max(0, (exit_date - episode["entry_date"]).days),
            initial_risk=risk,
            r_multiple=(pnl / risk) if risk and risk > 0 else None,
            mfe=mfe,
            mae=mae,
            exit_reason=reason,
            is_open=is_open,
        ))

    ordered_fills = [
        fill for _, fill in sorted(
            enumerate(fills), key=lambda pair: (pd.Timestamp(pair[1].date), pair[0]),
        )
    ]
    for fill in ordered_fills:
        fill_date = pd.Timestamp(fill.date).tz_localize(None).normalize()
        if fill.side == "buy":
            if fill.symbol in active:
                raise ValueError(f"Overlapping buy episodes are not supported for {fill.symbol}")
            stop_pct = None
            if fill.atr14 is not None and fill.atr14 > 0 and fill.price > 0:
                raw = fill.atr14 * atr_stop_multiple / fill.price
                stop_pct = max(min_stop_pct, min(max_stop_pct, raw))
            active[fill.symbol] = {
                "entry_date": fill_date,
                "entry_price": float(fill.price),
                "entry_qty": float(fill.qty),
                "remaining_qty": float(fill.qty),
                "proceeds": 0.0,
                "initial_risk": None if stop_pct is None else float(fill.notional) * stop_pct,
                "last_reason": "open",
            }
            continue
        episode = active.get(fill.symbol)
        if episode is None:
            continue
        qty = min(float(fill.qty), float(episode["remaining_qty"]))
        episode["remaining_qty"] -= qty
        episode["proceeds"] += qty * float(fill.price)
        episode["last_reason"] = fill.reason
        if episode["remaining_qty"] <= 1e-9:
            finish(fill.symbol, fill_date, float(fill.price), fill.reason, False)

    for symbol in list(active):
        frame = bars.get(symbol, pd.DataFrame())
        if frame is None or frame.empty:
            continue
        work = _normalise_frame(frame).loc[:end]
        if work.empty:
            continue
        finish(symbol, end, float(work["Close"].iloc[-1]), "open_at_end", True)
    return records
