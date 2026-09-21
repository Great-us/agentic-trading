"""A-4 (c2c_a7e2 PLAN §五): minimal OFFLINE render tests for the Today funnel.

The project has NO frontend test infrastructure (frontend/package.json has no
test script; no vitest/jsdom installed), so this file adds none either. It
bundles the REAL frontend sources (src/pages/Today.tsx + its api.ts import)
with the esbuild binary that already ships inside node_modules as a vite
dependency, renders the exported TodayView with react-dom/server, and asserts
on the markup. No network, no browser, no new npm packages, no writes outside
a temp dir plus a session-scoped scratch file that is deleted afterwards.

Honest scope —
covered:   the presentational TodayView (the entire loaded page except the
           useEffect fetch wiring): funnel summary, per-chain detail (stage,
           reason, attempts/created_runs/observed_runs vs wait_events, order
           status via the backend's own phase/submit_phase semantics with
           accepted != filled), the echoed sizing snapshot with EVERY margin
           (R8: cash / exposure / sector-theme / stop-risk room / position
           cap / pre-post haircut / available / minimum; zero vs NULL kept
           apart), historical-snapshot labeling on superseded/terminal
           chains, the unlinked-fills section, the carryover section kept out
           of the denominator, old-schema/unknown degradation, funnel load
           failure, and the llm_book (non-P1) variant.
not covered: the default Today export's fetch/loading lifecycle (needs jsdom),
           CSS, browser interaction, and the HTTP endpoint itself — that is
           tests/test_dashboard.py::test_funnel_endpoint_is_read_only_aggregation.

Review R8 (c2c_a7e2): at least one test runs a REAL size_position() with a
real RiskConfig, pushes diagnostics.to_dict() into a funnel event, aggregates
with funnel_summary() and renders — hand fixtures must be internally
consistent (the old "exposure room 0 but $65.62 budget" data is gone).

Skipped (not failed) when node or the vendored esbuild is missing so the
Python suite still runs green on machines without the frontend toolchain.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agentic_trading.config import RiskConfig
from agentic_trading.dashboard.views import connect_ro, funnel_summary
from agentic_trading.journal.logger import (
    connect, record_cycle, record_intent_event,
)
from agentic_trading.risk.manager import size_position

TRADING_ROOT = Path(__file__).resolve().parents[1]
FRONTEND = TRADING_ROOT / "src" / "agentic_trading" / "dashboard" / "frontend"
NODE = shutil.which("node")
ESBUILD_JS = FRONTEND / "node_modules" / "esbuild" / "bin" / "esbuild"

# The entry imports the REAL component by relative path; esbuild resolves
# react/react-dom from frontend/node_modules by walking up from the importer.
ENTRY = """\
import { createElement } from "react";
import { readFileSync } from "node:fs";
import { renderToStaticMarkup } from "react-dom/server";
import { TodayView } from "./src/pages/Today";

const fixtures = JSON.parse(readFileSync(process.argv[2], "utf8"));
const markup = renderToStaticMarkup(createElement(
  TodayView,
  { data: fixtures.data, funnel: fixtures.funnel ?? null,
    funnelError: fixtures.funnelError ?? null }));
