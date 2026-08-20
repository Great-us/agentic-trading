"""Stateful broker used by the backtest (and by any multi-cycle dry run).

Fills are T+1 open relative to when `run_cycle` submitted the order: the
backtest loop calls `process_bar(date)` at the start of each session (fills
overnight orders at that day's open, then checks resting stops against the
day's range) and `run_cycle(asof=date)` at the close (new decisions, new
resting stops).

A stop that is gapped through fills at the open, not at the stop price —
matching the live caveat that a stop is not a floor.
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field

import pandas as pd

from .broker import Account, OpenOrder, OrderResult, Position


@dataclass
class Fill:
    date: pd.Timestamp
    symbol: str
    side: str
    qty: float
    price: float
    reason: str  # signal | stop | gap_stop
    notional: float


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
    bars: dict[str, pd.DataFrame] = field(default_factory=dict)

    cash: float = field(init=False)
    positions: dict[str, Position] = field(default_factory=dict)
    pending_buys: list[_PendingBuy] = field(default_factory=list)
    pending_sells: list[_PendingSell] = field(default_factory=list)
    stops: dict[str, OpenOrder] = field(default_factory=dict)
    fills: list[Fill] = field(default_factory=list)
    equity_curve: list[tuple[pd.Timestamp, float, float]] = field(default_factory=list)
    current_date: pd.Timestamp | None = None
    _ids: itertools.count = field(default_factory=lambda: itertools.count(1))

    def __post_init__(self) -> None:
        self.cash = self.starting_cash

    def _next_id(self) -> str:
        return f"sim-{next(self._ids)}"

    def _bar(self, symbol: str, date: pd.Timestamp) -> pd.Series | None:
        df = self.bars.get(symbol)
        if df is None or df.empty:
            return None
        idx = df.index
        if not isinstance(idx, pd.DatetimeIndex):
            return None
        day = pd.Timestamp(date).normalize()
        if idx.tz is not None:
            day = day.tz_localize(idx.tz) if day.tzinfo is None else day.tz_convert(idx.tz)
            day = day.normalize()
            match = df.loc[idx.normalize() == day]
        else:
            day = day.tz_localize(None) if day.tzinfo is not None else day
            match = df.loc[idx.tz_localize(None).normalize() == day.normalize()]
        if match.empty:
            return None
        return match.iloc[0]

    def _slip(self, price: float, side: str, bps: float) -> float:
        signed = 1.0 if side == "buy" else -1.0
        return price * (1.0 + signed * bps / 10_000.0)

    def _mark_to_market(self, date: pd.Timestamp) -> None:
        for symbol, pos in list(self.positions.items()):
            bar = self._bar(symbol, date)
            if bar is None:
                continue
            close = float(bar["Close"])
            pos.current_price = close
            pos.market_value = pos.qty * close

    def equity(self) -> float:
        # Cash is reserved when a buy is submitted; keep that notional in equity
        # so the curve doesn't dip for a day while the fill is in flight.
        in_flight = sum(p.notional for p in self.pending_buys)
        return self.cash + in_flight + sum(p.market_value for p in self.positions.values())

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
            self.positions.pop(pending.symbol, None)
            self.stops.pop(pending.symbol, None)
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
            price = self._slip(float(bar["Open"]), "buy", self.slippage_bps)
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
                        qty=qty, price=price, reason="signal", notional=pending.notional)
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
        if self.current_date is not None:
            self._mark_to_market(self.current_date)

    def get_positions(self) -> dict[str, Position]:
        self._refresh_mtm()
        return dict(self.positions)

    def is_market_open(self) -> bool:
        return True

    def submit_market_order(self, symbol: str, qty: float, side: str) -> OrderResult | None:
        if side != "sell":
            return None
        pos = self.positions.get(symbol)
        if pos is None:
            return None
        order_id = self._next_id()
        self.pending_sells.append(_PendingSell(order_id=order_id, symbol=symbol, qty=qty))
        self.stops.pop(symbol, None)
        return OrderResult(symbol=symbol, side="sell", qty=qty, status="accepted", order_id=order_id)

    def submit_notional_buy(self, symbol: str, notional: float, *, atr14: float | None = None) -> OrderResult | None:
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

    def submit_stop_sell(self, symbol: str, qty: float, stop_price: float) -> OrderResult | None:
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
