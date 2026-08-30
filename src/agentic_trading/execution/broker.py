"""Alpaca paper-trading client, plus a DryRunBroker fallback so the rest of the
pipeline (signals, LLM analysis, decisions) can be exercised before Alpaca
credentials are configured. DryRunBroker never touches a real account — it just
logs what it would have done and reports an empty, zero-cash book.
"""
from __future__ import annotations

import itertools
import logging
from dataclasses import dataclass
from typing import Protocol

logger = logging.getLogger(__name__)


@dataclass
class Position:
    symbol: str
    qty: float
    avg_entry_price: float
    current_price: float
    market_value: float


@dataclass
class Account:
    equity: float
    cash: float


@dataclass
class OrderResult:
    symbol: str
    side: str
    qty: float
    status: str
    order_id: str | None


def _enum_str(value) -> str:
    """Alpaca returns enums whose str() is 'OrderStatus.ACCEPTED'; the bare
    value ('accepted') is what belongs in logs and the journal."""
    return str(getattr(value, "value", value))


def _is_not_found(exc: Exception) -> bool:
    """True when an exception means 'order does not exist' (already filled,
    expired, or cancelled elsewhere) rather than a real failure."""
    status = getattr(exc, "status_code", None) or getattr(getattr(exc, "response", None), "status_code", None)
    if status == 404:
        return True
    text = str(exc).lower()
    return "404" in text or "not found" in text


class OrderIdMinter:
    """Mints client_order_ids that are unique per placement *action*.

    Alpaca never recycles a consumed client_order_id: re-submitting the same id
    for a later placement in the same cycle (cancel/replace of a stop, the
    post-failed-sell restore) is rejected with 422, and the position sits
    without its replacement until the next cycle (observed live 2026-08-24).
    The idempotency that matters — retrying an ambiguous submit with the SAME
    id so a duplicate is a harmless server-side rejection — is preserved by
    minting once per action and reusing that value for the action's retries.
    The sequence number plus the cycle stamp keeps ids unique even across
    in-cycle replacements; a process restart starts a new cycle timestamp, so
    no cross-process collision is possible."""

    def __init__(self, cycle_timestamp: str) -> None:
        self._stamp = "".join(ch for ch in cycle_timestamp if ch.isalnum())
        self._seq = itertools.count(1)

    def mint(self, purpose: str, symbol: str) -> str:
        return f"at-{purpose}-{symbol}-{self._stamp}-{next(self._seq)}"[:120]


@dataclass
class OpenOrder:
    order_id: str
    symbol: str
    side: str
    order_type: str
    qty: float
    stop_price: float | None


class Broker(Protocol):
    def get_account(self) -> Account: ...
    def get_positions(self) -> dict[str, Position]: ...
    def is_market_open(self) -> bool: ...
    def get_today_open(self, symbol: str) -> float | None: ...
    def submit_market_order(self, symbol: str, qty: float, side: str, *, client_order_id: str | None = None) -> OrderResult | None: ...
    def submit_notional_buy(self, symbol: str, notional: float, *, atr14: float | None = None, client_order_id: str | None = None) -> OrderResult | None: ...
    def get_open_orders(self) -> list[OpenOrder]: ...
    def submit_stop_sell(self, symbol: str, qty: float, stop_price: float, *, client_order_id: str | None = None) -> OrderResult | None: ...
    def cancel_order(self, order_id: str) -> bool: ...


