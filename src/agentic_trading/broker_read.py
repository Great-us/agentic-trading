"""Read-only Alpaca access, one reader per book.

Why this exists: the journals record order *intents* (order_qty is 0/None,
fill_price is always empty) — actual fills, average cost, and the resting
protective stops live only at the broker. The dashboard and daily_report
therefore query Alpaca's paper REST API directly.

Shared (not under dashboard/) so daily_report can import this on books that
have no dashboard package (P2).

Safety posture:
- GET-only. This module has no submit/cancel/patch/delete code at all.
- The base URL must contain "paper" or every call refuses outright (same
  invariant as Settings.is_paper in the trading system itself).
- Credentials come from that book root's own .env (ALPACA_API_KEY /
  ALPACA_SECRET_KEY / ALPACA_BASE_URL), read via dotenv_values so nothing
  leaks into this process's environment.

Uses urllib from the stdlib — alpaca-py's client exposes positions and orders
but no activities endpoint, and adding another HTTP dependency for three GETs
is not worth it.
"""
from __future__ import annotations

import json
import time
import urllib.request
from pathlib import Path

from dotenv import dotenv_values


class BrokerError(RuntimeError):
    pass


def _load_credentials(book_root: Path) -> tuple[str, str, str]:
    env_path = book_root / ".env"
    values = dotenv_values(env_path) if env_path.exists() else {}
    key = values.get("ALPACA_API_KEY") or ""
    secret = values.get("ALPACA_SECRET_KEY") or ""
    base = (values.get("ALPACA_BASE_URL") or "https://paper-api.alpaca.markets").rstrip("/")
    if not key or not secret:
        raise BrokerError(f"no ALPACA credentials in {env_path}")
    if "paper" not in base:
        # Same hard refusal as the trading system: never talk to a live
        # endpoint, even read-only, by accident.
        raise BrokerError(f"refusing non-paper base URL {base!r}")
    return key, secret, base


class BookBrokerReader:
    """Cached read-only view of one book's Alpaca paper account."""

    TTL_SECONDS = 60.0

    def __init__(self, book_root: Path):
        self._book_root = Path(book_root)
        self._cache: dict[str, tuple[float, object]] = {}

    def _get(self, path: str, params: dict[str, str] | None = None) -> object:
        key, secret, base = _load_credentials(self._book_root)
        query = ""
        if params:
            from urllib.parse import urlencode

            query = "?" + urlencode(params)
        request = urllib.request.Request(
            f"{base}{path}{query}",
            headers={
                "APCA-API-KEY-ID": key,
                "APCA-API-SECRET-KEY": secret,
                "Accept": "application/json",
            },
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.loads(response.read().decode("utf-8"))

    def _cached(self, name: str, producer):
        hit = self._cache.get(name)
        now = time.monotonic()
        if hit and now - hit[0] < self.TTL_SECONDS:
            return hit[1], None
        try:
            value = producer()
        except BrokerError:
            raise
        except Exception as exc:  # noqa: BLE001 — surface any API failure as a reason string
            return None, f"{type(exc).__name__}: {exc}"
        self._cache[name] = (now, value)
        return value, None

    # ---- public read-only views -------------------------------------------

    def account(self) -> tuple[dict | None, str | None]:
        def produce() -> dict:
            raw = self._get("/v2/account")
            if not isinstance(raw, dict):
                raise BrokerError("unexpected /v2/account payload")
            return {
                "equity": float(raw.get("equity") or 0.0),
                "cash": float(raw.get("cash") or 0.0),
                "invested": float(raw.get("long_market_value") or 0.0),
                "last_equity": float(raw.get("last_equity") or 0.0),
                "status": raw.get("status"),
            }

        return self._cached("account", produce)

    def positions(self) -> tuple[list[dict] | None, str | None]:
        def produce() -> list[dict]:
            raw = self._get("/v2/positions")
            if not isinstance(raw, list):
                raise BrokerError("unexpected /v2/positions payload")
            out = []
            for p in raw:
                out.append(
                    {
                        "symbol": p.get("symbol"),
                        "qty": float(p.get("qty") or 0.0),
                        "avg_entry_price": float(p.get("avg_entry_price") or 0.0),
                        "current_price": float(p.get("current_price") or 0.0),
                        "market_value": float(p.get("market_value") or 0.0),
                        "unrealized_pl": float(p.get("unrealized_pl") or 0.0),
                        "unrealized_plpc": float(p.get("unrealized_plpc") or 0.0),
                    }
                )
            return sorted(out, key=lambda x: -x["market_value"])

        return self._cached("positions", produce)

    def open_stop_orders(self) -> tuple[list[dict] | None, str | None]:
        """Open sell stops — the resting protective orders placed by cycle
        reconciliation. Used to show each position's live stop distance."""

        def produce() -> list[dict]:
            raw = self._get("/v2/orders", {"status": "open", "limit": "500"})
            if not isinstance(raw, list):
                raise BrokerError("unexpected /v2/orders payload")
            out = []
            for o in raw:
                if o.get("type") == "stop" and o.get("side") == "sell":
                    out.append(
                        {
                            "symbol": o.get("symbol"),
                            "stop_price": float(o.get("stop_price") or 0.0),
                            "submitted_at": o.get("submitted_at"),
                        }
                    )
            return out

        return self._cached("stops", produce)

    def fills(self, max_records: int = 1000) -> tuple[list[dict] | None, str | None]:
        """Fill history, newest first, oldest-last capped at max_records."""

        def produce() -> list[dict]:
            out: list[dict] = []
            page_token: str | None = None
            while len(out) < max_records:
                params = {"direction": "desc", "page_size": "100"}
                if page_token:
                    params["page_token"] = page_token
                raw = self._get("/v2/account/activities/FILL", params)
                if not isinstance(raw, list):
                    raise BrokerError("unexpected activities payload")
                for a in raw:
                    qty = float(a.get("qty") or 0.0)
                    price = float(a.get("price") or 0.0)
                    out.append(
                        {
                            "symbol": a.get("symbol"),
                            "side": a.get("side"),  # "buy" | "sell"
                            "qty": qty,
                            "price": price,
                            "notional": round(qty * price, 2),
                            "transaction_time": a.get("transaction_time"),
                            "order_status": a.get("order_status"),
                        }
                    )
                next_token = None
                if isinstance(raw, list) and raw:
                    # The Activities endpoint paginates via the last id when
                    # using page_token; Alpaca returns up to page_size items.
                    next_token = raw[-1].get("id")
                if not raw or not next_token or len(raw) < 100:
                    break
                page_token = str(next_token)
            return out[:max_records]

        return self._cached("fills", produce)

    def clock(self) -> tuple[dict | None, str | None]:
        """Alpaca /v2/clock — market open/close. Used by the live cockpit."""

        def produce() -> dict:
            raw = self._get("/v2/clock")
            if not isinstance(raw, dict):
                raise BrokerError("unexpected /v2/clock payload")
            return {
                "is_open": bool(raw.get("is_open")),
                "timestamp": raw.get("timestamp"),
                "next_open": raw.get("next_open"),
                "next_close": raw.get("next_close"),
            }

        return self._cached("clock", produce)
