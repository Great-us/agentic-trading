from datetime import date, datetime, timezone

from agentic_trading.journal.logger import connect, record_position_state
from agentic_trading.journal.p1_diagnostics import _add_months, diagnose


def test_protocol_and_complete_session_coverage(tmp_path):
    journal = tmp_path / "j.db"
    heartbeat = tmp_path / "heartbeat.json"
    watchlist = tmp_path / "watchlist.yaml"
    heartbeat.write_text('{"deep": {}}', encoding="utf-8")
    watchlist.write_text("symbols: [NVDA]\n", encoding="utf-8")
    conn = connect(journal)
    try:
        conn.execute(
            "INSERT INTO cycles (timestamp, mode) VALUES (?, ?)",
            ("2026-08-24T20:15:00+00:00", "paper"),
        )
        conn.commit()
    finally:
        conn.close()

    result = diagnose(
        journal, heartbeat, watchlist,
        now=datetime(2026, 8, 24, 21, tzinfo=timezone.utc),
    )

    assert result["protocol"]["start"] == "2026-08-22"
    assert result["protocol"]["first_review"] == "2027-02-22"
    assert result["protocol"]["days_elapsed"] == 2
    assert result["protocol"]["days_remaining"] == 182
    assert result["protocol"]["status"] == "observing"
    assert result["sessions"]["expected_trading_days"] == 1
    assert result["sessions"]["observed_trading_days"] == 1
    assert result["sessions"]["days_missing"] == []
    assert result["sessions"]["cycles_per_day"] == {
        "min": 1, "median": 1, "max": 1,
    }


# --- helpers -----------------------------------------------------------

def _insert_cycle(conn, timestamp: str, mode: str = "paper") -> int:
    cur = conn.execute(
        "INSERT INTO cycles (timestamp, mode) VALUES (?, ?)", (timestamp, mode)
    )
    conn.commit()
    return cur.lastrowid


def _insert_outcome(conn, cycle_id: int, symbol: str, *, ret_1d, ret_5d, ret_20d) -> None:
    cur = conn.execute(
        "INSERT INTO decisions (cycle_id, symbol, action) VALUES (?, ?, 'buy')",
        (cycle_id, symbol),
    )
    decision_id = cur.lastrowid
    conn.execute(
        """INSERT INTO signal_outcomes
           (decision_id, asof, ret_1d, ret_5d, ret_20d, mfe_20d, mae_20d, evaluated_at)
           VALUES (?, '2026-08-24', ?, ?, ?, NULL, NULL, '2026-08-24T00:00:00+00:00')""",
        (decision_id, ret_1d, ret_5d, ret_20d),
    )
    conn.commit()


def _empty_journal(tmp_path):
    journal = tmp_path / "j.db"
    heartbeat = tmp_path / "heartbeat.json"
    watchlist = tmp_path / "watchlist.yaml"
    heartbeat.write_text('{"deep": {}}', encoding="utf-8")
    watchlist.write_text("symbols: []\n", encoding="utf-8")
    conn = connect(journal)
    conn.close()
    return journal, heartbeat, watchlist


# --- _add_months: real month-end clamp ----------------------------------

def test_add_months_clamps_to_month_end():
    # August has a 31st; six months later, February does not — the naive
    # date.replace(month=..., day=31) this used to be would raise ValueError.
    assert _add_months(date(2026, 8, 31), 6) == date(2027, 2, 28)
    assert _add_months(date(2026, 8, 22), 6) == date(2027, 2, 22)


# --- protocol status on either side of first_review ----------------------

def test_protocol_status_observing_before_first_review(tmp_path):
    journal, heartbeat, watchlist = _empty_journal(tmp_path)
    result = diagnose(
        journal, heartbeat, watchlist,
        now=datetime(2027, 2, 21, 12, tzinfo=timezone.utc),
    )
    assert result["protocol"]["status"] == "observing"
    assert result["protocol"]["days_remaining"] == 1


