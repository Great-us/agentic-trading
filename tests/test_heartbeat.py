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
    early_block = source[anchor:anchor + 900]
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


# --- coverage unknown ≠ coverage fine (review R2) ----------------------------------
#
# get_open_orders failed → the reconciliation returned None → the old stamp
# simply omitted the coverage keys, and --check then read the OTHER mode's
# older "all covered" entry. A failed check must alert; a cycle that never
# ran a check (market-closed fast exit, pre-field stamps) must not.

def test_unverified_coverage_after_a_good_cycle_alerts(tmp_path, monkeypatch):
    path = tmp_path / "hb.json"
    monkeypatch.setattr(hb, "_utcnow", lambda: WEDNESDAY - timedelta(hours=1))
    write_heartbeat(None, "deep", path=path, stop_coverage=(4, 4))       # yesterday's clean check
    write_heartbeat(None, "fast", path=path, stop_coverage=(4, 4))
    assert check_heartbeat(now=WEDNESDAY - timedelta(minutes=59), path=path) is None

    monkeypatch.setattr(hb, "_utcnow", lambda: WEDNESDAY)
    write_heartbeat(None, "fast", path=path, stop_coverage=None,          # this scan: read failed
                    stop_coverage_unknown="open orders unreadable — protective-stop coverage not verified this cycle",
                    positions=4)
    entry = json.loads(path.read_text(encoding="utf-8"))["fast"]
    assert entry["stops_unknown"] is True and entry["stops_covered"] is None and entry["positions"] == 4
    problem = check_heartbeat(now=WEDNESDAY + timedelta(minutes=1), path=path)
    assert problem is not None
    assert "没能核验止损覆盖" in problem and "4 个持仓" in problem and "open orders unreadable" in problem
    assert "全部覆盖」不能沿用" in problem


def test_unverified_coverage_is_not_hidden_by_a_later_skipped_scan(tmp_path, monkeypatch):
    # deep check failed at 16:15; the 16:35 fast scan found the market closed
    # and stamped without any coverage keys. The failure is still the newest
    # *check* and must still alert.
    path = tmp_path / "hb.json"
    monkeypatch.setattr(hb, "_utcnow", lambda: WEDNESDAY)
    write_heartbeat(None, "deep", path=path, stop_coverage_unknown="open orders unreadable", positions=3)
    monkeypatch.setattr(hb, "_utcnow", lambda: WEDNESDAY + timedelta(minutes=20))
    write_heartbeat(None, "fast", path=path)                             # skipped scan, no check
    problem = check_heartbeat(now=WEDNESDAY + timedelta(minutes=21), path=path)
    assert problem is not None and "没能核验止损覆盖" in problem


def test_skipped_scan_after_a_clean_check_is_quiet(tmp_path, monkeypatch):
    path = tmp_path / "hb.json"
    monkeypatch.setattr(hb, "_utcnow", lambda: WEDNESDAY)
    write_heartbeat(None, "deep", path=path, stop_coverage=(3, 3))
    monkeypatch.setattr(hb, "_utcnow", lambda: WEDNESDAY + timedelta(minutes=20))
    write_heartbeat(None, "fast", path=path)                             # market closed: no check ran
    assert check_heartbeat(now=WEDNESDAY + timedelta(minutes=21), path=path) is None


def test_unverified_coverage_clears_once_a_later_check_succeeds(tmp_path, monkeypatch):
    path = tmp_path / "hb.json"
    monkeypatch.setattr(hb, "_utcnow", lambda: WEDNESDAY)
    write_heartbeat(None, "deep", path=path, stop_coverage_unknown="open orders unreadable", positions=3)
    write_heartbeat(None, "fast", path=path)
    assert "没能核验" in check_heartbeat(now=WEDNESDAY + timedelta(minutes=1), path=path)
    monkeypatch.setattr(hb, "_utcnow", lambda: WEDNESDAY + timedelta(minutes=20))
    write_heartbeat(None, "fast", path=path, stop_coverage=(3, 3))
    assert check_heartbeat(now=WEDNESDAY + timedelta(minutes=21), path=path) is None


