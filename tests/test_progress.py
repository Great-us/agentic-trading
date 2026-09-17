"""Operator handoff card: schema, next-job calendar, isolation from live data/."""
from __future__ import annotations

from datetime import datetime, timezone
from agentic_trading import progress as progress_mod
from agentic_trading.data.market_data import ET
from agentic_trading.progress import (
    emit_progress,
    infer_book_id,
    next_job_p1,
    next_job_p3,
    read_progress,
    write_progress,
)


WED = datetime(2026, 9, 9, 14, 0, tzinfo=ET)          # Wednesday 14:00 ET
AFTER_CLOSE = datetime(2026, 9, 9, 16, 20, tzinfo=ET)  # after 16:15 deep
FRIDAY_EVE = datetime(2026, 9, 11, 16, 35, tzinfo=ET)
SATURDAY = datetime(2026, 9, 12, 12, 0, tzinfo=ET)
MIDDAY_DONE = datetime(2026, 9, 9, 10, 45, tzinfo=ET)


def test_conftest_redirects_progress_path(tmp_path):
    assert progress_mod.PROGRESS_PATH == tmp_path / "progress.json"


def test_write_and_read_roundtrip(tmp_path):
    path = tmp_path / "card.json"
    emit_progress(
        book_id="p1", round_name="fast", status="ok", round_id="12",
        asof="2026-09-09", did=["1 order"], did_not=["no LLM"],
        broker={"equity": 10000, "cash": 2000, "n_positions": 3},
        risk={"exposure_pct": 0.80, "stops_covered": 3, "stops_total": 3},
        now=WED, path=path,
    )
    card = read_progress(path)
    assert card is not None
    assert card["schema_version"] == 1
    assert card["round"]["status"] == "ok"
    assert card["did"] == ["1 order"]
    assert card["next_job"]["slot"] in {"fast", "deep"}
    assert "instruction" in card["next_job"]
    assert "api_key" not in card


def test_missing_file_is_none(tmp_path):
    assert read_progress(tmp_path / "nope.json") is None


def test_corrupt_file_is_none(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{not json", encoding="utf-8")
    assert read_progress(path) is None


def test_write_strips_secrets(tmp_path):
    path = tmp_path / "card.json"
    write_progress({"book_id": "p1", "alpaca_api_key": "AKIA", "did": ["x"]}, path=path)
    text = path.read_text(encoding="utf-8")
    assert "AKIA" not in text
    assert "alpaca_api_key" not in text


def test_write_never_raises(tmp_path, monkeypatch):
    import agentic_trading.progress as mod

    def boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(mod.Path, "mkdir", boom)
    write_progress({"book_id": "p1"}, path=tmp_path / "blocked" / "progress.json")


def test_next_job_p1_after_afternoon_is_next_session_open():
    job = next_job_p1(AFTER_CLOSE.astimezone(timezone.utc), last_round="deep")
    # Fast scan starts 09:35, before the 09:45 deep — that is the real next task.
    assert job["slot"] == "fast"
    assert job["when_et"] == "09:35"
    assert "flush" in job["instruction"]


def test_next_job_p1_midday_is_fast_scan():
    job = next_job_p1(WED.astimezone(timezone.utc), last_round="fast")
    assert job["slot"] == "fast"
    assert "flush" in job["instruction"]


def test_next_job_p1_weekend_skips_to_monday():
    job = next_job_p1(SATURDAY.astimezone(timezone.utc), last_round="deep")
    assert job["slot"] == "fast"
    assert job["when_et"] == "09:35"
    when = datetime.fromisoformat(job["when"])
    assert when.astimezone(ET).weekday() == 0


def test_next_job_p3_after_midday_is_evening():
    job = next_job_p3(MIDDAY_DONE.astimezone(timezone.utc), last_round="midday")
    assert job["slot"] == "evening"
    assert job["when_et"] == "16:30"
    assert "禁止新开仓" in job["instruction"]


def test_next_job_p3_after_evening_is_next_midday():
    job = next_job_p3(FRIDAY_EVE.astimezone(timezone.utc), last_round="evening")
    assert job["slot"] == "midday"
    assert job["when_et"] == "10:30"
    when = datetime.fromisoformat(job["when"])
    assert when.astimezone(ET).weekday() == 0  # Monday


def test_next_job_p3_flatten_incomplete_forbids_entries():
    job = next_job_p3(FRIDAY_EVE.astimezone(timezone.utc),
                      last_round="flatten", status="incomplete")
    assert job["slot"] == "flatten"
    assert "禁止新开仓" in job["instruction"]


def test_next_job_p3_flatten_failed_forbids_entries():
    job = next_job_p3(FRIDAY_EVE.astimezone(timezone.utc),
                      last_round="flatten", status="failed")
    assert job["slot"] == "flatten"
    assert "禁止新开仓" in job["instruction"]


def test_next_job_p3_healthy_flatten_points_at_next_midday_not_entries_now():
    job = next_job_p3(FRIDAY_EVE.astimezone(timezone.utc),
                      last_round="flatten", status="ok")
    assert job["slot"] == "midday"
    assert "禁止新开仓" in job["instruction"]
    when = datetime.fromisoformat(job["when"])
    assert when.astimezone(ET).weekday() == 0


def test_infer_book_id_from_folder_name(tmp_path):
    assert infer_book_id(tmp_path / "Trading") == "p1"
    assert infer_book_id(tmp_path / "Trading-P2") == "p2"
    assert infer_book_id(tmp_path / "Trading-P3") == "p3"
    assert infer_book_id(tmp_path / "Trading-P4") == "p4"


def test_stamp_cycle_progress_live_writes():
    from types import SimpleNamespace

    from agentic_trading.run import _stamp_cycle_progress

    _stamp_cycle_progress(
        asof=None, fast_mode=True, cycle_no=7,
        account=SimpleNamespace(equity=10000.0, cash=2000.0),
        positions={"AAA": object()}, orders_this_cycle=0, status="ok",
    )
    card = read_progress()
    assert card is not None
    assert card["round"]["name"] == "fast"
    assert card["round"]["round_id"] == "7"
    assert card["broker"]["n_positions"] == 1
    assert card["broker"]["equity"] == 10000.0
    assert card["next_job"]["instruction"]


def test_stamp_cycle_progress_skips_backtest():
    from agentic_trading.run import _stamp_cycle_progress

    if progress_mod.PROGRESS_PATH.exists():
        progress_mod.PROGRESS_PATH.unlink()
    _stamp_cycle_progress(
        asof="2026-09-09", fast_mode=False, cycle_no=1,
        account=None, positions=None, status="ok",
    )
    assert read_progress() is None