def test_protocol_status_review_due_on_first_review_date(tmp_path):
    journal, heartbeat, watchlist = _empty_journal(tmp_path)
    result = diagnose(
        journal, heartbeat, watchlist,
        now=datetime(2027, 2, 22, 12, tzinfo=timezone.utc),
    )
    assert result["protocol"]["status"] == "review_due"
    assert result["protocol"]["days_remaining"] == 0


# --- missing trading day ------------------------------------------------

def test_missing_trading_day_is_flagged(tmp_path):
    journal, heartbeat, watchlist = _empty_journal(tmp_path)
    conn = connect(journal)
    try:
        # PROTOCOL_START 2026-08-22 is a Saturday; the first three weekdays in
        # range are Mon 08-24, Tue 08-25, Wed 08-26. Only 08-24 and 08-26 get
        # a paper cycle — 08-25 must show up as missing, not silently skipped.
        _insert_cycle(conn, "2026-08-24T14:00:00+00:00", "paper")
        _insert_cycle(conn, "2026-08-26T14:00:00+00:00", "paper")
    finally:
        conn.close()

    result = diagnose(
        journal, heartbeat, watchlist,
        now=datetime(2026, 8, 26, 20, tzinfo=timezone.utc),
    )
    sessions = result["sessions"]
    assert sessions["expected_trading_days"] == 3
    assert sessions["observed_trading_days"] == 2
    assert sessions["days_missing"] == ["2026-08-25"]


# --- dry_run cycles must not count as paper-forward evidence ------------

def test_dry_run_cycle_excluded_from_sessions_and_outcomes(tmp_path):
    journal, heartbeat, watchlist = _empty_journal(tmp_path)
    conn = connect(journal)
    try:
        cycle_id = _insert_cycle(conn, "2026-08-24T14:00:00+00:00", "dry_run")
        _insert_outcome(conn, cycle_id, "AAA", ret_1d=0.01, ret_5d=0.02, ret_20d=0.03)
    finally:
        conn.close()

    result = diagnose(
        journal, heartbeat, watchlist,
        now=datetime(2026, 8, 24, 20, tzinfo=timezone.utc),
    )
    sessions = result["sessions"]
    # 08-24 is the only expected weekday in range, and the only cycle that
    # day is dry_run — it must not be counted as an observed paper session.
    assert sessions["expected_trading_days"] == 1
    assert sessions["observed_trading_days"] == 0
    assert sessions["days_missing"] == ["2026-08-24"]
    outcomes = result["outcomes"]
    for bucket in ("protocol_window", "all_history"):
        assert outcomes[bucket]["ret_1d_mature"] == 0
        assert outcomes[bucket]["ret_5d_mature"] == 0
        assert outcomes[bucket]["ret_20d_mature"] == 0
        assert outcomes[bucket]["distinct_symbol_days"] == 0


# --- pool / legacy classification and share_of_positions -----------------

def test_holdings_all_pool(tmp_path):
    journal, heartbeat, watchlist = _empty_journal(tmp_path)
    watchlist.write_text("symbols:\n  - AAA\n  - BBB\n", encoding="utf-8")
    conn = connect(journal)
    try:
        record_position_state(conn, "AAA", 10, 1000.0, "2026-08-24T14:00:00+00:00")
        record_position_state(conn, "BBB", 5, 500.0, "2026-08-24T14:00:00+00:00")
    finally:
        conn.close()

    result = diagnose(journal, heartbeat, watchlist,
                       now=datetime(2026, 8, 24, 20, tzinfo=timezone.utc))
    holdings = result["holdings"]
    assert holdings["pool"]["market_value"] == 1500.0
    assert holdings["pool"]["share_of_positions"] == 1.0
    assert holdings["legacy"]["market_value"] == 0.0
    assert holdings["legacy"]["share_of_positions"] == 0.0
    assert holdings["legacy"]["symbols"] == []