def test_unverified_coverage_without_a_position_count_still_alerts(tmp_path):
    path = tmp_path / "hb.json"
    fresh = (WEDNESDAY - timedelta(minutes=5)).isoformat()
    _write_raw(path, {
        "deep": {"timestamp": fresh, "cycle_id": 1, "stops_unknown": True,
                 "stops_covered": None, "positions": None},
        "fast": {"timestamp": fresh, "cycle_id": 1},
    })
    problem = check_heartbeat(now=WEDNESDAY, path=path)
    assert problem is not None and "没能核验止损覆盖" in problem and "持仓的保护状态未知" in problem


# --- the check record must survive skipped cycles (review round 2, R2) --------------
#
# write_heartbeat rebuilt data[mode] on every call, so a fast scan that ran a
# check and failed (stops_unknown) was wiped by the NEXT fast scan that ran
# no check at all (market closed / yielded). The watchdog then fell back to
# deep's older "all covered" and the alert cleared without a successful
# re-check. Liveness (timestamp/cycle) and "last actual stop check" are now
# separate records; skipping a check leaves the check record alone.

def _seq(path, monkeypatch, steps):
    """steps: (minutes_after_WEDNESDAY, mode, kwargs) written in order."""
    for minutes, mode, kwargs in steps:
        monkeypatch.setattr(hb, "_utcnow", lambda m=minutes: WEDNESDAY + timedelta(minutes=m))
        write_heartbeat(None, mode, path=path, **kwargs)


def test_same_mode_skip_does_not_erase_a_failed_check(tmp_path, monkeypatch):
    path = tmp_path / "hb.json"
    _seq(path, monkeypatch, [
        (0, "deep", {"stop_coverage": (4, 4)}),                                 # 09:45 deep: fine
        (10, "fast", {"stop_coverage_unknown": "open orders unreadable", "positions": 4}),  # 09:55: read failed
        (30, "fast", {}),                                                       # 10:15: yielded / closed, no check
    ])
    data = json.loads(path.read_text(encoding="utf-8"))
    assert "stops_unknown" not in data["fast"], "the skipped scan is not itself a failed check"
    problem = check_heartbeat(now=WEDNESDAY + timedelta(minutes=31), path=path)
    assert problem is not None and "没能核验止损覆盖" in problem, \
        "the failed check is still the most recent check — a skipped scan must not clear it"


def test_cross_mode_success_clears_the_watchdog(tmp_path, monkeypatch):
    path = tmp_path / "hb.json"
    _seq(path, monkeypatch, [
        (0, "deep", {"stop_coverage_unknown": "open orders unreadable", "positions": 3}),
        (20, "fast", {"stop_coverage": (3, 3)}),
    ])
    assert check_heartbeat(now=WEDNESDAY + timedelta(minutes=21), path=path) is None


def test_failure_then_other_mode_success_then_skip_does_not_reactivate(tmp_path, monkeypatch):
    # The naive fix — copying the old check fields onto the new stamp — would
    # resurrect fast's stale failure here. The most recent CHECK is deep's
    # success; fast's later skip changes nothing.
    path = tmp_path / "hb.json"
    _seq(path, monkeypatch, [
        (0, "fast", {"stop_coverage_unknown": "open orders unreadable", "positions": 3}),
        (10, "deep", {"stop_coverage": (3, 3)}),
        (30, "fast", {}),
    ])
    assert check_heartbeat(now=WEDNESDAY + timedelta(minutes=31), path=path) is None
    record = json.loads(path.read_text(encoding="utf-8"))["stop_check"]
    assert record["mode"] == "deep" and record["stops_covered"] == 3 and not record.get("unknown")


