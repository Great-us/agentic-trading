"""FastAPI app exposing the read-only dashboard API.

Run: python -m agentic_trading.dashboard.api  (binds 127.0.0.1:8600)

Every route is a GET. Journal databases open read-only; broker access is the
GET-only BookBrokerReader. In production mode the built frontend (dist/) is
served from this same process; in dev the Vite server proxies /api here.
"""
from __future__ import annotations

import argparse
import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

from .broker_read import BookBrokerReader
from .compare import normalized_equity, overlap_timeline, rolling_correlation
from .registry import Book, get_book, load_books
from .rosters import latest_rosters
from .trades import realized_pnl_timeline, round_trips
from .views import (
    connect_ro,
    cycles_index,
    decisions as decisions_view,
    equity_series,
    latest_cycle,
    logic_payload,
    outcomes_summary,
    position_snapshot,
)

log = logging.getLogger(__name__)

app = FastAPI(title="Agentic Trading Dashboard", docs_url="/api/docs")
_READERS: dict[str, BookBrokerReader] = {}


def _books() -> list[Book]:
    return load_books()


def _reader(book: Book) -> BookBrokerReader:
    if book.id not in _READERS:
        _READERS[book.id] = BookBrokerReader(book.root)
    return _READERS[book.id]


def _conn(book_id: str):
    book = get_book(_books(), book_id)
    if book is None:
        raise HTTPException(status_code=404, detail=f"unknown book {book_id!r}")
    if not book.db_path.exists():
        raise HTTPException(status_code=404, detail=f"journal not found: {book.db_path}")
    return connect_ro(book.db_path)


def _book_or_404(book_id: str) -> Book:
    found = get_book(_books(), book_id)
    if found is None:
        raise HTTPException(status_code=404, detail=f"unknown book {book_id!r}")
    return found


# ---- books & summary -------------------------------------------------------

@app.get("/api/books")
def list_books() -> JSONResponse:
    out = []
    for book in _books():
        summary: dict = {
            "id": book.id,
            "display_name": book.display_name,
            "kind": book.kind,
            "root": str(book.root),
        }
        if book.db_path.exists():
            conn = connect_ro(book.db_path)
            try:
                cycle = latest_cycle(conn) or {}
                summary.update(
                    {
                        "last_cycle_at": cycle.get("timestamp"),
                        "equity": cycle.get("equity"),
                        "cash": cycle.get("cash"),
                        "regime_label": cycle.get("regime_label"),
                        "regime_score": cycle.get("regime_score"),
                    }
                )
            finally:
                conn.close()
        positions, reason = _reader(book).positions()
        summary["open_positions"] = len(positions) if positions is not None else None
        if positions is None and reason:
            summary["broker_error"] = reason
        out.append(summary)
    return JSONResponse({"books": out})


# ---- per-book journal views -------------------------------------------------

@app.get("/api/books/{book_id}/equity")
def get_equity(book_id: str, days: int = 180) -> JSONResponse:
    conn = _conn(book_id)
    try:
        return JSONResponse({"series": equity_series(conn, days=days)})
    finally:
        conn.close()


@app.get("/api/books/{book_id}/cycles")
def get_cycles(book_id: str, limit: int = 50) -> JSONResponse:
    conn = _conn(book_id)
    try:
        return JSONResponse({"cycles": cycles_index(conn, limit=min(limit, 500))})
    finally:
        conn.close()


@app.get("/api/books/{book_id}/decisions")
def get_decisions(book_id: str, cycle_id: int | None = None, symbol: str | None = None,
                  limit: int = 200) -> JSONResponse:
    conn = _conn(book_id)
    try:
        rows = decisions_view(conn, cycle_id=cycle_id, symbol=symbol, limit=min(limit, 1000))
        return JSONResponse({"decisions": rows})
    finally:
        conn.close()


@app.get("/api/books/{book_id}/signal-outcomes")
def get_outcomes(book_id: str) -> JSONResponse:
    conn = _conn(book_id)
    try:
        return JSONResponse({"buckets": outcomes_summary(conn)})
    finally:
        conn.close()


@app.get("/api/books/{book_id}/logic")
def get_logic(book_id: str) -> JSONResponse:
    book = _book_or_404(book_id)
    return JSONResponse(logic_payload(book.config_dir))


