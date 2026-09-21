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


# P1-B-4: book_health now distinguishes a live-scanner failure (fast hasn't
# ticked during market hours) from the market simply being shut, instead of
# lumping both under state="weekend" (P1-B-3's finding). Six scenarios below:
# intraday timeout, intraday normal, after-hours normal, weekend, a whole
# weekday with no heartbeat at all, and pre-P0-B-3 stamps without the new
# lateness fields.

def test_book_health_intraday_fast_stall_is_a_real_alert(tmp_path):
    """Fast hasn't ticked in over FAST_MAX_AGE_MINUTES while we're squarely
    inside the trading-hours window — that's the scanner itself stalling,
    not the market being closed, and must read as an alert."""
    from datetime import datetime, timedelta, timezone

    from agentic_trading.dashboard.views import book_health
    from agentic_trading.heartbeat import FAST_MAX_AGE_MINUTES

    wednesday = datetime(2026, 9, 9, 17, 0, tzinfo=timezone.utc)  # 13:00 ET, mid-session
    fast_stamp = wednesday - timedelta(minutes=FAST_MAX_AGE_MINUTES + 25)  # same day, over limit
    deep_stamp = wednesday - timedelta(minutes=10)  # fresh
    path = tmp_path / "heartbeat.json"
    path.write_text(
        json.dumps({
            "deep": {"timestamp": deep_stamp.isoformat(), "cycle_id": 1,
                     "stops_covered": 2, "positions": 2},
            "fast": {"timestamp": fast_stamp.isoformat(), "cycle_id": 5,
                     "stops_covered": 2, "positions": 2},
        }),
        encoding="utf-8",
    )
    health = book_health(path, now=wednesday)
    assert health["deep"]["state"] == "ok"
    assert health["fast"]["missed_sessions"] == 0  # not a missed trading day
    assert health["fast"]["age_seconds"] >= (FAST_MAX_AGE_MINUTES + 25) * 60
    assert health["fast"]["state"] == "stale_intraday"
    assert health["status"] == "stale_intraday"


def test_book_health_intraday_normal(tmp_path):
    """Same trading-hours window, but fast is well within budget: ok, not
    conflated with any of the other states."""
    from datetime import datetime, timedelta, timezone

    from agentic_trading.dashboard.views import book_health

    wednesday = datetime(2026, 9, 9, 17, 0, tzinfo=timezone.utc)  # 13:00 ET
    fast_stamp = wednesday - timedelta(minutes=5)
    deep_stamp = wednesday - timedelta(minutes=10)
    path = tmp_path / "heartbeat.json"
    path.write_text(
        json.dumps({
            "deep": {"timestamp": deep_stamp.isoformat(), "cycle_id": 1,
                     "stops_covered": 2, "positions": 2},
            "fast": {"timestamp": fast_stamp.isoformat(), "cycle_id": 5,
                     "stops_covered": 2, "positions": 2},
        }),
        encoding="utf-8",
    )
    health = book_health(path, now=wednesday)
    assert health["deep"]["state"] == "ok"
    assert health["fast"]["state"] == "ok"
    assert health["status"] == "ok"


def test_book_health_after_hours_is_not_an_alert(tmp_path):
    """Weekday evening, well outside the 09:35-16:05 ET window: fast being
    old is expected (it doesn't run outside the window at all) and must not
    be reported as an alert as long as deep itself is still fresh."""
    from datetime import datetime, timedelta, timezone

    from agentic_trading.dashboard.views import book_health

    wednesday_evening = datetime(2026, 9, 9, 23, 0, tzinfo=timezone.utc)  # 19:00 ET
    deep_stamp = wednesday_evening - timedelta(hours=3)  # this afternoon's deep cycle
    fast_stamp = wednesday_evening - timedelta(hours=7)  # last fast tick before the window closed
    path = tmp_path / "heartbeat.json"
    path.write_text(
        json.dumps({
            "deep": {"timestamp": deep_stamp.isoformat(), "cycle_id": 1,
                     "stops_covered": 2, "positions": 2},
            "fast": {"timestamp": fast_stamp.isoformat(), "cycle_id": 20,
                     "stops_covered": 2, "positions": 2},
        }),
        encoding="utf-8",
    )
    health = book_health(path, now=wednesday_evening)
    assert health["deep"]["state"] == "after_hours"
    assert health["fast"]["state"] == "after_hours"
    assert health["status"] == "after_hours"