def test_stop_check_record_is_written_once_per_actual_check(tmp_path, monkeypatch):
    path = tmp_path / "hb.json"
    _seq(path, monkeypatch, [(0, "deep", {"stop_coverage": (2, 4)})])
    data = json.loads(path.read_text(encoding="utf-8"))
    record = data["stop_check"]
    assert record["checked_at"] == WEDNESDAY.isoformat()
    assert record["mode"] == "deep" and record["stops_covered"] == 2 and record["positions"] == 4
    assert record.get("unknown") in (None, False)
    _seq(path, monkeypatch, [(5, "fast", {})])
    assert json.loads(path.read_text(encoding="utf-8"))["stop_check"] == record
    _seq(path, monkeypatch, [(9, "fast", {"stop_coverage_unknown": "502", "positions": 4})])
    record2 = json.loads(path.read_text(encoding="utf-8"))["stop_check"]
    assert record2["unknown"] is True and record2["reason"] == "502" and record2["mode"] == "fast"
    assert record2["checked_at"] == (WEDNESDAY + timedelta(minutes=9)).isoformat()


def test_old_format_stamps_without_a_check_record_still_work(tmp_path):
    fresh = (WEDNESDAY - timedelta(minutes=5)).isoformat()
    path = tmp_path / "hb.json"
    _write_raw(path, {"deep": {"timestamp": fresh, "cycle_id": 1, "stops_covered": 5, "positions": 6},
                      "fast": {"timestamp": fresh, "cycle_id": 1}})
    problem = check_heartbeat(now=WEDNESDAY, path=path)
    assert problem is not None and "1/6" in problem                 # naked gap still read per mode
    _write_raw(path, {"deep": {"timestamp": fresh, "cycle_id": 1, "stops_covered": 6, "positions": 6},
                      "fast": {"timestamp": fresh, "cycle_id": 1}})
    assert check_heartbeat(now=WEDNESDAY, path=path) is None         # and no false unknown


# --- first write over an OLD-format file must not lose its last check (round 3, R2)

def _legacy_file(path, *, deep_minutes, fast_minutes, fast_unknown=True):
    """A heartbeat written by the pre-`stop_check` code: coverage lives only in
    the per-mode stamps. deep checked fine earlier; fast failed later."""
    deep_ts = (WEDNESDAY - timedelta(minutes=deep_minutes)).isoformat()
    fast_ts = (WEDNESDAY - timedelta(minutes=fast_minutes)).isoformat()
    fast = {"timestamp": fast_ts, "cycle_id": 41}
    if fast_unknown:
        fast.update({"stops_covered": None, "positions": 4, "stops_unknown": True,
                     "stops_unknown_reason": "open orders unreadable"})
    _write_raw(path, {"deep": {"timestamp": deep_ts, "cycle_id": 40, "stops_covered": 4, "positions": 4},
                      "fast": fast})
    return fast_ts


def test_first_skipped_write_over_a_legacy_file_keeps_the_failed_check(tmp_path, monkeypatch):
    path = tmp_path / "hb.json"
    fast_ts = _legacy_file(path, deep_minutes=60, fast_minutes=10)
    assert "没能核验" in check_heartbeat(now=WEDNESDAY, path=path)   # legacy read already alerts
    # First write by the new code: a fast scan that ran no check.
    monkeypatch.setattr(hb, "_utcnow", lambda: WEDNESDAY)
    write_heartbeat(None, "fast", path=path)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert "stop_check" in data, "the legacy last check must be migrated before the entry is rebuilt"
    assert data["stop_check"]["unknown"] is True and data["stop_check"]["mode"] == "fast"
    assert data["stop_check"]["checked_at"] == fast_ts, "migration keeps the original check time"
    assert data["stop_check"]["reason"] == "open orders unreadable"
    problem = check_heartbeat(now=WEDNESDAY + timedelta(minutes=1), path=path)
    assert problem is not None and "没能核验止损覆盖" in problem