def test_holdings_all_legacy(tmp_path):
    journal, heartbeat, watchlist = _empty_journal(tmp_path)
    watchlist.write_text("symbols:\n  - AAA\n", encoding="utf-8")
    conn = connect(journal)
    try:
        record_position_state(conn, "XYZ", 3, 300.0, "2026-08-24T14:00:00+00:00")
    finally:
        conn.close()

    result = diagnose(journal, heartbeat, watchlist,
                       now=datetime(2026, 8, 24, 20, tzinfo=timezone.utc))
    holdings = result["holdings"]
    assert holdings["pool"]["market_value"] == 0.0
    assert holdings["pool"]["share_of_positions"] == 0.0
    assert holdings["legacy"]["market_value"] == 300.0
    assert holdings["legacy"]["share_of_positions"] == 1.0
    assert holdings["legacy"]["symbols"] == ["XYZ"]


def test_holdings_empty_positions(tmp_path):
    journal, heartbeat, watchlist = _empty_journal(tmp_path)
    result = diagnose(journal, heartbeat, watchlist,
                       now=datetime(2026, 8, 24, 20, tzinfo=timezone.utc))
    holdings = result["holdings"]
    assert holdings["pool"]["market_value"] == 0.0
    assert holdings["pool"]["share_of_positions"] is None
    assert holdings["legacy"]["market_value"] == 0.0
    assert holdings["legacy"]["share_of_positions"] is None
    assert holdings["total_market_value"] == 0.0
    assert holdings["snapshot_updated_at"] is None


# --- horizons mature independently --------------------------------------

def test_outcomes_horizons_mature_independently(tmp_path):
    journal, heartbeat, watchlist = _empty_journal(tmp_path)
    conn = connect(journal)
    try:
        cycle_id = _insert_cycle(conn, "2026-08-24T14:00:00+00:00", "paper")
        # 5 decisions: all 5 have a matured 1d return, only 3 a matured 5d
        # return, only 1 a matured 20d return.
        for i in range(5):
            _insert_outcome(
                conn, cycle_id, f"SYM{i}",
                ret_1d=0.01,
                ret_5d=(0.02 if i < 3 else None),
                ret_20d=(0.03 if i < 1 else None),
            )
    finally:
        conn.close()

    result = diagnose(journal, heartbeat, watchlist,
                       now=datetime(2026, 8, 24, 20, tzinfo=timezone.utc))
    # All 5 decisions are on/after PROTOCOL_START, so both buckets agree here
    # — the split itself is covered by test_outcomes_split_by_protocol_window.
    for bucket in ("protocol_window", "all_history"):
        outcomes = result["outcomes"][bucket]
        assert outcomes["ret_1d_mature"] == 5
        assert outcomes["ret_5d_mature"] == 3
        assert outcomes["ret_20d_mature"] == 1
        assert outcomes["distinct_symbol_days"] == 5


# --- R8: outcomes must not leak pre-protocol samples into the observation
# window's evidence ------------------------------------------------------

def test_outcomes_split_by_protocol_window(tmp_path):
    journal, heartbeat, watchlist = _empty_journal(tmp_path)
    conn = connect(journal)
    try:
        # One paper cycle/decision before PROTOCOL_START (2026-08-22), one
        # on it, one comfortably after it.
        pre_cycle = _insert_cycle(conn, "2026-08-10T14:00:00+00:00", "paper")
        _insert_outcome(conn, pre_cycle, "PRE", ret_1d=0.01, ret_5d=0.01, ret_20d=0.01)
        start_cycle = _insert_cycle(conn, "2026-08-22T14:00:00+00:00", "paper")
        _insert_outcome(conn, start_cycle, "START", ret_1d=0.01, ret_5d=0.01, ret_20d=0.01)
        post_cycle = _insert_cycle(conn, "2026-08-24T14:00:00+00:00", "paper")
        _insert_outcome(conn, post_cycle, "POST", ret_1d=0.01, ret_5d=0.01, ret_20d=0.01)
    finally:
        conn.close()

    result = diagnose(
        journal, heartbeat, watchlist,
        now=datetime(2026, 8, 24, 20, tzinfo=timezone.utc),
    )
    outcomes = result["outcomes"]
    # protocol_window: only START (on PROTOCOL_START, inclusive) and POST —
    # the PRE-dated sample must not count as observation-window evidence.
    window = outcomes["protocol_window"]
    assert window["start"] == "2026-08-22"
    assert window["end"] == "2026-08-24"  # now's ET date
    assert window["ret_1d_mature"] == 2
    assert window["ret_5d_mature"] == 2
    assert window["ret_20d_mature"] == 2
    assert window["distinct_symbol_days"] == 2
    # all_history: all three, including the pre-protocol sample.
    history = outcomes["all_history"]
    assert history["ret_1d_mature"] == 3
    assert history["ret_5d_mature"] == 3
    assert history["ret_20d_mature"] == 3
    assert history["distinct_symbol_days"] == 3