def test_book_health_weekday_dark_all_day_is_ambiguous_not_a_hard_alert(tmp_path):
    """A weekday with zero heartbeat activity (holiday, or a real failure —
    without a market-holiday calendar there is no way to tell) must read as
    closed_or_holiday with a note, not silently as "ok" and not as a hard
    "stale" alert — deep is still under DEEP_MAX_AGE_HOURS old, so this is
    the ambiguous case, not the "actually broken" one."""
    from datetime import datetime, timedelta, timezone

    from agentic_trading.dashboard.views import book_health
    from agentic_trading.heartbeat import DEEP_MAX_AGE_HOURS

    # Monday 20:00 UTC deep stamp; Tuesday 16:00 UTC (12:00 ET, well inside
    # the fast window — this is not a "before today's first slot" case) "now"
    # — 20h elapsed (< DEEP_MAX_AGE_HOURS), but the calendar date rolled over
    # a weekday.
    assert DEEP_MAX_AGE_HOURS > 20
    monday_stamp = datetime(2026, 9, 7, 20, 0, tzinfo=timezone.utc)
    tuesday_now = datetime(2026, 9, 8, 16, 0, tzinfo=timezone.utc)
    path = tmp_path / "heartbeat.json"
    path.write_text(
        json.dumps({
            "deep": {"timestamp": monday_stamp.isoformat(), "cycle_id": 1,
                     "stops_covered": 2, "positions": 2},
            "fast": {"timestamp": monday_stamp.isoformat(), "cycle_id": 5,
                     "stops_covered": 2, "positions": 2},
        }),
        encoding="utf-8",
    )
    health = book_health(path, now=tuesday_now)
    assert health["deep"]["missed_sessions"] == 1
    assert health["deep"]["state"] == "closed_or_holiday"
    assert health["fast"]["state"] == "closed_or_holiday"
    assert health["status"] == "closed_or_holiday"
    assert health["note"] == "无交易所日历，可能是假日也可能是漏跑"


def test_book_health_weekday_dark_all_day_escalates_once_deep_is_truly_overdue(tmp_path):
    """Same missed-weekday shape, but now deep itself has also blown past
    DEEP_MAX_AGE_HOURS — that's not ambiguous anymore, it's stale."""
    from datetime import datetime, timedelta, timezone

    from agentic_trading.dashboard.views import book_health
    from agentic_trading.heartbeat import DEEP_MAX_AGE_HOURS

    # +6h beyond the bare DEEP_MAX_AGE_HOURS threshold specifically to land
    # after today's first scheduled slot (09:35 ET) — otherwise "today hasn't
    # started yet" would (correctly, per the fix above) suppress the missed
    # count and this test would stop meaning what it says.
    monday_stamp = datetime(2026, 9, 7, 8, 0, tzinfo=timezone.utc)
    tuesday_now = monday_stamp + timedelta(hours=DEEP_MAX_AGE_HOURS + 6)
    path = tmp_path / "heartbeat.json"
    path.write_text(
        json.dumps({
            "deep": {"timestamp": monday_stamp.isoformat(), "cycle_id": 1,
                     "stops_covered": 2, "positions": 2},
        }),
        encoding="utf-8",
    )
    health = book_health(path, now=tuesday_now, deep_only=True)
    assert health["deep"]["missed_sessions"] >= 1
    assert health["deep"]["state"] == "stale"
    assert health["status"] == "stale"


# ---- R7 (ChatGPT review, PROGRESS.md §9): a fresh deep stamp today must not
# ---- mask a genuine same-day fast stall as "maybe a holiday", and a quiet
# ---- pre-market morning must not be misread as a missed trading day. -----