def test_legacy_migration_then_a_real_success_clears_watchdog_and_dashboard(tmp_path, monkeypatch):
    # P2 ships no dashboard package; the watchdog half is covered by the test above.
    book_health = pytest.importorskip("agentic_trading.dashboard.views").book_health

    path = tmp_path / "hb.json"
    _legacy_file(path, deep_minutes=60, fast_minutes=10)
    monkeypatch.setattr(hb, "_utcnow", lambda: WEDNESDAY)
    write_heartbeat(None, "fast", path=path)                          # skipped scan: still unknown
    assert book_health(path, now=WEDNESDAY)["status"] == "stops_unknown"
    monkeypatch.setattr(hb, "_utcnow", lambda: WEDNESDAY + timedelta(minutes=20))
    write_heartbeat(None, "deep", path=path, stop_coverage=(4, 4))    # real re-check
    now = WEDNESDAY + timedelta(minutes=21)
    assert check_heartbeat(now=now, path=path) is None
    health = book_health(path, now=now)
    assert health["status"] != "stops_unknown" and health["stop_check"]["unknown"] is False


def test_legacy_file_without_any_check_migrates_nothing(tmp_path, monkeypatch):
    path = tmp_path / "hb.json"
    fresh = (WEDNESDAY - timedelta(minutes=5)).isoformat()
    _write_raw(path, {"deep": {"timestamp": fresh, "cycle_id": 1}, "fast": {"timestamp": fresh, "cycle_id": 1}})
    monkeypatch.setattr(hb, "_utcnow", lambda: WEDNESDAY)
    write_heartbeat(None, "fast", path=path)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert "stop_check" not in data
    assert check_heartbeat(now=WEDNESDAY + timedelta(minutes=1), path=path) is None


def test_legacy_migration_prefers_unknown_on_a_timestamp_tie(tmp_path, monkeypatch):
    path = tmp_path / "hb.json"
    ts = (WEDNESDAY - timedelta(minutes=10)).isoformat()
    _write_raw(path, {"deep": {"timestamp": ts, "cycle_id": 1, "stops_covered": 4, "positions": 4},
                      "fast": {"timestamp": ts, "cycle_id": 1, "stops_covered": None, "positions": 4,
                               "stops_unknown": True, "stops_unknown_reason": "502"}})
    monkeypatch.setattr(hb, "_utcnow", lambda: WEDNESDAY)
    write_heartbeat(None, "deep", path=path)
    assert json.loads(path.read_text(encoding="utf-8"))["stop_check"]["unknown"] is True


def test_cli_exits_1_and_toasts_on_unverified_coverage(tmp_path, monkeypatch, capsys):
    fired = []
    monkeypatch.setattr(hb, "_notify_toast", fired.append)
    path = tmp_path / "hb.json"
    monkeypatch.setattr(hb, "HEARTBEAT_PATH", path)
    monkeypatch.setattr(hb, "_utcnow", lambda: WEDNESDAY)
    write_heartbeat(None, "deep", path=path, stop_coverage_unknown="open orders unreadable", positions=4)
    write_heartbeat(None, "fast", path=path)
    with pytest.raises(SystemExit) as exc:
        hb.main(["--check"])
    assert exc.value.code == 1
    assert "[ALERT]" in capsys.readouterr().out
    assert len(fired) == 1 and "没能核验" in fired[0]


# --- cycle timeliness (P0-B-3) ----------------------------------------------------
#
# 2026-09-16: the 16:15 ET deep cycle ran at 19:19 ET (machine asleep, Task
# Scheduler StartWhenAvailable catch-up). The stamp said "ok". A late cycle
# decides on a stale time point; a skipped deep slot decides on nothing.

from zoneinfo import ZoneInfo  # noqa: E402 — test-local helper import

ET = ZoneInfo("America/New_York")


def _et(y, m, d, hh, mm):
    return datetime(y, m, d, hh, mm, tzinfo=ET).astimezone(timezone.utc)