# ---- per-book broker views (read-only Alpaca) -------------------------------

@app.get("/api/books/{book_id}/positions")
def get_positions(book_id: str) -> JSONResponse:
    book = _book_or_404(book_id)
    live, reason = _reader(book).positions()
    stops, _ = _reader(book).open_stop_orders()
    stop_by_symbol = {s["symbol"]: s for s in (stops or [])}
    conn = _conn(book_id)
    try:
        journal_rows = position_snapshot(conn)
    finally:
        conn.close()
    peaks = {row["symbol"]: row.get("high_water_mark") for row in journal_rows}
    out = []
    if live is not None:
        for pos in live:
            symbol = pos["symbol"]
            stop = stop_by_symbol.get(symbol, {})
            peak = peaks.get(symbol)
            current = pos["current_price"]
            out.append(
                {
                    **pos,
                    "stop_price": stop.get("stop_price"),
                    "high_water_mark": peak,
                    "stop_distance_pct": (
                        round((current - stop["stop_price"]) / current, 4)
                        if stop.get("stop_price") and current else None
                    ),
                    "giveback_pct": (
                        round((current - peak) / peak, 4) if peak and current else None
                    ),
                    "source": "broker",
                }
            )
        return JSONResponse({"positions": out})
    # No credentials / API failure → journal snapshot only, clearly labelled.
    for row in journal_rows:
        out.append({**row, "source": "journal"})
    return JSONResponse(
        {"positions": out, "degraded": True, "reason": reason or "broker unavailable"}
    )


@app.get("/api/books/{book_id}/trades")
def get_trades(book_id: str) -> JSONResponse:
    book = _book_or_404(book_id)
    fills, reason = _reader(book).fills()
    if fills is None:
        return JSONResponse(
            {"trades": [], "timeline": [], "fills": [], "degraded": True,
             "reason": reason or "broker unavailable"}, status_code=200,
        )
    trips = round_trips(fills)
    return JSONResponse(
        {"trades": trips, "timeline": realized_pnl_timeline(trips), "fills": fills[:300]}
    )


# ---- P2 rotation roster ------------------------------------------------------

@app.get("/api/books/{book_id}/roster")
def get_roster(book_id: str) -> JSONResponse:
    book = _book_or_404(book_id)
    if book.kind != "rs_rotation":
        raise HTTPException(status_code=404, detail=f"{book_id} is not a rotation book")
    return JSONResponse(latest_rosters(book.rosters_dir))


# ---- cross-book comparison ---------------------------------------------------

@app.get("/api/compare")
def compare_books(days: int = 180) -> JSONResponse:
    books = {b.id: b for b in _books()}
    ids = [i for i in ("p1", "p2") if i in books] or sorted(books)
    series_map: dict[str, list[dict]] = {}
    fills_map: dict[str, list[dict]] = {}
    for bid in ids:
        conn = _conn(bid)
        try:
            series_map[bid] = equity_series(conn, days=max(days, 250))
        finally:
            conn.close()
        fills, _ = _reader(books[bid]).fills()
        fills_map[bid] = fills or []
    p1, p2 = ids[0], ids[-1]
    return JSONResponse(
        {
            "normalized_equity": normalized_equity(series_map[p1], series_map[p2], days=days),
            "overlap": overlap_timeline(fills_map[p1], fills_map[p2]),
            "rolling_correlation": rolling_correlation(series_map[p1], series_map[p2]),
            "note": "重叠度与滚动相关对应 PAPER2-THEME-ROTATION.md §5 的增量信息检验",
        }
    )


# ---- static frontend (production mode) ----------------------------------------
# A plain StaticFiles mount would 404 on client-side routes like /p1/trades
# (no refresh/deep-link support), so serve assets explicitly with an index.html
# fallback instead. API routes above are registered first and win.
_DIST = Path(__file__).resolve().parent / "frontend" / "dist"
if _DIST.is_dir():
    from fastapi.responses import FileResponse

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa(full_path: str):
        target = (_DIST / full_path) if full_path else _DIST / "index.html"
        if not str(target.resolve()).startswith(str(_DIST.resolve())):
            raise HTTPException(status_code=404)
        if target.is_file():
            return FileResponse(target)
        return FileResponse(_DIST / "index.html")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8600)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
