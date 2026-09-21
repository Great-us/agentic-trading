"""A-4 (c2c_a7e2 PLAN §五): read-only Today execution-funnel aggregation.

Contract under test (dashboard/views.funnel_summary):
- Cohort denominator = decision_buy events of THIS session (ET day, paper
  mode, before the cutoff), grouped by (run_id, symbol). Ten flush_wait
  retries on one intent are attempts, never ten decisions.
- Carryover intents (created on an earlier ET day, still flushing today) are
  listed separately and never mixed into the cohort denominator.
- Identity layers: decision (run_id+symbol) -> intent version (intent_id) ->
  order (order_id). A replaced intent's old failure stays with the decision
  that owned the old version; a new version's fill is not credited twice.
- accepted is not filled; fills evidence comes from broker fills matched by
  order_id (a match is positive evidence; no match proves nothing). A
  partial-fill activity is NOT a complete order (R5): completion = an
  order_filled observation, or a fill activity the broker itself marked
  "fill" (its completing-fill marker). Later unknown observations never
  erase earlier reliable completion evidence (R5).
- Chains are built from the FULL event history up to the cutoff, any ET day
  (R4): yesterday's submission joins today's fill via intent_id/order_id.
  Today's cohort denominator still counts only today's decision_buy events.
  Today's fills for orders with no ledger events stay visible as unlinked
  positive evidence instead of vanishing.
- Submit facts are separated: acknowledged acceptance (order_id +
  submit_status), explicit rejection (submit_status "rejected" — only for a
  positively confirmed rejection), and unknown outcome. ITERATION-3 (R5):
  run.py records a None broker result as submit_status="unknown" — an
  unknown is neither an accepted submission nor an explicit rejection. A
  NULL-order_id order_submitted is not an accepted submission.
- Identity-less order observations (R4, ITERATION-3) join a chain when the
  order_id matches EXACTLY ONE identified order_submitted event (same book,
  same mode). Conflicts (one order_id under several intents) and
  submissions without identity are listed as unattributed — never paired
  by symbol. The real three-cycle journal (run_scenario + run_cycle, the
  leader's recovery path) is fed to funnel_summary UNMODIFIED: the recovered
  order_filled observations must show confirmed completion, land in
  carryover across the ET day boundary without inflating the denominator,
  and prove completion with fills=None.
- Old schema / identity-less rows / failed or truncated fills degrade
  explicitly — nothing is paired by symbol+near-time, nothing is invented.
  The identity-less gate (R6, ITERATION-3) covers EVERY kind that needs an
  intent identity — the legacy flush five AND flush_wait / intent_cleared /
  order_submitted / order_* / clear_failed — listed per mode (paper and
  dry_run stay distinguishable; mode-NULL history is an unsplit reference).

Everything runs on temporary journals; `fills` is plain data (no network).
One test pins the aggregation itself as read-only (identical dump before and
after) and idempotent.
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))  # sibling scenario module import

from agentic_trading.dashboard.views import connect_ro, funnel_summary
from agentic_trading.journal.logger import connect, record_cycle, record_intent_event

# ET session under test: 2026-09-20 (pinned by the latest paper cycle in the
# fixture). NOW = 2026-09-21T02:00Z = 2026-09-20 22:00 ET — the cutoff.
NOW = datetime(2026, 9, 21, 2, 0, tzinfo=timezone.utc)


def t(day: int, hour: int, minute: int) -> str:
    return f"2026-09-{day:02d}T{hour:02d}:{minute:02d}:00+00:00"


def rid(tag: str) -> str:
    return f"Trading:2026-09-20T13:00:00+00:00:{tag}"


def dk(run_id: str, symbol: str) -> str:
    return f"{run_id}:{symbol}"


# A distinctive sizing snapshot (the PLAN's budget example): available $65.62
# below the $390.23 minimum. The aggregation must echo it from the event
# payload, never recompute it.
SIZING_VETO = {
    "cash_available": 65.62,
    "exposure_room": 0.0,
    "position_cap": 975.5,
    "stop_risk_room_pct": None,
    "stop_risk_room_notional": None,
    "sector_theme_room": 1200.0,
    "pre_haircut_notional": 975.5,
    "post_haircut_notional": 975.5,
    "available_notional": 65.62,
    "min_position_notional": 390.23,
    "binding_constraints": ["exposure_cap", "cash"],
    "reject_code": "below_min_position",
}
SIZING_OK = {
    "cash_available": 5000.0,
    "exposure_room": 3000.0,
    "position_cap": 1500.0,
    "stop_risk_room_pct": 0.01,
    "stop_risk_room_notional": 2000.0,
    "sector_theme_room": 2500.0,
    "pre_haircut_notional": 1500.0,
    "post_haircut_notional": 1500.0,
    "available_notional": 1500.0,
    "min_position_notional": 390.23,
    "binding_constraints": ["position_cap"],
    "reject_code": None,
}


def ev(conn, symbol, kind, *, ts_iso, run=None, mode="paper", dkey=None,
       intent=None, order=None, payload=None, deferred=False, detail=None):
    record_intent_event(conn, symbol, kind, deferred, detail, timestamp=ts_iso,
                        run_id=run, mode=mode, decision_key=dkey, intent_id=intent,
                        order_id=order, payload=payload)


@pytest.fixture()
def db(tmp_path: Path) -> Path:
    """Migrated journal whose latest paper cycle pins the session to 09-20 ET."""
    path = tmp_path / "journal.db"
    conn = connect(path)
    record_cycle(conn, "2026-09-19T15:00:00+00:00", "paper", 10_000.0, 900.0, [])
    record_cycle(conn, "2026-09-20T15:00:00+00:00", "paper", 10_050.0, 850.0, [])
    conn.close()
    return path


def summarize(db_path: Path, **kwargs):
    conn = connect_ro(db_path)
    try:
        return funnel_summary(conn, now=NOW, **kwargs)
    finally:
        conn.close()


# ---- the 3/5 rule ----------------------------------------------------------------

def test_three_of_five_funnel(db: Path):
    conn = connect(db)
    runs = [rid(f"r{i}") for i in range(5)]
    # AAA: submitted then observed filled.
    ev(conn, "AAA", "decision_buy", ts_iso=t(20, 13, 1), run=runs[0])
    ev(conn, "AAA", "intent_created", ts_iso=t(20, 13, 2), run=runs[0],
       dkey=dk(runs[0], "AAA"), intent="v-aaa")
    ev(conn, "AAA", "order_submitted", ts_iso=t(20, 14, 0), run=runs[0],
       intent="v-aaa", order="o-1",
       payload={"client_order_id": "cid-1", "reason": "risk sized", "sizing": SIZING_OK})
    ev(conn, "AAA", "order_filled", ts_iso=t(20, 14, 1), run=runs[0],
       intent="v-aaa", order="o-1",
       payload={"order_status": "filled", "filled_qty": 4, "filled_avg_price": 100.0,
                "observed_at": t(20, 14, 1)})
    # BBB: submitted, no observation, but broker fills match the order_id.
    ev(conn, "BBB", "decision_buy", ts_iso=t(20, 13, 3), run=runs[1])
    ev(conn, "BBB", "intent_created", ts_iso=t(20, 13, 4), run=runs[1],
       dkey=dk(runs[1], "BBB"), intent="v-bbb")
    ev(conn, "BBB", "order_submitted", ts_iso=t(20, 14, 2), run=runs[1],
       intent="v-bbb", order="o-2",
       payload={"client_order_id": "cid-2", "reason": "risk sized", "sizing": SIZING_OK})
    # CCC: submitted, observed accepted — NOT filled.
    ev(conn, "CCC", "decision_buy", ts_iso=t(20, 13, 5), run=runs[2])
    ev(conn, "CCC", "intent_created", ts_iso=t(20, 13, 6), run=runs[2],
       dkey=dk(runs[2], "CCC"), intent="v-ccc")
    ev(conn, "CCC", "order_submitted", ts_iso=t(20, 14, 3), run=runs[2],
       intent="v-ccc", order="o-3",
       payload={"client_order_id": "cid-3", "reason": "risk sized", "sizing": SIZING_OK})
    ev(conn, "CCC", "order_observed", ts_iso=t(20, 14, 4), run=runs[2],
       intent="v-ccc", order="o-3",
       payload={"order_status": "accepted", "observed_at": t(20, 14, 4)})
    # DDD / EEE: BUY decisions that never produced an intent, each with a reason.
    ev(conn, "DDD", "decision_buy", ts_iso=t(20, 13, 7), run=runs[3])
    ev(conn, "DDD", "intent_not_created", ts_iso=t(20, 13, 8), run=runs[3],
       detail="dry_run", payload={"reason": "dry_run"})
    ev(conn, "EEE", "decision_buy", ts_iso=t(20, 13, 9), run=runs[4])
    ev(conn, "EEE", "intent_not_created", ts_iso=t(20, 13, 10), run=runs[4],
       detail="max_new_orders", payload={"reason": "max_new_orders"})
    conn.close()

    fills = [{"id": "f-1", "order_id": "o-2", "symbol": "BBB", "side": "buy",
              "qty": 2.0, "price": 50.0, "notional": 100.0,
              "transaction_time": t(20, 14, 5), "order_status": "fill"}]
    out = summarize(db, fills=fills)
    s = out["summary"]
    assert s["decisions"] == 5
    assert s["submitted"] == 3
    assert s["submitted_frac"] == "3/5"
    assert s["with_intent"] == 3
    assert s["not_created"] == 2
    assert s["not_created_reasons"] == {"dry_run": 1, "max_new_orders": 1}
    assert s["evidence_missing"] == 0
    # REVIEW FIX (R5): the old expectation credited "any single fill" as a
    # completed order. BBB is counted complete for the CORRECT reason now —
    # its fill activity carries the broker's own completing marker
    # order_status="fill" — not because a quantity happened to be positive.
    assert s["filled_verified"] == 2  # AAA (order_filled event) + BBB (fill marked "fill")
    assert s["partial"] == 0
    assert s["waiting"] == 0
    # R5 structure: per-order phase is explicit, never to be re-derived by
    # the frontend from a status string.
    chains = {c["symbol"]: c for c in out["chains"]}
    assert chains["AAA"]["phase"] == "complete"
    assert chains["BBB"]["phase"] == "complete"
    assert chains["CCC"]["phase"] == "submitted"  # accepted ≠ filled
    assert chains["CCC"]["orders"][0]["broker_status"] == "accepted"
    # The attribution identity: every denominator decision lands in exactly
    # one of created / not_created / evidence_missing.
    assert s["with_intent"] + s["not_created"] + s["evidence_missing"] == s["decisions"]
    chains = {c["symbol"]: c for c in out["chains"]}
    assert set(chains) == {"AAA", "BBB", "CCC", "DDD", "EEE"}
    assert chains["DDD"]["stage"] == "intent_not_created"
    assert chains["DDD"]["reason"] == "dry_run"
    assert chains["EEE"]["reason"] == "max_new_orders"
    assert out["schema_degraded"] is False
    assert out["session_date"] == "2026-09-20"


# ---- ten retries are attempts, not decisions -------------------------------------

def test_ten_flush_waits_do_not_inflate_denominator(db: Path):
    conn = connect(db)
    r0 = rid("w0")
    ev(conn, "AAA", "decision_buy", ts_iso=t(20, 13, 0), run=r0)
    ev(conn, "AAA", "intent_created", ts_iso=t(20, 13, 1), run=r0,
       dkey=dk(r0, "AAA"), intent="v-w")
    for i in range(10):
        ev(conn, "AAA", "flush_wait", ts_iso=t(20, 14, i), run=rid(f"w{i + 1}"),
           intent="v-w", deferred=True, detail="not_before",
           payload={"reason": "not_before", "not_before": t(20, 15, 0)})
    conn.close()

    out = summarize(db, fills=[])
    s = out["summary"]
    assert s["decisions"] == 1
    assert s["with_intent"] == 1
    assert s["submitted"] == 0
    assert s["waiting"] == 1
    chain = out["chains"][0]
    assert chain["wait_events"] == 10
    # REVIEW FIX (R6): attempts = flush attempts — distinct runs doing
    # flush-stage work. The creation run is NOT an attempt: ten flushes are
    # ten attempts, not eleven (the old count included the creating run).
    # Creation and status-observation runs are listed separately.
    assert chain["attempts"] == 10
    assert chain["created_runs"] == 1
    assert chain["observed_runs"] == 0
    assert chain["intent_id"] == "v-w"


# ---- carryover intents live outside the denominator -------------------------------

def test_carryover_intent_shown_separately(db: Path):
    conn = connect(db)
    # Yesterday's decision + intent (ET 2026-09-19).
    ry = rid("y0")
    ev(conn, "OLD", "decision_buy", ts_iso=t(19, 13, 0), run=ry)
    ev(conn, "OLD", "intent_created", ts_iso=t(19, 13, 1), run=ry,
       dkey=dk(ry, "OLD"), intent="v-old")
    # Today: that old intent keeps flushing (2 waits, then a sizing veto).
    ev(conn, "OLD", "flush_wait", ts_iso=t(20, 14, 10), run=rid("t1"),
       intent="v-old", deferred=True, detail="not_before",
       payload={"reason": "not_before"})
    ev(conn, "OLD", "flush_wait", ts_iso=t(20, 14, 30), run=rid("t2"),
       intent="v-old", deferred=True, detail="not_before",
       payload={"reason": "not_before"})
    ev(conn, "OLD", "sizing", ts_iso=t(20, 15, 0), run=rid("t2"),
       intent="v-old", deferred=True, detail="only $65.62 available",
       payload={"reason": "only $65.62 available", "cash_available": 65.62,
                "available_notional": 65.62, "min_notional": 390.23,
                "sizing": SIZING_VETO})
    # One fresh decision today (the cohort denominator).
    rn = rid("n0")
    ev(conn, "FRESH", "decision_buy", ts_iso=t(20, 14, 5), run=rn)
    ev(conn, "FRESH", "intent_created", ts_iso=t(20, 14, 6), run=rn,
       dkey=dk(rn, "FRESH"), intent="v-new")
    ev(conn, "FRESH", "order_submitted", ts_iso=t(20, 14, 7), run=rn,
       intent="v-new", order="o-9", payload={"client_order_id": "cid-9"})
    conn.close()

    out = summarize(db, fills=[])
    s = out["summary"]
    assert s["decisions"] == 1  # FRESH only — the carryover is NOT in the cohort
    assert s["submitted"] == 1
    assert s["carryover_intents"] == 1
    assert [c["symbol"] for c in out["chains"]] == ["FRESH"]
    carry = out["carryover"][0]
    assert carry["intent_id"] == "v-old"
    assert carry["symbol"] == "OLD"
    assert carry["created_on"] == "2026-09-19"
    assert carry["created_by_decision"] == dk(ry, "OLD")
    # REVIEW FIX (R6): attempts counts flush attempts only — the creation run
    # is excluded (two today runs: two flush_waits + one sizing on t2).
    assert carry["attempts"] == 2
    assert carry["created_runs"] == 1
    assert carry["observed_runs"] == 0
    assert carry["wait_events"] == 2
    assert carry["stage"] == "sizing"  # latest known stage today
    assert carry["classification"] == "waiting"


# ---- replacement: the old version's failure must not pollute the new one ---------

def test_replaced_intent_versions_stay_separate(db: Path):
    conn = connect(db)
    # Decision 1 creates v1, which gets a sizing veto (a failure).
    r1 = rid("p1")
    ev(conn, "AAA", "decision_buy", ts_iso=t(20, 13, 0), run=r1)
    ev(conn, "AAA", "intent_created", ts_iso=t(20, 13, 1), run=r1,
       dkey=dk(r1, "AAA"), intent="v1")
    ev(conn, "AAA", "sizing", ts_iso=t(20, 14, 0), run=r1, intent="v1",
       deferred=True, detail="only $65.62 available",
       payload={"reason": "only $65.62 available", "cash_available": 65.62,
                "available_notional": 65.62, "min_notional": 390.23,
                "sizing": SIZING_VETO})
    # Decision 2 replaces it with v2, which submits and fills.
    r2 = rid("p2")
    ev(conn, "AAA", "decision_buy", ts_iso=t(20, 15, 0), run=r2)
    ev(conn, "AAA", "intent_replaced", ts_iso=t(20, 15, 1), run=r2,
       dkey=dk(r2, "AAA"), intent="v2", payload={"previous_version": "v1"})
    ev(conn, "AAA", "order_submitted", ts_iso=t(20, 15, 2), run=r2,
       intent="v2", order="o-2", payload={"client_order_id": "cid-2"})
    ev(conn, "AAA", "order_filled", ts_iso=t(20, 15, 3), run=r2,
       intent="v2", order="o-2",
       payload={"order_status": "filled", "filled_qty": 5, "filled_avg_price": 210.0,
                "observed_at": t(20, 15, 3)})
    conn.close()

    out = summarize(db, fills=[])
    s = out["summary"]
    assert s["decisions"] == 2
    assert s["submitted"] == 1      # only decision 2 reached an order
    assert s["filled_verified"] == 1  # the fill is credited to ONE decision
    # REVIEW FIX (R5/R6 display): the replaced v1 was asserted as "still
    # waiting" — that hid the replacement. The old chain must say it was
    # superseded (by which version) while KEEPING its sizing-failure history.
    assert s["waiting"] == 0
    assert s["superseded"] == 1
    chains = {c["run_id"]: c for c in out["chains"]}
    assert chains[r1]["stage"] == "sizing"
    assert chains[r1]["classification"] == "superseded"
    assert chains[r1]["superseded_by"] == "v2"
    assert chains[r1]["orders"] == []
    assert chains[r2]["stage"] == "order_filled"
    assert chains[r2]["replaced_previous_version"] == "v1"
    assert chains[r2]["intent_id"] == "v2"
    # The sizing snapshot comes from the event payload, verbatim.
    snap = chains[r1]["sizing"]
    assert snap["available_notional"] == 65.62
    assert snap["min_notional"] == 390.23
    assert snap["sizing"]["reject_code"] == "below_min_position"
    assert snap["sizing"]["binding_constraints"] == ["exposure_cap", "cash"]


# ---- accepted is not filled --------------------------------------------------------

def test_accepted_without_fill_is_not_a_fill(db: Path):
    conn = connect(db)
    r = rid("a0")
    ev(conn, "AAA", "decision_buy", ts_iso=t(20, 13, 0), run=r)
    ev(conn, "AAA", "intent_created", ts_iso=t(20, 13, 1), run=r,
       dkey=dk(r, "AAA"), intent="v-a")
    ev(conn, "AAA", "order_submitted", ts_iso=t(20, 14, 0), run=r,
       intent="v-a", order="o-a", payload={"client_order_id": "cid-a"})
    ev(conn, "AAA", "order_observed", ts_iso=t(20, 14, 1), run=r,
       intent="v-a", order="o-a",
       payload={"order_status": "accepted", "observed_at": t(20, 14, 1)})
    conn.close()

    out = summarize(db, fills=[])  # fills available, no match for o-a
    s = out["summary"]
    assert s["submitted"] == 1
    assert s["filled_verified"] == 0
    assert s["partial"] == 0
    order = out["chains"][0]["orders"][0]
    assert order["broker_status"] == "accepted"
    assert order["last_observation_kind"] == "order_observed"
    assert not order["filled_qty"]
    assert order["fills_matched"] is False  # no match — proves nothing either way


# ---- partial fill ------------------------------------------------------------------

def test_partial_fill_multiple_observations_one_order(db: Path):
    conn = connect(db)
    r = rid("q0")
    ev(conn, "AAA", "decision_buy", ts_iso=t(20, 13, 0), run=r)
    ev(conn, "AAA", "intent_created", ts_iso=t(20, 13, 1), run=r,
       dkey=dk(r, "AAA"), intent="v-q")
    ev(conn, "AAA", "order_submitted", ts_iso=t(20, 14, 0), run=r,
       intent="v-q", order="o-q", payload={"client_order_id": "cid-q"})
    ev(conn, "AAA", "order_partial", ts_iso=t(20, 14, 5), run=r,
       intent="v-q", order="o-q",
       payload={"order_status": "partially_filled", "filled_qty": 3,
                "filled_avg_price": 99.0, "observed_at": t(20, 14, 5)})
    ev(conn, "AAA", "order_partial", ts_iso=t(20, 14, 10), run=r,
       intent="v-q", order="o-q",
       payload={"order_status": "partially_filled", "filled_qty": 5,
                "filled_avg_price": 99.4, "observed_at": t(20, 14, 10)})
    conn.close()

    fills = [{"id": "f-q", "order_id": "o-q", "symbol": "AAA", "side": "buy",
              "qty": 5.0, "price": 99.4, "notional": 497.0,
              "transaction_time": t(20, 14, 12), "order_status": "partial_fill"}]
    out = summarize(db, fills=fills)
    chain = out["chains"][0]
    assert chain["stage"] == "order_partial"
    # Two observations of one order are ONE order.
    assert len(chain["orders"]) == 1
    order = chain["orders"][0]
    assert order["filled_qty"] == 5
    assert order["fills_matched"] is True
    assert order["fills_qty"] == 5.0
    s = out["summary"]
    assert s["partial"] == 1
    assert s["filled_verified"] == 0  # one fill activity is not a complete fill


# ---- mode separation ---------------------------------------------------------------

def test_paper_dry_run_backtest_are_separated(db: Path):
    conn = connect(db)
    rp, rd, rb = rid("m0"), rid("m1"), rid("m2")
    ev(conn, "AAA", "decision_buy", ts_iso=t(20, 13, 0), run=rp)
    ev(conn, "AAA", "intent_created", ts_iso=t(20, 13, 1), run=rp,
       dkey=dk(rp, "AAA"), intent="v-p")
    ev(conn, "BBB", "decision_buy", ts_iso=t(20, 13, 2), run=rd, mode="dry_run")
    ev(conn, "BBB", "intent_created", ts_iso=t(20, 13, 3), run=rd, mode="dry_run",
       dkey=dk(rd, "BBB"), intent="v-d")
    ev(conn, "CCC", "decision_buy", ts_iso=t(20, 13, 4), run=rb, mode="backtest")
    conn.close()

    paper = summarize(db, fills=[])
    assert paper["summary"]["decisions"] == 1
    assert paper["summary"]["with_intent"] == 1
    assert [c["symbol"] for c in paper["chains"]] == ["AAA"]

    dry = summarize(db, fills=[], mode="dry_run")
    assert dry["summary"]["decisions"] == 1
    assert [c["symbol"] for c in dry["chains"]] == ["BBB"]

    backtest = summarize(db, fills=[], mode="backtest")
    assert backtest["summary"]["decisions"] == 1
    assert backtest["summary"]["with_intent"] == 0


# ---- ET/UTC day boundary + cutoff ---------------------------------------------------

def test_et_utc_boundary_and_cutoff(db: Path):
    conn = connect(db)
    # A: 2026-09-20T03:30Z == 2026-09-19 23:30 ET -> belongs to the 19th.
    ev(conn, "AAA", "decision_buy", ts_iso=t(20, 3, 30), run=rid("b1"))
    # H: 2026-09-20T15:00Z == 11:00 ET on the 20th -> in session.
    ev(conn, "HHH", "decision_buy", ts_iso=t(20, 15, 0), run=rid("b2"))
    # G: 2026-09-21T01:00Z == 2026-09-20 21:00 ET -> in session, before NOW.
    ev(conn, "GGG", "decision_buy", ts_iso=t(21, 1, 0), run=rid("b3"))
    # B: 2026-09-21T03:30Z == 23:30 ET on the 20th, but AFTER NOW (02:00Z) -> cut off.
    ev(conn, "BBB", "decision_buy", ts_iso=t(21, 3, 30), run=rid("b4"))
    # C: 2026-09-21T05:00Z == 2026-09-21 01:00 ET -> next session day.
    ev(conn, "CCC", "decision_buy", ts_iso=t(21, 5, 0), run=rid("b5"))
    conn.close()

    out = summarize(db, fills=[])
    symbols = {c["symbol"] for c in out["chains"]}
    assert symbols == {"HHH", "GGG"}
    assert out["summary"]["decisions"] == 2
    assert out["summary"]["evidence_missing"] == 2  # both bare decision_buys


# ---- scale: the SQLite ledger, not the 120-line JSONL tail --------------------------

def test_more_than_jsonl_tail_event_counts(db: Path):
    conn = connect(db)
    # 25 decisions, each with a structured not_created reason.
    for i in range(25):
        run = rid(f"s{i}")
        ev(conn, f"S{i:02d}", "decision_buy", ts_iso=t(20, 13, 0), run=run)
        ev(conn, f"S{i:02d}", "intent_not_created", ts_iso=t(20, 13, 1), run=run,
           detail="max_new_orders", payload={"reason": "max_new_orders"})
    # One carryover intent absorbing 150 flush_wait events today.
    ry = rid("sy")
    ev(conn, "BIG", "decision_buy", ts_iso=t(19, 13, 0), run=ry)
    ev(conn, "BIG", "intent_created", ts_iso=t(19, 13, 1), run=ry,
       dkey=dk(ry, "BIG"), intent="v-big")
    for i in range(150):
        ev(conn, "BIG", "flush_wait", ts_iso=t(20, 12, 0), run=rid(f"sy{i}"),
           intent="v-big", deferred=True, detail="no_quote",
           payload={"reason": "no_quote"})
    conn.close()

    out = summarize(db, fills=[])
    s = out["summary"]
    assert s["decisions"] == 25
    assert s["not_created"] == 25
    assert s["not_created_reasons"] == {"max_new_orders": 25}
    assert s["carryover_intents"] == 1
    assert out["carryover"][0]["wait_events"] == 150
    # REVIEW FIX (R6): attempts = the 150 flush runs, creation run excluded.
    assert out["carryover"][0]["attempts"] == 150
    assert out["carryover"][0]["created_runs"] == 1
    assert out["carryover"][0]["observed_runs"] == 0


# ---- old schema degrades explicitly --------------------------------------------------

def test_old_schema_degrades_without_crashing(tmp_path: Path):
    path = tmp_path / "old.db"
    raw = sqlite3.connect(path)
    raw.executescript(
        """
        CREATE TABLE intent_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL, symbol TEXT NOT NULL, kind TEXT NOT NULL,
            deferred INTEGER NOT NULL, detail TEXT
        );
        """
    )
    raw.executemany(
        "INSERT INTO intent_events (timestamp, symbol, kind, deferred, detail) VALUES (?,?,?,?,?)",
        [
            ("2026-09-20T14:10:00+00:00", "AAA", "sizing", 1, "only $0 available"),
            ("2026-09-20T14:20:00+00:00", "BBB", "ttl", 0, "72h old"),
            ("2026-09-18T14:20:00+00:00", "OLD", "gap", 0, "before this session"),
        ],
    )
    raw.commit()
    raw.close()

    conn = connect_ro(path)
    try:
        # No cycles table either — the session day must fall back to ET of `now`.
        out = funnel_summary(conn, now=NOW, fills=[])
    finally:
        conn.close()
    assert out["schema_degraded"] is True
    assert "run_id" in out["schema_reason"]
    assert out["legacy_event_counts"] == {"sizing": 1, "ttl": 1}  # ET-day filtered
    assert out["chains"] == []
    assert out["summary"]["decisions"] == 0
    assert out["session_date"] == "2026-09-20"


def test_identity_less_rows_in_migrated_db_count_as_degraded(db: Path):
    conn = connect(db)
    # Legacy-style row (identity columns all NULL) inside a migrated journal.
    record_intent_event(conn, "2026-09-20T14:05:00+00:00", "CCC", "sizing",
                        deferred=True, detail="only $0 available")
    # A decision_buy with no run_id — unattributable, never a fabricated chain.
    ev(conn, "DDD", "decision_buy", ts_iso=t(20, 14, 6), run=None, mode=None)
    conn.close()

    out = summarize(db, fills=[])
    assert out["schema_degraded"] is False
    assert out["summary"]["decisions"] == 0
    d = out["degraded"]
    assert d["unattributed_decision_events"] == 1
    assert d["legacy_flush_events"] == 1


# ---- fills failure / truncation ------------------------------------------------------

def test_fills_failure_and_truncation_degrade_explicitly(db: Path):
    conn = connect(db)
    r = rid("f0")
    ev(conn, "AAA", "decision_buy", ts_iso=t(20, 13, 0), run=r)
    ev(conn, "AAA", "intent_created", ts_iso=t(20, 13, 1), run=r,
       dkey=dk(r, "AAA"), intent="v-f")
    ev(conn, "AAA", "order_submitted", ts_iso=t(20, 14, 0), run=r,
       intent="v-f", order="o-f", payload={"client_order_id": "cid-f"})
    conn.close()

    failed = summarize(db, fills=None, fills_reason="URLError: timed out")
    assert failed["degraded"]["fills"] == {
        "degraded": True, "reason": "URLError: timed out", "truncated": None,
    }
    order = failed["chains"][0]["orders"][0]
    assert order["fills_matched"] is None      # unavailable — not "no fill"
    assert order["fills_unavailable"] is True
    assert failed["summary"]["filled_verified"] == 0  # nothing fabricated

    truncated_fills = [{"id": "f-2", "order_id": "o-f", "symbol": "AAA",
                        "side": "buy", "qty": 1.0, "price": 80.0, "notional": 80.0,
                        "transaction_time": t(20, 14, 1), "order_status": "partial_fill"}]
    partial_view = summarize(db, fills=truncated_fills, fills_truncated=True)
    assert partial_view["degraded"]["fills"]["truncated"] is True
    assert partial_view["degraded"]["fills"]["degraded"] is False
    # A matched fill inside a truncated window is still positive evidence —
    # but REVIEW FIX (R5): a single partial_fill activity is NOT a completed
    # order. The old fixture used order_status="fill" and asserted
    # filled_verified==1, encoding the exact wrong rule the review called
    # out; the honest outcome is partial.
    assert partial_view["chains"][0]["orders"][0]["fills_matched"] is True
    assert partial_view["summary"]["filled_verified"] == 0
    assert partial_view["summary"]["partial"] == 1
    assert partial_view["chains"][0]["phase"] == "partial"


# ---- read-only + idempotent ----------------------------------------------------------

def test_aggregation_reads_but_never_writes(db: Path):
    conn = connect(db)
    r = rid("ro0")
    ev(conn, "AAA", "decision_buy", ts_iso=t(20, 13, 0), run=r)
    ev(conn, "AAA", "intent_created", ts_iso=t(20, 13, 1), run=r,
       dkey=dk(r, "AAA"), intent="v-ro")
    ev(conn, "AAA", "sizing", ts_iso=t(20, 14, 0), run=r, intent="v-ro",
       deferred=True, detail="only $65.62 available",
       payload={"reason": "only $65.62 available", "cash_available": 65.62,
                "available_notional": 65.62, "min_notional": 390.23,
                "sizing": SIZING_VETO})
    conn.close()

    ro = connect_ro(db)
    try:
        before = "\n".join(ro.iterdump())
        first = funnel_summary(ro, now=NOW, fills=[])
        second = funnel_summary(ro, now=NOW, fills=[])
        after = "\n".join(ro.iterdump())
    finally:
        ro.close()
    assert before == after          # no mutation through the read-only path
    assert first == second          # deterministic on the same inputs


# ---- run-level flush skips are listed, not chains ------------------------------------

def test_run_level_flush_skips_listed_separately(db: Path):
    conn = connect(db)
    ev(conn, "*", "flush_skipped", ts_iso=t(20, 12, 0), run=rid("k1"),
       detail="outside_window", payload={"reason": "outside_window"})
    ev(conn, "*", "flush_skipped", ts_iso=t(20, 16, 30), run=rid("k2"),
       detail="market_closed", payload={"reason": "market_closed"})
    conn.close()

    out = summarize(db, fills=[])
    assert out["summary"]["decisions"] == 0
    assert [s["reason"] for s in out["run_skips"]] == ["outside_window", "market_closed"]


# ---- intents without a cohort decision never vanish silently --------------------------

def test_orphan_and_unknown_origin_intents_are_visible(db: Path):
    conn = connect(db)
    # Created today with a decision_key whose decision_buy event is missing.
    ev(conn, "ORPH", "intent_created", ts_iso=t(20, 13, 1), run=rid("x1"),
       dkey=dk(rid("x1"), "ORPH"), intent="v-orph")
    # Activity today with no creation event anywhere (origin unknown).
    ev(conn, "GHOST", "flush_wait", ts_iso=t(20, 14, 0), run=rid("x2"),
       intent="v-ghost", deferred=True, detail="not_before",
       payload={"reason": "not_before"})
    conn.close()

    out = summarize(db, fills=[])
    assert out["summary"]["decisions"] == 0
    assert out["carryover"] == []
    d = out["degraded"]
    assert [o["intent_id"] for o in d["orphan_intents"]] == ["v-orph"]
    assert [o["intent_id"] for o in d["unknown_origin_intents"]] == ["v-ghost"]


# ==== R5: state semantics ============================================================

# ---- R5 red case 1: accepted + ONE partial_fill activity is NOT a completion -------

def test_single_partial_fill_activity_is_not_complete(db: Path):
    # The exact ChatGPT R5 case: accepted order + one partial_fill activity,
    # no successful full observation. The OLD rule (qty_positive and
    # last_kind != "order_partial" → filled_verified) called this complete;
    # that was wrong and the tests encoded it. A partial quantity is a
    # partial, never a completion.
    conn = connect(db)
    r = rid("sp0")
    ev(conn, "AAA", "decision_buy", ts_iso=t(20, 13, 0), run=r)
    ev(conn, "AAA", "intent_created", ts_iso=t(20, 13, 1), run=r,
       dkey=dk(r, "AAA"), intent="v-sp")
    ev(conn, "AAA", "order_submitted", ts_iso=t(20, 14, 0), run=r,
       intent="v-sp", order="o-sp",
       payload={"client_order_id": "cid-sp", "submit_status": "accepted"})
    ev(conn, "AAA", "order_observed", ts_iso=t(20, 14, 1), run=r,
       intent="v-sp", order="o-sp",
       payload={"order_status": "accepted", "observed_at": t(20, 14, 1)})
    conn.close()

    fills = [{"id": "fa-1", "order_id": "o-sp", "symbol": "AAA", "side": "buy",
              "qty": 1.0, "price": 100.0, "notional": 100.0,
              "transaction_time": t(20, 14, 2), "order_status": "partial_fill"}]
    out = summarize(db, fills=fills)
    order = out["chains"][0]["orders"][0]
    assert order["phase"] == "partial"      # fills exist, order not complete
    assert order["phase"] != "complete"
    s = out["summary"]
    assert s["submitted"] == 1              # still a real acknowledged submission
    assert s["filled_verified"] == 0
    assert s["partial"] == 1


# ---- R5 red case 2: partial fills then canceled — terminal, still not complete -----

def test_partial_then_canceled_is_not_complete(db: Path):
    # ITERATION-3 recorder shape: a canceled order with a partial fill is
    # recorded as kind=order_partial carrying BOTH facts in the payload —
    # order_status="canceled" AND filled_qty (execution side R5). A realistic
    # journal can also hold the earlier order_partial(partially_filled) from
    # a previous cycle's observation. The terminal fact must be read off
    # payload.order_status REGARDLESS of the event kind, the partial quantity
    # must survive, and the order must NOT count as an entire-order completion.
    conn = connect(db)
    r = rid("pc0")
    ev(conn, "AAA", "decision_buy", ts_iso=t(20, 13, 0), run=r)
    ev(conn, "AAA", "intent_created", ts_iso=t(20, 13, 1), run=r,
       dkey=dk(r, "AAA"), intent="v-pc")
    ev(conn, "AAA", "order_submitted", ts_iso=t(20, 14, 0), run=r,
       intent="v-pc", order="o-pc",
       payload={"client_order_id": "cid-pc", "submit_status": "accepted"})
    ev(conn, "AAA", "order_partial", ts_iso=t(20, 14, 5), run=r,
       intent="v-pc", order="o-pc",
       payload={"order_status": "partially_filled", "filled_qty": 2,
                "filled_avg_price": 100.0, "observed_at": t(20, 14, 5)})
    # this cycle's end-of-pass observation: canceled WITH the partial quantity
    ev(conn, "AAA", "order_partial", ts_iso=t(20, 14, 30), run=r,
       intent="v-pc", order="o-pc",
       payload={"order_status": "canceled", "filled_qty": 2.0,
                "filled_avg_price": 100.0, "observed_at": t(20, 14, 30)})
    conn.close()

    fills = [
        {"id": "fb-1", "order_id": "o-pc", "symbol": "AAA", "side": "buy",
         "qty": 2.0, "price": 100.0, "notional": 200.0,
         "transaction_time": t(20, 14, 6), "order_status": "partial_fill"},
    ]
    out = summarize(db, fills=fills)
    order = out["chains"][0]["orders"][0]
    # the terminal non-fill status is visible even though the event is NOT
    # order_observed…
    assert order["broker_status"] == "canceled"
    assert order["terminal_status"] == "canceled"
    # …and the partial quantity survives next to it
    assert order["filled_qty"] == 2.0
    assert order["phase"] == "partial"
    assert order["phase"] != "complete"
    s = out["summary"]
    assert s["filled_verified"] == 0
    assert s["partial"] == 1


# ---- R5 red case 3: filled then unknown — reliable completion must not regress ------

def test_filled_then_unknown_does_not_regress(db: Path):
    # order_filled was observed; a LATER observation failed (order_unknown,
    # fills feed unavailable). The confirmed completion must survive — the
    # old code kept only last_observation_kind, so the completion vanished
    # from the count as soon as the newest observation was unknown.
    conn = connect(db)
    r = rid("fu0")
    ev(conn, "AAA", "decision_buy", ts_iso=t(20, 13, 0), run=r)
    ev(conn, "AAA", "intent_created", ts_iso=t(20, 13, 1), run=r,
       dkey=dk(r, "AAA"), intent="v-fu")
    ev(conn, "AAA", "order_submitted", ts_iso=t(20, 14, 0), run=r,
       intent="v-fu", order="o-fu",
       payload={"client_order_id": "cid-fu", "submit_status": "accepted"})
    ev(conn, "AAA", "order_filled", ts_iso=t(20, 14, 1), run=r,
       intent="v-fu", order="o-fu",
       payload={"order_status": "filled", "filled_qty": 4,
                "filled_avg_price": 100.0, "observed_at": t(20, 14, 1)})
    ev(conn, "AAA", "order_unknown", ts_iso=t(20, 15, 0), run=r,
       intent="v-fu", order="o-fu",
       payload={"reason": "observe_unavailable"})
    conn.close()

    out = summarize(db, fills=None)     # fills unavailable on the later pass
    order = out["chains"][0]["orders"][0]
    assert order["last_observation_kind"] == "order_unknown"
    assert order["last_observation_failed"] is True
    assert order["phase"] == "complete"        # reliable history kept
    s = out["summary"]
    assert s["filled_verified"] == 1


# ---- R5 red case 4: a completing fill activity proves completion --------------------

def test_completing_fill_activity_proves_completion(db: Path):
    # Positive rule: the broker marks the activity that completed the order
    # with order_status="fill". With no observation event at all, that marker
    # is completion evidence — this is why BBB in the 3/5 test counts, and a
    # "partial_fill"-marked activity (case 1) does not.
    conn = connect(db)
    r = rid("cf0")
    ev(conn, "AAA", "decision_buy", ts_iso=t(20, 13, 0), run=r)
    ev(conn, "AAA", "intent_created", ts_iso=t(20, 13, 1), run=r,
       dkey=dk(r, "AAA"), intent="v-cf")
    ev(conn, "AAA", "order_submitted", ts_iso=t(20, 14, 0), run=r,
       intent="v-cf", order="o-cf",
       payload={"client_order_id": "cid-cf", "submit_status": "accepted"})
    conn.close()

    fills = [{"id": "fc-1", "order_id": "o-cf", "symbol": "AAA", "side": "buy",
              "qty": 4.0, "price": 100.0, "notional": 400.0,
              "transaction_time": t(20, 14, 2), "order_status": "fill"}]
    out = summarize(db, fills=fills)
    order = out["chains"][0]["orders"][0]
    assert order["phase"] == "complete"
    assert out["summary"]["filled_verified"] == 1
    assert out["summary"]["partial"] == 0


# ---- R5 red case 5: a None submit is UNKNOWN — never accepted, never rejected -------

def test_submit_none_records_unknown_not_rejected(db: Path):
    # ITERATION-3 run.py shape: a None broker result (submit raised, request
    # lost, response unreadable) is recorded with submit_status="unknown" and
    # NO order_id. It must stay out of accepted submissions AND out of the
    # explicit-rejection bucket — "unknown" is its own honest state. (The old
    # expectation here claimed run.py wrote "rejected" for None; that claim
    # was the bug.)
    conn = connect(db)
    r = rid("rj0")
    ev(conn, "REJ", "decision_buy", ts_iso=t(20, 13, 0), run=r)
    ev(conn, "REJ", "intent_created", ts_iso=t(20, 13, 1), run=r,
       dkey=dk(r, "REJ"), intent="v-rj")
    ev(conn, "REJ", "order_submitted", ts_iso=t(20, 14, 0), run=r,
       intent="v-rj", order=None,
       payload={"submit_status": "unknown", "notional": 500.0,
                "reason": "risk sized", "sizing": None})
    conn.close()

    out = summarize(db, fills=[])
    order = out["chains"][0]["orders"][0]
    assert order["order_id"] is None
    assert order["submit_status"] == "unknown"
    assert order["submit_phase"] == "submit_unknown"
    assert order["phase"] == "submit_unknown"
    s = out["summary"]
    assert s["submitted"] == 0               # NOT an accepted submission
    assert s["submit_rejected"] == 0         # and NOT a confirmed rejection
    assert s["submit_unknown"] == 1          # its own bucket
    assert out["chains"][0]["classification"] == "submit_unknown"
    assert out["chains"][0]["phase"] == "submit_unknown"


# ---- R5 red case 6: an attempt without a readable acknowledgement is unknown --------

def test_submit_attempt_without_order_id_or_status_is_unknown(db: Path):
    # order_submitted with neither an order_id nor a submit_status: an
    # attempt whose outcome cannot be determined. Not submitted, not
    # rejected — its own bucket.
    conn = connect(db)
    r = rid("uk0")
    ev(conn, "UNK", "decision_buy", ts_iso=t(20, 13, 0), run=r)
    ev(conn, "UNK", "intent_created", ts_iso=t(20, 13, 1), run=r,
       dkey=dk(r, "UNK"), intent="v-uk")
    ev(conn, "UNK", "order_submitted", ts_iso=t(20, 14, 0), run=r,
       intent="v-uk", order=None, payload={"reason": "response lost"})
    conn.close()

    out = summarize(db, fills=[])
    order = out["chains"][0]["orders"][0]
    assert order["submit_phase"] == "submit_unknown"
    assert order["phase"] == "submit_unknown"
    s = out["summary"]
    assert s["submitted"] == 0
    assert s["submit_rejected"] == 0
    assert s["submit_unknown"] == 1
    assert out["chains"][0]["classification"] == "submit_unknown"


# ---- R5 red case 7: acknowledgement vs unrecorded acknowledgement, one view ---------

def test_submit_acknowledgement_is_distinguished(db: Path):
    # order_id + submit_status → submitted_accepted (受理).
    # order_id present but submit_status never recorded (older events) →
    # submit_unknown: a real order exists, but the acknowledgement itself is
    # unverified. Both create a real order, so both count as submitted — the
    # DISTINCTION lives on the order, not by merging the numbers.
    conn = connect(db)
    ra, rb = rid("sa0"), rid("sa1")
    ev(conn, "AAA", "decision_buy", ts_iso=t(20, 13, 0), run=ra)
    ev(conn, "AAA", "intent_created", ts_iso=t(20, 13, 1), run=ra,
       dkey=dk(ra, "AAA"), intent="v-sa")
    ev(conn, "AAA", "order_submitted", ts_iso=t(20, 14, 0), run=ra,
       intent="v-sa", order="o-sa",
       payload={"client_order_id": "cid-sa", "submit_status": "accepted"})
    ev(conn, "BBB", "decision_buy", ts_iso=t(20, 13, 2), run=rb)
    ev(conn, "BBB", "intent_created", ts_iso=t(20, 13, 3), run=rb,
       dkey=dk(rb, "BBB"), intent="v-sb")
    ev(conn, "BBB", "order_submitted", ts_iso=t(20, 14, 1), run=rb,
       intent="v-sb", order="o-sb",
       payload={"client_order_id": "cid-sb"})   # pre-submit_status era event
    conn.close()

    out = summarize(db, fills=[])
    chains = {c["symbol"]: c for c in out["chains"]}
    assert chains["AAA"]["orders"][0]["submit_phase"] == "submitted_accepted"
    assert chains["BBB"]["orders"][0]["submit_phase"] == "submit_unknown"
    assert chains["BBB"]["orders"][0]["submit_status"] is None
    s = out["summary"]
    assert s["submitted"] == 2
    assert s["submit_rejected"] == 0
    assert s["submit_unknown"] == 0    # both chains reached a real order


# ==== R5 tail: fill indexing =========================================================

# ---- duplicate activities count once -------------------------------------------------

def test_duplicate_fill_activities_are_deduped(db: Path):
    # R5 tail: the fill index dedupes by activity id. The same activity
    # listed twice must be counted once — a duplicated feed row is not a
    # second execution.
    conn = connect(db)
    r = rid("dp0")
    ev(conn, "AAA", "decision_buy", ts_iso=t(20, 13, 0), run=r)
    ev(conn, "AAA", "intent_created", ts_iso=t(20, 13, 1), run=r,
       dkey=dk(r, "AAA"), intent="v-dp")
    ev(conn, "AAA", "order_submitted", ts_iso=t(20, 14, 0), run=r,
       intent="v-dp", order="o-dp",
       payload={"client_order_id": "cid-dp", "submit_status": "accepted"})
    conn.close()

    fill = {"id": "act-1", "order_id": "o-dp", "symbol": "AAA", "side": "buy",
            "qty": 5.0, "price": 100.0, "notional": 500.0,
            "transaction_time": t(20, 14, 2), "order_status": "fill"}
    out = summarize(db, fills=[fill, dict(fill)])
    order = out["chains"][0]["orders"][0]
    assert order["fills_matched"] is True
    assert order["fills_qty"] == 5.0            # once, not 10.0
    assert order["fills_notional"] == 500.0
    assert out["degraded"]["duplicate_fill_activities"] == 1
    assert out["summary"]["filled_verified"] == 1


# ---- fills after the cutoff never enter ----------------------------------------------

def test_fills_after_cutoff_do_not_enter(db: Path):
    # R5 tail: the fill index applies transaction_time <= now. With a
    # historical cutoff, executions AFTER it must not leak in as evidence.
    conn = connect(db)
    r = rid("co0")
    ev(conn, "AAA", "decision_buy", ts_iso=t(20, 13, 0), run=r)
    ev(conn, "AAA", "intent_created", ts_iso=t(20, 13, 1), run=r,
       dkey=dk(r, "AAA"), intent="v-co")
    ev(conn, "AAA", "order_submitted", ts_iso=t(20, 14, 0), run=r,
       intent="v-co", order="o-co",
       payload={"client_order_id": "cid-co", "submit_status": "accepted"})
    conn.close()

    # NOW = 2026-09-21T02:00Z; this fill is two hours after the cutoff.
    late = [{"id": "act-late", "order_id": "o-co", "symbol": "AAA",
             "side": "buy", "qty": 4.0, "price": 100.0, "notional": 400.0,
             "transaction_time": t(21, 4, 0), "order_status": "fill"}]
    out = summarize(db, fills=late)
    order = out["chains"][0]["orders"][0]
    assert order["fills_matched"] is False      # no in-window evidence
    assert out["summary"]["filled_verified"] == 0
    assert out["degraded"]["post_cutoff_fills"] == 1
    assert out["unlinked_fills"] == []          # dropped, not re-filed elsewhere


# ---- history itself respects the cutoff ----------------------------------------------

def test_history_respects_cutoff(db: Path):
    # R4: history is included up to `now` — an order_filled observation
    # stamped AFTER the cutoff is invisible. The chain still appears (a
    # today flush_wait is in-window), but it can only show the submission;
    # the completion cannot be claimed from the future.
    conn = connect(db)
    ry = rid("hc")
    ev(conn, "OLD", "decision_buy", ts_iso=t(19, 13, 0), run=ry)
    ev(conn, "OLD", "intent_created", ts_iso=t(19, 13, 1), run=ry,
       dkey=dk(ry, "OLD"), intent="v-hc")
    ev(conn, "OLD", "order_submitted", ts_iso=t(19, 14, 0), run=ry,
       intent="v-hc", order="o-hc",
       payload={"client_order_id": "cid-hc", "submit_status": "accepted"})
    ev(conn, "OLD", "order_filled", ts_iso=t(21, 4, 0), run=rid("hc-late"),
       intent="v-hc", order="o-hc",
       payload={"order_status": "filled", "filled_qty": 4,
                "observed_at": t(21, 4, 0)})
    # In-window activity today keeps the chain on the page.
    ev(conn, "OLD", "flush_wait", ts_iso=t(20, 14, 0), run=rid("hc-today"),
       intent="v-hc", deferred=True, detail="not_before",
       payload={"reason": "not_before"})
    conn.close()

    out = summarize(db, fills=[])
    assert len(out["carryover"]) == 1
    carry = out["carryover"][0]
    assert carry["stage"] == "flush_wait"       # the post-cutoff event is unseen
    orders = carry["orders"]
    assert len(orders) == 1
    assert orders[0]["submitted_at"] == t(19, 14, 0)
    assert orders[0]["last_observation_kind"] is None   # future event unseen
    assert orders[0]["phase"] == "submitted"
    assert out["summary"]["filled_verified"] == 0


# ==== R4: cross-day chains and fills-only days ========================================

# ---- yesterday submit + today fill → one full chain, empty denominator ---------------

def test_cross_day_chain_keeps_yesterday_submission(db: Path):
    # R4: yesterday created + submitted; today only the carryover recheck's
    # order_filled arrives. The chain must show the FULL history —
    # yesterday's order_id, client_order_id and sizing snapshot join today's
    # observation — and the cohort denominator stays empty.
    conn = connect(db)
    ry = rid("cy")
    ev(conn, "OLD", "decision_buy", ts_iso=t(19, 13, 0), run=ry)
    ev(conn, "OLD", "intent_created", ts_iso=t(19, 13, 1), run=ry,
       dkey=dk(ry, "OLD"), intent="v-cd")
    ev(conn, "OLD", "order_submitted", ts_iso=t(19, 14, 0), run=ry,
       intent="v-cd", order="o-cd",
       payload={"client_order_id": "cid-cd", "submit_status": "accepted",
                "sizing": SIZING_OK})
    rt = rid("ct")
    ev(conn, "OLD", "order_filled", ts_iso=t(20, 14, 0), run=rt,
       intent="v-cd", order="o-cd",
       payload={"order_status": "filled", "filled_qty": 4,
                "filled_avg_price": 100.0, "observed_at": t(20, 14, 0)})
    conn.close()

    out = summarize(db, fills=[])
    s = out["summary"]
    assert s["decisions"] == 0          # denominator: no decision today
    assert s["filled_verified"] == 0    # carryover never enters cohort counters
    assert len(out["carryover"]) == 1
    carry = out["carryover"][0]
    assert carry["intent_id"] == "v-cd"
    assert carry["created_on"] == "2026-09-19"
    assert carry["stage"] == "order_filled"
    # attempts = flush attempts: yesterday's submit run only (ITERATION-3 R6:
    # the submit IS flush work; status observations are re-queries, counted
    # separately as observed_runs — the creation run is not an attempt).
    assert carry["attempts"] == 1
    assert carry["created_runs"] == 1
    assert carry["observed_runs"] == 1     # today's order_filled run
    orders = carry["orders"]
    assert len(orders) == 1
    o = orders[0]
    assert o["submitted_at"] == t(19, 14, 0)    # yesterday's submit, not lost
    assert o["client_order_id"] == "cid-cd"
    assert o["submit_status"] == "accepted"
    assert o["submit_phase"] == "submitted_accepted"
    assert o["last_observation_kind"] == "order_filled"
    assert o["phase"] == "complete"
    assert carry["sizing"]["sizing"]["available_notional"] == 1500.0


# ---- today only fills, order known from yesterday → chain appears --------------------

def test_fills_only_surface_yesterday_order(db: Path):
    # R4: 今日仅有 fills、没有任何今日意图/观察事件——昨日提交的订单链仍要
    # 出现，成交按 order_id 归到它的链上（正面证据），分母不动。
    conn = connect(db)
    ry = rid("fo")
    ev(conn, "OLD", "decision_buy", ts_iso=t(19, 13, 0), run=ry)
    ev(conn, "OLD", "intent_created", ts_iso=t(19, 13, 1), run=ry,
       dkey=dk(ry, "OLD"), intent="v-fo")
    ev(conn, "OLD", "order_submitted", ts_iso=t(19, 14, 0), run=ry,
       intent="v-fo", order="o-fo",
       payload={"client_order_id": "cid-fo", "submit_status": "accepted"})
    conn.close()

    fills = [{"id": "act-fo", "order_id": "o-fo", "symbol": "OLD",
              "side": "buy", "qty": 4.0, "price": 100.0, "notional": 400.0,
              "transaction_time": t(20, 14, 0), "order_status": "fill"}]
    out = summarize(db, fills=fills)
    s = out["summary"]
    assert s["decisions"] == 0
    assert s["filled_verified"] == 0
    assert len(out["carryover"]) == 1
    o = out["carryover"][0]["orders"][0]
    assert o["submitted_at"] == t(19, 14, 0)
    assert o["fills_matched"] is True
    assert o["fills_qty"] == 4.0
    assert o["phase"] == "complete"


# ---- carryover fills stay outside the cohort counters --------------------------------

def test_carryover_fills_do_not_inflate_cohort_counters(db: Path):
    # One fresh decision today (submitted + completing fill) plus a
    # carryover chain that also got fills today: the carryover's fill must
    # not inflate the cohort's filled_verified, and the cohort's own fill
    # must still count.
    conn = connect(db)
    ry = rid("ci")
    ev(conn, "OLD", "decision_buy", ts_iso=t(19, 13, 0), run=ry)
    ev(conn, "OLD", "intent_created", ts_iso=t(19, 13, 1), run=ry,
       dkey=dk(ry, "OLD"), intent="v-ci")
    ev(conn, "OLD", "order_submitted", ts_iso=t(19, 14, 0), run=ry,
       intent="v-ci", order="o-ci-old",
       payload={"submit_status": "accepted"})
    rn = rid("cn")
    ev(conn, "NEW", "decision_buy", ts_iso=t(20, 13, 0), run=rn)
    ev(conn, "NEW", "intent_created", ts_iso=t(20, 13, 1), run=rn,
       dkey=dk(rn, "NEW"), intent="v-cn")
    ev(conn, "NEW", "order_submitted", ts_iso=t(20, 14, 0), run=rn,
       intent="v-cn", order="o-ci-new",
       payload={"submit_status": "accepted"})
    conn.close()

    fills = [
        {"id": "act-old", "order_id": "o-ci-old", "symbol": "OLD",
         "side": "buy", "qty": 3.0, "price": 50.0, "notional": 150.0,
         "transaction_time": t(20, 15, 0), "order_status": "fill"},
        {"id": "act-new", "order_id": "o-ci-new", "symbol": "NEW",
         "side": "buy", "qty": 2.0, "price": 60.0, "notional": 120.0,
         "transaction_time": t(20, 15, 1), "order_status": "fill"},
    ]
    out = summarize(db, fills=fills)
    s = out["summary"]
    assert s["decisions"] == 1           # NEW only
    assert s["filled_verified"] == 1     # NEW's fill only — OLD stays outside
    assert s["carryover_intents"] == 1
    assert out["carryover"][0]["orders"][0]["phase"] == "complete"
    assert [c["symbol"] for c in out["chains"]] == ["NEW"]
    assert out["chains"][0]["phase"] == "complete"


# ---- fills for orders with no ledger events stay visible ------------------------------

def test_unlinked_fills_stay_visible(db: Path):
    # R5/R4 display: a fill whose order_id matches NO ledger event cannot be
    # attributed — but as positive evidence it must be listed, not dropped
    # and not fabricated into a chain.
    fills = [{"id": "act-ghost", "order_id": "o-ghost", "symbol": "GHS",
              "side": "buy", "qty": 2.0, "price": 25.0, "notional": 50.0,
              "transaction_time": t(20, 14, 0), "order_status": "fill"}]
    out = summarize(db, fills=fills)
    s = out["summary"]
    assert s["decisions"] == 0
    assert s["filled_verified"] == 0
    assert out["chains"] == []
    assert len(out["unlinked_fills"]) == 1
    row = out["unlinked_fills"][0]
    assert row["order_id"] == "o-ghost"
    assert row["qty"] == 2.0
    assert row["fill_status"] == "fill"
    assert row["activity_id"] == "act-ghost"


# ---- cross-day replacement marks the old chain superseded -----------------------------

def test_carryover_superseded_by_todays_replacement(db: Path):
    # Yesterday's intent is replaced TODAY by a new version. The old chain
    # (with its failure history) must say it was superseded instead of
    # looking alive-and-waiting forever.
    conn = connect(db)
    ry = rid("sy0")
    ev(conn, "OLD", "decision_buy", ts_iso=t(19, 13, 0), run=ry)
    ev(conn, "OLD", "intent_created", ts_iso=t(19, 13, 1), run=ry,
       dkey=dk(ry, "OLD"), intent="v-sold")
    ev(conn, "OLD", "sizing", ts_iso=t(19, 14, 0), run=ry, intent="v-sold",
       deferred=True, detail="only $65.62 available",
       payload={"reason": "only $65.62 available", "cash_available": 65.62,
                "available_notional": 65.62, "min_notional": 390.23,
                "sizing": SIZING_VETO})
    rt = rid("st0")
    ev(conn, "OLD", "decision_buy", ts_iso=t(20, 13, 0), run=rt)
    ev(conn, "OLD", "intent_replaced", ts_iso=t(20, 13, 1), run=rt,
       dkey=dk(rt, "OLD"), intent="v-snew", payload={"previous_version": "v-sold"})
    conn.close()

    out = summarize(db, fills=[])
    s = out["summary"]
    assert s["decisions"] == 1
    assert s["carryover_intents"] == 1
    carry = out["carryover"][0]
    assert carry["intent_id"] == "v-sold"
    assert carry["superseded_by"] == "v-snew"
    assert carry["classification"] == "superseded"
    assert carry["stage"] == "sizing"          # old failure history retained
    assert carry["sizing"]["available_notional"] == 65.62
    # R6: the sizing veto IS a flush attempt; the creation run is not.
    assert carry["attempts"] == 1
    assert carry["created_runs"] == 1
    assert carry["observed_runs"] == 0
    assert s["superseded"] == 0                # cohort counters untouched by carryover
    assert s["waiting"] == 0


# ---- rejection then re-submission: both facts, one truthful chain ---------------------

def test_rejected_attempt_then_resubmission(db: Path):
    # A first attempt came back None (rejected), a later attempt submitted
    # and was accepted. The chain counts as submitted (a real order exists)
    # while BOTH facts stay on their own orders — the rejection is not
    # erased and not double-counted as a submission.
    conn = connect(db)
    r = rid("rr0")
    ev(conn, "AAA", "decision_buy", ts_iso=t(20, 13, 0), run=r)
    ev(conn, "AAA", "intent_created", ts_iso=t(20, 13, 1), run=r,
       dkey=dk(r, "AAA"), intent="v-rr")
    ev(conn, "AAA", "order_submitted", ts_iso=t(20, 14, 0), run=r,
       intent="v-rr", order=None,
       payload={"submit_status": "rejected", "reason": "insufficient buying power"})
    ev(conn, "AAA", "order_submitted", ts_iso=t(20, 15, 0), run=r,
       intent="v-rr", order="o-rr2",
       payload={"client_order_id": "cid-rr2", "submit_status": "accepted"})
    conn.close()

    out = summarize(db, fills=[])
    chain = out["chains"][0]
    phases = [o["phase"] for o in chain["orders"]]
    assert "submit_rejected" in phases and "submitted" in phases
    s = out["summary"]
    assert s["submitted"] == 1            # the chain reached a real order, once
    assert s["submit_rejected"] == 0      # not a pure-rejection chain
    assert chain["classification"] == "submitted"
    assert chain["phase"] == "submitted"  # best chain truth, details per order


# ==== R6: attempt/wait counting and the identity-less gate ===========================

# ---- open_missing is a skipped check, not a wait -------------------------------------

def test_open_missing_is_a_skip_not_a_wait(db: Path):
    # REVIEW FIX (R6): flush_wait with reason open_missing means one check
    # could not run (open-relative gate unavailable) — the intent neither
    # waited nor passed. It must NOT inflate wait_events. It is still a
    # flush attempt by its run (the flush did run and touched the intent).
    conn = connect(db)
    r0 = rid("om0")
    ev(conn, "AAA", "decision_buy", ts_iso=t(20, 13, 0), run=r0)
    ev(conn, "AAA", "intent_created", ts_iso=t(20, 13, 1), run=r0,
       dkey=dk(r0, "AAA"), intent="v-om")
    ev(conn, "AAA", "flush_wait", ts_iso=t(20, 14, 0), run=rid("om1"),
       intent="v-om", deferred=True, detail="not_before",
       payload={"reason": "not_before", "not_before": t(20, 15, 0)})
    ev(conn, "AAA", "flush_wait", ts_iso=t(20, 14, 30), run=rid("om2"),
       intent="v-om", deferred=True, detail="open_missing",
       payload={"reason": "open_missing"})
    conn.close()

    out = summarize(db, fills=[])
    chain = out["chains"][0]
    assert chain["wait_events"] == 1      # the not_before wait only
    assert chain["attempts"] == 2         # both runs attempted the flush
    assert chain["created_runs"] == 1
    assert chain["observed_runs"] == 0


# ---- legacy gate keys on the missing identity, not on mode ---------------------------

def test_legacy_identityless_flush_counted_regardless_of_mode(db: Path):
    # REVIEW FIX (R6): an old intent legitimately has version NULL. When
    # today's flush touches it, the recorder's run mode is "paper" — the
    # event is still identity-less (intent_id NULL). The degraded gate must
    # key on the MISSING IDENTITY, not on mode: these observations are
    # counted AND listed as "observed today, old identity missing" — never
    # fabricated into a chain, never given an invented version, never
    # dropped from the funnel's degraded accounting.
    conn = connect(db)
    record_intent_event(conn, "LEG", "sizing", True, "only $65.62 available",
                        timestamp=t(20, 14, 5), run_id=rid("lg0"), mode="paper")
    conn.close()

    out = summarize(db, fills=[])
    assert out["degraded"]["legacy_flush_events"] == 1
    listed = out["degraded"]["identityless_observed_today"]
    assert len(listed) == 1
    assert listed[0]["kind"] == "sizing"
    assert listed[0]["symbol"] == "LEG"
    assert listed[0]["mode"] == "paper"
    assert out["degraded"]["identityless_by_mode"] == {"paper": 1}
    assert out["summary"]["decisions"] == 0
    assert out["chains"] == []
    assert out["carryover"] == []


# ---- R6: identity-less events of every identity-needing kind, per mode ---------------

def test_identityless_events_are_mode_distinguishable(db: Path):
    # ITERATION-3 R6: the gate covers EVERY kind that needs an intent
    # identity — the legacy five AND flush_wait / order_* — and the listing
    # stays mode-distinguishable: paper and dry_run are separable, history
    # with mode NULL is an "unsplit" reference bucket (never folded into the
    # paper page's own accounting).
    conn = connect(db)
    record_intent_event(conn, "LEG", "sizing", True, "only $65.62 available",
                        timestamp=t(20, 14, 5), run_id=rid("lg0"), mode="paper")
    record_intent_event(conn, "LEG", "flush_wait", True, "no_quote",
                        timestamp=t(20, 14, 6), run_id=rid("lg1"), mode="dry_run",
                        intent_id=None)
    record_intent_event(conn, "2026-09-20T14:07:00+00:00", "LEG", "gap",
                        deferred=False, detail="gap too wide")
    conn.close()

    out = summarize(db, fills=[])
    d = out["degraded"]
    # the gate itself is mode-blind (iter-2 fix): all three are counted/listed
    assert d["legacy_flush_events"] == 3
    # …but the per-mode split keeps them distinguishable
    assert d["identityless_by_mode"] == {"paper": 1, "dry_run": 1, "unsplit": 1}
    modes = {e["kind"]: e["mode"] for e in d["identityless_observed_today"]}
    assert modes == {"sizing": "paper", "flush_wait": "dry_run", "gap": None}
    assert out["chains"] == []


# ---- R6: 1 submit + 10 requeries = 1 attempt, 10 observation runs --------------------

def test_one_submit_plus_ten_requeries_is_one_attempt(db: Path):
    # R6 review scenario verbatim: 1 submit + 10 status re-queries used to
    # display 11 attempts. The submit IS flush work (it counts); observations
    # are re-queries (they do not) — attempts=1, observed_runs=10.
    conn = connect(db)
    r = rid("aq0")
    ev(conn, "AAA", "decision_buy", ts_iso=t(20, 13, 0), run=r)
    ev(conn, "AAA", "intent_created", ts_iso=t(20, 13, 1), run=r,
       dkey=dk(r, "AAA"), intent="v-aq")
    ev(conn, "AAA", "order_submitted", ts_iso=t(20, 14, 0), run=r,
       intent="v-aq", order="o-aq",
       payload={"client_order_id": "cid-aq", "submit_status": "accepted"})
    for i in range(10):
        ev(conn, "AAA", "order_observed", ts_iso=t(20, 14, 1 + i),
           run=rid(f"aq{i + 1}"), intent="v-aq", order="o-aq",
           payload={"order_status": "accepted", "observed_at": t(20, 14, 1 + i)})
    conn.close()

    out = summarize(db, fills=[])
    chain = out["chains"][0]
    assert chain["attempts"] == 1
    assert chain["observed_runs"] == 10
    assert chain["created_runs"] == 1
    assert out["summary"]["submitted"] == 1


# ---- R6: run-level skip keeps the detail string and forwards the remainder ----------

def test_run_skips_preserve_detail_and_structured_remainder(db: Path):
    # ITERATION-3 R6: the detail prose passes through VERBATIM — the
    # aggregation never parses text. The structured remainder reaches the
    # frontend as DATA when the recorder's payload carries it (whitelisted
    # key), and as None when it does not.
    conn = connect(db)
    ev(conn, "*", "flush_skipped", ts_iso=t(20, 14, 0), run=rid("rs0"),
       detail="max_new_orders_reached; unprocessed: AAA, BBB",
       payload={"reason": "max_new_orders_reached", "limit": 1,
                "unprocessed": ["AAA", "BBB"]})
    ev(conn, "*", "flush_skipped", ts_iso=t(20, 14, 1), run=rid("rs1"),
       detail="outside_window", payload={"reason": "outside_window"})
    conn.close()

    out = summarize(db, fills=[])
    skips = {s["reason"]: s for s in out["run_skips"]}
    assert set(skips) == {"max_new_orders_reached", "outside_window"}
    cap = skips["max_new_orders_reached"]
    assert cap["detail"] == "max_new_orders_reached; unprocessed: AAA, BBB"
    assert cap["unprocessed"] == ["AAA", "BBB"]
    assert skips["outside_window"]["detail"] == "outside_window"
    assert skips["outside_window"]["unprocessed"] is None


# ==== R4 (ITERATION-3): identity-less observations join a UNIQUE submission ==========

def test_identityless_observation_joins_unique_submission(db: Path):
    # An order observation with NO intent_id (old journal shape, before the
    # recovery carried identity) still lands on the original chain when the
    # SAME order_id has exactly ONE identified submission in the same book
    # and mode. Yesterday's submit + today's identity-less fill → the
    # carryover chain shows confirmed completion; the denominator is
    # untouched and nothing is listed as degraded.
    conn = connect(db)
    ry = rid("at0")
    ev(conn, "OLD", "decision_buy", ts_iso=t(19, 13, 0), run=ry)
    ev(conn, "OLD", "intent_created", ts_iso=t(19, 13, 1), run=ry,
       dkey=dk(ry, "OLD"), intent="v-at")
    ev(conn, "OLD", "order_submitted", ts_iso=t(19, 14, 0), run=ry,
       intent="v-at", order="o-at",
       payload={"client_order_id": "cid-at", "submit_status": "accepted"})
    # today: only the identity-less recovery observation
    ev(conn, "OLD", "order_filled", ts_iso=t(20, 15, 0), run=rid("at1"),
       order="o-at",
       payload={"order_status": "filled", "filled_qty": 4,
                "filled_avg_price": 100.0, "observed_at": t(20, 15, 0)})
    conn.close()

    out = summarize(db, fills=None)
    assert out["summary"]["decisions"] == 0
    assert out["summary"]["filled_verified"] == 0   # cohort untouched by carryover
    assert len(out["carryover"]) == 1
    carry = out["carryover"][0]
    assert carry["intent_id"] == "v-at"
    assert carry["created_on"] == "2026-09-19"
    assert carry["phase"] == "complete"             # the observation joins the chain
    o = carry["orders"][0]
    assert o["last_observation_kind"] == "order_filled"
    assert o["observed_at"] == t(20, 15, 0)
    d = out["degraded"]
    assert d["unattributed_order_events"] == []
    assert d["identityless_observed_today"] == []
    assert d["legacy_flush_events"] == 0


def test_identityless_observation_same_day_completes_cohort_chain(db: Path):
    # Same attribution rule inside today's cohort: the identity-less
    # observation completes the chain AND the cohort counter.
    conn = connect(db)
    r = rid("at2")
    ev(conn, "AAA", "decision_buy", ts_iso=t(20, 13, 0), run=r)
    ev(conn, "AAA", "intent_created", ts_iso=t(20, 13, 1), run=r,
       dkey=dk(r, "AAA"), intent="v-a2")
    ev(conn, "AAA", "order_submitted", ts_iso=t(20, 14, 0), run=r,
       intent="v-a2", order="o-a2",
       payload={"client_order_id": "cid-a2", "submit_status": "accepted"})
    ev(conn, "AAA", "order_filled", ts_iso=t(20, 15, 0), run=rid("at3"),
       order="o-a2",
       payload={"order_status": "filled", "filled_qty": 4,
                "observed_at": t(20, 15, 0)})
    conn.close()

    out = summarize(db, fills=None)
    chain = out["chains"][0]
    assert chain["intent_id"] == "v-a2"
    assert chain["phase"] == "complete"
    assert out["summary"]["filled_verified"] == 1
    assert out["degraded"]["unattributed_order_events"] == []


def test_conflicting_order_ownership_is_unattributable(db: Path):
    # The same order_id submitted under TWO intents (same book, same mode):
    # the identity-less fill belongs to nobody provable. No chain may claim
    # the completion and the fill must be listed as unattributed — never
    # guessed onto a chain by symbol.
    conn = connect(db)
    ra, rb = rid("cf0"), rid("cf1")
    for run, sym, ver in ((ra, "AAA", "v-ca"), (rb, "BBB", "v-cb")):
        ev(conn, sym, "decision_buy", ts_iso=t(20, 13, 0), run=run)
        ev(conn, sym, "intent_created", ts_iso=t(20, 13, 1), run=run,
           dkey=dk(run, sym), intent=ver)
        ev(conn, sym, "order_submitted", ts_iso=t(20, 14, 0), run=run,
           intent=ver, order="o-shared",
           payload={"client_order_id": f"cid-{sym}", "submit_status": "accepted"})
    ev(conn, "AAA", "order_filled", ts_iso=t(20, 15, 0), run=rid("cf2"),
       order="o-shared",
       payload={"order_status": "filled", "filled_qty": 1,
                "observed_at": t(20, 15, 0)})
    conn.close()

    out = summarize(db, fills=None)
    chains = {c["symbol"]: c for c in out["chains"]}
    assert chains["AAA"]["phase"] == "submitted"
    assert chains["BBB"]["phase"] == "submitted"
    assert out["summary"]["filled_verified"] == 0
    unattr = out["degraded"]["unattributed_order_events"]
    assert len(unattr) == 1
    assert unattr[0]["order_id"] == "o-shared"
    assert unattr[0]["kind"] == "order_filled"
    assert unattr[0]["symbol"] == "AAA"
    assert "多个意图" in unattr[0]["note"]

    # R4 (ITERATION-4): the same fixture WITH a completing fill activity in
    # the feed — the fill obeys the SAME ownership rule. Granting it to both
    # chains by raw order_id used to move completion 0 → 2; it must stay
    # withheld from both, listed order-level with the conflict reason, and
    # pull in no chain.
    fills = [{"id": "act-shared", "order_id": "o-shared", "symbol": "AAA",
              "side": "buy", "qty": 1.0, "price": 100.0, "notional": 100.0,
              "transaction_time": t(20, 15, 1), "order_status": "fill"}]
    out2 = summarize(db, fills=fills)
    s2 = out2["summary"]
    chains2 = {c["symbol"]: c for c in out2["chains"]}
    assert chains2["AAA"]["phase"] == "submitted"   # 不选择任一版本
    assert chains2["BBB"]["phase"] == "submitted"
    assert s2["filled_verified"] == 0               # 不是 0 变 2
    assert s2["partial"] == 0
    assert out2["carryover"] == []
    for sym in ("AAA", "BBB"):
        o = chains2[sym]["orders"][0]
        assert o["filled_evidence"] is False
        assert o["fills_matched"] is None
        assert o["fills_withheld"] is True          # 券商有成交，链无权认领
    unlinked = out2["unlinked_fills"]
    assert len(unlinked) == 1
    assert unlinked[0]["order_id"] == "o-shared"
    assert unlinked[0]["activity_id"] == "act-shared"
    assert "多个意图" in unlinked[0]["note"]         # 无法归因原因可见


def test_conflicting_order_fill_via_recovery_pass_not_double_counted(db: Path):
    # R4 (ITERATION-4), the review's exact bypass: the conflicted order_id is
    # observed by the REAL cycle-end recovery pass (observe_unresolved — the
    # recorder can mint no identity for a two-owner order) AND a completing
    # fill arrives in the feed. Both chains can see the same fill by raw
    # order_id; neither may claim it and completion must stay at zero.
    from agentic_trading.execution.broker import OrderObservation
    from agentic_trading.execution_funnel import FunnelRecorder

    conn = connect(db)
    ra, rb = rid("cw0"), rid("cw1")
    for run, sym, ver in ((ra, "AAA", "v-wa"), (rb, "BBB", "v-wb")):
        ev(conn, sym, "decision_buy", ts_iso=t(20, 13, 0), run=run)
        ev(conn, sym, "intent_created", ts_iso=t(20, 13, 1), run=run,
           dkey=dk(run, sym), intent=ver)
        ev(conn, sym, "order_submitted", ts_iso=t(20, 14, 0), run=run,
           intent=ver, order="o-shared",
           payload={"client_order_id": f"cid-{sym}", "submit_status": "accepted"})

    class _FilledBroker:
        def observe_order(self, order_id):
            return OrderObservation(
                order_id=order_id, status="filled", observed_at=t(20, 15, 0),
                filled_qty=1.0, filled_avg_price=100.0, filled_at=t(20, 15, 0),
                error=None)

    FunnelRecorder(conn, rid("cw2"), "paper", jsonl=False).observe_unresolved(
        conn, _FilledBroker())
    conn.close()

    fills = [{"id": "act-shared", "order_id": "o-shared", "symbol": "AAA",
              "side": "buy", "qty": 1.0, "price": 100.0, "notional": 100.0,
              "transaction_time": t(20, 15, 1), "order_status": "fill"}]
    out = summarize(db, fills=fills)
    s = out["summary"]
    chains = {c["symbol"]: c for c in out["chains"]}
    assert chains["AAA"]["phase"] == "submitted"
    assert chains["BBB"]["phase"] == "submitted"
    assert s["filled_verified"] == 0          # never 0 → 2
    assert s["partial"] == 0
    assert out["carryover"] == []             # the fill pulls in no version
    # the recovered observation itself is unattributable, with the reason
    unattr = out["degraded"]["unattributed_order_events"]
    assert len(unattr) == 1
    assert unattr[0]["order_id"] == "o-shared"
    assert unattr[0]["kind"] == "order_filled"
    assert "多个意图" in unattr[0]["note"]
    # the order entries stay submit-only; the fill is order-level evidence
    for sym in ("AAA", "BBB"):
        orders = chains[sym]["orders"]
        assert len(orders) == 1
        assert orders[0]["filled_evidence"] is False
        assert orders[0]["fills_withheld"] is True
    unlinked = out["unlinked_fills"]
    assert len(unlinked) == 1
    assert unlinked[0]["order_id"] == "o-shared"
    assert "多个意图" in unlinked[0]["note"]


def test_other_mode_same_order_id_cannot_steal_or_suppress(db: Path):
    # R4 (ITERATION-4): a same-order_id submission under ANOTHER mode is a
    # different attribution key. It must not steal the paper identity (the
    # identity-less paper recovery observation still attaches to the paper
    # owner) and must not suppress the paper fill attribution (the completing
    # fill still completes the paper chain). The dry_run page keeps its own
    # submission and never receives the paper observation.
    conn = connect(db)
    rp, rd = rid("mx0"), rid("mx1")
    ev(conn, "AAA", "decision_buy", ts_iso=t(20, 13, 0), run=rp)
    ev(conn, "AAA", "intent_created", ts_iso=t(20, 13, 1), run=rp,
       dkey=dk(rp, "AAA"), intent="v-mp")
    ev(conn, "AAA", "order_submitted", ts_iso=t(20, 14, 0), run=rp,
       intent="v-mp", order="o-mx",
       payload={"client_order_id": "cid-mp", "submit_status": "accepted"})
    # the twin: same order_id, ANOTHER mode, its own intent version
    ev(conn, "AAA", "decision_buy", ts_iso=t(20, 13, 2), run=rd, mode="dry_run")
    ev(conn, "AAA", "intent_created", ts_iso=t(20, 13, 3), run=rd, mode="dry_run",
       dkey=dk(rd, "AAA"), intent="v-md")
    ev(conn, "AAA", "order_submitted", ts_iso=t(20, 14, 1), run=rd,
       mode="dry_run", intent="v-md", order="o-mx",
       payload={"client_order_id": "cid-md", "submit_status": "accepted"})
    # today's identity-less PAPER recovery observation
    ev(conn, "AAA", "order_filled", ts_iso=t(20, 15, 0), run=rid("mx2"),
       order="o-mx",
       payload={"order_status": "filled", "filled_qty": 2,
                "observed_at": t(20, 15, 0)})
    conn.close()

    fills = [{"id": "act-mx", "order_id": "o-mx", "symbol": "AAA",
              "side": "buy", "qty": 2.0, "price": 50.0, "notional": 100.0,
              "transaction_time": t(20, 15, 1), "order_status": "fill"}]
    out = summarize(db, fills=fills)
    s = out["summary"]
    assert s["decisions"] == 1               # the dry_run twin is not the cohort
    chain = out["chains"][0]
    assert chain["intent_id"] == "v-mp"
    assert chain["phase"] == "complete"      # paper lookback not suppressed
    o = chain["orders"][0]
    assert o["last_observation_kind"] == "order_filled"   # identity not stolen
    assert o["fills_matched"] is True
    assert o["filled_evidence"] is True
    assert s["filled_verified"] == 1
    assert out["degraded"]["unattributed_order_events"] == []
    assert out["unlinked_fills"] == []

    dry = summarize(db, fills=None, mode="dry_run")
    assert [c["intent_id"] for c in dry["chains"]] == ["v-md"]
    assert dry["chains"][0]["orders"][0]["last_observation_kind"] is None
    assert dry["degraded"]["unattributed_order_events"] == []


def test_identityless_submission_leaves_observation_unattributable(db: Path):
    # A version-NULL (old) intent produced a submit and its observation with
    # no identity anywhere: nothing may be grafted onto a chain, and BOTH
    # order events surface in the identity-less gate (R6: order_* kinds are
    # identity-needing kinds now), while the observation is also listed as
    # unattributed with the reason named.
    conn = connect(db)
    ev(conn, "OLD", "order_submitted", ts_iso=t(20, 14, 0), run=rid("il1"),
       order="o-old", payload={"submit_status": "accepted"})
    ev(conn, "OLD", "order_filled", ts_iso=t(20, 15, 0), run=rid("il2"),
       order="o-old",
       payload={"order_status": "filled", "filled_qty": 2,
                "observed_at": t(20, 15, 0)})
    conn.close()

    out = summarize(db, fills=None)
    assert out["chains"] == []                # nothing to chain to — no fabrication
    assert out["summary"]["filled_verified"] == 0
    d = out["degraded"]
    assert d["legacy_flush_events"] == 2      # order_submitted + order_filled
    assert d["identityless_by_mode"] == {"paper": 2}
    assert {e["kind"] for e in d["identityless_observed_today"]} == {
        "order_submitted", "order_filled"}
    unattr = d["unattributed_order_events"]
    assert len(unattr) == 1 and unattr[0]["order_id"] == "o-old"
    assert "intent_id 为空" in unattr[0]["note"]


# ==== R4 penetration: the REAL three-cycle journal feeds funnel_summary ==============

def _journal_events(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT id, kind, symbol, run_id, mode, intent_id, order_id, payload "
        "FROM intent_events ORDER BY id").fetchall()
    return [dict(zip(("id", "kind", "symbol", "run_id", "mode", "intent_id",
                      "order_id", "payload"), r)) for r in rows]


def _run_three_cycle_journal(monkeypatch, tmp_path: Path, *, recovery_day: int):
    """The leader's real recovery path, UNMODIFIED: two frozen-clock cycles
    (run_scenario), then a third live cycle whose cycle-end recovery pass
    observes the previously submitted orders FILLED. Every funnel event comes
    from the real FunnelRecorder through run_cycle — nothing is hand-fed.
    recovery_day=26 keeps everything on one ET day; recovery_day=27 shifts
    the RECOVERY cycle to the next day (cycle clock AND event clock moved
    together — the review's unified frozen clocks)."""
    import agentic_trading.journal.logger as logger_mod
    from datetime import timedelta
    from test_trade_trace_baseline import (
        FIXED_NOW, FrozenDateTime, HarnessFeed, frames, make_settings,
        neutral_regime, run_scenario)
    from agentic_trading.execution.broker import OrderObservation
    from agentic_trading.journal.logger import clear_trade_intent
    from agentic_trading.run import run_cycle

    recovery_now = FIXED_NOW.replace(day=recovery_day)
    if recovery_day != 26:
        class RecoveryDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return recovery_now if tz is not None else recovery_now.replace(tzinfo=None)
    # cycles 1-2: cycle clock AND event clock frozen on the same day
    monkeypatch.setattr(logger_mod, "datetime", FrozenDateTime)
    result = run_scenario(monkeypatch.setattr, tmp_path)
    submitted = [e for e in _journal_events(result.conn)
                 if e["kind"] == "order_submitted"]
    assert len(submitted) == 2
    observed_at = recovery_now.isoformat()
    for row in submitted:
        result.broker.observations[row["order_id"]] = OrderObservation(
            order_id=row["order_id"], status="filled",
            observed_at=observed_at, filled_qty=1.0,
            filled_avg_price=100.0, filled_at=observed_at,
            error=None)
    # retire the two kept intents so the recovery cycle cannot re-submit
    clear_trade_intent(result.conn, "VETO")
    clear_trade_intent(result.conn, "CHASE")
    if recovery_day != 26:
        monkeypatch.setattr(logger_mod, "datetime", RecoveryDateTime)
        monkeypatch.setattr("agentic_trading.run.datetime", RecoveryDateTime)
    run_cycle(skip_llm=True, settings=make_settings(), broker=result.broker,
              feed=HarnessFeed(frames()), conn=result.conn,
              regime_model=neutral_regime)
    return result, submitted


def test_real_three_cycle_journal_shows_confirmed_completion(monkeypatch, tmp_path):
    # R4 penetration, same ET day: the REAL journal (creation -> submission ->
    # end-of-cycle recovery observation) fed straight into funnel_summary.
    # The chains must show CONFIRMED COMPLETION from the recovered
    # order_filled events alone (fills=None — no fill feed at all), the
    # observations must sit on the ORIGINAL intent versions, and the orders
    # must have been submitted exactly once.
    from datetime import timedelta
    from test_trade_trace_baseline import FIXED_NOW
    result, submitted = _run_three_cycle_journal(
        monkeypatch, tmp_path, recovery_day=26)
    conn = result.conn

    # submitted exactly once per order — recovery observed, never re-submitted
    subs = [e for e in _journal_events(conn) if e["kind"] == "order_submitted"]
    assert len(subs) == 2 and len({e["order_id"] for e in subs}) == 2
    filled_ev = [e for e in _journal_events(conn) if e["kind"] == "order_filled"]
    assert {e["order_id"] for e in filled_ev} == {e["order_id"] for e in subs}
    # each recovered observation rides the ORIGINAL submit identity
    by_order = {e["order_id"]: e["intent_id"] for e in subs}
    assert all(e["intent_id"] and e["intent_id"] == by_order[e["order_id"]]
               for e in filled_ev)

    out = funnel_summary(conn, now=FIXED_NOW + timedelta(hours=4), fills=None)
    s = out["summary"]
    assert s["decisions"] == 12          # 4 decisions x 3 cycles
    assert s["not_created"] == 4         # BUYA/BUYB re-decisions, pending_buy-gated
    assert s["submitted"] == 2
    assert s["filled_verified"] == 2     # BUYA + BUYB, from events alone
    assert s["partial"] == 0
    assert out["carryover"] == []        # same day — nothing carried
    completed = {c["symbol"]: c for c in out["chains"] if c["phase"] == "complete"}
    assert set(completed) == {"BUYA", "BUYB"}
    for sym in ("BUYA", "BUYB"):
        o = completed[sym]["orders"][0]
        assert o["phase"] == "complete"
        assert o["last_observation_kind"] == "order_filled"
        assert o["submitted_at"]         # the real cycle-2 submission fact
        assert o["fills_unavailable"] is True


def test_real_cross_day_recovery_lands_in_carryover_not_denominator(monkeypatch, tmp_path):
    # R4 penetration, cross ET day: creation + submission on day 1, the
    # recovery observation on day 2. The session day becomes day 2 (the
    # cycles table's latest paper cycle), so the completed chains appear as
    # CARRYOVER and the cohort denominator holds only day-2 decisions — the
    # day-1 completions must not inflate it.
    from datetime import timedelta
    from test_trade_trace_baseline import FIXED_NOW
    result, _ = _run_three_cycle_journal(monkeypatch, tmp_path, recovery_day=27)
    conn = result.conn

    recovery_now = FIXED_NOW.replace(day=27)
    out = funnel_summary(conn, now=recovery_now + timedelta(hours=4), fills=None)
    s = out["summary"]
    assert out["session_date"] == "2026-08-27"
    assert s["decisions"] == 4           # the recovery day's own decisions only
    assert s["not_created"] == 2         # BUYA/BUYB re-decisions (pending_buy)
    assert s["filled_verified"] == 0     # day-1 completions stay OUT of the cohort
    assert s["carryover_intents"] == 2
    carried = {c["symbol"]: c for c in out["carryover"]}
    assert set(carried) == {"BUYA", "BUYB"}
    for sym in ("BUYA", "BUYB"):
        entry = carried[sym]
        assert entry["phase"] == "complete"      # observation events alone suffice
        assert entry["created_on"] == "2026-08-26"
        o = entry["orders"][0]
        assert o["submitted_at"].startswith("2026-08-26")
        assert o["last_observation_kind"] == "order_filled"
        assert o["observed_at"].startswith("2026-08-27")
        assert o["fills_unavailable"] is True    # fills=None: no feed was consulted


def test_sizing_snapshot_carries_ts_and_attempt():
    """R8 closure: the snapshot exposes ITS OWN event timestamp and attempt
    identity (leader follow-up after the frontend flagged the gap)."""
    import json as _json
    from agentic_trading.dashboard.views import funnel_summary
    from agentic_trading.journal.logger import connect, save_trade_intent, TradeIntent
    conn = connect(":memory:")
    from datetime import datetime, timezone as _tz
    FIXED_NOW = datetime(2026, 8, 26, 14, 30, 0, tzinfo=_tz.utc)
    save_trade_intent(conn, TradeIntent(symbol="SNAP", created_at=FIXED_NOW.isoformat(),
                                        signal_price=100.0, atr14=3.0, quant_score=0.5,
                                        combined_score=0.5, reasoning="t"))
    conn.execute(
        "INSERT INTO intent_events (timestamp, symbol, kind, deferred, run_id, mode,"
        " decision_key, intent_id, attempt_id, payload) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (FIXED_NOW.isoformat(), "SNAP", "decision_buy", 0, "b:r1", "paper",
         "b:r1:SNAP", None, None, None),
    )
    conn.execute(
        "INSERT INTO intent_events (timestamp, symbol, kind, deferred, run_id, mode,"
        " decision_key, intent_id, payload) VALUES (?,?,?,?,?,?,?,?,?)",
        (FIXED_NOW.isoformat(), "SNAP", "intent_created", 0, "b:r1", "paper",
         "b:r1:SNAP", "v-1", None),
    )
    conn.execute(
        "INSERT INTO intent_events (timestamp, symbol, kind, deferred, detail, run_id,"
        " mode, intent_id, attempt_id, payload) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (FIXED_NOW.isoformat(), "SNAP", "sizing", 1, "veto", "b:r1",
         "paper", "v-1", "v-1:r1",
         _json.dumps({"reason": "cap", "sizing": {"available_notional": 5.0}})),
    )
    conn.commit()
    out = funnel_summary(conn, mode="paper", session_day="2026-08-26")
    chains = [c for c in out["chains"] if c["symbol"] == "SNAP"]
    assert chains, "SNAP chain missing"
    snap = chains[0]["sizing"]
    assert snap and snap.get("event_ts") == FIXED_NOW.isoformat()
    assert snap.get("attempt_id") == "v-1:r1"
