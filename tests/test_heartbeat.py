"""Tests for the scheduler heartbeat module (observability-only):

- fresh deep+fast stamps pass the check, and carry the latest cycle id
- stale fast scan trips on a weekday; exactly-at-threshold stamps still pass
- the fast rule is exempt on weekends, the deep rule never is
- missing file / corrupt JSON / wrong shape fail closed
- missing or unparsable per-mode entries fail closed
- write_heartbeat preserves sibling entries, tolerates conn=None, bare
  databases and unwritable paths without raising
- the CLI exits 0/1 correctly and only toasts on the alert path
- run_cycle really calls write_heartbeat at both normal exits (source-level)
"""
from __future__ import annotations

import base64
import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import agentic_trading.heartbeat as hb
from agentic_trading.heartbeat import (
    DEEP_MAX_AGE_HOURS, FAST_MAX_AGE_MINUTES, check_heartbeat, write_heartbeat,
)
from agentic_trading.journal.logger import connect, record_cycle

# Fixed reference points — a Wednesday and a Sunday, so the weekday gate is
# deterministic no matter when the suite runs.
WEDNESDAY = datetime(2026, 8, 26, 15, 0, tzinfo=timezone.utc)
SUNDAY = datetime(2026, 8, 30, 15, 0, tzinfo=timezone.utc)


def _stamp(dt: datetime) -> dict:
    return {"timestamp": dt.isoformat(), "cycle_id": None}