def test_book_health_fresh_deep_today_unmasks_stale_fast_from_yesterday(tmp_path):
    """deep already ran today (so today is demonstrably live, not a
    holiday); fast's last tick is still dated yesterday and we're squarely
    inside the fast window right now. That combination must read as
    stale_intraday — a real live-scanner alert — not closed_or_holiday."""
    from datetime import datetime, timezone

    from agentic_trading.dashboard.views import book_health

    yesterday_fast = datetime(2026, 9, 17, 19, 0, tzinfo=timezone.utc)   # Thu 15:00 ET
    today_deep = datetime(2026, 9, 18, 13, 45, tzinfo=timezone.utc)      # Fri 09:45 ET
    now = datetime(2026, 9, 18, 17, 0, tzinfo=timezone.utc)              # Fri 13:00 ET, in window
    path = tmp_path / "heartbeat.json"
    path.write_text(
        json.dumps({
            "deep": {"timestamp": today_deep.isoformat(), "cycle_id": 10,
                     "stops_covered": 4, "positions": 4},
            "fast": {"timestamp": yesterday_fast.isoformat(), "cycle_id": 40,
                     "stops_covered": 4, "positions": 4},
        }),
        encoding="utf-8",
    )
    health = book_health(path, now=now)
    assert health["deep"]["state"] == "ok"
    assert health["fast"]["state"] == "stale_intraday"
    assert health["status"] == "stale_intraday"


def test_book_health_before_todays_first_slot_is_not_a_missed_day(tmp_path):
    """Quiet pre-market morning: yesterday's stamps are still under their
    max-age budgets, and today's first scheduled slot (09:35 ET) hasn't
    happened yet. This must not be reported as a missed trading day."""
    from datetime import datetime, timezone

    from agentic_trading.dashboard.views import book_health

    yesterday_evening = datetime(2026, 9, 17, 21, 0, tzinfo=timezone.utc)  # Thu 17:00 ET
    pre_market = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)         # Fri 08:00 ET, before 09:35
    path = tmp_path / "heartbeat.json"
    path.write_text(
        json.dumps({
            "deep": {"timestamp": yesterday_evening.isoformat(), "cycle_id": 1,
                     "stops_covered": 4, "positions": 4},
            "fast": {"timestamp": yesterday_evening.isoformat(), "cycle_id": 30,
                     "stops_covered": 4, "positions": 4},
        }),
        encoding="utf-8",
    )
    health = book_health(path, now=pre_market)
    assert health["deep"]["missed_sessions"] == 0
    assert health["fast"]["missed_sessions"] == 0
    assert health["deep"]["state"] != "closed_or_holiday"
    assert health["fast"]["state"] != "closed_or_holiday"
    assert health["status"] not in {"closed_or_holiday", "stale"}


def test_book_health_surfaces_stops_unknown(tmp_path):
    """P0-B-2/R2: a cycle that couldn't read open orders writes
    stops_covered=None + stops_unknown=True instead of pretending coverage
    was fine. book_health must surface that explicitly, not just leave
    stops_covered blank the same way an old pre-P0-B-2 stamp would."""
    from datetime import datetime, timezone

    from agentic_trading.dashboard.views import book_health

    now = datetime(2026, 9, 9, 17, 0, tzinfo=timezone.utc)  # Wed 13:00 ET
    path = tmp_path / "heartbeat.json"
    path.write_text(
        json.dumps({
            "deep": {"timestamp": now.isoformat(), "cycle_id": 1,
                     "stops_covered": None, "positions": 4,
                     "stops_unknown": True, "stops_unknown_reason": "open orders unreadable"},
        }),
        encoding="utf-8",
    )
    health = book_health(path, now=now, deep_only=True)
    assert health["deep"]["stops_unknown"] is True
    assert health["deep"]["stops_unknown_reason"] == "open orders unreadable"
    assert health["deep"]["naked"] is False  # not misread as "0 uncovered"
    # Not knowing whether stops are covered is itself an alert — it must move
    # `overall`, not just sit as a quiet per-mode footnote (B's R7 follow-up).
    assert health["status"] == "stops_unknown"
    assert health["note"] == "止损覆盖未核验：open orders unreadable"


def test_book_health_stops_unknown_overrides_an_otherwise_calm_state(tmp_path):
    """Same idea, but proving it specifically: a stamp that is perfectly
    fresh and well inside its trading window — which alone would read
    "ok" — must still surface as the stops_unknown alert once that flag is
    set, not be swallowed by the calm time-based state."""
    from datetime import datetime, timezone

    from agentic_trading.dashboard.views import book_health

    now = datetime(2026, 9, 9, 17, 0, tzinfo=timezone.utc)  # Wed 13:00 ET, in window
    path = tmp_path / "heartbeat.json"
    path.write_text(
        json.dumps({
            "deep": {"timestamp": now.isoformat(), "cycle_id": 1,
                     "stops_covered": None, "positions": 4,
                     "stops_unknown": True, "stops_unknown_reason": "open orders unreadable"},
            "fast": {"timestamp": now.isoformat(), "cycle_id": 40,
                     "stops_covered": 4, "positions": 4},
        }),
        encoding="utf-8",
    )
    health = book_health(path, now=now)
    assert health["fast"]["state"] == "ok"  # fast itself has nothing wrong
    assert health["status"] == "stops_unknown"  # but overall still alerts