# --- R8 round 2: protocol_window must also have an upper bound (now's ET
# date), not just a lower bound -------------------------------------------

def test_outcomes_window_excludes_records_after_now(tmp_path):
    journal, heartbeat, watchlist = _empty_journal(tmp_path)
    conn = connect(journal)
    try:
        # A decision on PROTOCOL_START (in-window) and one three weeks later,
        # after the `now` this diagnose() call reports as of.
        in_window = _insert_cycle(conn, "2026-08-22T14:00:00+00:00", "paper")
        _insert_outcome(conn, in_window, "INWIN", ret_1d=0.01, ret_5d=0.01, ret_20d=0.01)
        after_now = _insert_cycle(conn, "2026-09-18T14:00:00+00:00", "paper")
        _insert_outcome(conn, after_now, "AFTER", ret_1d=0.02, ret_5d=0.02, ret_20d=0.02)
    finally:
        conn.close()

    # now is 2026-08-24 — well before the 09-18 decision. sessions (which has
    # always been now-bounded) and protocol_window must agree on that cutoff.
    result = diagnose(
        journal, heartbeat, watchlist,
        now=datetime(2026, 8, 24, 20, tzinfo=timezone.utc),
    )
    window = result["outcomes"]["protocol_window"]
    assert window["end"] == "2026-08-24"
    assert window["ret_1d_mature"] == 1  # only INWIN — AFTER is beyond `now`
    assert window["distinct_symbol_days"] == 1
    # all_history is unbounded by design and must still see both.
    history = result["outcomes"]["all_history"]
    assert history["ret_1d_mature"] == 2
    assert history["distinct_symbol_days"] == 2


def test_outcomes_window_empty_when_now_predates_protocol_start(tmp_path):
    journal, heartbeat, watchlist = _empty_journal(tmp_path)
    conn = connect(journal)
    try:
        # This decision predates PROTOCOL_START itself, let alone `now`.
        cycle_id = _insert_cycle(conn, "2026-08-01T14:00:00+00:00", "paper")
        _insert_outcome(conn, cycle_id, "EARLY", ret_1d=0.01, ret_5d=0.01, ret_20d=0.01)
    finally:
        conn.close()

    # now is before PROTOCOL_START (2026-08-22) — end < start, an empty
    # window. Must not raise, and every count must come back 0.
    result = diagnose(
        journal, heartbeat, watchlist,
        now=datetime(2026, 8, 10, 12, tzinfo=timezone.utc),
    )
    window = result["outcomes"]["protocol_window"]
    assert window["start"] == "2026-08-22"
    assert window["end"] == "2026-08-10"
    assert window["ret_1d_mature"] == 0
    assert window["ret_5d_mature"] == 0
    assert window["ret_20d_mature"] == 0
    assert window["distinct_symbol_days"] == 0
    # all_history is unaffected by the empty window and still sees the record.
    assert result["outcomes"]["all_history"]["ret_1d_mature"] == 1


