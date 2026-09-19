"""Round-trip accounting over the broker's fill history.

Average-cost walk per symbol: buys raise the lot's average entry, sells realize
(price − average cost) × qty against it. A trip closes when the position
returns to zero; a currently-held name stays open with its unrealized P&L.

A sell fill that arrives while no position is open (the fills window doesn't
reach back far enough to see the opening buy — see AGENTS.md F6) does not get
silently dropped: it becomes its own trip with `incomplete: True`, no entry
price, and no realized P&L, so callers can flag it instead of showing a false
$0 P&L or a phantom open lot. The same applies to a sell that *partially*
oversells a tracked lot (e.g. the window saw a 1-share buy but a 2-share
sell) — the matched share closes normally, the extra share folds into the
same orphan/incomplete accounting instead of silently vanishing (see
PROGRESS.md §9 R5).

Fills sharing an exact `transaction_time` are ordered by the numeric prefix
of the broker's own activity id when one is present (Alpaca ids look like
"<millisecond-timestamp>::<uuid>", finer-grained than transaction_time,
which can collide across several fills in the same second). A same-timestamp
group that mixes buys and sells is marked `ambiguous: True` — instead of
trusting whatever order a stable sort leaves it in — in either of two cases
(PROGRESS.md §10/§11 R5): the *entire* sort key ties (no id prefix at all on
any of them, or the same prefix on two that differ only in their trailing
UUID), or only *some* of the group have a parseable prefix — a missing
prefix sorts as `""`, which puts it first among its ties, but that is a
sorting artifact, not evidence the fill actually happened first.
`reconcile_positions()` reports an ambiguous symbol as "undetermined" rather
than as either a match or a diff, and `realized_pnl_timeline()`/callers
exclude ambiguous trips from confident totals.

Shared (not under dashboard/) so daily_report can import this on books that
have no dashboard package (P2).
"""
from __future__ import annotations


def _numeric_id_prefix(fill: dict) -> str | None:
    """The sortable timestamp prefix of a broker activity id, or None when
    the id is missing or doesn't look like that shape. Callers must not
    assume an order from a fill without one."""
    fid = str(fill.get("id") or "")
    prefix, sep, _rest = fid.partition("::")
    return prefix if sep and prefix.isdigit() else None


def _sort_key(fill: dict) -> tuple[str, str]:
    return (fill.get("transaction_time") or "", _numeric_id_prefix(fill) or "")


def _ambiguous_sort_keys(series: list[dict]) -> set[tuple[str, str]]:
    """Full sort keys shared by 2+ fills whose sides differ — a genuine tie
    the walk's order cannot be trusted for.

    Grouping by the *complete* `_sort_key()` (not just `transaction_time`)
    catches two fills that share both the same transaction_time AND the same
    numeric id prefix, differing only in the id's trailing UUID — same
    millisecond, sortable prefix present on both, but still not evidence of
    which one actually came first. Same-side ties (two buys, two sells)
    never change the walk's totals regardless of order, so only mixed-side
    ties matter here. This does NOT catch a *partial*-prefix group (some
    fills have one, some don't) — see `_ambiguous_timestamps_missing_prefix`
    for that case, which ties on `transaction_time` alone instead.
    """
    by_key: dict[tuple[str, str], list[dict]] = {}
    for fill in series:
        by_key.setdefault(_sort_key(fill), []).append(fill)
    return {
        key for key, group in by_key.items()
        if len(group) > 1 and len({f["side"] for f in group}) > 1
    }


def _ambiguous_timestamps_missing_prefix(series: list[dict]) -> set[str]:
    """transaction_time values shared by 2+ fills whose sides differ, where
    at least one of them lacks a parseable id prefix (ChatGPT review R5,
    PROGRESS.md §11: a *partial*-prefix group was previously missed).

    A missing prefix sorts as `""`, which is lower than any real numeric
    prefix — so in a mixed group (one fill with a prefix, one without) the
    prefix-less fill always sorts first. That is an artifact of how empty
    strings compare, not evidence it actually happened first; `_sort_key()`
    alone would silently treat it as resolved. `_ambiguous_sort_keys()`
    doesn't catch this because it only flags a tie on the *complete* key —
    here the keys differ (`(T, "")` vs `(T, "12345")`), so this needs its
    own check keyed on `transaction_time` alone.
    """
    by_ts: dict[str, list[dict]] = {}
    for fill in series:
        by_ts.setdefault(fill.get("transaction_time") or "", []).append(fill)
    return {
        ts for ts, group in by_ts.items()
        if len(group) > 1
        and len({f["side"] for f in group}) > 1
        and not all(_numeric_id_prefix(f) is not None for f in group)
    }


