"""Round-trip accounting over the broker's fill history.

Average-cost walk per symbol: buys raise the lot's average entry, sells realize
(price − average cost) × qty against it. A trip closes when the position
returns to zero; a currently-held name stays open with its unrealized P&L.
"""
from __future__ import annotations


def round_trips(fills: list[dict]) -> list[dict]:
    """fills: newest-first as returned by broker_read.fills(); the walk runs
    chronologically. Returns closed trips first (by close time desc), then
    open trips."""
    by_symbol: dict[str, list[dict]] = {}
    for fill in fills:
        by_symbol.setdefault(fill["symbol"], []).append(fill)
    for series in by_symbol.values():
        series.sort(key=lambda f: f["transaction_time"] or "")

    trips: list[dict] = []
    for symbol, series in by_symbol.items():
        qty = 0.0
        cost = 0.0  # total basis of the open lot
        opened_at: str | None = None
        bought_qty = 0.0
        sold_notional = 0.0
        realized = 0.0
        last_side: str | None = None
        for fill in series:
            side = fill["side"]
            price = fill["price"]
            qty_abs = abs(float(fill["qty"]))
            if side == "buy":
                if qty <= 1e-9 and last_side != "buy":
                    opened_at = fill["transaction_time"]
                    bought_qty = 0.0
                elif opened_at is None:
                    opened_at = fill["transaction_time"]
                qty += qty_abs
                cost += qty_abs * price
                bought_qty += qty_abs
            else:  # sell
                if qty > 1e-9:
                    avg = cost / qty
                    realized += (price - avg) * min(qty_abs, qty)
                    sold_notional += price * min(qty_abs, qty)
                    qty -= qty_abs
                    cost = avg * max(qty, 0.0)
            last_side = side
            if qty <= 1e-9 and opened_at is not None:
                qty = 0.0
                trips.append(
                    {
                        "symbol": symbol,
                        "opened_at": opened_at,
                        "closed_at": fill["transaction_time"],
                        "qty": round(bought_qty, 6),
                        "avg_entry": round(cost / bought_qty, 4) if bought_qty else None,
                        "avg_exit": round(sold_notional / bought_qty, 4) if bought_qty else None,
                        "realized_pnl": round(realized, 2),
                        "open": False,
                    }
                )
                qty, cost, opened_at, bought_qty, sold_notional, realized = 0.0, 0.0, None, 0.0, 0.0, 0.0
        # Whatever is still held is an open trip marked at average cost.
        if qty > 1e-9 and opened_at is not None:
            avg_entry = cost / qty
            last_price = series[-1]["price"] if series[-1]["side"] == "buy" else None
            # For an open lot the latest fill may be a partial sell; use its
            # price only when no better mark exists — the positions endpoint
            # supplies the live one in the UI layer.
            trips.append(
                {
                    "symbol": symbol,
                    "opened_at": opened_at,
                    "closed_at": None,
                    "qty": round(qty, 6),
                    "avg_entry": round(avg_entry, 4),
                    "avg_exit": None,
                    "realized_pnl": round(realized, 2),
                    "unrealized_pnl": (
                        round((last_price - avg_entry) * qty, 2) if last_price else None
                    ),
                    "open": True,
                }
            )
    trips.sort(key=lambda t: t["closed_at"] or "", reverse=True)
    return trips


def realized_pnl_timeline(trips: list[dict]) -> list[dict]:
    """Cumulative realized P&L per closed trip, chronological."""
    closed = sorted((t for t in trips if not t["open"]), key=lambda t: t["closed_at"] or "")
    running = 0.0
    out: list[dict] = []
    for trip in closed:
        running += float(trip["realized_pnl"] or 0.0)
        out.append({"closed_at": trip["closed_at"], "cum_realized_pnl": round(running, 2),
                    "symbol": trip["symbol"]})
    return out