def test_book_health_stops_unknown_absent_on_normal_stamps(tmp_path):
    from datetime import datetime, timezone

    from agentic_trading.dashboard.views import book_health

    now = datetime(2026, 9, 9, 17, 0, tzinfo=timezone.utc)
    path = tmp_path / "heartbeat.json"
    path.write_text(
        json.dumps({
            "deep": {"timestamp": now.isoformat(), "cycle_id": 1,
                     "stops_covered": 4, "positions": 4},
        }),
        encoding="utf-8",
    )
    health = book_health(path, now=now, deep_only=True)
    assert health["deep"]["stops_unknown"] is False
    assert health["deep"]["stops_unknown_reason"] is None


# --- dashboard and watchdog must agree on "the most recent actual check" (round 2, R2)

def _health_after(tmp_path, steps, *, now):
    """Write a real heartbeat sequence through write_heartbeat and read it back
    with book_health, so the dashboard sees exactly what the watchdog sees."""
    from datetime import timedelta

    import agentic_trading.heartbeat as hb
    from agentic_trading.dashboard.views import book_health

    path = tmp_path / "heartbeat.json"
    real_now = hb._utcnow
    try:
        for minutes, mode, kwargs in steps:
            hb._utcnow = lambda m=minutes: now - timedelta(minutes=60 - m)
            hb.write_heartbeat(None, mode, path=path, **kwargs)
    finally:
        hb._utcnow = real_now
    return book_health(path, now=now), hb.check_heartbeat(now=now, path=path)


def test_book_health_clears_stops_unknown_after_a_later_successful_check(tmp_path):
    # deep's read failed at 09:45; the 10:15 fast scan re-checked and found
    # 3/3. Both readers must report the latest check — not "any mode still
    # carries a stale unknown flag".
    from datetime import datetime, timezone

    now = datetime(2026, 9, 9, 17, 0, tzinfo=timezone.utc)  # Wed 13:00 ET
    health, problem = _health_after(tmp_path, [
        (0, "deep", {"stop_coverage_unknown": "open orders unreadable", "positions": 3}),
        (30, "fast", {"stop_coverage": (3, 3)}),
    ], now=now)
    assert health["status"] == "ok", health
    assert health["stop_check"]["unknown"] is False
    assert health["stop_check"]["mode"] == "fast" and health["stop_check"]["stops_covered"] == 3
    assert health["note"] is None
    assert problem is None  # watchdog agrees


def test_book_health_keeps_stops_unknown_across_a_skipped_scan(tmp_path):
    # fast's read failed at 09:55; the 10:15 fast scan ran no check (yielded /
    # market closed). The failure is still the most recent check: dashboard
    # and watchdog both stay on stops_unknown.
    from datetime import datetime, timezone

    now = datetime(2026, 9, 9, 17, 0, tzinfo=timezone.utc)
    health, problem = _health_after(tmp_path, [
        (0, "deep", {"stop_coverage": (4, 4)}),
        (10, "fast", {"stop_coverage_unknown": "open orders unreadable", "positions": 4}),
        (30, "fast", {}),
    ], now=now)
    assert health["status"] == "stops_unknown", health
    assert health["stop_check"]["unknown"] is True
    assert health["note"] == "止损覆盖未核验：open orders unreadable"
    assert problem is not None and "没能核验止损覆盖" in problem


def test_book_health_failure_then_other_mode_success_then_skip_stays_clear(tmp_path):
    from datetime import datetime, timezone

    now = datetime(2026, 9, 9, 17, 0, tzinfo=timezone.utc)
    health, problem = _health_after(tmp_path, [
        (0, "fast", {"stop_coverage_unknown": "open orders unreadable", "positions": 3}),
        (10, "deep", {"stop_coverage": (3, 3)}),
        (30, "fast", {}),
    ], now=now)
    assert health["status"] == "ok", health
    assert health["stop_check"]["mode"] == "deep"
    assert problem is None