def test_scheduled_slot_before_picks_the_right_grid_slot():
    # P1 deep 09:45 / 16:15; fast 09:35 + 20 min through 15:55.
    assert hb.scheduled_slot_before("deep", _et(2026, 9, 16, 19, 19), "p1") == _et(2026, 9, 16, 16, 15)
    assert hb.scheduled_slot_before("deep", _et(2026, 9, 17, 9, 46), "p1") == _et(2026, 9, 17, 9, 45)
    # Monday pre-open → Friday's 16:15 (weekend has no slots).
    assert hb.scheduled_slot_before("deep", _et(2026, 9, 14, 9, 0), "p1") == _et(2026, 9, 11, 16, 15)
    assert hb.scheduled_slot_before("fast", _et(2026, 9, 17, 9, 36), "p1") == _et(2026, 9, 17, 9, 35)
    assert hb.scheduled_slot_before("fast", _et(2026, 9, 17, 12, 59), "p1") == _et(2026, 9, 17, 12, 55)
    # P2 runs its own grid (09:50/16:20, fast 10:10 → 15:50).
    assert hb.scheduled_slot_before("deep", _et(2026, 9, 17, 9, 52), "p2") == _et(2026, 9, 17, 9, 50)
    assert hb.scheduled_slot_before("fast", _et(2026, 9, 17, 16, 30), "p2") == _et(2026, 9, 17, 15, 50)
    # Books without a registered schedule get nothing rather than P1's grid.
    assert hb.scheduled_slot_before("deep", _et(2026, 9, 17, 9, 52), "p4") is None


def test_slots_between_counts_only_weekday_slots_strictly_inside():
    # 09-15 16:15 → 09-16 16:15 skipped the 09-16 09:45 deep run.
    assert hb.slots_between("deep", _et(2026, 9, 15, 16, 15), _et(2026, 9, 16, 16, 15), "p1") == 1
    # Friday close → Monday open: nothing scheduled in between.
    assert hb.slots_between("deep", _et(2026, 9, 11, 16, 15), _et(2026, 9, 14, 9, 45), "p1") == 0
    # Consecutive slots → 0; same slot twice → 0.
    assert hb.slots_between("deep", _et(2026, 9, 16, 9, 45), _et(2026, 9, 16, 16, 15), "p1") == 0
    assert hb.slots_between("deep", _et(2026, 9, 16, 9, 45), _et(2026, 9, 16, 9, 45), "p1") == 0
    # A morning of sleep skips seven fast scans (10:35 … 12:35).
    assert hb.slots_between("fast", _et(2026, 9, 16, 10, 15), _et(2026, 9, 16, 12, 55), "p1") == 7
    assert hb.slots_between("deep", _et(2026, 9, 15, 16, 15), _et(2026, 9, 16, 16, 15), "p4") is None


def test_on_time_deep_stamp_carries_slot_fields_and_passes(tmp_path, monkeypatch):
    monkeypatch.setattr(hb, "_book_schedule", lambda book_id=None: hb.BOOK_SCHEDULES["p1"])
    started = _et(2026, 9, 17, 9, 46)
    monkeypatch.setattr(hb, "_utcnow", lambda: started + timedelta(minutes=4))
    path = tmp_path / "hb.json"
    write_heartbeat(None, "deep", path=path, started_at=started)
    write_heartbeat(None, "fast", path=path, started_at=started - timedelta(minutes=10))
    entry = json.loads(path.read_text(encoding="utf-8"))["deep"]
    assert entry["started_at"] == started.isoformat()
    assert entry["scheduled_slot"] == _et(2026, 9, 17, 9, 45).isoformat()
    assert entry["late_minutes"] == 1.0
    assert "missed_slots" not in entry  # first stamp with slot info: nothing to compare to
    assert check_heartbeat(now=started + timedelta(minutes=5), path=path) is None


