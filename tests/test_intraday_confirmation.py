"""Intraday scoring rides the still-forming daily bar, so a symbol can cross
the buy threshold, fall back, and cross again inside an hour. The confirmation
streak is what stops a single noisy crossing from becoming an order."""
from agentic_trading.journal.logger import (
    bump_intraday_confirmation, clear_intraday_confirmation, connect,
)

TS = "2026-08-20T14:00:00+00:00"


def _conn():
    return connect(":memory:")


def test_first_sighting_counts_as_one():
    conn = _conn()
    assert bump_intraday_confirmation(conn, "MSFT", TS) == 1


def test_consecutive_sightings_accumulate():
    conn = _conn()
    assert bump_intraday_confirmation(conn, "MSFT", TS) == 1
    assert bump_intraday_confirmation(conn, "MSFT", TS) == 2
    assert bump_intraday_confirmation(conn, "MSFT", TS) == 3


def test_clearing_resets_the_streak():
    conn = _conn()
    bump_intraday_confirmation(conn, "MSFT", TS)
    bump_intraday_confirmation(conn, "MSFT", TS)
    clear_intraday_confirmation(conn, "MSFT")
    # A symbol that dropped out and came back starts over, so two unrelated
    # crossings never add up to a confirmed entry.
    assert bump_intraday_confirmation(conn, "MSFT", TS) == 1


def test_symbols_are_tracked_independently():
    conn = _conn()
    bump_intraday_confirmation(conn, "MSFT", TS)
    bump_intraday_confirmation(conn, "MSFT", TS)
    assert bump_intraday_confirmation(conn, "NVDA", TS) == 1
    assert bump_intraday_confirmation(conn, "MSFT", TS) == 3


def test_clearing_an_untracked_symbol_is_harmless():
    conn = _conn()
    clear_intraday_confirmation(conn, "NEVER_SEEN")  # must not raise
    assert bump_intraday_confirmation(conn, "NEVER_SEEN", TS) == 1


def test_first_seen_is_preserved_across_bumps():
    conn = _conn()
    bump_intraday_confirmation(conn, "MSFT", "2026-08-20T14:00:00+00:00")
    bump_intraday_confirmation(conn, "MSFT", "2026-08-20T14:20:00+00:00")
    row = conn.execute(
        "SELECT first_seen_at, last_seen_at FROM intraday_confirmations WHERE symbol = 'MSFT'"
    ).fetchone()
    assert row[0] == "2026-08-20T14:00:00+00:00"   # when the streak started
    assert row[1] == "2026-08-20T14:20:00+00:00"   # most recent confirmation