def test_book_health_naked_positions_come_from_the_latest_check(tmp_path):
    from datetime import datetime, timezone

    now = datetime(2026, 9, 9, 17, 0, tzinfo=timezone.utc)
    health, problem = _health_after(tmp_path, [
        (0, "deep", {"stop_coverage": (4, 4)}),
        (30, "fast", {"stop_coverage": (3, 4)}),
    ], now=now)
    assert health["stop_check"]["naked"] is True
    assert health["stop_check"]["stops_covered"] == 3 and health["stop_check"]["positions"] == 4
    assert problem is not None and "1/4" in problem


def test_book_health_surfaces_late_start_flag(tmp_path):
    """P0-B-3 wrote late_minutes/missed_slots into heartbeat entries; a late
    (but not yet stale) cycle start must be visible in the health payload so
    the dashboard can flag it without waiting for FAST/DEEP_MAX_AGE to trip."""
    from datetime import datetime, timedelta, timezone

    from agentic_trading.dashboard.views import book_health
    from agentic_trading.heartbeat import LATE_MAX_MINUTES

    now = datetime(2026, 9, 16, 23, 19, tzinfo=timezone.utc)
    path = tmp_path / "heartbeat.json"
    path.write_text(
        json.dumps({
            "deep": {
                "timestamp": now.isoformat(), "cycle_id": 9,
                "stops_covered": 4, "positions": 5,
                "started_at": now.isoformat(),
                "scheduled_slot": (now - timedelta(minutes=184)).isoformat(),
                "late_minutes": 184.0, "missed_slots": 0,
            },
        }),
        encoding="utf-8",
    )
    health = book_health(path, now=now, deep_only=True)
    assert health["deep"]["late_minutes"] == 184.0
    assert health["deep"]["missed_slots"] == 0
    assert health["deep"]["late"] is True
    assert LATE_MAX_MINUTES < 184.0


def test_book_health_late_minutes_absent_on_older_stamps(tmp_path):
    """Stamps written before P0-B-3 (or by a book whose schedule isn't in
    BOOK_SCHEDULES, e.g. P3/P4) have no late_minutes at all — must not crash
    and must not be misreported as late."""
    from datetime import datetime, timezone

    from agentic_trading.dashboard.views import book_health

    now = datetime(2026, 9, 9, 18, 0, tzinfo=timezone.utc)
    path = tmp_path / "heartbeat.json"
    path.write_text(
        json.dumps({
            "deep": {"timestamp": now.isoformat(), "cycle_id": 1,
                     "stops_covered": 2, "positions": 2},
        }),
        encoding="utf-8",
    )
    health = book_health(path, now=now, deep_only=True)
    assert health["deep"]["late_minutes"] is None
    assert health["deep"]["late"] is False


def test_book_health_late_within_threshold_is_not_flagged(tmp_path):
    from datetime import datetime, timezone

    from agentic_trading.dashboard.views import book_health
    from agentic_trading.heartbeat import LATE_MAX_MINUTES

    now = datetime(2026, 9, 9, 18, 0, tzinfo=timezone.utc)
    path = tmp_path / "heartbeat.json"
    path.write_text(
        json.dumps({
            "deep": {"timestamp": now.isoformat(), "cycle_id": 1,
                     "stops_covered": 2, "positions": 2,
                     "late_minutes": LATE_MAX_MINUTES - 5},
        }),
        encoding="utf-8",
    )
    health = book_health(path, now=now, deep_only=True)
    assert health["deep"]["late"] is False


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
        # Heartbeat is stamped with real wall-clock "now" and read back with
        # real wall-clock "now" too — both stamps are fresh either way, so
        # the only thing that varies is which calendar/clock bucket the
        # actual moment this test runs in falls into (P1-B-4).
        assert body["status"] in {"ok", "after_hours", "weekend"}
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

    def test_funnel_endpoint_is_read_only_aggregation(self, client: TestClient):
        # A-4 (c2c_a7e2 §五): GET-only funnel summary over the seeded journal.
        # The seeded journal's only intent_events row is the legacy-style
        # sizing veto (identity columns NULL) — it must surface as an
        # identity-less degraded count, not a fabricated chain.
        body = client.get("/api/books/p1/funnel").json()
        assert body["session_date"] == "2026-08-21"
        assert body["mode"] == "paper"
        assert body["schema_degraded"] is False
        assert body["summary"]["decisions"] == 0
        assert body["degraded"]["legacy_flush_events"] == 1
        assert body["degraded"]["fills"]["degraded"] is False  # FakeReader fills are data
        assert body["chains"] == [] and body["carryover"] == []
        # Mode filtering is available on the same endpoint. Identity-less
        # legacy rows predate the mode column entirely, so they count as a
        # degraded bucket under every mode (never attributed to dry_run).
        dry = client.get("/api/books/p1/funnel?mode=dry_run").json()
        assert dry["summary"]["decisions"] == 0
        assert dry["degraded"]["legacy_flush_events"] == 1