def test_late_deep_cycle_is_flagged_with_planned_and_actual_times(tmp_path, monkeypatch):
    monkeypatch.setattr(hb, "_book_schedule", lambda book_id=None: hb.BOOK_SCHEDULES["p1"])
    started = _et(2026, 9, 16, 19, 19)  # the real one
    monkeypatch.setattr(hb, "_utcnow", lambda: started + timedelta(minutes=3))
    path = tmp_path / "hb.json"
    write_heartbeat(None, "deep", path=path, started_at=started)
    write_heartbeat(None, "fast", path=path, started_at=started - timedelta(hours=4))
    entry = json.loads(path.read_text(encoding="utf-8"))["deep"]
    assert entry["late_minutes"] == 184.0
    problem = check_heartbeat(now=started + timedelta(minutes=10), path=path)
    assert problem is not None
    assert "晚了 184 分钟" in problem
    assert "09-16 16:15" in problem and "09-16 19:19" in problem


def test_lateness_exactly_at_threshold_passes_one_minute_beyond_fails(tmp_path):
    path = tmp_path / "hb.json"
    now = WEDNESDAY
    fresh = (now - timedelta(minutes=5)).isoformat()

    def stamp(late):
        _write_raw(path, {
            "deep": {"timestamp": fresh, "cycle_id": 1, "late_minutes": late,
                     "scheduled_slot": fresh, "started_at": fresh},
            "fast": {"timestamp": fresh, "cycle_id": 1},
        })

    stamp(hb.LATE_MAX_MINUTES)
    assert check_heartbeat(now=now, path=path) is None
    stamp(hb.LATE_MAX_MINUTES + 1)
    assert "晚了" in check_heartbeat(now=now, path=path)


def test_only_the_newest_stamp_s_lateness_counts(tmp_path):
    # Yesterday's late deep is history once a fast scan has run on time.
    path = tmp_path / "hb.json"
    now = WEDNESDAY
    _write_raw(path, {
        "deep": {"timestamp": (now - timedelta(hours=3)).isoformat(), "cycle_id": 1, "late_minutes": 184.0},
        "fast": {"timestamp": (now - timedelta(minutes=5)).isoformat(), "cycle_id": 2, "late_minutes": 1.2},
    })
    assert check_heartbeat(now=now, path=path) is None


def test_missed_deep_slot_is_counted_and_alerts(tmp_path, monkeypatch):
    monkeypatch.setattr(hb, "_book_schedule", lambda book_id=None: hb.BOOK_SCHEDULES["p1"])
    path = tmp_path / "hb.json"
    first = _et(2026, 9, 15, 16, 16)
    monkeypatch.setattr(hb, "_utcnow", lambda: first + timedelta(minutes=2))
    write_heartbeat(None, "deep", path=path, started_at=first)
    # The 09-16 09:45 slot never stamps; the 16:15 one does.
    second = _et(2026, 9, 16, 16, 16)
    monkeypatch.setattr(hb, "_utcnow", lambda: second + timedelta(minutes=2))
    write_heartbeat(None, "deep", path=path, started_at=second)
    write_heartbeat(None, "fast", path=path, started_at=second - timedelta(minutes=30))
    entry = json.loads(path.read_text(encoding="utf-8"))["deep"]
    assert entry["missed_slots"] == 1
    problem = check_heartbeat(now=second + timedelta(minutes=5), path=path)
    assert problem is not None and "漏跑了 1 个计划槽" in problem


def test_consecutive_deep_slots_report_zero_missed(tmp_path, monkeypatch):
    monkeypatch.setattr(hb, "_book_schedule", lambda book_id=None: hb.BOOK_SCHEDULES["p1"])
    path = tmp_path / "hb.json"
    for started in (_et(2026, 9, 11, 16, 15), _et(2026, 9, 14, 9, 47)):  # Fri close → Mon open
        monkeypatch.setattr(hb, "_utcnow", lambda s=started: s + timedelta(minutes=2))
        write_heartbeat(None, "deep", path=path, started_at=started)
    entry = json.loads(path.read_text(encoding="utf-8"))["deep"]
    assert entry["missed_slots"] == 0 and entry["late_minutes"] == 2.0