def _write_raw(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


@pytest.fixture()
def jconn():
    conn = connect(":memory:")
    yield conn
    conn.close()


# --- fresh stamps pass ---------------------------------------------------------

def test_fresh_heartbeats_pass_and_carry_the_cycle_id(jconn, tmp_path, monkeypatch):
    monkeypatch.setattr(hb, "_utcnow", lambda: WEDNESDAY)
    path = tmp_path / "hb.json"
    assert record_cycle(jconn, WEDNESDAY.isoformat(), "paper", 100000.0, 50000.0, []) == 1

    write_heartbeat(jconn, "fast", path=path)
    write_heartbeat(jconn, "deep", path=path)

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["fast"]["cycle_id"] == 1
    assert data["deep"]["cycle_id"] == 1
    # Still fine 10 minutes / a few hours later — well inside both thresholds.
    assert check_heartbeat(now=WEDNESDAY + timedelta(minutes=10), path=path) is None
    # Atomic writes must not leave temp files behind next to the real stamp.
    assert not list(tmp_path.glob(".heartbeat-*"))


def test_threshold_boundaries_pass_and_one_tick_beyond_fails(tmp_path):
    path = tmp_path / "hb.json"
    now = WEDNESDAY
    _write_raw(path, {
        "fast": _stamp(now - timedelta(minutes=FAST_MAX_AGE_MINUTES)),
        "deep": _stamp(now - timedelta(hours=DEEP_MAX_AGE_HOURS)),
    })
    # Exactly at each threshold is still fresh (the rules are strict ">").
    assert check_heartbeat(now=now, path=path) is None

    _write_raw(path, {
        "fast": _stamp(now - timedelta(minutes=FAST_MAX_AGE_MINUTES + 1)),
        "deep": _stamp(now - timedelta(hours=DEEP_MAX_AGE_HOURS + 1)),
    })
    reason = check_heartbeat(now=now, path=path)
    assert reason is not None
    assert "快扫" in reason and "深周期" in reason


# --- per-rule behaviour ---------------------------------------------------------

def test_stale_fast_scan_trips_on_a_weekday_even_when_deep_is_fresh(tmp_path):
    path = tmp_path / "hb.json"
    now = WEDNESDAY
    _write_raw(path, {
        "fast": _stamp(now - timedelta(minutes=FAST_MAX_AGE_MINUTES + 5)),
        "deep": _stamp(now - timedelta(hours=2)),
    })
    reason = check_heartbeat(now=now, path=path)
    assert reason is not None and "快扫" in reason


def test_stale_fast_scan_is_tolerated_on_a_weekend(tmp_path):
    path = tmp_path / "hb.json"
    _write_raw(path, {
        "fast": _stamp(SUNDAY - timedelta(hours=3)),   # far past the weekday limit
        "deep": _stamp(SUNDAY - timedelta(hours=2)),
    })
    assert check_heartbeat(now=SUNDAY, path=path) is None


def test_stale_deep_cycle_trips_at_any_time_weekends_included(tmp_path):
    path = tmp_path / "hb.json"
    _write_raw(path, {
        "fast": _stamp(SUNDAY - timedelta(minutes=1)),
        "deep": _stamp(SUNDAY - timedelta(hours=DEEP_MAX_AGE_HOURS + 4)),
    })
    reason = check_heartbeat(now=SUNDAY, path=path)
    assert reason is not None and "深周期" in reason


# --- broken files / entries fail closed ------------------------------------------

@pytest.mark.parametrize("scenario,keyword", [
    ("missing", "不存在"),
    ("corrupt", "损坏"),
    ("wrong-shape", "格式异常"),
])
def test_unusable_heartbeat_file_fails_closed(tmp_path, scenario, keyword):
    path = tmp_path / "hb.json"
    if scenario == "corrupt":
        path.write_text("{not json", encoding="utf-8")
    elif scenario == "wrong-shape":
        path.write_text("[]", encoding="utf-8")
    reason = check_heartbeat(now=WEDNESDAY, path=path)
    assert reason is not None and keyword in reason


def test_missing_or_unparsable_entries_fail_on_a_weekday(tmp_path):
    path = tmp_path / "hb.json"
    now = WEDNESDAY

    _write_raw(path, {"deep": _stamp(now - timedelta(hours=1))})          # fast entry absent
    assert "快扫" in check_heartbeat(now=now, path=path)

    _write_raw(path, {"fast": _stamp(now - timedelta(minutes=1))})        # deep entry absent
    assert "深周期" in check_heartbeat(now=now, path=path)

    _write_raw(path, {"fast": {"timestamp": "garbage"},                   # both unparsable
                      "deep": {"timestamp": "garbage"}})
    reason = check_heartbeat(now=now, path=path)
    assert reason is not None and "快扫" in reason and "深周期" in reason


# --- write_heartbeat robustness ---------------------------------------------------

def test_backtest_replays_do_not_stamp_the_watchdog(monkeypatch):
    """run_cycle with asof set (backtest / test replay) must not touch the real
    heartbeat: it carries a throwaway journal whose fresh timestamp and bogus
    cycle id would falsely convince the watchdog the scheduler is alive."""
    import agentic_trading.run as run_mod
    from agentic_trading.data.feed import HistoricalFeed
    from agentic_trading.execution.sim_broker import SimulatedBroker
    from agentic_trading.signals.macro import MacroRegime
    from tests.test_backtest import _settings, _trend, _with_macro

    calls = []
    monkeypatch.setattr(run_mod, "write_heartbeat",
                        lambda *a, **k: calls.append(a))
    monkeypatch.setattr(
        "agentic_trading.run.assess_regime",
        lambda **_k: MacroRegime(score=0.4, label="risk_on", notes=["test"]),
    )
    _, df = _trend("AAA", n=90, step=0.4)
    bars = _with_macro({"AAA": df})
    asof = df.index[-1]
    broker = SimulatedBroker(starting_cash=100_000, slippage_bps=0, bars=bars)
    broker.current_date = asof
    conn = connect(":memory:")
    try:
        run_mod.run_cycle(skip_llm=True, settings=_settings(["AAA"]),
                          broker=broker, feed=HistoricalFeed(bars), conn=conn,
                          asof=asof)
    finally:
        conn.close()
    assert calls == []

def test_write_tolerates_none_conn_and_a_bare_database(tmp_path, monkeypatch):
    monkeypatch.setattr(hb, "_utcnow", lambda: WEDNESDAY)
    path = tmp_path / "hb.json"

    write_heartbeat(None, "deep", path=path)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["deep"]["cycle_id"] is None
    # deep is fresh; the weekday fast rule can't fire because fast was also
    # just stamped below.
    write_heartbeat(None, "fast", path=tmp_path / "hb.json")
    assert check_heartbeat(now=WEDNESDAY + timedelta(minutes=5), path=path) is None

    bare = sqlite3.connect(":memory:")  # no journal schema → query fails, id stays None
    try:
        write_heartbeat(bare, "fast", path=tmp_path / "other.json")
        other = json.loads((tmp_path / "other.json").read_text(encoding="utf-8"))
        assert other["fast"]["cycle_id"] is None
    finally:
        bare.close()


def test_write_preserves_the_other_mode_and_recovers_from_corruption(jconn, tmp_path, monkeypatch):
    path = tmp_path / "hb.json"
    stamps = iter([WEDNESDAY - timedelta(hours=1), WEDNESDAY, WEDNESDAY + timedelta(minutes=1)])
    monkeypatch.setattr(hb, "_utcnow", lambda: next(stamps))

    write_heartbeat(jconn, "deep", path=path)
    write_heartbeat(jconn, "fast", path=path)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert set(data) == {"deep", "fast"}
    assert data["deep"]["timestamp"] == (WEDNESDAY - timedelta(hours=1)).isoformat()

    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{{{junk", encoding="utf-8")
    write_heartbeat(None, "fast", path=corrupt)
    recovered = json.loads(corrupt.read_text(encoding="utf-8"))
    assert set(recovered) == {"fast"}  # unreadable prior content is replaced cleanly


def test_write_never_raises_on_an_unwritable_path(tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("a file, not a directory", encoding="utf-8")
    # Parent "directory" is actually a file: mkdir fails inside write_heartbeat,
    # which must swallow it instead of blowing up the caller's cycle.
    write_heartbeat(None, "fast", path=blocker / "hb.json")
    assert not (blocker / "hb.json").exists()


# --- toast helper + CLI wiring -----------------------------------------------------

def test_toast_command_escapes_single_quotes():
    cmd = hb._toast_command("标题", "快扫停摆 'quoted'")
    assert cmd[0] == "powershell" and "-EncodedCommand" in cmd
    script = base64.b64decode(cmd[-1]).decode("utf-16-le")
    # PowerShell doubles embedded single quotes inside literal strings; if the
    # message were interpolated raw this would read 快扫停摆 'quoted' instead.
    assert "快扫停摆 ''quoted''" in script


def test_notify_toast_is_best_effort(monkeypatch):
    captured = {}

    class FakeCompleted:
        returncode = 0
        stderr = b""

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return FakeCompleted()

    monkeypatch.setattr(hb.subprocess, "run", fake_run)
    hb._notify_toast("任何消息")
    assert captured["cmd"][0] == "powershell"

    def boom(*args, **kwargs):
        raise OSError("no desktop session")

    monkeypatch.setattr(hb.subprocess, "run", boom)
    hb._notify_toast("仍然不能抛错")  # must not raise


def test_cli_exit_codes_and_alert_only_toast(tmp_path, monkeypatch, capsys):
    fired = []
    monkeypatch.setattr(hb, "_notify_toast", fired.append)

    monkeypatch.setattr(hb, "check_heartbeat", lambda *a, **k: None)
    with pytest.raises(SystemExit) as exc:
        hb.main(["--check"])
    assert exc.value.code == 0
    assert "[OK]" in capsys.readouterr().out
    assert fired == []

    monkeypatch.setattr(hb, "check_heartbeat", lambda *a, **k: "快扫调度可能已停摆")
    with pytest.raises(SystemExit) as exc:
        hb.main(["--check"])
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "[ALERT]" in out and "快扫" in out
    assert len(fired) == 1


def test_cli_without_check_flag_exits_2(capsys):
    with pytest.raises(SystemExit) as exc:
        hb.main([])
    assert exc.value.code == 2
    capsys.readouterr()


# --- run.py insertion points -------------------------------------------------------

def test_run_cycle_stamps_the_heartbeat_at_both_normal_exits():
    import agentic_trading.run as run_mod

    source = Path(run_mod.__file__).read_text(encoding="utf-8")
    # Import-name order/company is free to change; what matters is that
    # write_heartbeat comes from the heartbeat module.
    assert re.search(r"^from \.heartbeat import .*\bwrite_heartbeat\b", source,
                     flags=re.MULTILINE)

    call_sites = re.findall(r"^\s*write_heartbeat\(.*\)$", source, flags=re.MULTILINE)
    assert len(call_sites) >= 2, "both normal exits of run_cycle must stamp the heartbeat"

    # Exit 1: the market-closed fast-scan early return stamps before returning.
    anchor = source.index("Fast-tier scan skipped")
    early_block = source[anchor:anchor + 400]
    assert early_block.index("write_heartbeat(") < early_block.index("return")

    # Exit 2: the full-cycle end stamps while conn is still open (before close).
    tail = source[source.index("_journal_safe(log, record_cycle"):]
    assert "conn.close()" in tail.split("def main")[0]
    assert tail.split("def main")[0].index("write_heartbeat(") \
        < tail.split("def main")[0].index("conn.close()")


def test_run_cycle_never_stamps_the_live_heartbeat_file(tmp_path, monkeypatch):
    """Iron rule #6: the suite must not touch data/heartbeat.json.

    write_heartbeat resolves HEARTBEAT_PATH at call time so tests/conftest.py can
    redirect it. Binding the default in the signature instead silently sent every
    run_cycle() test's stamp to the live watchdog file, which then reported a
    fresh deep cycle that never happened.
    """
    import agentic_trading.heartbeat as hb

    redirected = tmp_path / "hb.json"
    monkeypatch.setattr(hb, "HEARTBEAT_PATH", redirected)
    hb.write_heartbeat(None, "deep", stop_coverage=(2, 3))

    assert redirected.exists(), "the redirect must be honoured"
    entry = json.loads(redirected.read_text(encoding="utf-8"))["deep"]
    assert entry["stops_covered"] == 2 and entry["positions"] == 3


def test_check_flags_a_position_without_a_resting_stop(tmp_path):
    import agentic_trading.heartbeat as hb

    path = tmp_path / "hb.json"
    now = datetime(2026, 8, 26, 15, 0, tzinfo=timezone.utc)  # a Wednesday
    fresh = (now - timedelta(minutes=5)).isoformat()
    path.write_text(json.dumps({
        "deep": {"timestamp": fresh, "cycle_id": 9, "stops_covered": 5, "positions": 6},
        "fast": {"timestamp": fresh, "cycle_id": 9},
    }), encoding="utf-8")
    problem = hb.check_heartbeat(now=now, path=path)
    assert problem is not None and "没有挂上保护性止损单" in problem


def test_check_is_quiet_when_every_position_is_covered(tmp_path):
    import agentic_trading.heartbeat as hb

    path = tmp_path / "hb.json"
    now = datetime(2026, 8, 26, 15, 0, tzinfo=timezone.utc)
    fresh = (now - timedelta(minutes=5)).isoformat()
    path.write_text(json.dumps({
        "deep": {"timestamp": fresh, "cycle_id": 9, "stops_covered": 6, "positions": 6},
        "fast": {"timestamp": fresh, "cycle_id": 9},
    }), encoding="utf-8")
    assert hb.check_heartbeat(now=now, path=path) is None