class AlpacaBroker:
    def __init__(self, api_key: str, secret_key: str, paper: bool = True):
        from alpaca.trading.client import TradingClient

        if not paper:
            raise ValueError("AlpacaBroker is paper-only in this project; refusing paper=False.")
        self._client = TradingClient(api_key, secret_key, paper=True)
        self._api_key = api_key
        self._secret_key = secret_key
        self._data = None  # StockHistoricalDataClient, created lazily

    def get_account(self) -> Account:
        acct = self._client.get_account()
        return Account(equity=float(acct.equity), cash=float(acct.cash))

    def get_positions(self) -> dict[str, Position]:
        positions = self._client.get_all_positions()
        return {
            p.symbol: Position(
                symbol=p.symbol,
                qty=float(p.qty),
                avg_entry_price=float(p.avg_entry_price),
                current_price=float(p.current_price),
                market_value=float(p.market_value),
            )
            for p in positions
        }

    def is_market_open(self) -> bool:
        return bool(self._client.get_clock().is_open)

    def get_today_open(self, symbol: str) -> float | None:
        """Today's regular-session open, for the entry chase guard.

        None when there is no session bar yet or the request fails; callers
        treat None as 'this gate cannot run' and fall back to the
        signal-relative chase check, not as 'gate passed'."""
        try:
            if self._data is None:
                from alpaca.data.historical import StockHistoricalDataClient

                self._data = StockHistoricalDataClient(self._api_key, self._secret_key)
            from alpaca.data.requests import StockSnapshotRequest

            snapshot = self._data.get_stock_snapshot(StockSnapshotRequest(symbol_or_symbols=symbol))
            bar = getattr(snapshot, "daily_bar", None)
            if bar is None or not bar.open:
                return None
            return float(bar.open)
        except Exception:
            logger.exception("Could not fetch today's open for %s", symbol)
            return None

    def submit_market_order(self, symbol: str, qty: float, side: str, *, client_order_id: str | None = None) -> OrderResult:
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import MarketOrderRequest

        order_side = OrderSide.BUY if side == "buy" else OrderSide.SELL
        request = MarketOrderRequest(
            symbol=symbol, qty=qty, side=order_side, time_in_force=TimeInForce.DAY,
            client_order_id=client_order_id,
        )
        try:
            order = self._client.submit_order(request)
        except Exception:
            # A rejected order must not abort the cycle. A failed SELL is the
            # dangerous case — the position stays open and unhedged — so it is
            # logged at error level for the operator to act on.
            level = logger.error if side == "sell" else logger.warning
            level("%s order REJECTED for %s x%s", side.upper(), symbol, qty, exc_info=True)
            return None
        logger.info("Submitted %s order: %s x%s (id=%s, status=%s)", side, symbol, qty, order.id, order.status)
        return OrderResult(symbol=symbol, side=side, qty=qty, status=_enum_str(order.status), order_id=str(order.id))

    def submit_notional_buy(self, symbol: str, notional: float, *, atr14: float | None = None, client_order_id: str | None = None) -> OrderResult:
        """Buys a dollar amount rather than a share count. On a small account,
        whole-share rounding is a real distortion — $1,800 of a $780 stock is
        2 shares (13% under budget) — and fractional shares remove it.
        Fractional orders must be DAY, not GTC."""
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import MarketOrderRequest

        request = MarketOrderRequest(
            symbol=symbol, notional=round(notional, 2), side=OrderSide.BUY, time_in_force=TimeInForce.DAY,
            client_order_id=client_order_id,
        )
        try:
            order = self._client.submit_order(request)
        except Exception:
            # Notional orders are only accepted during regular market hours, so
            # a cycle run after the close will land here rather than queueing.
            logger.warning("Notional buy REJECTED for %s ($%.2f)", symbol, notional, exc_info=True)
            return None
        filled_qty = float(order.qty) if order.qty else 0.0
        logger.info("Submitted notional buy: %s $%.2f (id=%s, status=%s)", symbol, notional, order.id, order.status)
        return OrderResult(symbol=symbol, side="buy", qty=filled_qty, status=_enum_str(order.status), order_id=str(order.id))

    def get_open_orders(self) -> list[OpenOrder]:
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        orders = self._client.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN))
        return [
            OpenOrder(
                order_id=str(o.id),
                symbol=o.symbol,
                side=_enum_str(o.side),
                order_type=_enum_str(o.type),
                qty=float(o.qty) if o.qty else 0.0,
                stop_price=float(o.stop_price) if o.stop_price else None,
            )
            for o in orders
        ]

    def submit_stop_sell(self, symbol: str, qty: float, stop_price: float, *, client_order_id: str | None = None) -> OrderResult | None:
        """Broker-side protective stop. Plain stop rather than trailing, because
        Alpaca does not accept trailing stops on fractional positions — the
        trailing behaviour is reproduced by ratcheting this order's price up on
        each cycle. Fractional orders are DAY-only, so this is re-placed every
        cycle rather than left GTC.

        Returns None on rejection instead of raising: a missing protective order
        must not abort the cycle, and the client-side exit check still runs.
        """
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import StopOrderRequest

        request = StopOrderRequest(
            symbol=symbol, qty=qty, side=OrderSide.SELL,
            time_in_force=TimeInForce.DAY, stop_price=round(stop_price, 2),
            client_order_id=client_order_id,
        )
        try:
            order = self._client.submit_order(request)
        except Exception:
            logger.exception("Protective stop rejected for %s (qty=%s, stop=%.2f)", symbol, qty, stop_price)
            return None
        logger.info("Protective stop placed: %s x%s @ %.2f (id=%s)", symbol, qty, stop_price, order.id)
        return OrderResult(symbol=symbol, side="sell", qty=qty, status=_enum_str(order.status), order_id=str(order.id))

    def cancel_order(self, order_id: str) -> bool:
        try:
            self._client.cancel_order_by_id(order_id)
            return True
        except Exception as exc:
            if _is_not_found(exc):
                # Already filled / expired / cancelled elsewhere is a SUCCESS
                # for our purposes: the caller may now place its replacement.
                logger.info("Cancel %s: order no longer exists (404) — treating as cancelled.", order_id)
                return True
            logger.exception("Failed to cancel order %s", order_id)
            return False


class DryRunBroker:
    """No credentials required. Reports an empty book and logs intended orders."""

    def get_account(self) -> Account:
        return Account(equity=100_000.0, cash=100_000.0)

    def get_positions(self) -> dict[str, Position]:
        return {}

    def is_market_open(self) -> bool:
        return True

    def get_today_open(self, symbol: str) -> float | None:
        # No simulated session bar: the open-relative chase gate cannot run in
        # dry runs, so only the signal-relative gate applies.
        return None

    def submit_market_order(self, symbol: str, qty: float, side: str, *, client_order_id: str | None = None) -> OrderResult:
        logger.info("[DRY RUN] would submit %s order: %s x%s", side, symbol, qty)
        return OrderResult(symbol=symbol, side=side, qty=qty, status="dry_run", order_id=None)

    def submit_notional_buy(self, symbol: str, notional: float, *, atr14: float | None = None, client_order_id: str | None = None) -> OrderResult:
        logger.info("[DRY RUN] would buy %s worth $%.2f", symbol, notional)
        return OrderResult(symbol=symbol, side="buy", qty=0.0, status="dry_run", order_id=None)

    def get_open_orders(self) -> list[OpenOrder]:
        return []

    def submit_stop_sell(self, symbol: str, qty: float, stop_price: float, *, client_order_id: str | None = None) -> OrderResult | None:
        logger.info("[DRY RUN] would place protective stop: %s x%s @ %.2f", symbol, qty, stop_price)
        return OrderResult(symbol=symbol, side="sell", qty=qty, status="dry_run", order_id=None)

    def cancel_order(self, order_id: str) -> bool:
        logger.info("[DRY RUN] would cancel order %s", order_id)
        return True