def test_missed_fast_slots_are_recorded_but_do_not_alert(tmp_path, monkeypatch):
    # A fast scan that finds the deep cycle holding the lock exits without a
    # stamp by design, so a skipped fast slot is dashboard data, not a page.
    monkeypatch.setattr(hb, "_book_schedule", lambda book_id=None: hb.BOOK_SCHEDULES["p1"])
    path = tmp_path / "hb.json"
    for started in (_et(2026, 9, 16, 9, 35), _et(2026, 9, 16, 10, 15)):
        monkeypatch.setattr(hb, "_utcnow", lambda s=started: s + timedelta(seconds=30))
        write_heartbeat(None, "fast", path=path, started_at=started)
    monkeypatch.setattr(hb, "_utcnow", lambda: _et(2026, 9, 16, 9, 46))
    write_heartbeat(None, "deep", path=path, started_at=_et(2026, 9, 16, 9, 45))
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["fast"]["missed_slots"] == 1  # 09:55 skipped
    assert check_heartbeat(now=_et(2026, 9, 16, 10, 20), path=path) is None


def test_unknown_book_schedule_writes_only_started_at(tmp_path, monkeypatch):
    monkeypatch.setattr(hb, "_book_schedule", lambda book_id=None: None)
    path = tmp_path / "hb.json"
    write_heartbeat(None, "deep", path=path, started_at=WEDNESDAY)
    write_heartbeat(None, "fast", path=path, started_at=WEDNESDAY)
    entry = json.loads(path.read_text(encoding="utf-8"))["deep"]
    assert entry["started_at"] == WEDNESDAY.isoformat()
    assert "scheduled_slot" not in entry and "late_minutes" not in entry
    assert check_heartbeat(now=WEDNESDAY + timedelta(minutes=1), path=path) is None


def test_run_cycle_passes_its_start_time_to_both_stamps():
    import agentic_trading.run as run_mod

    source = Path(run_mod.__file__).read_text(encoding="utf-8")
    call_sites = re.findall(r"^\s*write_heartbeat\(.*\)$", source, flags=re.MULTILINE)
    assert call_sites and all("started_at=cycle_started_at" in c for c in call_sites), call_sites


# --- llm_status in the stamp (P0-B-1) ---------------------------------------------

def test_llm_status_is_written_sanitised_and_an_open_circuit_alerts(tmp_path, monkeypatch):
    monkeypatch.setattr(hb, "_utcnow", lambda: WEDNESDAY)
    path = tmp_path / "hb.json"
    write_heartbeat(None, "deep", path=path, llm_status={
        "state": "circuit_open", "category": "quota", "failures": 3, "calls": 3,
        "retry_hint": "try again at Sep 20th, 2026 4:00 PM",
        "detail": "x" * 1000, "prompt": "must not be copied",
    })
    # A later fast scan that never consulted the analyst must not mask it.
    monkeypatch.setattr(hb, "_utcnow", lambda: WEDNESDAY + timedelta(minutes=10))
    write_heartbeat(None, "fast", path=path, llm_status={
        "state": "off", "reason": "fast tier: no symbol escalated to the analyst this scan", "calls": 0,
    })
    entry = json.loads(path.read_text(encoding="utf-8"))["deep"]
    status = entry["llm_status"]
    assert status["state"] == "circuit_open" and status["category"] == "quota"
    assert status["failures"] == 3 and len(status["detail"]) == 400
    assert "prompt" not in status
    problem = check_heartbeat(now=WEDNESDAY + timedelta(minutes=11), path=path)
    assert problem is not None
    assert "circuit_open" in problem and "quota" in problem and "Sep 20th" in problem
    assert "fail-closed" in problem
    # The alert text already says it; the INFO line never repeats circuit_open
    # (here it describes the newer fast stamp's "off", which is a different mode).
    note = hb.llm_status_note(json.loads(path.read_text(encoding="utf-8")))
    assert note is None or "circuit_open" not in note


