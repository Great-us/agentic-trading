"""Stateful broker used by the backtest (and by any multi-cycle dry run).

Fills are T+1 open relative to when `run_cycle` submitted the order: the
backtest loop calls `process_bar(date)` at the start of each session (fills
overnight orders at that day's open, then checks resting stops against the
day's range) and `run_cycle(asof=date)` at the close (new decisions, new
resting stops).

A stop that is gapped through fills at the open, not at the stop price —
matching the live caveat that a stop is not a floor.

Buys respect the same **gap veto as the live stack**: an entry whose fill-day
open has already run more than `max_entry_gap_atr` ATR past the decision-day
close is refunded instead of filled, matching the TradeIntent revalidation
live buys go through (`run.py`). Without this the replay would buy every
overnight gap that the live system deliberately refuses to chase.
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from datetime import date as calendar_date

import pandas as pd

from .broker import Account, OpenOrder, OrderResult, Position


@dataclass
class Fill:
    date: pd.Timestamp
    symbol: str
    side: str
    qty: float
    price: float
    reason: str  # signal | stop | gap_stop | delisting
    notional: float
    atr14: float | None = None


@dataclass
class _PendingBuy:
    order_id: str
    symbol: str
    notional: float
    atr14: float | None = None


@dataclass
class _PendingSell:
    order_id: str
    symbol: str
    qty: float


@dataclass
class SimulatedBroker:
    starting_cash: float = 100_000.0
    slippage_bps: float = 5.0
    stop_slippage_bps: float = 10.0
    atr_stop_multiple: float = 2.5
    min_stop_pct: float = 0.06
    max_stop_pct: float = 0.20
    max_entry_gap_atr: float = 0.75
    bars: dict[str, pd.DataFrame] = field(default_factory=dict)

    cash: float = field(init=False)
    positions: dict[str, Position] = field(default_factory=dict)
    pending_buys: list[_PendingBuy] = field(default_factory=list)
    pending_sells: list[_PendingSell] = field(default_factory=list)
    stops: dict[str, OpenOrder] = field(default_factory=dict)
    fills: list[Fill] = field(default_factory=list)
    # (date, symbol, gap_in_atr) for entries the gap veto refused — the live
    # system's WAITs, kept visible so replay-vs-live divergence stays auditable.
    gap_vetoed_buys: list[tuple[pd.Timestamp, str, float]] = field(default_factory=list)
    equity_curve: list[tuple[pd.Timestamp, float, float]] = field(default_factory=list)
    current_date: pd.Timestamp | None = None
    _ids: itertools.count = field(default_factory=lambda: itertools.count(1))
    _bar_positions: dict[str, dict[calendar_date, int]] = field(
        init=False, default_factory=dict, repr=False,
    )
    _mtm_date: pd.Timestamp | None = field(init=False, default=None, repr=False)

    def __post_init__(self) -> None:
        self.cash = self.starting_cash
        for symbol, df in self.bars.items():
            if df is None or df.empty or not isinstance(df.index, pd.DatetimeIndex):
                continue
            # Backtests address bars by exchange calendar date. Building this
            # once turns the broker's hottest path from a full-index timezone
            # normalization into an O(1) lookup.
            self._bar_positions[symbol] = {
                pd.Timestamp(timestamp).date(): position
                for position, timestamp in enumerate(df.index)
            }

    def _next_id(self) -> str:
        return f"sim-{next(self._ids)}"

    def _bar(self, symbol: str, date: pd.Timestamp) -> pd.Series | None:
        df = self.bars.get(symbol)
        if df is None or df.empty:
            return None
        position = self._bar_positions.get(symbol, {}).get(pd.Timestamp(date).date())
        if position is None:
            return None
        return df.iloc[position]

    def _slip(self, price: float, side: str, bps: float) -> float:
        signed = 1.0 if side == "buy" else -1.0
        return price * (1.0 + signed * bps / 10_000.0)

    def _previous_close(self, symbol: str, date: pd.Timestamp) -> float | None:
        df = self.bars.get(symbol)
        if df is None or df.empty or "Close" not in df:
            return None
        idx = pd.DatetimeIndex(df.index)
        day = pd.Timestamp(date)
        if idx.tz is not None:
            day = day.tz_localize(idx.tz) if day.tzinfo is None else day.tz_convert(idx.tz)
        elif day.tzinfo is not None:
            day = day.tz_localize(None)
        prior = df.loc[idx < day]
        if prior.empty:
            return None
        return float(prior["Close"].iloc[-1])

    def _mark_to_market(self, date: pd.Timestamp) -> None:
        for symbol, pos in list(self.positions.items()):
            bar = self._bar(symbol, date)
            if bar is None:
                continue
            close = float(bar["Close"])
            pos.current_price = close
            pos.market_value = pos.qty * close
        self._mtm_date = pd.Timestamp(date)

    def equity(self) -> float:
        # Cash is reserved when a buy is submitted; keep that notional in equity
        # so the curve doesn't dip for a day while the fill is in flight.
        in_flight = sum(p.notional for p in self.pending_buys)
        return self.cash + in_flight + sum(p.market_value for p in self.positions.values())

    def accrue_cash(self, daily_return: float) -> float:
        """Credit interest to settled cash and return the dollar amount earned."""
        if daily_return != daily_return or daily_return <= -1.0:
            return 0.0
        earned = self.cash * daily_return
        self.cash += earned
        return earned

    def cancel_pending_buys(self, allowed_symbols: set[str] | None = None) -> int:
        """Refund unfilled buys, optionally retaining only currently eligible names."""
        kept: list[_PendingBuy] = []
        cancelled = 0
        for pending in self.pending_buys:
            if allowed_symbols is not None and pending.symbol in allowed_symbols:
                kept.append(pending)
                continue
            self.cash += pending.notional
            cancelled += 1
        self.pending_buys = kept
        return cancelled

    def process_bar(self, date: pd.Timestamp) -> list[Fill]:
        """Open fills, then stop checks against today's range, then close MTM."""
        self.current_date = pd.Timestamp(date)
        day_fills: list[Fill] = []

        still_selling: list[_PendingSell] = []
        for pending in self.pending_sells:
            pos = self.positions.get(pending.symbol)
            if pos is None:
                continue
            bar = self._bar(pending.symbol, date)
            if bar is None:
                still_selling.append(pending)
                continue
            qty = min(pending.qty, pos.qty)
            price = self._slip(float(bar["Open"]), "sell", self.slippage_bps)
            proceeds = qty * price
            self.cash += proceeds
            fill = Fill(date=self.current_date, symbol=pending.symbol, side="sell",
                        qty=qty, price=price, reason="signal", notional=proceeds)
            self.fills.append(fill)
            day_fills.append(fill)
            pos.qty -= qty
            if pos.qty <= 1e-9:
                self.positions.pop(pending.symbol, None)
                self.stops.pop(pending.symbol, None)
            else:
                pos.market_value = pos.qty * float(bar["Close"]) if "Close" in bar else pos.qty * price
                stop = self.stops.get(pending.symbol)
                if stop is not None:
                    stop.qty = pos.qty
        self.pending_sells = still_selling

        still_buying: list[_PendingBuy] = []
        for pending in self.pending_buys:
            if pending.symbol in self.positions:
                # Already filled (e.g. a same-day cycle); refund reserved cash.
                self.cash += pending.notional
                continue
            bar = self._bar(pending.symbol, date)
            if bar is None:
                still_buying.append(pending)
                continue
            open_px = float(bar["Open"])
            if (
                pending.atr14 is not None and pending.atr14 > 0
                and self.max_entry_gap_atr > 0
            ):
                prev_close = self._previous_close(pending.symbol, date)
                if prev_close and (open_px - prev_close) / pending.atr14 > self.max_entry_gap_atr:
                    # Same veto the live TradeIntent path applies at the open:
                    # chasing a gap the signal never saw is not an entry.
                    self.cash += pending.notional
                    self.gap_vetoed_buys.append(
                        (self.current_date, pending.symbol, (open_px - prev_close) / pending.atr14),
                    )
                    continue
            price = self._slip(open_px, "buy", self.slippage_bps)
            if price <= 0:
                self.cash += pending.notional
                continue
            qty = pending.notional / price
            pos = Position(
                symbol=pending.symbol, qty=qty, avg_entry_price=price,
                current_price=price, market_value=qty * price,
            )
            self.positions[pending.symbol] = pos
            fill = Fill(date=self.current_date, symbol=pending.symbol, side="buy",
                        qty=qty, price=price, reason="signal", notional=pending.notional,
                        atr14=pending.atr14)
            self.fills.append(fill)
            day_fills.append(fill)
            if pending.atr14 is not None and pending.atr14 > 0:
                # Arm a stop at fill so the entry day is not naked. Live 9:45
                # does this in the same cycle; live 16:15 waits until next run.
                raw = pending.atr14 * self.atr_stop_multiple / price
                stop_pct = max(self.min_stop_pct, min(self.max_stop_pct, raw))
                stop_price = round(price * (1.0 - stop_pct), 2)
                self.stops[pending.symbol] = OpenOrder(
                    order_id=self._next_id(), symbol=pending.symbol, side="sell",
                    order_type="stop", qty=qty, stop_price=stop_price,
                )
        self.pending_buys = still_buying

        # Point-in-time datasets may provide a CRSP-style delisting return.
        # It is the terminal total return from the prior close, so apply it
        # directly and close the position instead of forward-filling a stale
        # quote or pretending the security remained tradeable.
        for symbol, pos in list(self.positions.items()):
            bar = self._bar(symbol, date)
            if bar is None or "DelistingReturn" not in bar or pd.isna(bar["DelistingReturn"]):
                continue
            previous = self._previous_close(symbol, date)
            if previous is None:
                continue
            price = max(0.0, previous * (1.0 + float(bar["DelistingReturn"])))
            proceeds = pos.qty * price
            self.cash += proceeds
            fill = Fill(date=self.current_date, symbol=symbol, side="sell",
                        qty=pos.qty, price=price, reason="delisting", notional=proceeds)
            self.fills.append(fill)
            day_fills.append(fill)
            self.positions.pop(symbol, None)
            self.stops.pop(symbol, None)
            self.pending_sells = [pending for pending in self.pending_sells if pending.symbol != symbol]

        for symbol, pos in list(self.positions.items()):
            stop = self.stops.get(symbol)
            if stop is None or stop.stop_price is None:
                continue
            bar = self._bar(symbol, date)
            if bar is None:
                continue
            low = float(bar["Low"])
            open_px = float(bar["Open"])
            if low > stop.stop_price:
                continue
            reason = "gap_stop" if open_px <= stop.stop_price else "stop"
            raw_fill = open_px if open_px <= stop.stop_price else stop.stop_price
            price = self._slip(raw_fill, "sell", self.stop_slippage_bps)
            qty = pos.qty
            proceeds = qty * price
            self.cash += proceeds
            fill = Fill(date=self.current_date, symbol=symbol, side="sell",
                        qty=qty, price=price, reason=reason, notional=proceeds)
            self.fills.append(fill)
            day_fills.append(fill)
            self.positions.pop(symbol, None)
            self.stops.pop(symbol, None)
            self.pending_sells = [p for p in self.pending_sells if p.symbol != symbol]

        self._mark_to_market(date)
        self.equity_curve.append((self.current_date, self.equity(), self.cash))
        return day_fills

    def get_account(self) -> Account:
        self._refresh_mtm()
        return Account(equity=self.equity(), cash=self.cash)

    def _refresh_mtm(self) -> None:
        if self.current_date is not None and self._mtm_date != self.current_date:
            self._mark_to_market(self.current_date)

    def get_positions(self) -> dict[str, Position]:
        self._refresh_mtm()
        return dict(self.positions)

    def is_market_open(self) -> bool:
        return True

    def submit_market_order(self, symbol: str, qty: float, side: str, *, client_order_id: str | None = None) -> OrderResult | None:
        if side != "sell":
            return None
        pos = self.positions.get(symbol)
        if pos is None:
            return None
        order_id = self._next_id()
        self.pending_sells.append(_PendingSell(order_id=order_id, symbol=symbol, qty=qty))
        self.stops.pop(symbol, None)
        return OrderResult(symbol=symbol, side="sell", qty=qty, status="accepted", order_id=order_id)

    def submit_notional_buy(self, symbol: str, notional: float, *, atr14: float | None = None, client_order_id: str | None = None) -> OrderResult | None:
        notional = round(notional, 2)
        if notional <= 0 or self.cash + 1e-9 < notional:
            return None
        if symbol in self.positions or any(p.symbol == symbol for p in self.pending_buys):
            return None
        self.cash -= notional
        order_id = self._next_id()
        self.pending_buys.append(_PendingBuy(order_id=order_id, symbol=symbol, notional=notional, atr14=atr14))
        return OrderResult(symbol=symbol, side="buy", qty=0.0, status="accepted", order_id=order_id)

    def get_open_orders(self) -> list[OpenOrder]:
        orders = list(self.stops.values())
        for p in self.pending_buys:
            orders.append(OpenOrder(
                order_id=p.order_id, symbol=p.symbol, side="buy",
                order_type="market", qty=0.0, stop_price=None,
            ))
        for p in self.pending_sells:
            orders.append(OpenOrder(
                order_id=p.order_id, symbol=p.symbol, side="sell",
                order_type="market", qty=p.qty, stop_price=None,
            ))
        return orders

    def submit_stop_sell(self, symbol: str, qty: float, stop_price: float, *, client_order_id: str | None = None) -> OrderResult | None:
        if symbol not in self.positions:
            return None
        order_id = self._next_id()
        self.stops[symbol] = OpenOrder(
            order_id=order_id, symbol=symbol, side="sell",
            order_type="stop", qty=qty, stop_price=round(stop_price, 2),
        )
        return OrderResult(symbol=symbol, side="sell", qty=qty, status="accepted", order_id=order_id)

    def cancel_order(self, order_id: str) -> bool:
        for symbol, stop in list(self.stops.items()):
            if stop.order_id == order_id:
                del self.stops[symbol]
                return True
        for pending in list(self.pending_buys):
            if pending.order_id == order_id:
                self.cash += pending.notional
                self.pending_buys = [p for p in self.pending_buys if p.order_id != order_id]
                return True
        before = len(self.pending_sells)
        self.pending_sells = [p for p in self.pending_sells if p.order_id != order_id]
        return len(self.pending_sells) != before