def round_trips(fills: list[dict]) -> list[dict]:
    """fills: newest-first as returned by broker_read.fills(); the walk runs
    chronologically. Returns closed trips first (by close time desc), then
    open trips."""
    by_symbol: dict[str, list[dict]] = {}
    for fill in fills:
        by_symbol.setdefault(fill["symbol"], []).append(fill)
    for series in by_symbol.values():
        series.sort(key=_sort_key)

    trips: list[dict] = []
    for symbol, series in by_symbol.items():
        ambiguous_keys = _ambiguous_sort_keys(series)
        ambiguous_ts_missing_prefix = _ambiguous_timestamps_missing_prefix(series)
        qty = 0.0
        cost = 0.0  # basis of the currently open lot; walks down on each sell (WAC)
        opened_at: str | None = None
        bought_qty = 0.0
        bought_notional = 0.0  # cumulative buy notional for this trip; never decremented
        sold_qty = 0.0
        sold_notional = 0.0
        realized = 0.0
        last_side: str | None = None
        trip_ambiguous = False
        # A run of sells seen while flat (no opening buy in the window), plus
        # any oversold remainder folded in from a partial match below.
        orphan_qty = 0.0
        orphan_notional = 0.0
        orphan_closed_at: str | None = None
        orphan_ambiguous = False

        def _flush_orphan() -> None:
            nonlocal orphan_qty, orphan_notional, orphan_closed_at, orphan_ambiguous
            if orphan_qty > 1e-9:
                trips.append(
                    {
                        "symbol": symbol,
                        "opened_at": None,
                        "closed_at": orphan_closed_at,
                        "qty": round(orphan_qty, 6),
                        "avg_entry": None,
                        "avg_exit": round(orphan_notional / orphan_qty, 4),
                        "realized_pnl": None,
                        "open": False,
                        "incomplete": True,
                        "ambiguous": orphan_ambiguous,
                    }
                )
            orphan_qty, orphan_notional, orphan_closed_at, orphan_ambiguous = 0.0, 0.0, None, False

        for fill in series:
            side = fill["side"]
            price = fill["price"]
            qty_abs = abs(float(fill["qty"]))
            touched_ambiguous = (
                _sort_key(fill) in ambiguous_keys
                or (fill.get("transaction_time") or "") in ambiguous_ts_missing_prefix
            )
            if side == "buy":
                _flush_orphan()
                if qty <= 1e-9 and last_side != "buy":
                    opened_at = fill["transaction_time"]
                    bought_qty = 0.0
                    bought_notional = 0.0
                    sold_qty = 0.0
                    sold_notional = 0.0
                    realized = 0.0
                    trip_ambiguous = False
                elif opened_at is None:
                    opened_at = fill["transaction_time"]
                qty += qty_abs
                cost += qty_abs * price
                bought_qty += qty_abs
                bought_notional += qty_abs * price
                if touched_ambiguous:
                    trip_ambiguous = True
            else:  # sell
                if qty > 1e-9:
                    avg = cost / qty
                    fill_qty = min(qty_abs, qty)
                    realized += (price - avg) * fill_qty
                    sold_notional += price * fill_qty
                    sold_qty += fill_qty
                    remainder = qty_abs - fill_qty
                    qty -= fill_qty
                    cost = avg * max(qty, 0.0)
                    if touched_ambiguous:
                        trip_ambiguous = True
                    if remainder > 1e-9:
                        # Sold more than the tracked lot holds — the excess
                        # must have come from shares this window never saw
                        # bought (same shape as the "no open lot" branch
                        # below); fold it into orphan tracking instead of
                        # letting it vanish.
                        orphan_qty += remainder
                        orphan_notional += remainder * price
                        orphan_closed_at = fill["transaction_time"]
                        if touched_ambiguous:
                            orphan_ambiguous = True
                else:
                    # No open lot to sell against — the window is missing the
                    # opening buy (pre-dates the fills cap, or the position was
                    # established before this system started tracking it).
                    orphan_qty += qty_abs
                    orphan_notional += qty_abs * price
                    orphan_closed_at = fill["transaction_time"]
                    if touched_ambiguous:
                        orphan_ambiguous = True
            last_side = side
            if qty <= 1e-9 and opened_at is not None:
                qty = 0.0
                trips.append(
                    {
                        "symbol": symbol,
                        "opened_at": opened_at,
                        "closed_at": fill["transaction_time"],
                        "qty": round(bought_qty, 6),
                        "avg_entry": round(bought_notional / bought_qty, 4) if bought_qty else None,
                        "avg_exit": round(sold_notional / sold_qty, 4) if sold_qty else None,
                        "realized_pnl": round(realized, 2),
                        "open": False,
                        "ambiguous": trip_ambiguous,
                    }
                )
                qty, cost, opened_at = 0.0, 0.0, None
                bought_qty = bought_notional = sold_qty = sold_notional = realized = 0.0
                trip_ambiguous = False
        _flush_orphan()
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
                    "ambiguous": trip_ambiguous,
                }
            )
    trips.sort(key=lambda t: t["closed_at"] or "", reverse=True)
    return trips


