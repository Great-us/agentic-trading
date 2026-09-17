"""Cockpit JSONL: emit is best-effort and silent during asof backtests."""
from __future__ import annotations

from datetime import datetime, timezone

from agentic_trading import live_events
from agentic_trading.live_events import emit, next_scheduled_slot, read_tail


def test_emit_writes_stage_and_strips_secrets(tmp_path, monkeypatch):
    path = tmp_path / "live_events.jsonl"
    monkeypatch.setattr(live_events, "LIVE_EVENTS_PATH", path)
    emit("cycle_start", cycle=9, n_symbols=14, api_key="should-not-land")
    rows = read_tail(path)
    assert rows and rows[-1]["stage"] == "cycle_start"
    assert rows[-1]["cycle"] == 9
    assert "api_key" not in rows[-1]
    assert "t" in rows[-1]


def test_emit_never_raises_on_bad_path(monkeypatch):
    monkeypatch.setattr(live_events, "LIVE_EVENTS_PATH", None)  # type: ignore[arg-type]
    emit("cycle_end", cycle=1)  # must not raise


def test_next_scheduled_slot_weekday():
    # Friday 14:00 ET is inside the fast-scan grid.
    now = datetime(2026, 9, 4, 18, 0, tzinfo=timezone.utc)  # 14:00 ET
    slot = next_scheduled_slot(now)
    assert slot["kind"] in {"deep", "fast"}
    assert slot["seconds"] is not None and slot["seconds"] >= 0