def test_outcomes_window_end_follows_et_date_across_utc_midnight(tmp_path):
    journal, heartbeat, watchlist = _empty_journal(tmp_path)
    conn = connect(journal)
    try:
        # A decision at 2026-08-24 22:30 ET (still 08-24 in ET, but 08-25 in
        # UTC) must be included when `now` reports that same ET evening.
        cycle_id = _insert_cycle(conn, "2026-08-25T02:30:00+00:00", "paper")
        _insert_outcome(conn, cycle_id, "LATE", ret_1d=0.01, ret_5d=0.01, ret_20d=0.01)
    finally:
        conn.close()

    # now = 2026-08-25 02:00 UTC = 2026-08-24 22:00 ET — the window's `end`
    # must be computed from the ET date (08-24), not the naive UTC date
    # (08-25), matching how sessions/_as_et_date already treat cycle rows.
    now_utc = datetime(2026, 8, 25, 2, 0, tzinfo=timezone.utc)
    result = diagnose(journal, heartbeat, watchlist, now=now_utc)
    window = result["outcomes"]["protocol_window"]
    assert window["end"] == "2026-08-24"
    # The LATE decision's own ET date is also 08-24 (22:30 ET), so it falls
    # inside [start, end] and must be counted.
    assert window["ret_1d_mature"] == 1
    assert window["distinct_symbol_days"] == 1


# --- llm section: heartbeat missing / present without llm_status --------

def test_llm_section_heartbeat_file_missing(tmp_path):
    journal, heartbeat, watchlist = _empty_journal(tmp_path)
    missing_heartbeat = tmp_path / "does-not-exist.json"
    result = diagnose(journal, missing_heartbeat, watchlist,
                       now=datetime(2026, 8, 24, 20, tzinfo=timezone.utc))
    llm = result["llm"]
    assert llm["llm_status"] is None
    assert llm["note"]


def test_llm_section_heartbeat_present_without_llm_status(tmp_path):
    journal, heartbeat, watchlist = _empty_journal(tmp_path)
    heartbeat.write_text('{"deep": {"timestamp": "2026-08-24T14:00:00+00:00"}}',
                         encoding="utf-8")
    result = diagnose(journal, heartbeat, watchlist,
                       now=datetime(2026, 8, 24, 20, tzinfo=timezone.utc))
    llm = result["llm"]
    assert llm["llm_status"] is None
    assert llm["note"]


def test_llm_section_passes_through_status_when_present(tmp_path):
    journal, heartbeat, watchlist = _empty_journal(tmp_path)
    heartbeat.write_text(
        '{"deep": {"llm_status": {"state": "circuit_open", "reason": "quota"}}}',
        encoding="utf-8",
    )
    result = diagnose(journal, heartbeat, watchlist,
                       now=datetime(2026, 8, 24, 20, tzinfo=timezone.utc))
    llm = result["llm"]
    assert llm["llm_status"] == {"state": "circuit_open", "reason": "quota"}
    assert llm["note"] is None


# --- UTC midnight crossing while ET is still the previous day -----------

def test_now_and_cycle_timestamp_use_et_date_across_utc_midnight(tmp_path):
    journal, heartbeat, watchlist = _empty_journal(tmp_path)
    conn = connect(journal)
    try:
        # 2026-08-25 02:30 UTC is 2026-08-24 22:30 ET — a deep cycle running
        # very late in the ET evening (AGENTS.md F5) must still be filed
        # under the 08-24 ET session, not bucketed into 08-25 by UTC date.
        _insert_cycle(conn, "2026-08-25T02:30:00+00:00", "paper")
    finally:
        conn.close()

    now_utc = datetime(2026, 8, 25, 2, 30, tzinfo=timezone.utc)
    result = diagnose(journal, heartbeat, watchlist, now=now_utc)

    # "today" for the protocol clock is the ET date (08-24), two days after
    # the 08-22 protocol start — not 08-25, which the naive UTC date would give.
    assert result["protocol"]["days_elapsed"] == 2
    sessions = result["sessions"]
    assert sessions["expected_trading_days"] == 1
    assert sessions["observed_trading_days"] == 1
    assert sessions["days_missing"] == []