console.log(markup);
"""

requires_toolchain = pytest.mark.skipif(
    NODE is None or not ESBUILD_JS.is_file(),
    reason="offline render test needs node + the vendored esbuild (frontend toolchain)",
)


@pytest.fixture(scope="session")
def bundle() -> Path:
    """esbuild-bundle the real Today.tsx once per test session."""
    tag = uuid.uuid4().hex[:8]
    entry = FRONTEND / f".render-test-entry-{tag}.mjs"
    out = FRONTEND / f".render-test-bundle-{tag}.cjs"
    entry.write_text(ENTRY, encoding="utf-8")
    try:
        # CJS output so react-dom/server's native `require("stream")` works.
        proc = subprocess.run(
            [NODE, str(ESBUILD_JS), entry.name, "--bundle", "--format=cjs",
             "--platform=node", "--jsx=automatic", f"--outfile={out.name}"],
            cwd=FRONTEND, capture_output=True, text=True, encoding="utf-8",
            timeout=180,
        )
        assert proc.returncode == 0, (
            f"esbuild failed:\n{proc.stdout}\n{proc.stderr}\n"
            "(red state: Today.tsx does not export TodayView yet?)")
        return out
    finally:
        entry.unlink(missing_ok=True)


def render(bundle_path: Path, case: dict, tmp_path: Path) -> str:
    fixtures = tmp_path / "case.json"
    fixtures.write_text(json.dumps(case), encoding="utf-8")
    proc = subprocess.run(
        [NODE, str(bundle_path), str(fixtures)],
        cwd=FRONTEND, capture_output=True, text=True, encoding="utf-8",
        timeout=120,
    )
    assert proc.returncode == 0, f"render failed:\n{proc.stdout}\n{proc.stderr}"
    return proc.stdout


def teardown_module(module) -> None:
    for leftover in FRONTEND.glob(".render-test-bundle-*.cjs"):
        leftover.unlink(missing_ok=True)


# ---- fixtures: shapes mirror funnel_summary() in dashboard/views.py -------------

HEALTH = {
    "status": "ok", "message": "深周期 3 分钟前 · 快扫 1 分钟前 · 止损 6/6",
    "deep": {"timestamp": "2026-09-20T20:15:00+00:00", "cycle_id": 143,
             "age_seconds": 180, "missed_sessions": None, "stops_covered": 6,
             "positions": 6, "state": "ok"},
    "fast": {"timestamp": "2026-09-20T20:17:00+00:00", "cycle_id": 601,
             "age_seconds": 60, "missed_sessions": None, "stops_covered": 6,
             "positions": 6, "state": "ok"},
    "limits": {"deep_hours": 26, "fast_minutes": 45},
}


def today_payload(**over) -> dict:
    base = {
        "session_date": "2026-09-20", "calendar_today": "2026-09-20",
        "weekend": False, "health": HEALTH,
        "fills_today": [], "fills_degraded": False, "fills_reason": None,
        "intents": [], "intent_events": [], "vetoes": [], "holdings": [],
        "fail_closed_today": 0, "outcomes": [],
        "notes": {"fills": "成交来自券商 fills，不是 journal。",
                  "intents": "intent_events 自 2026-08-30 才落库。",
                  "legacy": "遗留仓走 legacy_sell_threshold。"},
    }
    base.update(over)
    return base


# The PLAN §五 budget example, echoed verbatim from a sizing snapshot.
# REVIEW FIX (R8): the old fixture had exposure_room 0.0 next to a $65.62
# budget — arithmetic that size_position() can never produce (the budget is
# the min of the rooms). This fixture is internally consistent: cash is the
# binding room, exposure still has $3,000 of headroom.
SIZING_VETO = {
    "cash_available": 65.62, "exposure_room": 3000.0, "position_cap": 975.5,
    "stop_risk_room_pct": None, "stop_risk_room_notional": None,
    "sector_theme_room": 1200.0, "pre_haircut_notional": 975.5,
    "post_haircut_notional": 975.5, "available_notional": 65.62,
    "min_position_notional": 390.23,
    "binding_constraints": ["cash"],
    "reject_code": "below_min_position",
}

assert SIZING_VETO["available_notional"] <= min(
    v for k, v in SIZING_VETO.items()
    if k in ("cash_available", "exposure_room", "position_cap",
             "sector_theme_room") and v is not None
), "hand fixture must be arithmetically consistent with size_position()"


def veto_snapshot(event_kind: str = "sizing", *, event_ts: str | None = None,
                  attempt_id: str | None = None) -> dict:
    snap = {
        "event_kind": event_kind, "sizing": SIZING_VETO,
        "reason": "only $65.62 available", "cash_available": 65.62,
        "available_notional": 65.62, "min_notional": 390.23,
    }
    # R8 (iteration-7 review): the aggregator stamps event_ts / attempt_id on
    # every snapshot it builds; hand fixtures pass them only when the case
    # needs the snapshot bound to (or detached from) its submit attempt.
    if event_ts is not None:
        snap["event_ts"] = event_ts
    if attempt_id is not None:
        snap["attempt_id"] = attempt_id
    return snap


# ---- R8 helpers: a REAL RiskConfig and a pinned session for the funnel run ----

# Same pinned session/cutoff as tests/test_funnel_aggregation.py.
NOW = datetime(2026, 9, 21, 2, 0, tzinfo=timezone.utc)


def t(day: int, hour: int, minute: int) -> str:
    return f"2026-09-{day:02d}T{hour:02d}:{minute:02d}:00+00:00"


def rid(tag: str) -> str:
    return f"Trading:2026-09-20T13:00:00+00:00:{tag}"


def dk(run_id: str, symbol: str) -> str:
    return f"{run_id}:{symbol}"


def real_risk_config() -> RiskConfig:
    """A real (not mocked) RiskConfig, in the spirit of tests/test_risk.py."""
    return RiskConfig(
        risk_per_trade_pct=0.02, max_position_pct=0.20, min_position_pct=0.03,
        max_total_exposure_pct=0.65, max_open_positions=8,
        max_new_orders_per_cycle=5, min_quant_score_to_consider=0.15,
        atr_stop_multiple=2.5, min_stop_pct=0.06, max_stop_pct=0.20,
        trailing_stop_pct=0.12, risk_off_size_multiplier=0.5,
        risk_off_score_penalty=0.15, escalation_cooldown_minutes=60,
        escalation_cooldown_score_delta=0.15,
    )


def zero_summary(**over) -> dict:
    base = {
        "decisions": 0, "with_intent": 0, "not_created": 0,
        "not_created_reasons": {}, "evidence_missing": 0, "submitted": 0,
        "submitted_frac": "0/0", "filled_verified": 0, "partial": 0,
        "waiting": 0, "discarded": 0, "cleared": 0, "created_pending": 0,
        "carryover_intents": 0,
    }
    base.update(over)
    return base


def empty_degraded(**over) -> dict:
    base = {
        "fills": {"degraded": False, "reason": None, "truncated": None},
        "unattributed_decision_events": 0, "unparseable_timestamps": 0,
        "unparseable_payloads": 0, "legacy_flush_events": 0,
        "orphan_intents": [], "unknown_origin_intents": [], "notes": [],
    }
    base.update(over)
    return base


def funnel_payload(**over) -> dict:
    base = {
        "session_date": "2026-09-20", "mode": "paper",
        "generated_at": "2026-09-21T02:00:00+00:00", "schema_degraded": False,
        "schema_reason": None, "summary": zero_summary(), "chains": [],
        "carryover": [], "run_skips": [], "degraded": empty_degraded(),
    }
    base.update(over)
    return base


# ---- 1. budget-insufficient chain (the PLAN's sizing example) -------------------

@requires_toolchain
def test_render_budget_insufficient_chain(bundle, tmp_path):
    funnel = funnel_payload(
        summary=zero_summary(
            decisions=2, with_intent=1, waiting=1, not_created=1,
            not_created_reasons={"dry_run": 1}, submitted_frac="0/2"),
        chains=[
            {"decision_key": "run-1:AAA", "run_id": "run-1", "symbol": "AAA",
             "first_event_at": "2026-09-20T13:00:00+00:00",
             "last_event_at": "2026-09-20T15:00:00+00:00",
             "stage": "sizing", "reason": "only $65.62 available",
             "intent_id": "v-1", "replaced_previous_version": None,
             "superseded_by": None, "phase": None,
             "attempts": 3, "created_runs": 1, "observed_runs": 0,
             "wait_events": 2, "orders": [],
             "sizing": veto_snapshot(), "classification": "waiting"},
            {"decision_key": "run-2:BBB", "run_id": "run-2", "symbol": "BBB",
             "first_event_at": "2026-09-20T13:05:00+00:00",
             "last_event_at": "2026-09-20T13:06:00+00:00",
             "stage": "intent_not_created", "reason": "dry_run",
             "intent_id": None, "replaced_previous_version": None,
             "superseded_by": None, "phase": None,
             "attempts": 0, "created_runs": 0, "observed_runs": 0,
             "wait_events": 0, "orders": [], "sizing": None,
             "classification": "not_created"},
        ],
        carryover=[
            {"intent_id": "v-old", "symbol": "OLD", "created_on": "2026-09-19",
             "created_by_decision": "run-0:OLD", "stage": "sizing",
             "reason": "not_before", "attempts": 2, "created_runs": 1,
             "observed_runs": 0, "wait_events": 2, "phase": None,
             "superseded_by": None, "orders": [], "sizing": None,
             "classification": "waiting"},
        ],
        run_skips=[{"run_id": "run-9", "reason": "market closed",
                    "at": "2026-09-20T14:00:00+00:00"}],
        degraded=empty_degraded(legacy_flush_events=1),
    )
    markup = render(bundle, {"data": today_payload(), "funnel": funnel}, tmp_path)

    # R8: EVERY margin of the snapshot is displayed — not just the budget.
    assert "现金 $65.62" in markup
    assert "敞口余量 $3,000.00" in markup
    # The sector/theme room label must name BOTH dimensions (R8 措辞要求).
    assert "行业/主题有效余量 $1,200.00" in markup
    # Stop-risk room was never evaluated here: NULL means 未评估, never $0.
    assert "组合止损风险余量 未评估" in markup
    assert "单仓上限 $975.50" in markup
    assert "折扣前 $975.50 / 折扣后 $975.50" in markup
    assert "可用预算 $65.62" in markup and "最低仓位 $390.23" in markup
    # A live waiting chain may still say the intent is kept…
    assert "本次 sizing 未通过（可用预算低于最低仓位），意图保留待后续尝试" in markup
    assert "限制项：cash" in markup
    # …and the snapshot carries its event identity (R8: the backend does not
    # expose a snapshot timestamp/attempt id, so the page shows the event kind).
    assert "事件 sizing" in markup
    # Attempts and wait events are displayed separately, never merged (R6:
    # creation runs are counted separately too).
    assert "尝试 3 次" in markup and "等待 2 次" in markup and "创建 1" in markup
    # Cohort summary: denominator, fraction, per-bucket counts, not-created reason.
    assert "0/2" in markup and "dry_run" in markup and "明确未建" in markup
    # Carryover has its own section and stays out of the denominator.
    assert "承接意图" in markup and "不算今天分母" in markup and "2026-09-19" in markup
    # Run-level skips and identity-less legacy rows degrade explicitly.
    assert "market closed" in markup and "旧行无身份" in markup


# ---- 2. partial fill + accepted-without-fill orders -----------------------------

@requires_toolchain
def test_render_partial_fill_and_accepted_not_filled(bundle, tmp_path):
    funnel = funnel_payload(
        summary=zero_summary(
            decisions=2, with_intent=2, submitted=2, submitted_frac="2/2",
            partial=1, filled_verified=1),
        chains=[
            {"decision_key": "run-1:PPP", "run_id": "run-1", "symbol": "PPP",
             "first_event_at": "2026-09-20T14:00:00+00:00",
             "last_event_at": "2026-09-20T14:10:00+00:00",
             "stage": "order_partial", "reason": None, "intent_id": "v-p",
             "replaced_previous_version": None, "superseded_by": None,
             "phase": "partial", "attempts": 1, "created_runs": 1,
             "observed_runs": 1, "wait_events": 0,
             "orders": [{"order_id": "o-p", "client_order_id": "cid-p",
                         "submitted_at": "2026-09-20T14:00:00+00:00",
                         "submit_status": "accepted",
                         "submit_phase": "submitted_accepted",
                         "last_observation_kind": "order_partial",
                         "last_observation_failed": False,
                         "observed_at": "2026-09-20T14:10:00+00:00",
                         "broker_status": "partially_filled",
                         "terminal_status": None,
                         "filled_evidence": False, "partial_evidence": True,
                         "filled_qty": 2, "filled_avg_price": 105.25,
                         "fills_matched": True,
                         "fills_qty": 2, "fills_notional": 210.5,
                         "fills_unavailable": False}],
             "sizing": None, "classification": "submitted"},
            {"decision_key": "run-2:CCC", "run_id": "run-2", "symbol": "CCC",
             "first_event_at": "2026-09-20T14:30:00+00:00",
             "last_event_at": "2026-09-20T14:35:00+00:00",
             "stage": "order_observed", "reason": None, "intent_id": "v-c",
             "replaced_previous_version": None, "superseded_by": None,
             "phase": "submitted", "attempts": 1, "created_runs": 1,
             "observed_runs": 1, "wait_events": 0,
             "orders": [{"order_id": "o-c", "client_order_id": "cid-c",
                         "submitted_at": "2026-09-20T14:30:00+00:00",
                         "submit_status": None, "submit_phase": "submit_unknown",
                         "last_observation_kind": "order_observed",
                         "last_observation_failed": False,
                         "observed_at": "2026-09-20T14:35:00+00:00",
                         "broker_status": "accepted", "terminal_status": None,
                         "filled_evidence": False, "partial_evidence": False,
                         "filled_qty": None, "filled_avg_price": None,
                         "fills_matched": False,
                         "fills_qty": None, "fills_notional": None,
                         "fills_unavailable": False}],
             "sizing": None, "classification": "submitted"},
        ],
    )
    markup = render(bundle, {"data": today_payload(), "funnel": funnel}, tmp_path)

    # Partial fill: own bucket, per-order detail with matched fills evidence.
    assert "部分成交" in markup and "o-p" in markup
    assert "部分成交 2 股" in markup and "$210.50" in markup
    # accepted is not filled: explicit, never folded into a fill count.
    assert "o-c" in markup and "accepted" in markup
    assert "已受理，未确认成交" in markup
    # The partial bucket is counted in the summary next to the submitted fraction.
    assert "部分成交" in markup and "2/2" in markup


# ---- 3. old schema / unknown data degrades honestly ------------------------------

@requires_toolchain
def test_render_old_schema_unknown_degrades(bundle, tmp_path):
    funnel = {
        "session_date": "2026-08-21", "mode": "paper",
        "generated_at": "2026-08-22T02:00:00+00:00", "schema_degraded": True,
        "schema_reason": "旧 schema——intent_events 缺少身份列: run_id, intent_id",
        "summary": zero_summary(), "chains": [], "carryover": [], "run_skips": [],
        "degraded": empty_degraded(),
        "legacy_event_counts": {"sizing": 3, "gap": 1},
    }
    markup = render(bundle, {"data": today_payload(), "funnel": funnel}, tmp_path)
    # Unknown/degraded is shown as such — with the reason and kind counts.
    assert "旧数据" in markup and "缺少身份列" in markup
    assert "sizing" in markup and "3" in markup and "gap" in markup
    # ... and the zeroed summary is NOT presented as a real "today: 0 buys" story.
    assert "0/0" not in markup


# ---- 4. funnel load failure keeps the rest of the page ---------------------------

@requires_toolchain
def test_render_funnel_load_failure_keeps_page(bundle, tmp_path):
    data = today_payload(
        fills_today=[{"symbol": "MSFT", "side": "buy", "qty": 1.0,
                      "price": 200.0,
                      "transaction_time": "2026-09-20T14:00:00+00:00"}])
    markup = render(
        bundle,
        {"data": data, "funnel": None,
         "funnelError": "Error: /api/books/p1/funnel: 500"},
        tmp_path)
    # The page itself still renders...
    assert "今天成交了什么" in markup and "交班" in markup and "MSFT" in markup
    # ...the funnel section says it is unavailable instead of inventing numbers.
    assert "漏斗不可用" in markup and "500" in markup
    assert "0/0" not in markup and "BUY 决策" not in markup


# ---- 5. non-P1 book (llm_book, no funnel endpoint) stays compatible ---------------

@requires_toolchain
def test_render_llm_book_without_funnel_endpoint(bundle, tmp_path):
    data = today_payload(
        book_kind="llm_book",
        holdings=[{"symbol": "SPY", "qty": 10, "market_value": 5400.0,
                   "unrealized_pl": 120.0, "source": "broker"}],
        progress={"present": True, "card": {
            "round": {"name": "round-7", "round_id": "r7",
                      "asof": "2026-09-20T15:00:00+00:00", "status": "ok"},
            "did": ["flatten check"], "did_not": ["no entry"],
            "next_job": {"slot": "next", "when_et": "2026-09-20T16:00:00+00:00",
                         "when": None, "instruction": "continue"},
        }})
    markup = render(
        bundle,
        {"data": data, "funnel": None,
         "funnelError": "Error: /api/books/p3/funnel: 404"},
        tmp_path)
    # The llm book variant renders its own panels without P1-only KPIs...
    assert "本轮拦截 / 未决" in markup and "SPY" in markup and "交班" in markup
    assert '<div class="label">排队意图</div>' not in markup
    assert '<div class="label">LLM fail-closed</div>' not in markup
    # ...and the missing funnel endpoint degrades to a note, not a crash.
    assert "漏斗不可用" in markup and "404" in markup


# ---- 6. legacy intent_events are labeled by stage, not blanket "丢弃" --------------

@requires_toolchain
def test_render_legacy_intent_events_labeled_by_stage(bundle, tmp_path):
    data = today_payload(intent_events=[
        {"timestamp": "2026-09-20T14:00:00+00:00", "symbol": "AAA",
         "kind": "sizing", "deferred": 1, "detail": "only $65.62 available"},
        {"timestamp": "2026-09-20T14:01:00+00:00", "symbol": "BBB",
         "kind": "gap", "deferred": 0, "detail": "gap too large"},
        {"timestamp": "2026-09-20T14:02:00+00:00", "symbol": "CCC",
         "kind": "order_submitted", "deferred": 0, "detail": None},
        {"timestamp": "2026-09-20T14:03:00+00:00", "symbol": "DDD",
         "kind": "order_filled", "deferred": 0, "detail": None},
    ])
    markup = render(bundle, {"data": data, "funnel": funnel_payload()}, tmp_path)
    # Old wait-kind events that kept the intent say so...
    assert "sizing（保留待触发）" in markup
    # ...old discard kinds stay discards...
    assert "gap（丢弃）" in markup
    # ...and NEW kinds are never blanket-labeled 丢弃 (they are their own stage).
    assert "order_submitted（丢弃）" not in markup
    assert "order_filled（丢弃）" not in markup
    assert "order_filled（保留" not in markup


# ---- 7. R8: a REAL size_position()贯通事件、聚合、渲染 ------------------------------

@requires_toolchain
def test_real_size_position_through_event_aggregation_render(bundle, tmp_path):
    """R8 red-test core: a real RiskConfig -> size_position() -> diagnostics.
    to_dict() into a funnel event -> funnel_summary() -> TodayView render.
    Every displayed number comes from the real computation; hand fixtures that
    contradict the arithmetic (the old "exposure room 0 but $65.62 budget")
    are banned by construction."""
    risk = real_risk_config()
    res = size_position(
        "REAL", last_price=100.0, equity=10_000.0, cash=6_500.0,
        invested_value=3_000.0, open_position_count=2, risk=risk,
        stop_pct=0.08, existing_stop_risk=100.0, corr_multiplier=0.5,
        sector_room=4_000.0,
    )
    d = res.diagnostics
    assert res.approved and d is not None
    # The budget is the min of the rooms — a real result can never show a
    # $65.62 budget next to a $0 exposure room (the old fixture's nonsense).
    assert d.available_notional == res.notional
    rooms = [v for k, v in d.to_dict().items()
             if k in ("cash_available", "exposure_room", "position_cap",
                      "sector_theme_room") and v is not None]
    assert rooms and d.available_notional <= min(rooms)
    # The scenario exercises every margin: haircuts and the stop-risk room
    # must actually be non-None, or the render assertions below prove nothing.
    assert d.stop_risk_room_pct is not None
    assert d.post_haircut_notional < d.pre_haircut_notional
    assert d.sector_theme_room is not None

    # Mirror run.py's order_submitted payload verbatim: notional/reason/sizing.
    db = tmp_path / "journal.db"
    conn = connect(db)
    record_cycle(conn, "2026-09-19T15:00:00+00:00", "paper", 10_000.0, 900.0, [])
    record_cycle(conn, "2026-09-20T15:00:00+00:00", "paper", 10_000.0, 850.0, [])
    record_intent_event(conn, "REAL", "decision_buy", False, None,
                        timestamp=t(20, 13, 0), run_id=rid("z0"), mode="paper")
    record_intent_event(conn, "REAL", "intent_created", False, None,
                        timestamp=t(20, 13, 1), run_id=rid("z0"), mode="paper",
                        decision_key=dk(rid("z0"), "REAL"), intent_id="v-real")
    record_intent_event(conn, "REAL", "order_submitted", False, None,
                        timestamp=t(20, 14, 0), run_id=rid("z0"), mode="paper",
                        intent_id="v-real", order_id="o-real",
                        payload={"client_order_id": "cid-real",
                                 "submit_status": "accepted",
                                 "notional": res.notional, "reason": res.reason,
                                 "sizing": d.to_dict()})
    conn.close()
    ro = connect_ro(db)
    try:
        funnel = funnel_summary(ro, now=NOW, fills=[])
    finally:
        ro.close()
    assert funnel["summary"]["decisions"] == 1
    chain = funnel["chains"][0]
    # The snapshot travels with the event that produced it — the page can name
    # that event kind (R8: this is the identity the backend exposes today).
    assert chain["sizing"]["event_kind"] == "order_submitted"
    assert chain["sizing"]["sizing"]["available_notional"] == pytest.approx(1000.0)

    markup = render(bundle, {"data": today_payload(), "funnel": funnel}, tmp_path)
    # Every margin label + value, all echoing the REAL size_position() result.
    for expected in (
        "sizing 快照", "事件 order_submitted",
        "现金 $6,500.00",
        "敞口余量 $3,500.00",
        "行业/主题有效余量 $4,000.00",
        "组合止损风险余量 5.00% / $6,250.00",
        "单仓上限 $2,000.00",
        "折扣前 $2,000.00 / 折扣后 $1,000.00",
        "可用预算 $1,000.00",
        "最低仓位 $300.00",
        "限制项：position_cap、correlation_haircut",
    ):
        assert expected in markup, f"missing from render: {expected}"
    # An approved sizing on a live chain: no veto sentence and no 未评估 —
    # every field was actually computed.
    assert "意图保留" not in markup
    assert "未评估" not in markup


# ---- 8. R8: zero vs NULL must stay distinguishable ---------------------------------

@requires_toolchain
def test_render_zero_versus_null_margins(bundle, tmp_path):
    """NULL = the field was never evaluated (未评估), 0 = really zero dollars.
    The NULL chain comes from a REAL size_position() early exit — the original
    code path never computed a number, so nothing may be backfilled as $0."""
    void = size_position(
        "VOID", last_price=0.0, equity=10_000.0, cash=0.0, invested_value=3_000.0,
        open_position_count=2, risk=real_risk_config(), stop_pct=0.08,
    )
    assert void.diagnostics is not None
    assert void.diagnostics.reject_code == "invalid_price"
    # Early exit: cash was passed as 0.0 on purpose — the diagnostics still
    # record None (unevaluated), NOT a computed $0.
    assert void.diagnostics.cash_available is None
    null_sizing = void.diagnostics.to_dict()

    # A genuinely zero-cash veto: internally consistent (min(cash, …) = 0).
    zero_sizing = {
        "cash_available": 0.0, "exposure_room": 3000.0, "position_cap": 2000.0,
        "stop_risk_room_pct": 0.05, "stop_risk_room_notional": 6250.0,
        "sector_theme_room": 2500.0, "pre_haircut_notional": 0.0,
        "post_haircut_notional": 0.0, "available_notional": 0.0,
        "min_position_notional": 300.0,
        "binding_constraints": ["cash"], "reject_code": "below_min_position",
    }
    assert zero_sizing["available_notional"] <= min(
        zero_sizing[k] for k in ("cash_available", "exposure_room", "position_cap",
                                 "sector_theme_room"))

    funnel = funnel_payload(
        summary=zero_summary(decisions=2, with_intent=2, waiting=2),
        chains=[
            {"decision_key": "run-n:NULL", "run_id": "run-n", "symbol": "NUL",
             "first_event_at": "2026-09-20T13:00:00+00:00",
             "last_event_at": "2026-09-20T13:01:00+00:00",
             "stage": "sizing", "reason": "invalid price",
             "intent_id": "v-null", "replaced_previous_version": None,
             "superseded_by": None, "phase": None,
             "attempts": 1, "created_runs": 1, "observed_runs": 0,
             "wait_events": 0, "orders": [],
             "sizing": {"event_kind": "sizing", "sizing": null_sizing,
                        "reason": "invalid price"},
             "classification": "waiting"},
            {"decision_key": "run-z:ZERO", "run_id": "run-z", "symbol": "ZRO",
             "first_event_at": "2026-09-20T13:10:00+00:00",
             "last_event_at": "2026-09-20T13:11:00+00:00",
             "stage": "sizing", "reason": "no cash",
             "intent_id": "v-zero", "replaced_previous_version": None,
             "superseded_by": None, "phase": None,
             "attempts": 1, "created_runs": 1, "observed_runs": 0,
             "wait_events": 0, "orders": [],
             "sizing": {"event_kind": "sizing", "sizing": zero_sizing,
                        "reason": "no cash", "cash_available": 0.0,
                        "available_notional": 0.0, "min_notional": 300.0},
             "classification": "waiting"},
        ],
    )
    markup = render(bundle, {"data": today_payload(), "funnel": funnel}, tmp_path)
    # The NULL chain: 未评估 everywhere, never a fabricated $0.
    assert "现金 未评估" in markup
    assert "敞口余量 未评估" in markup
    assert "可用预算 未评估" in markup
    assert "拒绝码 invalid_price" in markup
    # The ZERO chain: a real $0 is displayed as $0.
    assert "现金 $0.00" in markup
    assert "可用预算 $0.00" in markup
    assert "折扣前 $0.00 / 折扣后 $0.00" in markup
    assert "敞口余量 $3,000.00" in markup      # the zero chain still has exposure headroom
    assert "敞口余量 $0" not in markup          # zero cash is not zero exposure
    assert "意图保留待后续尝试" in markup        # the zero chain is a live waiting veto


# ---- 9. R8: historical snapshots + rejected/canceled wording + unlinked fills ------

@requires_toolchain
def test_render_historical_sizing_and_rejected_wording(bundle, tmp_path):
    """Replaced / terminal chains mark their sizing 历史快照 instead of the
    blanket 意图保留 (R8), and order wording follows the backend's
    phase/submit_phase semantics: rejected / canceled never read as 已受理."""
    funnel = funnel_payload(
        summary=zero_summary(decisions=2, with_intent=2, superseded=1,
                             submit_rejected=1),
        chains=[
            # Replaced old version: keeps its failure history, labeled historical.
            {"decision_key": "run-1:AAA", "run_id": "run-1", "symbol": "AAA",
             "first_event_at": "2026-09-20T13:00:00+00:00",
             "last_event_at": "2026-09-20T13:30:00+00:00",
             "stage": "sizing", "reason": "only $65.62 available",
             "intent_id": "v-1", "replaced_previous_version": None,
             "superseded_by": "v-2", "phase": None,
             "attempts": 1, "created_runs": 1, "observed_runs": 0,
             "wait_events": 0, "orders": [],
             "sizing": veto_snapshot(), "classification": "superseded"},
            # Explicit rejection (broker returned None, no order_id).
            {"decision_key": "run-2:BBB", "run_id": "run-2", "symbol": "BBB",
             "first_event_at": "2026-09-20T14:00:00+00:00",
             "last_event_at": "2026-09-20T14:01:00+00:00",
             "stage": "order_submitted", "reason": "insufficient buying power",
             "intent_id": "v-r", "replaced_previous_version": None,
             "superseded_by": None, "phase": "submit_rejected",
             "attempts": 1, "created_runs": 1, "observed_runs": 0,
             "wait_events": 0,
             "orders": [{"order_id": None, "client_order_id": None,
                         "submitted_at": "2026-09-20T14:00:00+00:00",
                         "submit_status": "rejected",
                         "submit_phase": "submit_rejected",
                         "last_observation_kind": None,
                         "last_observation_failed": False,
                         "observed_at": None, "broker_status": None,
                         "terminal_status": None, "filled_evidence": False,
                         "partial_evidence": False, "filled_qty": None,
                         "filled_avg_price": None, "fills_matched": False,
                         "fills_qty": None, "fills_notional": None,
                         "fills_unavailable": False, "phase": "submit_rejected"}],
             # R8 (iteration-7 review): the rejection wording may only come
             # from the attempt ITSELF — the snapshot carries the submit
             # event's own timestamp, so it pairs to that one attempt.
             "sizing": veto_snapshot(
                 "order_submitted", event_ts="2026-09-20T14:00:00+00:00",
                 attempt_id="v-r:2026-09-20T14:00:00+00:00"),
             "classification": "submit_rejected"},
            # Accepted at submit, later canceled: terminal, never "已受理，未确认成交".
            {"decision_key": "run-3:CCC", "run_id": "run-3", "symbol": "CCC",
             "first_event_at": "2026-09-20T15:00:00+00:00",
             "last_event_at": "2026-09-20T15:10:00+00:00",
             "stage": "order_observed", "reason": None, "intent_id": "v-c",
             "replaced_previous_version": None, "superseded_by": None,
             "phase": "terminal_unfilled",
             "attempts": 1, "created_runs": 1, "observed_runs": 1,
             "wait_events": 0,
             "orders": [{"order_id": "o-can", "client_order_id": "cid-can",
                         "submitted_at": "2026-09-20T15:00:00+00:00",
                         "submit_status": "accepted",
                         "submit_phase": "submitted_accepted",
                         "last_observation_kind": "order_observed",
                         "last_observation_failed": False,
                         "observed_at": "2026-09-20T15:10:00+00:00",
                         "broker_status": "canceled",
                         "terminal_status": "canceled",
                         "filled_evidence": False, "partial_evidence": False,
                         "filled_qty": None, "filled_avg_price": None,
                         "fills_matched": False, "fills_qty": None,
                         "fills_notional": None, "fills_unavailable": False,
                         "phase": "terminal_unfilled"}],
             "sizing": None, "classification": "waiting"},
        ],
        unlinked_fills=[{
            "activity_id": "act-ghost", "order_id": "o-ghost", "symbol": "GHS",
            "side": "buy", "qty": 2.0, "notional": 50.0,
            "transaction_time": "2026-09-20T14:30:00+00:00",
            "fill_status": "fill",
            "note": "order_id 在台账中无任何订单事件",
        }],
    )
    markup = render(bundle, {"data": today_payload(), "funnel": funnel}, tmp_path)
    # Historical labeling, with the replacing version named.
    assert "历史快照（已被 v-2 替代）" in markup
    # R8 (iteration-7 review): the rejection shows because the snapshot PAIRS
    # to its own (sole) rejected attempt — never via a chain-level borrow.
    # The old wording 历史快照（链已终结：提交被拒绝） is gone from the page.
    assert "历史快照（该次提交被拒绝）" in markup
    assert "链已终结" not in markup
    region = chain_sizing_region(markup, "BBB")
    assert "该次提交被拒绝" in region
    assert "意图保留" not in markup
    # Rejected: backend phase semantics surface, no acceptance wording.
    assert "状态 提交被拒绝" in markup
    assert "提交被拒绝——券商未受理" in markup
    assert "已受理，未确认成交" not in markup
    # Canceled after acceptance: terminal wording, not an open "accepted" claim.
    assert "订单已终结（canceled）且未成交" in markup
    # Unlinked fills are listed as their own positive evidence.
    assert "未归因成交" in markup and "o-ghost" in markup and "GHS" in markup


# ---- 10. R8 (iteration-3 review): intent LIFECYCLE vs order PHASE ------------------
#
# REVIEW-ITERATION-2 R8 red test: aggregate THREE REAL chains with
# funnel_summary() (events exactly as the recorder writes them — kind/payload
# mirror run.py + execution_funnel.FunnelRecorder) and render them:
#   ① sizing veto -> TTL deletion       (classification discarded, phase None)
#   ② sizing veto -> cleared (already held) (classification cleared, phase None)
#   ③ partial fill, order NOT terminal  (classification submitted, phase partial)
# ①② must never show 意图保留; ③ must never show 链已终结 — the intent
# lifecycle and the order terminal state are separate facts.

# An APPROVED sizing snapshot, arithmetically consistent with size_position()
# (available = min(cash, exposure, cap, sector) — same shape as the real
# order_submitted payload run.py writes: submit_status / notional / reason /
# sizing).
SIZING_APPROVED = {
    "cash_available": 5000.0, "exposure_room": 3000.0, "position_cap": 1500.0,
    "stop_risk_room_pct": 0.01, "stop_risk_room_notional": 2000.0,
    "sector_theme_room": 2500.0, "pre_haircut_notional": 1500.0,
    "post_haircut_notional": 1500.0, "available_notional": 1500.0,
    "min_position_notional": 390.23, "binding_constraints": ["position_cap"],
    "reject_code": None,
}

assert SIZING_APPROVED["available_notional"] == min(
    SIZING_APPROVED[k] for k in ("cash_available", "exposure_room",
                                 "position_cap", "sector_theme_room"))


def sizing_veto_event_payload() -> dict:
    """The sizing-event payload exactly as run.py's flush veto writes it
    (_intent_event("sizing", ..., payload={reason, cash_available,
    available_notional, min_notional, sizing}))."""
    return {
        "reason": "only $65.62 available",
        "cash_available": SIZING_VETO["cash_available"],
        "available_notional": SIZING_VETO["available_notional"],
        "min_notional": SIZING_VETO["min_position_notional"],
        "sizing": dict(SIZING_VETO),
    }


@requires_toolchain
def test_render_intent_lifecycle_separate_from_order_phase(bundle, tmp_path):
    conn = connect(tmp_path / "journal.db")
    record_cycle(conn, "2026-09-19T15:00:00+00:00", "paper", 10_000.0, 900.0, [])
    record_cycle(conn, "2026-09-20T15:00:00+00:00", "paper", 10_000.0, 850.0, [])

    # ① sizing 否决 → TTL 删除（classification=discarded, phase=None）
    r1 = rid("i3-a")
    record_intent_event(conn, "TTLD", "decision_buy", False, None,
                        timestamp=t(20, 13, 0), run_id=r1, mode="paper")
    record_intent_event(conn, "TTLD", "intent_created", False, None,
                        timestamp=t(20, 13, 1), run_id=r1, mode="paper",
                        decision_key=dk(r1, "TTLD"), intent_id="v-i3-ttl")
    record_intent_event(conn, "TTLD", "sizing", True, "only $65.62 available",
                        timestamp=t(20, 13, 2), run_id=r1, mode="paper",
                        intent_id="v-i3-ttl",
                        payload=sizing_veto_event_payload())
    # run.py: only after the row is really gone — detail is the TTL age.
    record_intent_event(conn, "TTLD", "ttl", False, "73h old",
                        timestamp=t(20, 14, 0), run_id=r1, mode="paper",
                        intent_id="v-i3-ttl")

    # ② sizing 否决 → 已持仓清除（classification=cleared, phase=None）
    r2 = rid("i3-b")
    record_intent_event(conn, "CLRD", "decision_buy", False, None,
                        timestamp=t(20, 13, 10), run_id=r2, mode="paper")
    record_intent_event(conn, "CLRD", "intent_created", False, None,
                        timestamp=t(20, 13, 11), run_id=r2, mode="paper",
                        decision_key=dk(r2, "CLRD"), intent_id="v-i3-clr")
    record_intent_event(conn, "CLRD", "sizing", True, "only $65.62 available",
                        timestamp=t(20, 13, 12), run_id=r2, mode="paper",
                        intent_id="v-i3-clr",
                        payload=sizing_veto_event_payload())
    # execution_funnel.FunnelRecorder.intent_cleared_held shape.
    record_intent_event(conn, "CLRD", "intent_cleared", False, "already_held",
                        timestamp=t(20, 14, 1), run_id=r2, mode="paper",
                        intent_id="v-i3-clr",
                        payload={"reason": "already_held"})

    # ③ 部分成交且订单未终结（classification=submitted, phase=partial）
    r3 = rid("i3-c")
    record_intent_event(conn, "PART", "decision_buy", False, None,
                        timestamp=t(20, 14, 2), run_id=r3, mode="paper")
    record_intent_event(conn, "PART", "intent_created", False, None,
                        timestamp=t(20, 14, 3), run_id=r3, mode="paper",
                        decision_key=dk(r3, "PART"), intent_id="v-i3-part")
    # run.py's funnel.order_submitted body.
    record_intent_event(conn, "PART", "order_submitted", False, None,
                        timestamp=t(20, 14, 4), run_id=r3, mode="paper",
                        intent_id="v-i3-part", order_id="o-i3-part",
                        payload={"client_order_id": "cid-i3-part",
                                 "submit_status": "accepted",
                                 "notional": 1500.0,
                                 "reason": "risk sized",
                                 "sizing": dict(SIZING_APPROVED)})
    # FunnelRecorder.observe() shape for a live partial.
    record_intent_event(conn, "PART", "order_partial", False, None,
                        timestamp=t(20, 15, 0), run_id=r3, mode="paper",
                        intent_id="v-i3-part", order_id="o-i3-part",
                        payload={"order_status": "partially_filled",
                                 "observed_at": t(20, 15, 0),
                                 "filled_qty": 2,
                                 "filled_avg_price": 100.0})
    conn.close()

    ro = connect_ro(tmp_path / "journal.db")
    try:
        funnel = funnel_summary(ro, now=NOW, fills=[])
    finally:
        ro.close()

    # The aggregates really are the three review scenarios (no hand fixtures).
    chains = {c["symbol"]: c for c in funnel["chains"]}
    assert chains["TTLD"]["classification"] == "discarded"
    assert chains["TTLD"]["stage"] == "ttl"
    assert chains["TTLD"]["phase"] is None
    assert chains["TTLD"]["sizing"] is not None
    assert chains["CLRD"]["classification"] == "cleared"
    assert chains["CLRD"]["stage"] == "intent_cleared"
    assert chains["CLRD"]["phase"] is None
    assert chains["PART"]["classification"] == "submitted"
    assert chains["PART"]["phase"] == "partial"

    markup = render(bundle, {"data": today_payload(), "funnel": funnel}, tmp_path)

    # ①②: dead intents — the sizing line is history, with the reason named,
    # and 意图保留 appears NOWHERE on the page.
    assert "历史快照（TTL 丢弃）" in markup
    assert "历史快照（意图已清除）" in markup
    assert "意图保留" not in markup
    # The backend classification itself is shown, not invented by the page.
    assert "已丢弃" in markup and "已清除" in markup
    # ③: submitted-but-partial is a submit-time snapshot; the chain is NOT
    # called terminal — the remainder may still fill.
    assert "链已终结" not in markup
    assert "提交时快照" in markup and "剩余订单未终结" in markup
    assert "部分成交 2 股" in markup


# ---- 11. R6-B (iteration-3 review): the structured attribution fields the
# backend already outputs must reach the page ----------------------------------
#
# REAL funnel_summary() output — run-cap remainder list (run.py's
# flush_skipped payload), mixed-mode identity-less history (paper + dry_run +
# mode-NULL), and an order-ownership conflict (one order_id under two
# intents, observed identity-less) — fed into the offline renderer. The page
# must show symbols, modes, the order_id and the un-attribution REASON, and
# must NOT conclude "no BUY decisions" from missing evidence.

@requires_toolchain
def test_render_structured_attribution_fields_reach_page(bundle, tmp_path):
    conn = connect(tmp_path / "journal.db")
    record_cycle(conn, "2026-09-19T15:00:00+00:00", "paper", 10_000.0, 900.0, [])
    record_cycle(conn, "2026-09-20T15:00:00+00:00", "paper", 10_000.0, 850.0, [])

    # ① run cap reached: run.py writes the remainder VERBATIM in detail and
    #    the structured unprocessed list in the payload.
    record_intent_event(
        conn, "*", "flush_skipped", False,
        "max_new_orders_reached; unprocessed: UNPA, UNPB",
        timestamp=t(20, 13, 0), run_id=rid("cap"), mode="paper",
        payload={"reason": "max_new_orders_reached", "limit": 2,
                 "unprocessed": ["UNPA", "UNPB"]})

    # ② identity-less history in THREE modes: paper, dry_run and mode-NULL
    #    (the backend's "unsplit" reference bucket). No versions are invented,
    #    so none of these join any chain — but the page must keep the modes
    #    apart instead of one blanket "已观察" line.
    record_intent_event(conn, "OLDA", "sizing", True, "only $65.62 available",
                        timestamp=t(20, 13, 5), mode="paper",
                        payload={"reason": "only $65.62 available"})
    record_intent_event(conn, "OLDB", "flush_wait", True, "not_before",
                        timestamp=t(20, 13, 6), mode="dry_run",
                        payload={"reason": "not_before"})
    record_intent_event(conn, "OLDC", "chase_signal", True, "chase_gap",
                        timestamp=t(20, 13, 7), mode=None,
                        payload={"reason": "chase_gap"})

    # ③ order-ownership conflict: the SAME order_id submitted under two
    #    intents (same book, same mode), then observed identity-less. The
    #    observation must land in unattributed_order_events with its reason.
    record_intent_event(conn, "CONX", "order_submitted", False, None,
                        timestamp=t(20, 13, 10), run_id=rid("cfa"), mode="paper",
                        intent_id="v-cfa", order_id="o-conf",
                        payload={"submit_status": "accepted"})
    record_intent_event(conn, "CONX", "order_submitted", False, None,
                        timestamp=t(20, 13, 11), run_id=rid("cfb"), mode="paper",
                        intent_id="v-cfb", order_id="o-conf",
                        payload={"submit_status": "accepted"})
    record_intent_event(conn, "CONX", "order_observed", False, None,
                        timestamp=t(20, 13, 12), run_id=rid("cfx"), mode="paper",
                        order_id="o-conf",
                        payload={"order_status": "accepted"})
    conn.close()

    ro = connect_ro(tmp_path / "journal.db")
    try:
        funnel = funnel_summary(ro, now=NOW, fills=[])
    finally:
        ro.close()

    # The aggregation really carries the three structures (sanity on the
    # REAL output — the red assertions are on the RENDER below).
    assert funnel["chains"] == [] and funnel["carryover"] == []
    assert funnel["run_skips"][0]["unprocessed"] == ["UNPA", "UNPB"]
    # The conflicted observation counts in BOTH structures (backend truth:
    # no intent_id → identity-less gate; conflict → unattributed list).
    assert funnel["degraded"]["identityless_by_mode"] == {
        "paper": 2, "dry_run": 1, "unsplit": 1}
    unattributed = funnel["degraded"]["unattributed_order_events"]
    assert len(unattributed) == 1
    assert unattributed[0]["order_id"] == "o-conf"
    assert "归属多个意图" in unattributed[0]["note"]

    markup = render(bundle, {"data": today_payload(), "funnel": funnel}, tmp_path)

    # ① the run-cap remainder: symbols AND the verbatim detail are visible.
    assert "UNPA" in markup and "UNPB" in markup
    assert "max_new_orders_reached" in markup
    # ② identity-less rows grouped BY mode — paper / dry_run / 未分模式
    #    separately, never one conflated "已观察" line.
    assert "paper 2 条" in markup and "dry_run 1 条" in markup
    assert "未分模式 1 条" in markup
    assert "身份缺失[paper]" in markup and "身份缺失[dry_run]" in markup
    assert "身份缺失[未分模式]" in markup
    assert "本轮已观察、旧身份缺失" not in markup
    # ③ the un-attributed order: order_id, symbol, mode and REASON on screen.
    assert "o-conf" in markup and "CONX" in markup
    assert "归属多个意图" in markup
    # Missing evidence must NOT be read as absence: no false "没有 BUY".
    assert "本会话没有 BUY 决策" not in markup
    assert "没有可完整归因" in markup


# ---- 12. R8 (iteration-3 review): partial fill THEN canceled -------------------
#
# From a REAL order_submitted event, through FunnelRecorder.observe() writing
# canceled + filled_qty>0 (the recorder's own kind/payload mapping), through
# funnel_summary() into the render. Canceled AND partially filled must both be
# visible; the order must not count as a whole-order completion; the sizing
# line must stop claiming "剩余订单未终结". A still-live partially_filled
# order is the control: it KEEPS the un-terminated wording.

class _SingleObsBroker:
    """Minimal _ObservingBroker returning one fixed observation."""

    def __init__(self, observation):
        self._observation = observation

    def observe_order(self, order_id):
        return self._observation


@requires_toolchain
def test_render_partial_then_canceled_sizing_reads_order_terminal_status(
        bundle, tmp_path, monkeypatch):
    from agentic_trading.execution.broker import OrderObservation
    from agentic_trading.execution_funnel import FunnelRecorder
    from agentic_trading.journal import logger as journal_logger

    # The recorder stamps the observation with its own clock — pin it so the
    # event lands inside the pinned session regardless of wall-clock time.
    observe_at = datetime(2026, 9, 20, 14, 0, tzinfo=timezone.utc)

    class _PinnedClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return observe_at

    monkeypatch.setattr(journal_logger, "datetime", _PinnedClock)

    conn = connect(tmp_path / "journal.db")
    record_cycle(conn, "2026-09-19T15:00:00+00:00", "paper", 10_000.0, 900.0, [])
    record_cycle(conn, "2026-09-20T15:00:00+00:00", "paper", 10_000.0, 850.0, [])

    def submit(symbol, run_id, intent_id, order_id):
        record_intent_event(conn, symbol, "decision_buy", False, None,
                            timestamp=t(20, 13, 0), run_id=run_id, mode="paper")
        record_intent_event(conn, symbol, "intent_created", False, None,
                            timestamp=t(20, 13, 1), run_id=run_id, mode="paper",
                            decision_key=dk(run_id, symbol), intent_id=intent_id)
        record_intent_event(conn, symbol, "order_submitted", False, None,
                            timestamp=t(20, 13, 2), run_id=run_id, mode="paper",
                            intent_id=intent_id, order_id=order_id,
                            payload={"client_order_id": f"cid-{order_id}",
                                     "submit_status": "accepted",
                                     "notional": 1500.0, "reason": "risk sized",
                                     "sizing": dict(SIZING_APPROVED)})

    # ① DEAD partial: submit, then the recorder's REAL observe() sees
    #    canceled with filled_qty>0 → kind=order_partial keeps BOTH facts.
    r1 = rid("r8-dead")
    submit("DEDP", r1, "v-r8-dead", "o-r8-dead")
    FunnelRecorder(conn, r1, "paper", jsonl=False).observe(
        "DEDP", "o-r8-dead",
        _SingleObsBroker(OrderObservation(
            order_id="o-r8-dead", status="canceled",
            observed_at=t(20, 14, 0), filled_qty=2.0,
            filled_avg_price=100.0, filled_at=None, error=None)),
        intent_id="v-r8-dead")

    # ② CONTROL: live partial — partially_filled, NO terminal evidence yet.
    r2 = rid("r8-live")
    submit("PARTL", r2, "v-r8-live", "o-r8-live")
    FunnelRecorder(conn, r2, "paper", jsonl=False).observe(
        "PARTL", "o-r8-live",
        _SingleObsBroker(OrderObservation(
            order_id="o-r8-live", status="partially_filled",
            observed_at=t(20, 14, 1), filled_qty=1.0,
            filled_avg_price=101.0, filled_at=None, error=None)),
        intent_id="v-r8-live")
    conn.close()

    ro = connect_ro(tmp_path / "journal.db")
    try:
        funnel = funnel_summary(ro, now=NOW, fills=[])
    finally:
        ro.close()

    # The aggregates carry the two independent facts (R5 holds): partial
    # evidence AND the canceled terminal status on the dead order; neither
    # chain is a whole-order completion.
    chains = {c["symbol"]: c for c in funnel["chains"]}
    dead, live = chains["DEDP"], chains["PARTL"]
    assert dead["phase"] == "partial" and live["phase"] == "partial"
    assert dead["orders"][0]["terminal_status"] == "canceled"
    assert dead["orders"][0]["filled_qty"] == pytest.approx(2.0)
    assert live["orders"][0]["terminal_status"] is None
    assert funnel["summary"]["filled_verified"] == 0
    assert funnel["summary"]["partial"] == 2

    markup = render(bundle, {"data": today_payload(), "funnel": funnel}, tmp_path)

    # Canceled and partial are BOTH visible on the dead order's line.
    assert "订单已终结（canceled）" in markup
    assert "部分成交 2 股" in markup
    # The dead partial's sizing no longer claims an unterminated remainder…
    assert "历史快照（部分成交，余单已取消）" in markup
    assert markup.count("剩余订单未终结") == 1   # …the LIVE control still does
    assert markup.count("提交时快照（部分成交") == 1
    assert "部分成交 1 股" in markup
    # …and neither is a whole-order completion.
    assert "整单完成" not in markup
    assert markup.count("历史快照（部分成交") == 1


# ---- 13. R8 (iteration-4 review): unique pairing vs pairing FAILURE ------------
#
# REVIEW-ITERATION-4 R8 red test, on the REAL recorder -> funnel_summary() ->
# render path. The sizing snapshot's binding to ITS OWN order splits into
# "uniquely paired" and "cannot determine the order":
#   ① AMBG: two orders submitted at the SAME instant — the match is ambiguous;
#   ② ZMTS: zero match — the LAST sizing payload came from a later flush
#      event whose timestamp equals no order's submitted_at;
#   ③ DIFF: one chain, two attempts with DIFFERENT states (attempt 1 canceled
#      WITH a partial fill, attempt 2 retried and still partially filled) —
#      the snapshot belongs to attempt 2 and must read attempt 2's OWN live
#      state, never borrowing attempt 1's canceled terminal status.
# On any uncertain pairing the page must NOT claim "剩余订单未终结" and must
# not borrow a terminal state. The uniquely-paired canceled+partial and
# still-live partially_filled controls stay covered by test 12, unchanged.

@requires_toolchain
def test_render_sizing_pairing_failure_vs_unique_pair(bundle, tmp_path):
    conn = connect(tmp_path / "journal.db")
    record_cycle(conn, "2026-09-19T15:00:00+00:00", "paper", 10_000.0, 900.0, [])
    record_cycle(conn, "2026-09-20T15:00:00+00:00", "paper", 10_000.0, 850.0, [])

    def submit_order(symbol, run_id, intent_id, order_id, ts):
        record_intent_event(conn, symbol, "order_submitted", False, None,
                            timestamp=ts, run_id=run_id, mode="paper",
                            intent_id=intent_id, order_id=order_id,
                            payload={"client_order_id": f"cid-{order_id}",
                                     "submit_status": "accepted",
                                     "notional": 1500.0, "reason": "risk sized",
                                     "sizing": dict(SIZING_APPROVED)})

    def observe_partial(symbol, run_id, intent_id, order_id, status, ts, qty):
        record_intent_event(conn, symbol, "order_partial", False, None,
                            timestamp=ts, run_id=run_id, mode="paper",
                            intent_id=intent_id, order_id=order_id,
                            payload={"order_status": status,
                                     "observed_at": ts, "filled_qty": qty,
                                     "filled_avg_price": 100.0})

    # ① AMBG — two orders submitted at the SAME instant, then a live partial.
    r = rid("r8-ambg")
    record_intent_event(conn, "AMBG", "decision_buy", False, None,
                        timestamp=t(20, 13, 0), run_id=r, mode="paper")
    record_intent_event(conn, "AMBG", "intent_created", False, None,
                        timestamp=t(20, 13, 1), run_id=r, mode="paper",
                        decision_key=dk(r, "AMBG"), intent_id="v-ambg")
    for oid in ("o-amg-a", "o-amg-b"):
        submit_order("AMBG", r, "v-ambg", oid, t(20, 14, 0))
    observe_partial("AMBG", r, "v-ambg", "o-amg-a", "partially_filled",
                    t(20, 14, 1), 1)

    # ② ZMTS — clean submit + live partial, then a LATER flush sizing veto
    #    (run.py's own payload shape): the last snapshot's timestamp matches
    #    no order's submitted_at.
    r = rid("r8-zmts")
    record_intent_event(conn, "ZMTS", "decision_buy", False, None,
                        timestamp=t(20, 13, 2), run_id=r, mode="paper")
    record_intent_event(conn, "ZMTS", "intent_created", False, None,
                        timestamp=t(20, 13, 3), run_id=r, mode="paper",
                        decision_key=dk(r, "ZMTS"), intent_id="v-zmts")
    submit_order("ZMTS", r, "v-zmts", "o-zmts", t(20, 14, 2))
    observe_partial("ZMTS", r, "v-zmts", "o-zmts", "partially_filled",
                    t(20, 14, 3), 1)
    record_intent_event(conn, "ZMTS", "sizing", True, "only $65.62 available",
                        timestamp=t(20, 14, 4), run_id=r, mode="paper",
                        intent_id="v-zmts",
                        payload=sizing_veto_event_payload())

    # ③ DIFF — attempt 1 canceled WITH a partial fill, attempt 2 retried and
    #    still partially filled. The last sizing payload is attempt 2's.
    r = rid("r8-diff")
    record_intent_event(conn, "DIFF", "decision_buy", False, None,
                        timestamp=t(20, 13, 4), run_id=r, mode="paper")
    record_intent_event(conn, "DIFF", "intent_created", False, None,
                        timestamp=t(20, 13, 5), run_id=r, mode="paper",
                        decision_key=dk(r, "DIFF"), intent_id="v-diff")
    submit_order("DIFF", r, "v-diff", "o-diff-a", t(20, 14, 5))
    observe_partial("DIFF", r, "v-diff", "o-diff-a", "canceled",
                    t(20, 14, 6), 2)
    submit_order("DIFF", r, "v-diff", "o-diff-b", t(20, 14, 7))
    observe_partial("DIFF", r, "v-diff", "o-diff-b", "partially_filled",
                    t(20, 14, 8), 1)
    conn.close()

    ro = connect_ro(tmp_path / "journal.db")
    try:
        funnel = funnel_summary(ro, now=NOW, fills=[])
    finally:
        ro.close()

    # The aggregates really are the three review situations.
    chains = {c["symbol"]: c for c in funnel["chains"]}
    ambg, zmts, diff = chains["AMBG"], chains["ZMTS"], chains["DIFF"]
    assert ambg["phase"] == "partial"
    assert [o["submitted_at"] for o in ambg["orders"]] == [t(20, 14, 0)] * 2
    assert ambg["sizing"]["event_ts"] == t(20, 14, 0)   # the same instant
    assert zmts["phase"] == "partial"
    assert zmts["sizing"]["event_ts"] == t(20, 14, 4)   # the LATER flush sizing
    assert zmts["sizing"]["event_ts"] != zmts["orders"][0]["submitted_at"]
    assert diff["phase"] == "partial"
    assert diff["orders"][0]["terminal_status"] == "canceled"
    assert diff["orders"][1]["terminal_status"] is None
    assert diff["sizing"]["event_ts"] == t(20, 14, 7)   # attempt 2's submit

    markup = render(bundle, {"data": today_payload(), "funnel": funnel}, tmp_path)

    # ①+② uncertain pairing: the page marks the snapshot undetermined and
    # makes NO survival claim — red against the old code, which printed
    # 提交时快照（部分成交，剩余订单未终结） for both of these chains.
    assert markup.count("对应订单/终态未能确定") == 2
    assert markup.count("剩余订单未终结") == 1           # only DIFF's live attempt 2
    assert "余单已取消" not in markup                     # no borrowed terminal status
    # ③ same chain, different attempts: the snapshot follows ITS OWN (latest)
    # attempt — a live, un-terminated remainder — not the sibling's canceled.
    assert "提交时快照（部分成交，剩余订单未终结）" in markup
    # Attempt 1's canceled+partial stays visible on its own order line.
    assert "订单已终结（canceled）" in markup


# ---- 14. R8: a sizing snapshot WITHOUT a timestamp makes no survival claim -----
#
# The real aggregator cannot emit a ts-less chain snapshot (chain intake
# requires a parseable in-session timestamp), so the missing-time case is
# exercised on a REAL funnel_summary() payload with the snapshot's event_ts
# cleared — the shape an older or hand-written payload can still deliver.

@requires_toolchain
def test_render_sizing_snapshot_without_timestamp_no_survival_claim(
        bundle, tmp_path):
    conn = connect(tmp_path / "journal.db")
    record_cycle(conn, "2026-09-19T15:00:00+00:00", "paper", 10_000.0, 900.0, [])
    record_cycle(conn, "2026-09-20T15:00:00+00:00", "paper", 10_000.0, 850.0, [])
    r = rid("r8-nots")
    record_intent_event(conn, "NOTS", "decision_buy", False, None,
                        timestamp=t(20, 13, 0), run_id=r, mode="paper")
    record_intent_event(conn, "NOTS", "intent_created", False, None,
                        timestamp=t(20, 13, 1), run_id=r, mode="paper",
                        decision_key=dk(r, "NOTS"), intent_id="v-nots")
    record_intent_event(conn, "NOTS", "order_submitted", False, None,
                        timestamp=t(20, 14, 0), run_id=r, mode="paper",
                        intent_id="v-nots", order_id="o-nots",
                        payload={"client_order_id": "cid-o-nots",
                                 "submit_status": "accepted",
                                 "notional": 1500.0, "reason": "risk sized",
                                 "sizing": dict(SIZING_APPROVED)})
    record_intent_event(conn, "NOTS", "order_partial", False, None,
                        timestamp=t(20, 14, 1), run_id=r, mode="paper",
                        intent_id="v-nots", order_id="o-nots",
                        payload={"order_status": "partially_filled",
                                 "observed_at": t(20, 14, 1), "filled_qty": 1,
                                 "filled_avg_price": 100.0})
    conn.close()

    ro = connect_ro(tmp_path / "journal.db")
    try:
        funnel = funnel_summary(ro, now=NOW, fills=[])
    finally:
        ro.close()
    assert funnel["chains"][0]["phase"] == "partial"
    funnel["chains"][0]["sizing"]["event_ts"] = None

    markup = render(bundle, {"data": today_payload(), "funnel": funnel}, tmp_path)
    # No timestamp → no pairing → no claim in either direction.
    assert "对应订单/终态未能确定" in markup
    assert "剩余订单未终结" not in markup
    assert "余单已" not in markup


# ---- 15. R6-B (iteration-4 review) ①: the empty state must not over-conclude ---
#
# REVIEW-ITERATION-4 R6-B ①: un-attributable evidence of ANY class — an
# un-attributed BUY decision (missing run_id), an orphan intent
# (intent_created wrote, decision_buy did not), an unparseable timestamp —
# means the chains could not be BUILT. The page must say "没有可完整归因的
# 执行链" with the concrete reason and must NOT conclude "没有 BUY 决策".
# A genuinely empty session keeps the original empty state.

@requires_toolchain
def test_render_empty_state_respects_unattributed_evidence(bundle, tmp_path):
    # ① The review's exact counterexample: one decision_buy with no run_id
    #    (and no mode) — unattributed_decision_events=1, chains/carryover empty.
    conn = connect(tmp_path / "j1.db")
    record_cycle(conn, "2026-09-19T15:00:00+00:00", "paper", 10_000.0, 900.0, [])
    record_cycle(conn, "2026-09-20T15:00:00+00:00", "paper", 10_000.0, 850.0, [])
    record_intent_event(conn, "GHOST", "decision_buy", False, None,
                        timestamp=t(20, 13, 0), run_id=None, mode=None)
    conn.close()
    ro = connect_ro(tmp_path / "j1.db")
    try:
        funnel = funnel_summary(ro, now=NOW, fills=[])
    finally:
        ro.close()
    assert funnel["chains"] == [] and funnel["carryover"] == []
    assert funnel["degraded"]["unattributed_decision_events"] == 1

    markup = render(bundle, {"data": today_payload(), "funnel": funnel}, tmp_path)
    assert "本会话没有 BUY 决策" not in markup
    assert "没有可完整归因" in markup
    assert "无法归因的 BUY 决策" in markup

    # ② Orphan intent: intent_created wrote, decision_buy did not — the
    #    backend lists the orphan; the empty state must not claim no BUY.
    conn = connect(tmp_path / "j2.db")
    record_cycle(conn, "2026-09-19T15:00:00+00:00", "paper", 10_000.0, 900.0, [])
    record_cycle(conn, "2026-09-20T15:00:00+00:00", "paper", 10_000.0, 850.0, [])
    record_intent_event(conn, "ORPH", "intent_created", False, None,
                        timestamp=t(20, 13, 1), run_id=rid("orph"), mode="paper",
                        decision_key=dk(rid("orph"), "ORPH"), intent_id="v-orph")
    conn.close()
    ro = connect_ro(tmp_path / "j2.db")
    try:
        funnel = funnel_summary(ro, now=NOW, fills=[])
    finally:
        ro.close()
    assert funnel["chains"] == [] and funnel["carryover"] == []
    assert len(funnel["degraded"]["orphan_intents"]) == 1

    markup = render(bundle, {"data": today_payload(), "funnel": funnel}, tmp_path)
    assert "本会话没有 BUY 决策" not in markup
    assert "没有可完整归因" in markup
    assert "孤儿意图" in markup

    # ③ Unparseable timestamp: the row can never join a chain; the parse-
    #    exception counter participates in the empty-state decision.
    conn = connect(tmp_path / "j3.db")
    record_cycle(conn, "2026-09-19T15:00:00+00:00", "paper", 10_000.0, 900.0, [])
    record_cycle(conn, "2026-09-20T15:00:00+00:00", "paper", 10_000.0, 850.0, [])
    record_intent_event(conn, "BADS", "sizing", True, "only $65.62 available",
                        timestamp="not-a-timestamp", run_id=rid("bads"),
                        mode="paper",
                        payload=sizing_veto_event_payload())
    conn.close()
    ro = connect_ro(tmp_path / "j3.db")
    try:
        funnel = funnel_summary(ro, now=NOW, fills=[])
    finally:
        ro.close()
    assert funnel["chains"] == [] and funnel["carryover"] == []
    assert funnel["degraded"]["unparseable_timestamps"] == 1

    markup = render(bundle, {"data": today_payload(), "funnel": funnel}, tmp_path)
    assert "本会话没有 BUY 决策" not in markup
    assert "没有可完整归因" in markup
    assert "时间戳不可解析" in markup

    # ④ A genuinely empty session keeps the ORIGINAL empty state.
    markup = render(bundle, {"data": today_payload(),
                             "funnel": funnel_payload()}, tmp_path)
    assert "本会话没有 BUY 决策，也没有承接意图。" in markup
    assert "没有可完整归因" not in markup


# ---- 16. R6-B (iteration-4 review) ②: unlinked fills are NEUTRAL + per-row note -
#
# R4's two conflicting submissions (one order_id under two intents) plus one
# fill in the feed: the section title must not preset "台账中无订单事件", and
# the row must show the backend's ownership-CONFLICT note verbatim.

@requires_toolchain
def test_render_unlinked_fills_show_backend_note(bundle, tmp_path):
    conn = connect(tmp_path / "journal.db")
    record_cycle(conn, "2026-09-19T15:00:00+00:00", "paper", 10_000.0, 900.0, [])
    record_cycle(conn, "2026-09-20T15:00:00+00:00", "paper", 10_000.0, 850.0, [])
    record_intent_event(conn, "CONY", "order_submitted", False, None,
                        timestamp=t(20, 13, 10), run_id=rid("cya"), mode="paper",
                        intent_id="v-cya", order_id="o-conf-y",
                        payload={"submit_status": "accepted"})
    record_intent_event(conn, "CONY", "order_submitted", False, None,
                        timestamp=t(20, 13, 11), run_id=rid("cyb"), mode="paper",
                        intent_id="v-cyb", order_id="o-conf-y",
                        payload={"submit_status": "accepted"})
    conn.close()

    fills = [{"id": "act-conf-y", "order_id": "o-conf-y", "symbol": "CONY",
              "side": "buy", "qty": 1.0, "price": 100.0, "notional": 100.0,
              "transaction_time": t(20, 15, 1), "order_status": "fill"}]
    ro = connect_ro(tmp_path / "journal.db")
    try:
        funnel = funnel_summary(ro, now=NOW, fills=fills)
    finally:
        ro.close()

    # The aggregation really is the conflict case: the fill stays order-level
    # with the ownership-conflict note (backend truth, no re-derivation).
    unlinked = funnel["unlinked_fills"]
    assert len(unlinked) == 1
    assert unlinked[0]["order_id"] == "o-conf-y"
    assert "归属多个意图" in unlinked[0]["note"]

    markup = render(bundle, {"data": today_payload(), "funnel": funnel}, tmp_path)
    # Neutral title, per-row backend note on screen — and the old blanket
    # "台账中无订单事件" wording appears nowhere on the page.
    assert "未归因成交" in markup
    assert "归属多个意图" in markup
    assert "台账中无订单事件" not in markup


# ---- 17. R8 (iteration-5 review) ①: the chain's terminal phase must not
#          override the snapshot's OWN paired order -----------------------------
#
# REVIEW-ITERATION-5 R8 counterexample: one chain, two attempts — order A
# filled completely, order B submitted later and still partially filled, the
# latest sizing snapshot carried by B's submit event. The backend's
# _chain_phase() returns "complete" (A's completion wins at chain level), so
# the OLD page hit its chain-level `orderTerminal` branch FIRST and printed
# 历史快照（链已终结：整单完成） on B's sizing line — B's live state buried
# under A's terminal. The assertions below are scoped to B's SizingLine block,
# so A's order detail legitimately reading 整单完成 cannot satisfy or mask
# them.

def chain_sizing_region(markup: str, symbol: str) -> str:
    """One chain card's SizingLine block: order lines render first and the
    sizing line LAST inside a decision-card, so take the chunk from this
    chain's symbol chip to the next card, then everything from the LAST
    `reasoning muted` div on (OrderLine divs are self-contained)."""
    start = markup.index(f'<span class="sym">{symbol}</span>')
    nxt = markup.find('<div class="decision-card">', start)
    chunk = markup[start:] if nxt == -1 else markup[start:nxt]
    return chunk[chunk.rindex('class="reasoning muted"'):]


@requires_toolchain
def test_render_chain_terminal_phase_does_not_override_paired_order(
        bundle, tmp_path):
    conn = connect(tmp_path / "journal.db")
    record_cycle(conn, "2026-09-19T15:00:00+00:00", "paper", 10_000.0, 900.0, [])
    record_cycle(conn, "2026-09-20T15:00:00+00:00", "paper", 10_000.0, 850.0, [])
    r = rid("r8-pair")
    record_intent_event(conn, "PAIR", "decision_buy", False, None,
                        timestamp=t(20, 15, 0), run_id=r, mode="paper")
    record_intent_event(conn, "PAIR", "intent_created", False, None,
                        timestamp=t(20, 15, 1), run_id=r, mode="paper",
                        decision_key=dk(r, "PAIR"), intent_id="v-pair")
    # Attempt A: submitted, then FILLED (recorder observe() shape — a filled
    # status is recorded under kind order_filled).
    record_intent_event(conn, "PAIR", "order_submitted", False, None,
                        timestamp=t(20, 15, 2), run_id=r, mode="paper",
                        intent_id="v-pair", order_id="o-pr-a",
                        payload={"client_order_id": "cid-o-pr-a",
                                 "submit_status": "accepted",
                                 "notional": 1500.0, "reason": "risk sized",
                                 "sizing": dict(SIZING_APPROVED)})
    record_intent_event(conn, "PAIR", "order_filled", False, None,
                        timestamp=t(20, 15, 3), run_id=r, mode="paper",
                        intent_id="v-pair", order_id="o-pr-a",
                        payload={"order_status": "filled",
                                 "observed_at": t(20, 15, 3),
                                 "filled_qty": 3, "filled_avg_price": 100.0})
    # Attempt B: submitted LATER — its submit event carries the LAST sizing
    # snapshot — then still partially filled, remainder unterminated.
    record_intent_event(conn, "PAIR", "order_submitted", False, None,
                        timestamp=t(20, 15, 4), run_id=r, mode="paper",
                        intent_id="v-pair", order_id="o-pr-b",
                        payload={"client_order_id": "cid-o-pr-b",
                                 "submit_status": "accepted",
                                 "notional": 1500.0, "reason": "risk sized",
                                 "sizing": dict(SIZING_APPROVED)})
    record_intent_event(conn, "PAIR", "order_partial", False, None,
                        timestamp=t(20, 15, 5), run_id=r, mode="paper",
                        intent_id="v-pair", order_id="o-pr-b",
                        payload={"order_status": "partially_filled",
                                 "observed_at": t(20, 15, 5),
                                 "filled_qty": 1, "filled_avg_price": 100.0})
    conn.close()

    ro = connect_ro(tmp_path / "journal.db")
    try:
        funnel = funnel_summary(ro, now=NOW, fills=[])
    finally:
        ro.close()

    # The aggregates really are the counterexample: the chain phase is
    # complete (A's completion), and the snapshot's instant equals ONLY B's
    # submitted_at — a unique, correct pairing.
    chain = funnel["chains"][0]
    assert chain["phase"] == "complete"
    assert chain["sizing"]["event_ts"] == t(20, 15, 4)
    assert [o["phase"] for o in chain["orders"]] == ["complete", "partial"]

    markup = render(bundle, {"data": today_payload(), "funnel": funnel}, tmp_path)

    region = chain_sizing_region(markup, "PAIR")
    # B's sizing line reads B's OWN live state — red against the old code,
    # which printed 历史快照（链已终结：整单完成） right here.
    assert "提交时快照（部分成交，剩余订单未终结）" in region
    assert "链已终结" not in region
    assert "整单完成" not in region
    # The chain-level terminal wording is gone from the page entirely; A's
    # completion fact stays on A's own order detail (and the chain head).
    assert "历史快照（链已终结：整单完成）" not in markup
    assert "状态 整单完成" in markup
    assert "订单 o-pr-a" in markup


@requires_toolchain
def test_render_completion_evidence_beats_retained_partial(bundle, tmp_path):
    # R8 (iteration-5 review) ④: the SAME order first shows partial evidence
    # and then a completion — the confirmed completion wins; the sizing is
    # history with completion wording, not a "still open" remainder claim.
    conn = connect(tmp_path / "journal.db")
    record_cycle(conn, "2026-09-19T15:00:00+00:00", "paper", 10_000.0, 900.0, [])
    record_cycle(conn, "2026-09-20T15:00:00+00:00", "paper", 10_000.0, 850.0, [])
    r = rid("r8-cpri")
    record_intent_event(conn, "CPRI", "decision_buy", False, None,
                        timestamp=t(20, 16, 0), run_id=r, mode="paper")
    record_intent_event(conn, "CPRI", "intent_created", False, None,
                        timestamp=t(20, 16, 1), run_id=r, mode="paper",
                        decision_key=dk(r, "CPRI"), intent_id="v-cpri")
    record_intent_event(conn, "CPRI", "order_submitted", False, None,
                        timestamp=t(20, 16, 2), run_id=r, mode="paper",
                        intent_id="v-cpri", order_id="o-cpr",
                        payload={"client_order_id": "cid-o-cpr",
                                 "submit_status": "accepted",
                                 "notional": 1500.0, "reason": "risk sized",
                                 "sizing": dict(SIZING_APPROVED)})
    record_intent_event(conn, "CPRI", "order_partial", False, None,
                        timestamp=t(20, 16, 3), run_id=r, mode="paper",
                        intent_id="v-cpri", order_id="o-cpr",
                        payload={"order_status": "partially_filled",
                                 "observed_at": t(20, 16, 3),
                                 "filled_qty": 1, "filled_avg_price": 100.0})
    record_intent_event(conn, "CPRI", "order_filled", False, None,
                        timestamp=t(20, 16, 4), run_id=r, mode="paper",
                        intent_id="v-cpri", order_id="o-cpr",
                        payload={"order_status": "filled",
                                 "observed_at": t(20, 16, 4),
                                 "filled_qty": 3, "filled_avg_price": 100.0})
    conn.close()

    ro = connect_ro(tmp_path / "journal.db")
    try:
        funnel = funnel_summary(ro, now=NOW, fills=[])
    finally:
        ro.close()

    # Both facts really accumulated on the ONE order; the snapshot pairs to it.
    chain = funnel["chains"][0]
    assert chain["orders"][0]["filled_evidence"] is True
    assert chain["orders"][0]["partial_evidence"] is True
    assert chain["phase"] == "complete"
    assert chain["sizing"]["event_ts"] == t(20, 16, 2)

    markup = render(bundle, {"data": today_payload(), "funnel": funnel}, tmp_path)

    region = chain_sizing_region(markup, "CPRI")
    # Completion wording — red against the old chain-level branch, which
    # printed 历史快照（链已终结：整单完成） instead of this order-level form.
    assert "历史快照（整单完成）" in region
    assert "链已终结" not in markup
    # No survival/remainder claim survives the confirmed completion.
    assert "剩余订单未终结" not in region
    assert "部分成交，余单" not in region


# ---- 18. R8 (iteration-5 review) ②: a terminal chain with NO pairable
#          snapshot stays "未能确定" — the chain terminal is never borrowed ----
#
# Chain complete / terminal_unfilled, but the snapshot lacks a usable pairing
# (later flush veto with a non-matching timestamp, or two same-instant
# submits): the old code borrowed the chain terminal; the page must say the
# order/terminal state cannot be determined instead.

@requires_toolchain
def test_render_terminal_chain_without_pairable_snapshot_stays_undetermined(
        bundle, tmp_path):
    conn = connect(tmp_path / "journal.db")
    record_cycle(conn, "2026-09-19T15:00:00+00:00", "paper", 10_000.0, 900.0, [])
    record_cycle(conn, "2026-09-20T15:00:00+00:00", "paper", 10_000.0, 850.0, [])

    def submit_order(symbol, run_id, intent_id, order_id, ts):
        record_intent_event(conn, symbol, "order_submitted", False, None,
                            timestamp=ts, run_id=run_id, mode="paper",
                            intent_id=intent_id, order_id=order_id,
                            payload={"client_order_id": f"cid-{order_id}",
                                     "submit_status": "accepted",
                                     "notional": 1500.0, "reason": "risk sized",
                                     "sizing": dict(SIZING_APPROVED)})

    # ① CMPL — the order FILLED (chain complete), but the LAST sizing payload
    #    came from a later flush veto whose timestamp matches no submit.
    r = rid("r8-cmpl")
    record_intent_event(conn, "CMPL", "decision_buy", False, None,
                        timestamp=t(20, 15, 10), run_id=r, mode="paper")
    record_intent_event(conn, "CMPL", "intent_created", False, None,
                        timestamp=t(20, 15, 11), run_id=r, mode="paper",
                        decision_key=dk(r, "CMPL"), intent_id="v-cmpl")
    submit_order("CMPL", r, "v-cmpl", "o-cmp", t(20, 15, 12))
    record_intent_event(conn, "CMPL", "order_filled", False, None,
                        timestamp=t(20, 15, 13), run_id=r, mode="paper",
                        intent_id="v-cmpl", order_id="o-cmp",
                        payload={"order_status": "filled",
                                 "observed_at": t(20, 15, 13),
                                 "filled_qty": 3, "filled_avg_price": 100.0})
    record_intent_event(conn, "CMPL", "sizing", True, "only $65.62 available",
                        timestamp=t(20, 15, 14), run_id=r, mode="paper",
                        intent_id="v-cmpl", payload=sizing_veto_event_payload())

    # ② TUNF — the order canceled WITHOUT a fill (chain terminal_unfilled),
    #    snapshot again from a later flush veto (zero match).
    r = rid("r8-tunf")
    record_intent_event(conn, "TUNF", "decision_buy", False, None,
                        timestamp=t(20, 15, 20), run_id=r, mode="paper")
    record_intent_event(conn, "TUNF", "intent_created", False, None,
                        timestamp=t(20, 15, 21), run_id=r, mode="paper",
                        decision_key=dk(r, "TUNF"), intent_id="v-tunf")
    submit_order("TUNF", r, "v-tunf", "o-tun", t(20, 15, 22))
    record_intent_event(conn, "TUNF", "order_observed", False, None,
                        timestamp=t(20, 15, 23), run_id=r, mode="paper",
                        intent_id="v-tunf", order_id="o-tun",
                        payload={"order_status": "canceled",
                                 "observed_at": t(20, 15, 23)})
    record_intent_event(conn, "TUNF", "sizing", True, "only $65.62 available",
                        timestamp=t(20, 15, 24), run_id=r, mode="paper",
                        intent_id="v-tunf", payload=sizing_veto_event_payload())

    # ③ AMBC — chain complete (one of two same-instant submits filled), the
    #    snapshot's instant matches BOTH submits: ambiguous pairing.
    r = rid("r8-ambc")
    record_intent_event(conn, "AMBC", "decision_buy", False, None,
                        timestamp=t(20, 15, 30), run_id=r, mode="paper")
    record_intent_event(conn, "AMBC", "intent_created", False, None,
                        timestamp=t(20, 15, 31), run_id=r, mode="paper",
                        decision_key=dk(r, "AMBC"), intent_id="v-ambc")
    for oid in ("o-abc-a", "o-abc-b"):
        submit_order("AMBC", r, "v-ambc", oid, t(20, 15, 32))
    record_intent_event(conn, "AMBC", "order_filled", False, None,
                        timestamp=t(20, 15, 33), run_id=r, mode="paper",
                        intent_id="v-ambc", order_id="o-abc-a",
                        payload={"order_status": "filled",
                                 "observed_at": t(20, 15, 33),
                                 "filled_qty": 3, "filled_avg_price": 100.0})
    conn.close()

    ro = connect_ro(tmp_path / "journal.db")
    try:
        funnel = funnel_summary(ro, now=NOW, fills=[])
    finally:
        ro.close()

    chains = {c["symbol"]: c for c in funnel["chains"]}
    assert chains["CMPL"]["phase"] == "complete"
    assert chains["CMPL"]["sizing"]["event_ts"] == t(20, 15, 14)  # zero match
    assert chains["TUNF"]["phase"] == "terminal_unfilled"
    assert chains["TUNF"]["sizing"]["event_ts"] == t(20, 15, 24)  # zero match
    assert chains["AMBC"]["phase"] == "complete"
    assert ([o["submitted_at"] for o in chains["AMBC"]["orders"]]
            == [t(20, 15, 32)] * 2)
    assert chains["AMBC"]["sizing"]["event_ts"] == t(20, 15, 32)  # both match

    markup = render(bundle, {"data": today_payload(), "funnel": funnel}, tmp_path)
    # All three unpairable snapshots stay undetermined — red against the old
    # code, which borrowed 历史快照（链已终结：…） on every one of them.
    assert markup.count("对应订单/终态未能确定") == 3
    assert "历史快照（链已终结：整单完成）" not in markup
    assert "历史快照（链已终结：终结未成交）" not in markup
    # The orders' OWN facts stay on their own lines — completion and
    # cancellation are not erased, just not lent to the sizing snapshot.
    assert "订单 o-cmp" in markup and "状态 整单完成" in markup
    assert "订单已终结（canceled）且未成交" in markup


# ---- 19. R8 (iteration-7 review): the submit attempt's RESULT is decoupled
#          from whether it ever got an order_id -----------------------------------
#
# REVIEW-ITERATION-6 R8 — the last open item. views.py keys each identity-less
# order_submitted as its OWN order entry (submitted_at / submit_status /
# submit_phase included), so an attempt without an order_id still carries a
# verifiable submit result. The page must pair the snapshot to that attempt
# by its submit event (unique submitted_at match, null entries included) and
# never fall back to the chain-level phase:
#   ① RJNK: attempt A earlier POSITIVELY rejected (order_id None), attempt B
#     later result UNKNOWN (order_id None) carrying the LAST snapshot — the
#     chain phase is submit_rejected (no attempt ever got an order), but B's
#     sizing region must NOT wear A's rejection. A's fact stays on A's line.
#   ② VETO/AMBR: A rejected, then a later flush SIZING veto (event kind, not
#     a submit) / two same-instant identity-less submits — the snapshot pairs
#     to nothing; the old rejection must not be applied to it either.

def _submit_reject_chain(conn, symbol: str, run: str, intent: str,
                         attempt_ts: str, submit_status: str,
                         sizing: dict) -> None:
    """run.py's funnel.order_submitted shape for an identity-less attempt:
    the broker returned None (submit_status 'unknown') or refused without an
    order (submit_status 'rejected') — either way NO order_id, but the submit
    event itself and its sizing payload are real, verifiable facts."""
    record_intent_event(conn, symbol, "order_submitted", False, None,
                        timestamp=attempt_ts, run_id=run, mode="paper",
                        intent_id=intent, order_id=None,
                        payload={"submit_status": submit_status,
                                 "notional": 1500.0,
                                 "reason": ("insufficient buying power"
                                            if submit_status == "rejected"
                                            else "submit raised"),
                                 "sizing": dict(sizing)})


@requires_toolchain
def test_render_rejected_attempt_not_borrowed_by_unknown_snapshot(
        bundle, tmp_path):
    conn = connect(tmp_path / "journal.db")
    record_cycle(conn, "2026-09-19T15:00:00+00:00", "paper", 10_000.0, 900.0, [])
    record_cycle(conn, "2026-09-20T15:00:00+00:00", "paper", 10_000.0, 850.0, [])
    r = rid("r8-rjnk")
    record_intent_event(conn, "RJNK", "decision_buy", False, None,
                        timestamp=t(20, 17, 0), run_id=r, mode="paper")
    record_intent_event(conn, "RJNK", "intent_created", False, None,
                        timestamp=t(20, 17, 1), run_id=r, mode="paper",
                        decision_key=dk(r, "RJNK"), intent_id="v-rjnk")
    _submit_reject_chain(conn, "RJNK", r, "v-rjnk", t(20, 17, 2),
                         "rejected", SIZING_APPROVED)
    # Attempt B is LATER and its submit event carries the LAST sizing payload.
    _submit_reject_chain(conn, "RJNK", r, "v-rjnk", t(20, 17, 4),
                         "unknown", SIZING_APPROVED)
    conn.close()

    ro = connect_ro(tmp_path / "journal.db")
    try:
        funnel = funnel_summary(ro, now=NOW, fills=[])
    finally:
        ro.close()

    # The aggregates really are the counterexample: the chain phase says
    # submit_rejected only because NO attempt ever got an order — that is
    # attempt A's fact — while the snapshot belongs to B, whose result is
    # unknown. Both attempts kept their own entries with own submit results.
    chain = funnel["chains"][0]
    assert chain["phase"] == "submit_rejected"
    assert [o["order_id"] for o in chain["orders"]] == [None, None]
    assert [o["phase"] for o in chain["orders"]] == [
        "submit_rejected", "submit_unknown"]
    assert [o["submit_status"] for o in chain["orders"]] == [
        "rejected", "unknown"]
    assert chain["sizing"]["event_ts"] == t(20, 17, 4)
    assert chain["sizing"]["event_kind"] == "order_submitted"

    markup = render(bundle, {"data": today_payload(), "funnel": funnel}, tmp_path)

    region = chain_sizing_region(markup, "RJNK")
    # B's sizing region must NOT wear A's rejection — no 被拒绝, no 链已终结,
    # no 历史快照. Red against the old unpairedChainRejected branch, which
    # printed 历史快照（链已终结：提交被拒绝） right here.
    assert "被拒绝" not in region
    assert "链已终结" not in region
    assert "历史快照" not in region
    # It stays the plain own-snapshot line: B's submit result is unknown.
    assert "sizing 快照 · 事件 order_submitted" in region
    # A's rejection fact is NOT erased — it lives on A's own order detail.
    assert "提交被拒绝——券商未受理" in markup


@requires_toolchain
def test_render_rejected_chain_unpaired_snapshot_stays_unknown(bundle, tmp_path):
    conn = connect(tmp_path / "journal.db")
    record_cycle(conn, "2026-09-19T15:00:00+00:00", "paper", 10_000.0, 900.0, [])
    record_cycle(conn, "2026-09-20T15:00:00+00:00", "paper", 10_000.0, 850.0, [])

    # ① VETO — attempt A rejected (no order), then a LATER flush SIZING veto:
    #    the last snapshot is a veto event whose timestamp matches no submit.
    r = rid("r8-veto")
    record_intent_event(conn, "VETO", "decision_buy", False, None,
                        timestamp=t(20, 18, 0), run_id=r, mode="paper")
    record_intent_event(conn, "VETO", "intent_created", False, None,
                        timestamp=t(20, 18, 1), run_id=r, mode="paper",
                        decision_key=dk(r, "VETO"), intent_id="v-veto")
    _submit_reject_chain(conn, "VETO", r, "v-veto", t(20, 18, 2),
                         "rejected", SIZING_APPROVED)
    record_intent_event(conn, "VETO", "sizing", True, "only $65.62 available",
                        timestamp=t(20, 18, 4), run_id=r, mode="paper",
                        intent_id="v-veto",
                        payload=sizing_veto_event_payload())

    # ② AMBR — TWO identity-less submits at the SAME instant (A rejected,
    #    B unknown, B's payload last): the snapshot's instant matches BOTH —
    #    ambiguous pairing, so neither attempt's result may be used.
    r = rid("r8-ambr")
    record_intent_event(conn, "AMBR", "decision_buy", False, None,
                        timestamp=t(20, 18, 10), run_id=r, mode="paper")
    record_intent_event(conn, "AMBR", "intent_created", False, None,
                        timestamp=t(20, 18, 11), run_id=r, mode="paper",
                        decision_key=dk(r, "AMBR"), intent_id="v-ambr")
    _submit_reject_chain(conn, "AMBR", r, "v-ambr", t(20, 18, 12),
                         "rejected", SIZING_APPROVED)
    _submit_reject_chain(conn, "AMBR", r, "v-ambr", t(20, 18, 12),
                         "unknown", SIZING_APPROVED)
    conn.close()

    ro = connect_ro(tmp_path / "journal.db")
    try:
        funnel = funnel_summary(ro, now=NOW, fills=[])
    finally:
        ro.close()

    chains = {c["symbol"]: c for c in funnel["chains"]}
    veto, ambr = chains["VETO"], chains["AMBR"]
    # Both chains really are submit_rejected at the chain level.
    assert veto["phase"] == "submit_rejected" and ambr["phase"] == "submit_rejected"
    assert veto["sizing"]["event_ts"] == t(20, 18, 4)     # zero match
    assert veto["sizing"]["event_kind"] == "sizing"       # a veto, NOT a submit
    assert [o["submitted_at"] for o in ambr["orders"]] == [t(20, 18, 12)] * 2
    assert ambr["sizing"]["event_ts"] == t(20, 18, 12)    # both match

    markup = render(bundle, {"data": today_payload(), "funnel": funnel}, tmp_path)

    for symbol in ("VETO", "AMBR"):
        region = chain_sizing_region(markup, symbol)
        # The old chain-level rejection is NOT applied to either snapshot —
        # red against the old unpairedChainRejected branch.
        assert "被拒绝" not in region
        assert "链已终结" not in region
        assert "历史快照" not in region
        # Unpaired on a submit_rejected chain stays in the UNDETERMINED
        # family — the same wording as an unpaired snapshot on any other
        # order-bearing chain, never a chain-level conclusion.
        assert "对应订单/终态未能确定" in region
    # A's rejection stays visible on the VETO chain's own order detail.
    assert "提交被拒绝——券商未受理" in markup


def test_same_timestamp_sizing_event_not_matched_to_rejected_submit(bundle, tmp_path):
    """ITERATION 7 final review: a NON-submit snapshot (event_kind="sizing")
    must never borrow a rejected submit's verdict even when the timestamps
    collide — the snapshot pairing gate must require event_kind ==
    "order_submitted" before any time matching."""
    conn = connect(tmp_path / "journal.db")
    record_cycle(conn, "2026-09-19T15:00:00+00:00", "paper", 10_000.0, 900.0, [])
    record_cycle(conn, "2026-09-20T15:00:00+00:00", "paper", 10_000.0, 850.0, [])

    r = rid("r8-same")
    record_intent_event(conn, "SNAP2", "decision_buy", False, None,
                        timestamp=t(20, 18, 0), run_id=r, mode="paper")
    record_intent_event(conn, "SNAP2", "intent_created", False, None,
                        timestamp=t(20, 18, 1), run_id=r, mode="paper",
                        decision_key=dk(r, "SNAP2"), intent_id="v-same")
    # attempt A: rejected submit with NO order_id at time T
    _submit_reject_chain(conn, "SNAP2", r, "v-same", t(20, 18, 4),
                         "rejected", SIZING_APPROVED)
    # then a budget-veto SIZING event written at the SAME time T — the last
    # snapshot belongs to it, and it is NOT a submit at all
    record_intent_event(conn, "SNAP2", "sizing", True, "only $65.62 available",
                        timestamp=t(20, 18, 4), run_id=r, mode="paper",
                        intent_id="v-same", payload=sizing_veto_event_payload())
    conn.close()

    ro = connect_ro(tmp_path / "journal.db")
    try:
        funnel = funnel_summary(ro, now=NOW, fills=[])
    finally:
        ro.close()
    chain = {c["symbol"]: c for c in funnel["chains"]}["SNAP2"]
    assert chain["phase"] == "submit_rejected"           # chain level: A won
    assert chain["sizing"]["event_kind"] == "sizing"     # snapshot is the veto
    assert chain["sizing"]["event_ts"] == t(20, 18, 4)   # same instant as A

    markup = render(bundle, {"data": today_payload(), "funnel": funnel}, tmp_path)
    region = chain_sizing_region(markup, "SNAP2")
    assert "该次提交被拒绝" not in region, (
        "a sizing-only snapshot borrowed the rejected submit's verdict "
        "(same-timestamp cross-kind match)")
    assert "拒绝" in markup  # the rejection stays on its own attempt's detail