# ---- R6: outcomes_summary mode filter + per-horizon small_sample ------------

def _insert_outcome_row(conn, decision_id, *, ret_1d=None, ret_5d=None, ret_20d=None):
    conn.execute(
        """INSERT INTO signal_outcomes (decision_id, asof, ret_1d, ret_5d, ret_20d,
               mfe_20d, mae_20d, evaluated_at)
           VALUES (?, '2026-08-20', ?, ?, ?, NULL, NULL, '2026-08-25T00:00:00+00:00')""",
        (decision_id, ret_1d, ret_5d, ret_20d),
    )


def _outcome_decision(symbol: str, *, action: str = "buy"):
    from agentic_trading.journal.logger import DecisionRow

    return DecisionRow(
        symbol=symbol, quant_score=0.5, llm_stance="bullish", llm_confidence=0.8,
        llm_rationale=None, combined_score=0.6, action=action, reasoning="",
    )


def test_outcomes_summary_defaults_to_paper_mode_only():
    import sqlite3

    from agentic_trading.dashboard.views import outcomes_summary
    from agentic_trading.journal.logger import connect, record_cycle

    conn = connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        record_cycle(conn, "2026-08-20T14:00:00+00:00", "paper", 100_000, 50_000,
                     [_outcome_decision("AAA")])
        record_cycle(conn, "2026-08-21T14:00:00+00:00", "dry_run", 100_000, 50_000,
                     [_outcome_decision("BBB")])
        record_cycle(conn, "2026-08-22T14:00:00+00:00", "backtest", 100_000, 50_000,
                     [_outcome_decision("CCC")])
        ids = {r[1]: r[0] for r in conn.execute("SELECT id, symbol FROM decisions")}
        _insert_outcome_row(conn, ids["AAA"], ret_1d=0.01)
        _insert_outcome_row(conn, ids["BBB"], ret_1d=0.02)
        _insert_outcome_row(conn, ids["CCC"], ret_1d=0.03)

        paper_only = outcomes_summary(conn)
        buy = next(r for r in paper_only if r["action"] == "buy")
        assert buy["n"] == 1  # only the paper-mode decision counts by default

        everything = outcomes_summary(conn, mode=None)
        buy_all = next(r for r in everything if r["action"] == "buy")
        assert buy_all["n"] == 3  # explicit mode=None keeps the audit escape hatch
    finally:
        conn.close()


def test_outcomes_summary_flags_thin_horizon_despite_large_bucket():
    import sqlite3

    from agentic_trading.dashboard.views import outcomes_summary
    from agentic_trading.journal.logger import DecisionRow, connect, record_cycle

    conn = connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        rows = [
            DecisionRow(
                symbol=f"S{i}", quant_score=0.5, llm_stance="bullish", llm_confidence=0.8,
                llm_rationale=None, combined_score=0.6, action="buy", reasoning="",
            )
            for i in range(40)
        ]
        record_cycle(conn, "2026-08-20T14:00:00+00:00", "paper", 100_000, 50_000, rows)
        ids = [r[0] for r in conn.execute("SELECT id FROM decisions ORDER BY id")]
        for i, decision_id in enumerate(ids):
            # All 40 mature at 1d; only the first one matures at 20d.
            _insert_outcome_row(
                conn, decision_id,
                ret_1d=0.01, ret_20d=0.03 if i == 0 else None,
            )

        result = outcomes_summary(conn)
        buy = next(r for r in result if r["action"] == "buy")
        assert buy["n"] == 40
        assert buy["n_1d"] == 40 and buy["n_1d_small"] is False
        assert buy["n_20d"] == 1 and buy["n_20d_small"] is True
    finally:
        conn.close()