def test_open_circuit_alert_clears_when_the_next_deep_cycle_answers(tmp_path, monkeypatch):
    path = tmp_path / "hb.json"
    monkeypatch.setattr(hb, "_utcnow", lambda: WEDNESDAY)
    write_heartbeat(None, "deep", path=path, llm_status={"state": "circuit_open", "category": "quota"})
    write_heartbeat(None, "fast", path=path)
    assert "熔断" in check_heartbeat(now=WEDNESDAY + timedelta(minutes=1), path=path)
    monkeypatch.setattr(hb, "_utcnow", lambda: WEDNESDAY + timedelta(hours=6))
    write_heartbeat(None, "deep", path=path, llm_status={"state": "ok", "calls": 14, "failures": 0})
    write_heartbeat(None, "fast", path=path)
    assert check_heartbeat(now=WEDNESDAY + timedelta(hours=6, minutes=1), path=path) is None


def test_degraded_and_off_states_are_info_not_alerts(tmp_path, monkeypatch):
    monkeypatch.setattr(hb, "_utcnow", lambda: WEDNESDAY)
    path = tmp_path / "hb.json"
    write_heartbeat(None, "deep", path=path, llm_status={
        "state": "degraded", "calls": 14, "failures": 2, "category": "timeout",
    })
    write_heartbeat(None, "fast", path=path)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert check_heartbeat(now=WEDNESDAY + timedelta(minutes=1), path=path) is None
    note = hb.llm_status_note(data)
    assert note and "degraded" in note and "timeout" in note and "失败 2 次" in note
    fresh = WEDNESDAY.isoformat()
    off = {"deep": {"timestamp": fresh, "llm_status": {"state": "off", "reason": "skipped via --skip-llm"}}}
    assert hb.llm_status_note(off) and "skip-llm" in hb.llm_status_note(off)
    assert hb._llm_problems(off) == []


def test_ok_llm_status_produces_no_note():
    fresh = WEDNESDAY.isoformat()
    assert hb.llm_status_note({"deep": {"timestamp": fresh, "llm_status": {"state": "ok"}}}) is None
    assert hb.llm_status_note({"deep": {"timestamp": fresh}}) is None


def test_cli_prints_the_llm_note_on_the_ok_path(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(hb, "_notify_toast", lambda _m: None)
    monkeypatch.setattr(hb, "check_heartbeat", lambda *a, **k: None)
    path = tmp_path / "hb.json"
    monkeypatch.setattr(hb, "HEARTBEAT_PATH", path)
    _write_raw(path, {"deep": {"timestamp": WEDNESDAY.isoformat(),
                               "llm_status": {"state": "degraded", "category": "timeout", "failures": 1}}})
    with pytest.raises(SystemExit) as exc:
        hb.main(["--check"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "[OK]" in out and "[INFO]" in out and "timeout" in out


def test_cli_toasts_and_exits_1_on_an_open_circuit(tmp_path, monkeypatch, capsys):
    fired = []
    monkeypatch.setattr(hb, "_notify_toast", fired.append)
    path = tmp_path / "hb.json"
    monkeypatch.setattr(hb, "HEARTBEAT_PATH", path)
    fresh = (WEDNESDAY - timedelta(minutes=5)).isoformat()
    monkeypatch.setattr(hb, "_utcnow", lambda: WEDNESDAY)
    _write_raw(path, {
        "deep": {"timestamp": fresh, "cycle_id": 1,
                 "llm_status": {"state": "circuit_open", "category": "quota",
                                "retry_hint": "try again at Sep 20th, 2026 4:00 PM"}},
        "fast": {"timestamp": fresh, "cycle_id": 1},
    })
    with pytest.raises(SystemExit) as exc:
        hb.main(["--check"])
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "[ALERT]" in out and "quota" in out and "Sep 20th" in out
    assert len(fired) == 1 and "quota" in fired[0]
