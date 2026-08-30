"""Lock priority between the deep cycle and the fast scan.

The two are independent Windows scheduled tasks that both fire at 9:45/16:15
ET. Before this, whoever grabbed the OS lock first won — and the loser was
often the deep cycle, which runs twice a day and (with allow_intraday_entries
off) is the only path that can open a position. Losing it silently drops a
whole decision point. The rule these tests pin down: the frequent, expendable
side yields; the rare, important side waits.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from agentic_trading.cycle_lock import DEEP_LOCK_WAIT_SECONDS, CycleLock
from agentic_trading.run import (
    DEEP_CYCLE_YIELD_MINUTES,
    RUN_TIMES_ET,
    _near_deep_cycle,
)

ET = ZoneInfo("America/New_York")


@pytest.fixture()
def lock_path(tmp_path):
    return tmp_path / "cycle.lock"


# --- non-blocking (fast scan) semantics are unchanged -------------------------

def test_uncontended_acquire_succeeds(lock_path):
    lock = CycleLock(lock_path)
    assert lock.acquire() is True
    lock.release()


def test_zero_timeout_fails_immediately_when_held(lock_path):
    holder = CycleLock(lock_path)
    assert holder.acquire() is True
    try:
        contender = CycleLock(lock_path)
        started = time.monotonic()
        assert contender.acquire() is False
        # The fast scan must not sit around: it has a 20-minute cadence to keep.
        assert time.monotonic() - started < 1.0
    finally:
        holder.release()


# --- bounded blocking (deep cycle) -------------------------------------------

def test_deep_cycle_waits_and_then_acquires(lock_path):
    """A fast scan holding the lock briefly must not cost the deep cycle its slot."""
    holder = CycleLock(lock_path)
    assert holder.acquire() is True
    released = threading.Event()

    def _release_soon():
        time.sleep(0.3)
        holder.release()
        released.set()

    threading.Thread(target=_release_soon, daemon=True).start()
    deep = CycleLock(lock_path)
    try:
        assert deep.acquire(timeout=5.0, poll_seconds=0.05) is True
        assert released.is_set(), "should only have won after the holder let go"
    finally:
        deep.release()


def test_deep_cycle_gives_up_after_its_timeout(lock_path):
    """Waiting is bounded — a wedged holder must not hang the cycle forever."""
    holder = CycleLock(lock_path)
    assert holder.acquire() is True
    try:
        deep = CycleLock(lock_path)
        started = time.monotonic()
        assert deep.acquire(timeout=0.3, poll_seconds=0.05) is False
        elapsed = time.monotonic() - started
        assert 0.3 <= elapsed < 3.0
    finally:
        holder.release()


def test_deep_wait_is_long_enough_to_outlast_a_fast_scan():
    # A fast scan is seconds of batched work; the budget should be far above it
    # so an ordinary overlap never costs a decision point.
    assert DEEP_LOCK_WAIT_SECONDS >= 60.0


# --- the fast scan's yield window --------------------------------------------

@pytest.mark.parametrize("hour, minute", RUN_TIMES_ET)
def test_fast_scan_yields_at_each_deep_slot(hour, minute):
    assert _near_deep_cycle(datetime(2026, 8, 26, hour, minute, tzinfo=ET))


@pytest.mark.parametrize("delta", [-DEEP_CYCLE_YIELD_MINUTES, DEEP_CYCLE_YIELD_MINUTES])
def test_fast_scan_yields_across_the_whole_window(delta):
    hour, minute = RUN_TIMES_ET[0]
    total = hour * 60 + minute + delta
    when = datetime(2026, 8, 26, total // 60, total % 60, tzinfo=ET)
    assert _near_deep_cycle(when)


def test_fast_scan_runs_normally_away_from_a_deep_slot():
    # 12:25 ET is a routine fast-scan firing with no deep cycle nearby.
    assert not _near_deep_cycle(datetime(2026, 8, 26, 12, 25, tzinfo=ET))


def test_yield_window_does_not_swallow_the_whole_session():
    """A too-wide window would silently disable the fast tier."""
    yielding = sum(
        _near_deep_cycle(datetime(2026, 8, 26, m // 60, m % 60, tzinfo=ET))
        for m in range(9 * 60 + 30, 16 * 60 + 1)  # 09:30–16:00 ET, by the minute
    )
    assert yielding <= 2 * (2 * DEEP_CYCLE_YIELD_MINUTES + 1)


def test_the_two_schedules_come_from_one_definition():
    """scheduler.py must not keep a second copy of the deep-run times."""
    from agentic_trading import scheduler

    assert scheduler.RUN_TIMES_ET is RUN_TIMES_ET
