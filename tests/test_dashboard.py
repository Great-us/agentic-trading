"""Tests for the read-only dashboard package.

The API tests build a temporary two-book registry with seeded journals and a
monkeypatched broker reader, so no Alpaca credentials or real journals are
touched.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from agentic_trading.dashboard import compare, registry, rosters, trades



# ---- fixtures ----------------------------------------------------------------

ROSTER_TEXT = """# Roster 2026-08-25 (midday round, 2026-08-25 16:30 UTC)

pool=251 scored | Top-10 | buyable(streak>=2)=['CHYM', 'CORT', 'EAT', 'NIQ', 'TXG', 'U'] | held=['EAT', 'PAY', 'WDAY', 'ZETA']

| rank | symbol | RS | ex_20d | ex_60d | last | streak | buyable |
|---:|---|---:|---:|---:|---:|---:|---|
| 1 | NIQ | 99.6 | +59.9% | +127.5% | 19.10 | 5 | yes |
| 2 | TXG | 99.2 | +36.6% | +131.1% | 65.83 | 5 | yes |
| 6 | HALO | 96.2 | +27.3% | +62.3% | 109.00 | 1 | no |

watchlist diff vs previous: +[HALO] -[VEEV]
"""


def _fill(symbol: str, side: str, qty: float, price: float, ts: str) -> dict:
    return {"symbol": symbol, "side": side, "qty": qty, "price": price,
            "notional": qty * price, "transaction_time": ts, "order_status": "filled"}


@pytest.fixture()
def book_env(tmp_path: Path) -> Path:
    """Two fake book roots, each with data/journal.db (seeded) and config/."""
    from datetime import datetime, timezone

    from agentic_trading.journal.logger import (
        DecisionRow, TradeIntent, connect, record_cycle, record_intent_event,
        record_position_state, save_trade_intent, update_position_peak,
    )

    roots = []
    for i, name in enumerate(("p1", "p2")):
        root = tmp_path / f"book-{name}"
        (root / "data").mkdir(parents=True)
        (root / "config").mkdir()
        conn = connect(root / "data" / "journal.db")
        rows = [
            DecisionRow(symbol="MSFT", quant_score=0.42, llm_stance="bullish",
                        llm_confidence=0.7, llm_rationale="solid trend",
                        combined_score=0.5, action="BUY", reasoning="ok"),
            DecisionRow(symbol="AAPL", quant_score=-0.3, llm_stance=None,
                        llm_confidence=None, llm_rationale=None,
                        combined_score=-0.3, action="HOLD", reasoning="weak"),
        ]
        record_cycle(conn, "2026-08-20T14:00:00+00:00", "paper", 10000.0 + i, 500.0,
                     rows, regime_score=0.1, regime_label="neutral")
        record_cycle(conn, "2026-08-21T14:00:00+00:00", "paper", 10100.0 + i, 400.0,
                     rows[:1], regime_score=0.15, regime_label="neutral")
        wait_row = DecisionRow(
            symbol="ANET", quant_score=0.4, llm_stance=None, llm_confidence=None,
            llm_rationale=None, combined_score=0.4, action="WAIT",
            reasoning="Skipped: the analyst produced no verdict for this symbol "
                      "(LLM failed mid-cycle) and require_llm_for_entry is on — "
                      "new entries fail closed.",
        )
        record_cycle(conn, "2026-08-21T20:15:00+00:00", "paper", 10120.0 + i, 400.0,
                     [wait_row], regime_score=0.0, regime_label="neutral")
        save_trade_intent(conn, TradeIntent(
            symbol="ANET", created_at="2026-08-21T13:45:00+00:00",
            signal_price=140.0, atr14=4.0, quant_score=0.39, combined_score=0.53,
            reasoning="queued", not_before="2026-08-21T14:00:00+00:00",
        ))
        record_intent_event(
            conn, "2026-08-21T14:04:00+00:00", "ANET", "sizing",
            deferred=True,
            detail="only $0 available (limited by the exposure cap — invested 65.2% of a 65% ceiling)",
        )
        record_position_state(conn, "MSFT", 2.0, 920.0, "2026-08-21T20:15:00+00:00")
        update_position_peak(conn, "MSFT", 470.0, "2026-08-21T20:15:00+00:00")
        stamp = datetime.now(timezone.utc).isoformat()
        (root / "data" / "heartbeat.json").write_text(
            json.dumps({
                "deep": {"timestamp": stamp, "cycle_id": 3, "stops_covered": 1, "positions": 1},
                "fast": {"timestamp": stamp, "cycle_id": 3, "stops_covered": 1, "positions": 1},
            }),
            encoding="utf-8",
        )
        conn.close()

        with open(root / "config" / "risk.yaml", "w", encoding="utf-8") as f:
            yaml.safe_dump({"buy_threshold": 0.25, "max_open_positions": 8}, f)
        with open(root / "config" / "watchlist.yaml", "w", encoding="utf-8") as f:
            yaml.safe_dump({"symbols": ["MSFT", "AAPL"], "context_symbols": ["SPY"]}, f)
        roots.append(root)

    reg = tmp_path / "dashboard.yaml"
    with open(reg, "w", encoding="utf-8") as f:
        yaml.safe_dump({
            "books": [
                {"id": "p1", "display_name": "一号盘", "kind": "fixed_pool", "root": str(roots[0])},
                {"id": "p2", "display_name": "二号盘", "kind": "rs_rotation", "root": str(roots[1])},
            ],
        }, f)
    return reg


@pytest.fixture()
def client(book_env: Path, monkeypatch):
    import agentic_trading.dashboard.api as api_mod

    def fake_load_books():
        return registry.load_books(book_env)

    monkeypatch.setattr(api_mod, "load_books", fake_load_books)

    class FakeReader:
        def __init__(self, *args, **kwargs): ...
        def account(self): return {"equity": 10000.0, "cash": 500.0, "invested": 9500.0}, None
        def positions(self):
            return [{"symbol": "MSFT", "qty": 2.0, "avg_entry_price": 450.0,
                     "current_price": 460.0, "market_value": 920.0,
                     "unrealized_pl": 20.0, "unrealized_plpc": 0.02}], None
        def open_stop_orders(self):
            return [{"symbol": "MSFT", "stop_price": 430.0, "submitted_at": "2026-08-21T14:00:00Z"}], None
        def fills(self, max_records: int = 1000):
            return [
                _fill("MSFT", "buy", 2.0, 450.0, "2026-08-21T14:05:00Z"),
                _fill("AAPL", "sell", 1.0, 190.0, "2026-08-20T15:00:00Z"),
                _fill("AAPL", "buy", 1.0, 180.0, "2026-08-19T15:00:00Z"),
            ], None
        def clock(self):
            return {"is_open": False, "timestamp": "2026-08-21T20:00:00Z",
                    "next_open": None, "next_close": None}, None

    monkeypatch.setattr(api_mod, "_READERS", {})
    monkeypatch.setattr(api_mod, "_reader", lambda book: FakeReader())
    return TestClient(api_mod.app)


# ---- pure-function units ------------------------------------------------------

class TestRoundTrips:
    def test_closed_trip_realized_pnl(self):
        fills = [
            _fill("X", "sell", 1.0, 110.0, "2026-01-03T15:00:00Z"),
            _fill("X", "buy", 1.0, 100.0, "2026-01-02T15:00:00Z"),
        ]
        trips = trades.round_trips(fills)
        assert len(trips) == 1 and not trips[0]["open"]
        assert trips[0]["realized_pnl"] == pytest.approx(10.0)

    def test_partial_sell_keeps_lot_open(self):
        fills = [_fill("Y", "buy", 2.0, 100.0, "2026-01-02T15:00:00Z"),
                 _fill("Y", "sell", 1.0, 120.0, "2026-01-03T15:00:00Z")]
        trips = trades.round_trips(fills)
        assert len(trips) == 1 and trips[0]["open"]
        assert trips[0]["qty"] == pytest.approx(1.0)
        assert trips[0]["realized_pnl"] == pytest.approx(20.0)

    def test_timeline_cumulative(self):
        fills = [
            _fill("A", "buy", 1.0, 10.0, "2026-01-02T15:00:00Z"),
            _fill("A", "sell", 1.0, 12.0, "2026-01-03T15:00:00Z"),
            _fill("B", "buy", 1.0, 10.0, "2026-01-04T15:00:00Z"),
            _fill("B", "sell", 1.0, 11.5, "2026-01-06T15:00:00Z"),
        ]
        timeline = trades.realized_pnl_timeline(trades.round_trips(fills))
        assert [point["cum_realized_pnl"] for point in timeline] == [pytest.approx(2.0), pytest.approx(3.5)]


class TestCompare:
    def test_overlap_full_when_identical(self):
        fills = [_fill("A", "buy", 1.0, 10.0, "2026-01-02T15:00:00Z")]
        overlap = compare.overlap_timeline(fills, fills)
        assert all(point["overlap"] == 1.0 for point in overlap)

    def test_overlap_zero_when_disjoint(self):
        fills_a = [_fill("A", "buy", 1.0, 10.0, "2026-01-02T15:00:00Z")]
        fills_b = [_fill("B", "buy", 1.0, 10.0, "2026-01-02T15:00:00Z")]
        overlap = compare.overlap_timeline(fills_a, fills_b)
        assert all(point["overlap"] == 0.0 for point in overlap)

    def test_perfectly_correlated_returns_give_corr_one(self):
        series_a = [{"date": f"2026-01-{d:02d}", "equity": float(d * 10)} for d in range(1, 30)]
        series_b = [{"date": f"2026-01-{d:02d}", "equity": float(d * 7)} for d in range(1, 30)]
        corr = compare.rolling_correlation(series_a, series_b, window=10)
        assert corr and corr[-1]["correlation"] is not None
        assert abs(corr[-1]["correlation"]) > 0.99

    def test_normalized_equity_rebases_to_one(self):
        series = [{"date": "2026-01-01", "equity": 50.0}, {"date": "2026-01-02", "equity": 75.0}]
        result = compare.normalized_equity(series, series, days=10)
        assert result["p1"][0]["value"] == 1.0 and result["p2"][0]["value"] == 1.0


class TestRosters:
    def test_parse_real_format(self):
        parsed = rosters.parse_roster(ROSTER_TEXT)
        assert parsed["round"] == "midday"
        assert parsed["pool_size"] == 251
        assert parsed["held"] == ["EAT", "PAY", "WDAY", "ZETA"]
        assert parsed["added"] == ["HALO"] and parsed["removed"] == ["VEEV"]
        assert len(parsed["rows"]) == 3
        first = parsed["rows"][0]
        assert first["symbol"] == "NIQ" and first["rs"] == 99.6 and first["buyable"] is True
        assert parsed["rows"][2]["buyable"] is False

    def test_latest_rosters_missing_dir(self, tmp_path: Path):
        payload = rosters.latest_rosters(tmp_path / "nope")
        assert payload["available"] is False


def test_registry_rejects_duplicate_ids(tmp_path: Path):
    reg = tmp_path / "bad.yaml"
    root = tmp_path / "r"
    (root / "data").mkdir(parents=True)
    with open(reg, "w", encoding="utf-8") as f:
        yaml.safe_dump({"books": [
            {"id": "x", "root": str(root)}, {"id": "x", "root": str(root)},
        ]}, f)
    with pytest.raises(ValueError, match="duplicate"):
        registry.load_books(reg)


def test_missed_sessions_ignores_weekends():
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo

    from agentic_trading.dashboard.views import book_health, missed_sessions

    et = ZoneInfo("America/New_York")
    friday = datetime(2026, 8, 28, 20, 15, tzinfo=timezone.utc)
    saturday = datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc)
    sunday_night_et = datetime(2026, 8, 30, 21, 0, tzinfo=et)
    monday = datetime(2026, 8, 31, 13, 45, tzinfo=timezone.utc)
    assert missed_sessions(friday, saturday) == 0
    assert missed_sessions(friday, sunday_night_et) == 0
    assert missed_sessions(friday, monday) == 1  # Monday is a session after Friday


def test_book_health_weekend_is_not_ok(tmp_path):
    from datetime import datetime, timezone

    from agentic_trading.dashboard.views import book_health

    stamp = datetime(2026, 8, 28, 20, 15, tzinfo=timezone.utc).isoformat()
    path = tmp_path / "heartbeat.json"
    path.write_text(
        json.dumps({
            "deep": {"timestamp": stamp, "cycle_id": 1, "stops_covered": 5, "positions": 5},
            "fast": {"timestamp": stamp, "cycle_id": 1, "stops_covered": 5, "positions": 5},
        }),
        encoding="utf-8",
    )
    sunday = datetime(2026, 8, 30, 18, 0, tzinfo=timezone.utc)
    health = book_health(path, now=sunday)
    assert health["status"] == "weekend"
    assert health["message"] != "周期在跑"


def test_book_health_missing_file_keeps_deep_only_flag(tmp_path):
    from agentic_trading.dashboard.views import book_health

    health = book_health(tmp_path / "nope.json", deep_only=True)
    assert health["status"] == "missing"
    assert health["deep_only"] is True


def test_book_health_deep_only_ignores_missing_fast(tmp_path):
    from datetime import datetime, timezone, timedelta

    from agentic_trading.dashboard.views import book_health
    from agentic_trading.heartbeat import DEEP_MAX_AGE_HOURS

    now = datetime(2026, 9, 9, 18, 0, tzinfo=timezone.utc)
    path = tmp_path / "heartbeat.json"
    path.write_text(
        json.dumps({
            "deep": {"timestamp": now.isoformat(), "cycle_id": 1,
                     "stops_covered": 2, "positions": 2},
        }),
        encoding="utf-8",
    )
    with_fast = book_health(path, now=now)
    assert with_fast["status"] == "missing"  # no fast stamp
    deep_only = book_health(path, now=now, deep_only=True)
    assert deep_only["status"] == "ok"
    assert deep_only["deep_only"] is True
    stale = book_health(
        path,
        now=now + timedelta(hours=DEEP_MAX_AGE_HOURS + 1),
        deep_only=True,
    )
    assert stale["status"] == "stale"
    assert "快" not in stale["message"]


def test_analyzed_progress_running_without_cycle_start_in_tail():
    import sqlite3

    from agentic_trading.dashboard.live import analyzed_progress

    events = [{"stage": "quant", "symbol": f"S{i}", "cycle": 9} for i in range(90)]
    events[-1] = {"stage": "decide", "symbol": "ZZZ", "cycle": 9, "action": "hold"}
    conn = sqlite3.connect(":memory:")
    try:
        progress = analyzed_progress(conn, ["AAA"], events)
    finally:
        conn.close()
    assert progress["running"] is True
    assert progress["cycle"] == 9
    assert progress["stage"] == "decide"


# ---- HTTP layer -----------------------------------------------------------------

class TestApi:
    def test_books_summary(self, client: TestClient):
        body = client.get("/api/books").json()
        ids = [b["id"] for b in body["books"]]
        assert ids == ["p1", "p2"]
        p1 = body["books"][0]
        assert p1["open_positions"] == 1 and p1["regime_label"] == "neutral"

    def test_equity_series_has_drawdown(self, client: TestClient):
        series = client.get("/api/books/p1/equity").json()["series"]
        assert len(series) == 2
        assert series[-1]["drawdown"] == 0.0

    def test_decisions_filter_by_symbol(self, client: TestClient):
        rows = client.get("/api/books/p1/decisions?symbol=aapl").json()["decisions"]
        assert len(rows) == 1 and rows[0]["action"] == "HOLD"

    def test_positions_merge_stops_and_peaks(self, client: TestClient):
        positions = client.get("/api/books/p1/positions").json()["positions"]
        msft = next(p for p in positions if p["symbol"] == "MSFT")
        assert msft["source"] == "broker"
        assert msft["stop_distance_pct"] == pytest.approx((460.0 - 430.0) / 460.0, abs=1e-4)

    def test_trades_from_fills(self, client: TestClient):
        body = client.get("/api/books/p1/trades").json()
        symbols = {t["symbol"]: t for t in body["trades"]}
        assert not symbols["AAPL"]["open"]
        assert symbols["AAPL"]["realized_pnl"] == pytest.approx(10.0)
        assert symbols["MSFT"]["open"]

    def test_roster_only_for_rotation_book(self, client: TestClient):
        assert client.get("/api/books/p1/roster").status_code == 404
        # p2 has no rosters dir → available:false rather than an error.
        body = client.get("/api/books/p2/roster").json()
        assert body["available"] is False

    def test_logic_payload(self, client: TestClient):
        logic = client.get("/api/books/p1/logic").json()
        assert logic["watchlist"] == ["MSFT", "AAPL"]
        assert logic["context_symbols"] == ["SPY"]
        assert logic["risk"]["buy_threshold"] == 0.25
        assert len(logic["pipeline"]) == 8

    def test_unknown_book_404(self, client: TestClient):
        assert client.get("/api/books/nope/equity").status_code == 404

    def test_compare_endpoint(self, client: TestClient):
        body = client.get("/api/compare").json()
        assert set(body.keys()) >= {"normalized_equity", "overlap", "rolling_correlation"}

    def test_health_reads_heartbeat(self, client: TestClient):
        body = client.get("/api/books/p1/health").json()
        assert body["status"] in {"ok", "weekend"}
        assert body["deep"]["stops_covered"] == 1
        assert body["limits"]["deep_hours"] == 26

    def test_intents_and_vetoes(self, client: TestClient):
        intents = client.get("/api/books/p1/intents").json()["intents"]
        assert intents and intents[0]["symbol"] == "ANET"
        assert intents[0]["status"] in {"pending", "waiting", "window_open", "expired"}
        vetoes = client.get("/api/books/p1/vetoes").json()["vetoes"]
        labels = [v["label"] for v in vetoes]
        assert any("fail closed" in v["label"] or "缺 LLM" in v["label"] for v in vetoes)
        sizing = next(v for v in vetoes if v.get("kind") == "sizing")
        assert sizing["total"] >= 1
        assert any("intent" in lab.lower() or "排队" in lab for lab in labels)

    def test_live_signals_does_not_fetch_prices(self, client: TestClient, monkeypatch):
        def boom(*_a, **_k):
            raise AssertionError("fetch_price_history must not run from the dashboard")
        monkeypatch.setattr("agentic_trading.data.market_data.fetch_price_history", boom)
        body = client.get("/api/books/p1/live/signals").json()
        assert "signals" in body
        assert body["note"]

    def test_live_stream_once_replays(self, client: TestClient):
        response = client.get("/api/books/p1/live/stream?once=1")
        assert response.status_code == 200
        assert "text/event-stream" in response.headers["content-type"]
        assert "event: replay" in response.text
        assert "event: meta" in response.text

    def test_today_uses_broker_fills_and_splits_pool(self, client: TestClient):
        body = client.get("/api/books/p1/today").json()
        assert body["session_date"] == "2026-08-21"
        assert body["fail_closed_today"] >= 1
        assert any(f["symbol"] == "MSFT" and f["side"] == "buy" for f in body["fills_today"])
        msft = next(h for h in body["holdings"] if h["symbol"] == "MSFT")
        assert msft["bucket"] == "pool"
        assert msft["in_pool"] is True
        anet = next(i for i in body["intents"] if i["symbol"] == "ANET")
        assert anet["quant_score"] == pytest.approx(0.39)
        buckets = {b["action"]: b for b in body["outcomes"]}
        # per-horizon counts are present even when zero
        assert "n_1d" in next(iter(buckets.values()), {"n_1d": 0}) or body["outcomes"] == []
        assert body["progress"]["present"] is False
        missing = client.get("/api/books/p1/progress").json()
        assert missing["present"] is False


    def test_today_includes_handoff_card(self, client: TestClient, book_env: Path):
        from datetime import datetime, timezone

        from agentic_trading.dashboard.registry import load_books
        from agentic_trading.progress import emit_progress

        books = load_books(book_env)
        p1 = books[0]
        emit_progress(
            book_id="p1", round_name="fast", status="ok",
            did=["no orders"], path=p1.root / "data" / "progress.json",
            now=datetime(2026, 9, 9, 14, 0, tzinfo=timezone.utc),
        )
        body = client.get("/api/books/p1/today").json()
        assert body["progress"]["present"] is True
        assert body["progress"]["card"]["round"]["name"] == "fast"
        assert body["progress"]["card"]["next_job"]["instruction"]
        assert client.get("/api/books/p1/progress").json()["present"] is True