def realized_pnl_timeline(trips: list[dict]) -> list[dict]:
    """Cumulative realized P&L per closed trip, chronological.

    Excludes `ambiguous` trips (order-undetermined, R5) the same way it
    already excludes `incomplete` ones (via `realized_pnl is None`) — a
    number computed from a guessed fill order does not belong in a
    confident running total."""
    closed = sorted(
        (t for t in trips
         if not t["open"] and t.get("realized_pnl") is not None and not t.get("ambiguous")),
        key=lambda t: t["closed_at"] or "",
    )
    running = 0.0
    out: list[dict] = []
    for trip in closed:
        running += float(trip["realized_pnl"] or 0.0)
        out.append({"closed_at": trip["closed_at"], "cum_realized_pnl": round(running, 2),
                    "symbol": trip["symbol"]})
    return out


def reconcile_positions(
    trips: list[dict], broker_positions: list[dict], *, tol: float = 1e-6
) -> list[dict]:
    """Compare fills-derived open lots (round_trips' open=True rows) against
    broker positions, by symbol. Returns one entry per symbol where the two
    sides disagree — present on only one side, or qty differs by more than
    `tol` — plus one `undetermined: True` entry per symbol touched by an
    `ambiguous` trip (R5), regardless of whether its qty happens to match
    the broker's: an order-undetermined fill sequence was never actually
    verified, so it must not be silently folded into "agrees" just because
    the numbers came out matching this time. Empty list means every other
    symbol both sides know about agrees; it does not by itself mean nothing
    is wrong (e.g. a position with zero fills history) — it only reports
    what this comparison can see.

    Pure/observational: never mutates its inputs, callers decide what (if
    anything) to do with the diffs (see AGENTS.md F1 — a real VEEV example).
    """
    lots_by_symbol = {
        t["symbol"]: float(t.get("qty") or 0.0) for t in trips if t.get("open")
    }
    ambiguous_symbols = {t["symbol"] for t in trips if t.get("ambiguous")}
    pos_by_symbol = {
        p["symbol"]: float(p.get("qty") or 0.0) for p in broker_positions if p.get("symbol")
    }
    diffs: list[dict] = []
    for symbol in sorted(set(lots_by_symbol) | set(pos_by_symbol) | ambiguous_symbols):
        fills_qty = lots_by_symbol.get(symbol)
        broker_qty = pos_by_symbol.get(symbol)
        if symbol in ambiguous_symbols:
            diffs.append({
                "symbol": symbol, "fills_qty": fills_qty, "broker_qty": broker_qty,
                "detail": f"{symbol}: fills 里有同一时刻买卖顺序无法确定的回合，账实对账结果无法确定",
                "undetermined": True,
            })
            continue
        if fills_qty is None:
            detail = f"{symbol}: 券商持仓 {broker_qty:g} 股，fills 派生记录里没有对应的未平仓回合"
        elif broker_qty is None:
            detail = f"{symbol}: fills 派生未平仓 {fills_qty:g} 股，但券商持仓中不存在"
        elif abs(fills_qty - broker_qty) > tol:
            detail = f"{symbol}: 数量不一致 —— fills 派生 {fills_qty:g} 股 vs 券商 {broker_qty:g} 股"
        else:
            continue
        diffs.append(
            {"symbol": symbol, "fills_qty": fills_qty, "broker_qty": broker_qty, "detail": detail}
        )
    return diffs
