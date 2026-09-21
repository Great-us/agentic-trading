"""Read-only SQL views over a book's journal.db.

Everything here opens the database with mode=ro — the dashboard can never
corrupt a live journal, and WAL lets it read while the trading system writes.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from datetime import time as dt_time
from pathlib import Path
from zoneinfo import ZoneInfo

from ..daily_report import (
    INTENT_EVENT_KINDS,
    REFERENCE_CATEGORIES,
    VETO_CATEGORIES,
    _count_category,
    _count_intent_events,
)
from ..heartbeat import (
    BOOK_SCHEDULES,
    DEEP_MAX_AGE_HOURS,
    FAST_MAX_AGE_MINUTES,
    LATE_MAX_MINUTES,
    latest_stop_check,
)
from ..journal.evaluate import DEFAULT_MODE as OUTCOMES_DEFAULT_MODE
from ..journal.evaluate import MIN_REPORT_N

ET = ZoneInfo("America/New_York")
# Keep in lockstep with run.TRADE_INTENT_TTL — dashboard must not import run.py.
_INTENT_TTL = timedelta(hours=72)


def connect_ro(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=10.0)
    conn.row_factory = sqlite3.Row
    return conn


def latest_cycle(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute(
        "SELECT id, timestamp, mode, equity, cash, regime_score, regime_label "
        "FROM cycles ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return dict(row) if row else None


def equity_series(conn: sqlite3.Connection, days: int = 180) -> list[dict]:
    """One point per day (last cycle of the day wins), oldest first.

    Paper cycles only: DryRunBroker reports a flat $100,000 account and dry runs
    write to the live journal, so an unfiltered series puts a 10x spike in the
    middle of the curve on any day whose last cycle was a rehearsal.
    """
    rows = conn.execute(
        "SELECT timestamp, equity, cash, regime_label FROM cycles "
        "WHERE mode = 'paper' ORDER BY timestamp"
    ).fetchall()
    by_day: dict[str, dict] = {}
    for row in rows:
        day = str(row["timestamp"])[:10]
        by_day[day] = {
            "date": day,
            "equity": row["equity"],
            "cash": row["cash"],
            "regime_label": row["regime_label"],
        }
    series = list(by_day.values())[-days:]
    peak = float("-inf")
    for point in series:
        equity = point["equity"]
        if equity is None:
            continue
        peak = max(peak, float(equity))
        drawdown = (float(equity) - peak) / peak if peak > 0 else 0.0
        point["drawdown"] = round(drawdown, 4)
    return series


def decisions(conn: sqlite3.Connection, *, cycle_id: int | None = None,
              symbol: str | None = None, limit: int = 200) -> list[dict]:
    query = (
        "SELECT d.id, d.cycle_id, c.timestamp AS cycle_time, c.mode, d.symbol,"
        " d.quant_score, d.combined_score, d.action, d.reasoning, d.sizing_reason,"
        " d.llm_stance, d.llm_confidence, d.llm_rationale, d.llm_risk_flags,"
        " d.llm_evidence_quality, d.grok_stance, d.grok_confidence, d.grok_summary,"
        " d.order_status, d.order_qty, d.fill_price, d.notional, d.stop_price"
        " FROM decisions d JOIN cycles c ON c.id = d.cycle_id WHERE 1=1"
    )
    params: list[object] = []
    if cycle_id is not None:
        query += " AND d.cycle_id = ?"
        params.append(cycle_id)
    if symbol:
        query += " AND UPPER(d.symbol) = ?"
        params.append(symbol.upper())
    query += " ORDER BY c.id DESC, d.id DESC LIMIT ?"
    params.append(limit)
    return [dict(row) for row in conn.execute(query, params)]


def cycles_index(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    rows = conn.execute(
        "SELECT id, timestamp, mode, equity, regime_label,"
        " (SELECT COUNT(*) FROM decisions d WHERE d.cycle_id = cycles.id) AS n_decisions"
        " FROM cycles ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(row) for row in rows]


def position_snapshot(conn: sqlite3.Connection) -> list[dict]:
    """Journal-side holdings snapshot (qty + last market value per symbol),
    plus the trailing-stop high-water mark. The broker reader supplies the
    live price/avg-cost view; this one works even without credentials."""
    rows = conn.execute(
        "SELECT s.symbol, s.last_qty, s.last_value, s.updated_at,"
        " p.high_water_mark FROM position_state s"
        " LEFT JOIN position_peaks p ON p.symbol = s.symbol"
        " ORDER BY s.last_value DESC"
    ).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        if item["last_qty"]:
            item["implied_last_price"] = round(item["last_value"] / item["last_qty"], 4)
        out.append(item)
    return out


def outcomes_summary(
    conn: sqlite3.Connection,
    *,
    mode: str | None = OUTCOMES_DEFAULT_MODE,
    min_n: int = MIN_REPORT_N,
) -> list[dict]:
    """Forward-return averages per action bucket, evaluate-style. Descriptive
    only — small n is shown as-is, never as a finding.

    n_1d / n_5d / n_20d are the per-horizon evaluated counts. Averaging two
    buckets that evaluated different horizons is a misread (hold +1d is a
    much smaller sample than avoid +1d). `n_1d_small`/`n_5d_small`/
    `n_20d_small` flag each horizon independently against `min_n`: a bucket
    with 40 decisions and 40 matured 1d returns but only 1 matured 20d return
    is small on the 20d horizon regardless of its decision-count total.

    `mode='paper'` by default — dry_run and backtest cycles have no real
    fills and must not count as paper-forward evaluation evidence. Pass
    `mode=None` for the full journal across every mode (audit/debugging only).
    """
    query = (
        "SELECT d.action, COUNT(*) AS n,"
        " SUM(CASE WHEN o.ret_1d IS NOT NULL THEN 1 ELSE 0 END) AS n_1d,"
        " SUM(CASE WHEN o.ret_5d IS NOT NULL THEN 1 ELSE 0 END) AS n_5d,"
        " SUM(CASE WHEN o.ret_20d IS NOT NULL THEN 1 ELSE 0 END) AS n_20d,"
        " AVG(o.ret_1d)*100 AS ret_1d, AVG(o.ret_5d)*100 AS ret_5d,"
        " AVG(o.ret_20d)*100 AS ret_20d,"
        " AVG(o.mfe_20d)*100 AS mfe_20d, AVG(o.mae_20d)*100 AS mae_20d"
        " FROM signal_outcomes o"
        " JOIN decisions d ON d.id = o.decision_id"
        " JOIN cycles c ON c.id = d.cycle_id"
    )
    params: list = []
    if mode is not None:
        query += " WHERE c.mode = ?"
        params.append(mode)
    query += " GROUP BY d.action ORDER BY n DESC"
    rows = conn.execute(query, params).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        for key in ("ret_1d", "ret_5d", "ret_20d", "mfe_20d", "mae_20d"):
            value = item[key]
            item[key] = round(value, 2) if value is not None else None
        item["n_1d_small"] = item["n_1d"] < min_n
        item["n_5d_small"] = item["n_5d"] < min_n
        item["n_20d_small"] = item["n_20d"] < min_n
        out.append(item)
    return out


def logic_payload(book_config_dir: Path) -> dict:
    """The 'why does it trade this way' page data: effective risk.yaml values,
    the watchlist breakdown, and a short static description of the pipeline.
    Reads YAML directly — deliberately does not run load_settings(), which
    would trigger Obsidian vault scans against another book's config."""
    import yaml

    risk_raw: dict = {}
    risk_path = book_config_dir / "risk.yaml"
    if risk_path.exists():
        with open(risk_path, "r", encoding="utf-8") as f:
            risk_raw = yaml.safe_load(f) or {}
    watchlist: list[str] = []
    context_symbols: list[str] = []
    watch_path = book_config_dir / "watchlist.yaml"
    if watch_path.exists():
        with open(watch_path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        watchlist = [str(s).upper() for s in (raw.get("symbols") or []) if s]
        context_symbols = [str(s).upper() for s in (raw.get("context_symbols") or []) if s]

    pipeline_steps = [
        ("0 · 市场体制", "五组跨资产比率(RSP/SPY、HYG/LQD、IWM/SPY、SPY/TLT、XLY/XLP)+VIX 惩罚 → 连续 regime 分数；risk-off 时仓位减半、入场门槛提高"),
        ("1 · 量化信号", "趋势(价 vs SMA50)、SMA20/50 交叉、20 日动量、MACD 柱、RSI14 合成 [-1,1] 分数；过度延伸(extended)只 WAIT 不追高"),
        ("2 · 风控优先", "已持仓先查 ATR 止损/trailing/take-profit，胜过一切其他信号"),
        ("3 · LLM 分析", "新闻+基本面+量化分交给 LLM 分析师，返回 stance/confidence/rationale/risk_flags"),
        ("4 · 决策合成", "BUY 要求量化分单独过 buy_threshold；LLM 只能否决或降级为 HOLD，不能发起交易"),
        ("5 · 风险 sizing", "等风险仓位 × 一组硬上限：单仓%、总敞口、持仓数、每轮订单数、book 级止损风险、行业 cap、相关性减半"),
        ("6 · 执行窗口", "BUY 存为 TradeIntent，只在 10:00–15:30 ET 窗口内经 gap/chase 校验后执行；72h 过期"),
        ("7 · 保护性止损", "每个仓位在 Alpaca 挂 broker 端 stop 单并逐周期 ratchet——程序不在线也有保护"),
    ]
    return {
        "risk": risk_raw,
        "watchlist": watchlist,
        "context_symbols": context_symbols,
        "pipeline": pipeline_steps,
    }


def _aware(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return _aware(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        return None


def et_date_of(ts: datetime) -> str:
    return ts.astimezone(ET).date().isoformat()


def missed_sessions(stamp: datetime, now: datetime) -> int:
    """Weekdays strictly after the stamp's calendar date, up to today.

    Distinguishes "quiet because the market is shut" from "the scheduler died".
    Copied from research/dashboard-phase0-demo.py — keep the rule identical.
    """
    stamp = _aware(stamp).astimezone(ET)
    now = _aware(now).astimezone(ET)
    day = stamp.date() + timedelta(days=1)
    missed = 0
    while day <= now.date():
        if day.weekday() < 5:
            missed += 1
        day += timedelta(days=1)
    return missed


def _effective_missed_sessions(stamp: datetime, now: datetime, *, today_not_started: bool) -> int:
    """`missed_sessions()`, but today itself is never counted before today's
    own first scheduled slot has arrived — a quiet pre-market morning is not
    "a whole trading day went by with no heartbeat" (P1-B-4 review, R7).
    `missed_sessions()` itself stays untouched (it's a frozen copy of the
    Phase-0 rule); this only adjusts how `book_health` reads its result."""
    missed = missed_sessions(stamp, now)
    if today_not_started and missed > 0:
        now_et = _aware(now).astimezone(ET)
        stamp_et = _aware(stamp).astimezone(ET)
        if now_et.date() > stamp_et.date():
            missed -= 1
    return missed


def _in_entry_window(et_now: datetime, start: str = "10:00", end: str = "15:30") -> bool:
    try:
        sh, sm = (int(p) for p in start.split(":"))
        eh, em = (int(p) for p in end.split(":"))
    except ValueError:
        sh, sm, eh, em = 10, 0, 15, 30
    minutes = et_now.hour * 60 + et_now.minute
    return (sh * 60 + sm) <= minutes <= (eh * 60 + em)


# P1-B-4: the weekday fast-scan window, derived from P1's own Task Scheduler
# grid (heartbeat.BOOK_SCHEDULES) rather than a hardcoded literal — this is
# the "should there be a heartbeat right now" reference for every book this
# dashboard package serves (P1/P3/P4). It works out to 09:35-16:05 ET.
def _fast_window() -> tuple[dt_time, dt_time]:
    (h, m), _interval, span = BOOK_SCHEDULES["p1"]["fast"]
    start = dt_time(h, m)
    end_total = h * 60 + m + span
    end = dt_time((end_total // 60) % 24, end_total % 60)
    return start, end


_FAST_WINDOW_START, _FAST_WINDOW_END = _fast_window()

# Priority order for combining per-mode states into one overall status —
# earlier entries win. "stale"/"stale_intraday" are real alerts; the rest are
# expected/benign states that just say *why* a stamp looks old right now.
_STATUS_PRIORITY = (
    "stale", "stale_intraday", "stops_unknown", "missing", "closed_or_holiday", "after_hours",
    "weekend", "ok",
)


def _mode_state(mode: str, *, missed: int, raw_stale: bool, deep_raw_stale: bool,
                is_weekend: bool, in_fast_window: bool, today_has_run_evidence: bool) -> str:
    """One mode's health label for `now`'s calendar/clock context.

    `today_has_run_evidence` (some mode already stamped earlier today) takes
    priority for fast specifically: if we're squarely inside the trading
    window right now and something has demonstrably already run today, a
    stale fast stamp is a live scanner stall — full stop, not "maybe a
    holiday" — no matter what `missed` says about the gap since fast's own
    last tick (which may be dated yesterday even though today is plainly a
    live session; R7, PROGRESS.md §9).

    A missed weekday (`missed > 0`) can only mean the schedule actually
    skipped a trading day — real trouble if `deep` is also badly overdue
    (`stale`), otherwise ambiguous without a market-holiday calendar
    (`closed_or_holiday`: could be a holiday, could be a quiet failure).
    `deep_raw_stale` (not this mode's own raw_stale) is what decides that
    escalation for BOTH modes: fast's own 35-minute budget is trivially blown
    by any full missed day, so using fast's own staleness there would call
    every missed day "stale" and defeat the point of the softer label — deep's
    26-hour budget is the one actually calibrated to tell "a day slipped by"
    from "the whole book has gone dark".

    Below that, plain calendar/clock context decides: weekend, the fast
    window during a weekday (`stale_intraday` if fast itself is overdue right
    now), or after-hours on a weekday.
    """
    if mode == "fast" and in_fast_window and today_has_run_evidence and raw_stale:
        return "stale_intraday"
    if missed > 0:
        return "stale" if deep_raw_stale else "closed_or_holiday"
    if is_weekend:
        return "weekend"
    if mode == "fast":
        if in_fast_window:
            return "stale_intraday" if raw_stale else "ok"
        return "after_hours"
    # deep, weekday, no missed session
    if in_fast_window:
        return "ok"
    return "stale" if raw_stale else "after_hours"


def book_health(heartbeat_path: Path, *, now: datetime | None = None,
                deep_only: bool = False) -> dict:
    now = _aware(now or datetime.now(timezone.utc))
    now_et = now.astimezone(ET)
    is_weekend = now_et.weekday() >= 5
    in_fast_window = (not is_weekend) and (_FAST_WINDOW_START <= now_et.time() < _FAST_WINDOW_END)
    empty = {"timestamp": None, "cycle_id": None, "age_seconds": None,
             "missed_sessions": None, "stops_covered": None, "positions": None,
             "late_minutes": None, "missed_slots": None, "late": False,
             "stops_unknown": False, "stops_unknown_reason": None,
             "note": None, "state": "missing"}
    if not heartbeat_path.exists():
        return {
            "status": "missing",
            "message": "无心跳文件",
            "deep": dict(empty),
            "fast": dict(empty),
            "stop_check": None,
            "limits": {"deep_hours": DEEP_MAX_AGE_HOURS, "fast_minutes": FAST_MAX_AGE_MINUTES},
            "deep_only": deep_only,
        }
    try:
        payload = json.loads(heartbeat_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "status": "corrupt",
            "message": f"心跳无法解析：{exc}",
            "deep": dict(empty),
            "fast": dict(empty),
            "stop_check": None,
            "limits": {"deep_hours": DEEP_MAX_AGE_HOURS, "fast_minutes": FAST_MAX_AGE_MINUTES},
            "deep_only": deep_only,
        }
    if not isinstance(payload, dict):
        payload = {}

    modes: dict[str, dict] = {}
    seen_states: list[str] = []
    checks = [("deep", timedelta(hours=DEEP_MAX_AGE_HOURS))]
    if not deep_only:
        checks.append(("fast", timedelta(minutes=FAST_MAX_AGE_MINUTES)))
    deep_stamp = parse_iso((payload.get("deep") or {}).get("timestamp"))
    deep_raw_stale = (
        deep_stamp is not None and (now - deep_stamp) > timedelta(hours=DEEP_MAX_AGE_HOURS)
    )
    # Evidence this calendar day (ET) is a live session, independent of
    # which mode is being evaluated — a fresh deep OR fast stamp from today
    # rules out "maybe today just hasn't started" for the other mode's stale
    # reading (R7).
    today_has_run_evidence = any(
        (s := parse_iso((payload.get(m) or {}).get("timestamp"))) is not None
        and s.astimezone(ET).date() == now_et.date()
        for m in ("deep", "fast")
    )
    today_not_started = (not is_weekend) and (now_et.time() < _FAST_WINDOW_START)
    for mode, max_age in checks:
        entry = payload.get(mode) or {}
        stamp = parse_iso(entry.get("timestamp"))
        if stamp is None:
            modes[mode] = dict(empty)
            seen_states.append("missing")
            continue
        missed = _effective_missed_sessions(stamp, now, today_not_started=today_not_started)
        raw_stale = (now - stamp) > max_age
        state = _mode_state(mode, missed=missed, raw_stale=raw_stale, deep_raw_stale=deep_raw_stale,
                            is_weekend=is_weekend, in_fast_window=in_fast_window,
                            today_has_run_evidence=today_has_run_evidence)
        seen_states.append(state)
        covered, total = entry.get("stops_covered"), entry.get("positions")
        late_minutes = entry.get("late_minutes")
        # Per-mode coverage fields describe what THAT mode's own last check
        # said (None when that cycle ran no check). Whether the book is in a
        # stops_unknown state is decided once, below, from the most recent
        # actual check across both modes (`latest_stop_check`) — the same
        # reading the watchdog uses — so a stale per-mode flag can neither
        # keep the dashboard red after a later successful re-check, nor hide
        # a failed check behind a later skipped scan (review round 2, R2).
        stops_unknown = bool(entry.get("stops_unknown"))
        modes[mode] = {
            "timestamp": entry.get("timestamp"),
            "cycle_id": entry.get("cycle_id"),
            "age_seconds": round((now - stamp).total_seconds()),
            "missed_sessions": missed,
            "stops_covered": covered,
            "positions": total,
            "naked": (
                total is not None and covered is not None and int(total) > int(covered)
            ),
            "stops_unknown": stops_unknown,
            "stops_unknown_reason": entry.get("stops_unknown_reason") if stops_unknown else None,
            # From heartbeat's P0-B-3 timeliness block (started_at vs the
            # Task Scheduler slot) — optional, so older stamps without it
            # just carry None/False here rather than breaking the payload.
            "late_minutes": late_minutes,
            "missed_slots": entry.get("missed_slots"),
            "late": late_minutes is not None and float(late_minutes) > LATE_MAX_MINUTES,
            "note": "无交易所日历，可能是假日也可能是漏跑" if state == "closed_or_holiday" else None,
            "state": state,
        }
    # One reading of "is stop coverage verified right now", shared with
    # `heartbeat --check`: the most recent cycle that actually reconciled,
    # whichever mode it was. Not knowing whether stops are covered is itself
    # an alert (moves `overall`), not a footnote next to a calm status.
    check = latest_stop_check(payload)
    stop_check = None
    if check is not None:
        stop_check = {
            "checked_at": check["checked_at"],
            "mode": check["mode"],
            "cycle_id": check["cycle_id"],
            "stops_covered": check["stops_covered"],
            "positions": check["positions"],
            "unknown": check["unknown"],
            "reason": check["reason"],
            "naked": check["naked"] > 0,
        }
        if check["unknown"]:
            seen_states.append("stops_unknown")
    overall = next((s for s in _STATUS_PRIORITY if s in seen_states), "ok")
    note = None
    if stop_check is not None and stop_check["unknown"]:
        note = f"止损覆盖未核验：{stop_check['reason'] or '券商挂单读取失败'}"
    if note is None:
        note = next((modes[m]["note"] for m in modes if modes[m].get("note")), None)
    stale_msg = "停摆：漏掉了交易日的深周期" if deep_only else "停摆：漏掉了交易日的深/快周期"
    if deep_only:
        modes.setdefault("fast", dict(empty))
    return {
        "status": overall,
        "stop_check": stop_check,
        "message": {
            "ok": "周期在跑",
            "stale_intraday": "盘中扫描停摆（快扫可能挂了）",
            "stops_unknown": "止损覆盖未核验（券商挂单读取失败）",
            "after_hours": "盘后 / 非交易时段（正常）",
            "weekend": "休市中（周末）",
            "closed_or_holiday": "工作日无心跳（可能是假日，也可能漏跑——无交易所日历，无法区分）",
            "stale": stale_msg,
            "missing": "没有可用心跳",
            "corrupt": "心跳损坏",
        }.get(overall, overall),
        "note": note,
        "deep": modes.get("deep", dict(empty)),
        "fast": modes.get("fast", dict(empty)),
        "limits": {"deep_hours": DEEP_MAX_AGE_HOURS, "fast_minutes": FAST_MAX_AGE_MINUTES},
        "deep_only": deep_only,
    }


def list_intents(conn: sqlite3.Connection, *, now: datetime | None = None) -> list[dict]:
    now = _aware(now or datetime.now(timezone.utc))
    try:
        rows = conn.execute(
            "SELECT symbol, created_at, signal_price, atr14, quant_score, "
            "combined_score, reasoning, not_before FROM trade_intents ORDER BY created_at"
        ).fetchall()
    except sqlite3.Error:
        return []
    out = []
    for row in rows:
        created = parse_iso(row["created_at"])
        not_before = parse_iso(row["not_before"])
        ttl_hours = None
        status = "pending"
        if created is not None:
            remaining = (created + _INTENT_TTL) - now
            ttl_hours = round(remaining.total_seconds() / 3600, 1)
            if remaining.total_seconds() <= 0:
                status = "expired"
        if status != "expired":
            if not_before is not None and now < not_before:
                status = "waiting"
            elif _in_entry_window(now.astimezone(ET)):
                status = "window_open"
            else:
                status = "pending"
        out.append({
            "symbol": row["symbol"],
            "created_at": row["created_at"],
            "not_before": row["not_before"],
            "signal_price": row["signal_price"],
            "quant_score": row["quant_score"],
            "combined_score": row["combined_score"],
            "reasoning": row["reasoning"],
            "ttl_hours": ttl_hours,
            "status": status,
        })
    return out


def veto_counts(conn: sqlite3.Connection, *, days: int = 7,
                now: datetime | None = None) -> list[dict]:
    now = _aware(now or datetime.now(timezone.utc))
    cutoff = (now - timedelta(days=max(1, days))).isoformat()
    out = []
    for label, condition, source in [*VETO_CATEGORIES, *REFERENCE_CATEGORIES]:
        recent, total = _count_category(conn, condition, cutoff)
        out.append({"label": label, "recent": recent, "total": total, "source": source})
    for kind, label in INTENT_EVENT_KINDS:
        recent, total = _count_intent_events(conn, kind, cutoff)
        out.append({"label": label, "recent": recent, "total": total,
                    "source": "intent_events", "kind": kind})
    return out


def recent_intent_events(conn: sqlite3.Connection, *, limit: int = 20) -> list[dict]:
    try:
        rows = conn.execute(
            "SELECT timestamp, symbol, kind, deferred, detail "
            "FROM intent_events ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    except sqlite3.Error:
        return []
    return [dict(row) for row in rows]


# ---- A-4 (c2c_a7e2 PLAN §五): read-only execution-funnel aggregation -----------
#
# The Today funnel is aggregated from the SQLite intent_events ledger — never
# from the rotating JSONL tail (120-line replay cap) and never by re-running
# sizing. Identity layers per PLAN §一: BUY decision (run_id + symbol) →
# intent version (intent_id = trade_intents.version) → order (order_id). Ten
# flush_wait retries are ten attempts on ONE intent, not ten decisions; a
# replaced intent's old failure stays with the decision that owned the old
# version. accepted is not filled. Broker fills matched by order_id are
# positive evidence; a non-match proves nothing. Old schema, identity-less
# rows and unavailable data sources degrade explicitly — nothing is paired by
# symbol + near-time and nothing is invented. Strictly read-only.
#
# Review R4 (c2c_a7e2 ITERATION-1): chains are built from the FULL event
# history up to the cutoff `now`, any ET day — yesterday's creation, order_id
# and sizing snapshot join today's observations via intent_id/order_id (the
# creation date never bounds order lookup). Today's cohort DENOMINATOR still
# counts only this session's decision_buy events; full history only enriches
# the chain detail. Today's fills whose order matches a historical order pull
# that chain onto the page even with no intent events today; fills whose
# order matches nothing stay listed as unlinked positive evidence.
#
# Review R4 (ITERATION-3): identity-less order observations (older journals,
# before the recovery pass carried intent_id) join a chain when their
# order_id matches EXACTLY ONE identified order_submitted event of the same
# book (run_id prefix) and the same mode. An order_id claimed by SEVERAL
# intents, or a submission that itself has no identity, is UNATTRIBUTABLE —
# the observation is listed in degraded.unattributed_order_events and never
# guessed onto a chain by symbol.
#
# Review R4 (ITERATION-4): the SAME ownership rule now gates the fill side.
# Broker fills join an intent chain only through the unique identified
# submission of their order_id (same book, same mode, full history); an
# order_id claimed by several intents, submitted without identity, or never
# submitted at all keeps its fill evidence order-level in unlinked_fills
# (with the reason) but gains no fill-attributed completion — one fill can
# never complete two chains. Another mode's same-order_id history is a
# different attribution key: it can neither steal the identity nor suppress
# this mode's lookback.
#
# Review R5: state rules are explicit on the output, so the frontend never
# re-derives meaning from a status string. Per order: ``phase`` ∈ complete |
# partial | terminal_unfilled | submit_rejected | submitted | submit_unknown
# | unknown, plus flags (filled_evidence / partial_evidence /
# last_observation_failed, submit_status / submit_phase). Completion =
# an order_filled observation, or a fill activity the broker itself marked
# ``order_status == "fill"`` (its completing-fill marker — the ledger stores
# notional, not order quantity, so a qty>=order-size comparison is NOT
# computable here and is never fabricated). A partial-fill activity is a
# partial, never a completion. Later unknown observations never erase earlier
# reliable completion evidence. Fill degree and terminal status are
# INDEPENDENT facts (R5 ITERATION-3): the terminal state is read from
# payload.order_status in ANY order event — a canceled-with-partial arrives
# as order_partial and keeps both the quantity and the canceled status, never
# becoming an entire-order completion. Submit facts are separated:
# acknowledged acceptance (order_id + submit_status), explicit rejection
# (submit_status "rejected" — only for a positively confirmed rejection; run.py
# records a None broker result as "unknown", ITERATION-3), and unknown
# outcome; a NULL-order_id order_submitted is never an accepted submission.
# Replaced intents say so: superseded_by, not "waiting".

_FUNNEL_INTENT_EVENT_COLUMNS = (
    "id", "timestamp", "symbol", "kind", "deferred", "detail",
    "event_id", "run_id", "mode", "decision_key", "intent_id",
    "attempt_id", "order_id", "payload",
)
# The identity columns added by A-1; a journal without them is pre-A-1.
_FUNNEL_REQUIRED_COLUMNS = (
    "run_id", "mode", "decision_key", "intent_id", "attempt_id", "order_id", "payload",
)
_FUNNEL_INTENT_KINDS = frozenset({"intent_created", "intent_replaced"})
_FUNNEL_ORDER_KINDS = frozenset({
    "order_submitted", "order_observed", "order_partial", "order_filled", "order_unknown",
})
# Stages that keep the intent alive for a later scan.
_FUNNEL_WAIT_KINDS = frozenset({"flush_wait", "chase_signal", "chase_open", "sizing"})
# Stages that discard the intent outright.
_FUNNEL_DISCARD_KINDS = frozenset({"ttl", "gap"})
# The pre-A-2 flush-outcome kinds (they carry identity columns since A-2).
_FUNNEL_LEGACY_FLUSH_KINDS = frozenset({"gap", "chase_signal", "chase_open", "sizing", "ttl"})
# Terminal non-fill broker statuses. Mirror of execution.broker.DEAD_ORDER_STATUSES —
# defined locally (not imported) so the read-only dashboard keeps zero dependency on
# the broker SDK module.
_DEAD_ORDER_STATUSES = frozenset({
    "rejected", "expired", "canceled", "cancelled", "replaced", "stopped",
    "suspended", "done_for_day",
})
# The broker's marker on the fill activity that COMPLETED its order
# (Alpaca activities: "fill" vs "partial_fill").
_FILL_COMPLETE_STATUS = "fill"
# R6 (ITERATION-3): kinds that count a run as an ATTEMPT — flush-stage WORK:
# waiting, veto, discard, submit. Submitting IS flush work; status
# observations are re-queries of an existing order and are NOT attempts
# (1 submit + 10 requeries is 1 attempt and 10 observation runs, not 11).
# intent_created/intent_replaced are creation, not attempts; they are counted
# separately (created_runs), and the re-query runs are listed separately too
# (observed_runs).
_FUNNEL_ATTEMPT_KINDS = frozenset({
    "flush_wait", "gap", "chase_signal", "chase_open", "sizing", "ttl",
    "order_submitted",
})
_FUNNEL_OBSERVATION_KINDS = frozenset({
    "order_observed", "order_partial", "order_filled", "order_unknown",
})
# R6 (ITERATION-3): every kind that NEEDS an intent identity. The
# identity-less degraded gate covers the pre-A-2 flush five AND the modern
# flush_wait / intent_cleared / order_submitted / order_* / clear_failed
# events produced for a version-NULL intent — those used to vanish silently.
_FUNNEL_IDENTITY_KINDS = (
    _FUNNEL_LEGACY_FLUSH_KINDS | _FUNNEL_ORDER_KINDS
    | {"flush_wait", "intent_cleared", "clear_failed"}
)


def _book_of(run_id: str | None) -> str | None:
    """The book segment of a run_id (``{book}:{cycle_start}:{hex}``). The
    journal is per-book, so this is the same-book guard on order→submission
    attribution (review R4: never attribute across books)."""
    if not run_id:
        return None
    return run_id.split(":", 1)[0]


def _funnel_zero_summary() -> dict:
    return {
        "decisions": 0, "with_intent": 0,
        "not_created": 0, "not_created_reasons": {},
        "evidence_missing": 0,
        "submitted": 0, "submitted_frac": "0/0",
        "submit_rejected": 0, "submit_unknown": 0, "superseded": 0,
        "filled_verified": 0, "partial": 0,
        "waiting": 0, "discarded": 0, "cleared": 0, "created_pending": 0,
        "carryover_intents": 0,
    }


def _funnel_empty_degraded() -> dict:
    return {
        "fills": {"degraded": True, "reason": None, "truncated": None},
        "unattributed_decision_events": 0,
        "unparseable_timestamps": 0,
        "unparseable_payloads": 0,
        "legacy_flush_events": 0,
        "identityless_by_mode": {},
        "duplicate_fill_activities": 0,
        "post_cutoff_fills": 0,
        "identityless_observed_today": [],
        "unattributed_order_events": [],
        "orphan_intents": [],
        "unknown_origin_intents": [],
        "notes": [],
    }


def _event_reason(event: dict) -> str | None:
    payload = event.get("payload") or {}
    for key in ("reason", "wait_reason", "skip_reason"):
        value = payload.get(key)
        if value:
            return str(value)
    return event.get("detail")


def _submit_phase(order_id: str | None, submit_status: str | None) -> str:
    """R5: the submit FACT, separated. "rejected" appears only when a
    rejection was POSITIVELY confirmed (run.py records a None broker result
    as "unknown" — no confirmed-rejection claim without evidence); an attempt
    with an order and a recorded status is an acknowledged acceptance;
    everything else stays honestly unknown."""
    if submit_status == "rejected":
        return "submit_rejected"
    if order_id and submit_status:
        return "submitted_accepted"
    return "submit_unknown"


def _order_phase(entry: dict) -> str:
    """R5 precedence: completion evidence > partial evidence > explicit
    rejection > terminal-unfilled > submitted > attempt-unknown."""
    if entry.get("filled_evidence"):
        return "complete"                      # 整单完成
    if entry.get("partial_evidence"):
        return "partial"                       # 存在成交，未整单完成
    if entry.get("submit_phase") == "submit_rejected":
        return "submit_rejected"               # 明确拒绝
    if entry.get("terminal_status"):
        return "terminal_unfilled"             # 订单终结但未完成
    if entry.get("submitted_at") and entry.get("order_id"):
        return "submitted"                     # 已提交（受理情况见 submit_phase）
    if entry.get("submitted_at"):
        return "submit_unknown"                # 仅提交尝试，结果不明
    return "unknown"


def _chain_phase(orders: list[dict]) -> str | None:
    """The chain's best order-level truth (R5: explicit, not re-derived).
    A chain that holds a real order reports submitted/reached-state even if
    an earlier attempt was rejected — that attempt stays visible on its own
    order; "submit_rejected" is reserved for chains with NO live order."""
    phases = [o.get("phase") for o in orders]
    for want in ("complete", "partial", "terminal_unfilled"):
        if want in phases:
            return want
    if any(o.get("submitted_at") and o.get("order_id") for o in orders):
        return "submitted"
    if "submit_rejected" in phases:
        return "submit_rejected"
    if "submit_unknown" in phases:
        return "submit_unknown"
    return None


def _funnel_orders(events: list[dict], fills_by_order: dict[str, list[dict]],
                   fills_available: bool,
                   allowed_oids: set[str] | None = None) -> list[dict]:
    """One entry per distinct order_id, in first-seen order. Multiple status
    observations of the same order and multiple partial fills merge into ONE
    order — they are not multiple submissions (PLAN §一). Evidence ACCUMULATES
    (R5): a later unknown observation never erases an earlier reliable
    completion; ``last_observation_failed`` records that the newest look
    failed without un-knowing what was confirmed before.

    R4 (ITERATION-4): ``allowed_oids`` is the chain's ownership grant — the
    order_ids this intent UNIQUELY owns (exactly one identified submission,
    same book and mode). Feed activity outside the grant is withheld from the
    chain: the order entry reports ``fills_matched=None`` plus
    ``fills_withheld=True`` and the activity stays order-level in
    unlinked_fills with the reason — never fill-attributed completion."""
    orders: dict[str, dict] = {}

    def _entry(oid: str) -> dict:
        return orders.setdefault(oid, {
            "order_id": oid if not oid.startswith("no-order-id:") else None,
            "client_order_id": None, "submitted_at": None,
            "submit_status": None, "submit_phase": None,
            "last_observation_kind": None, "observed_at": None,
            "last_observation_failed": False,
            "broker_status": None, "terminal_status": None,
            "filled_evidence": False, "partial_evidence": False,
            "filled_qty": None, "filled_avg_price": None,
            "fills_matched": None, "fills_qty": None, "fills_notional": None,
            "fills_withheld": False,
            "fills_unavailable": not fills_available,
        })

    for event in events:
        kind = event["kind"]
        if kind not in _FUNNEL_ORDER_KINDS:
            continue
        oid = event.get("order_id")
        key = str(oid) if oid else f"no-order-id:{event['id']}"
        entry = _entry(key)
        payload = event.get("payload") or {}
        if kind == "order_submitted":
            entry["submitted_at"] = event["ts"].isoformat()
            if payload.get("client_order_id"):
                entry["client_order_id"] = payload.get("client_order_id")
            entry["submit_status"] = payload.get("submit_status")
            entry["submit_phase"] = _submit_phase(
                entry["order_id"], entry["submit_status"])
        else:
            entry["last_observation_kind"] = kind
            entry["observed_at"] = event["ts"].isoformat()
            status = payload.get("order_status")
            if status is not None:
                entry["broker_status"] = status
            if kind == "order_unknown":
                entry["last_observation_failed"] = True
            else:
                entry["last_observation_failed"] = False
            # R5 (ITERATION-3): fill degree and terminal status are
            # INDEPENDENT facts, each read off its own evidence — the
            # terminal state comes from payload.order_status in ANY order
            # event (a canceled-with-partial arrives as kind=order_partial
            # and keeps BOTH the quantity and the canceled status).
            if kind == "order_filled" or status == "filled":
                entry["filled_evidence"] = True
            if kind == "order_partial" or status == "partially_filled":
                entry["partial_evidence"] = True
            if status in _DEAD_ORDER_STATUSES:
                entry["terminal_status"] = status
            if payload.get("filled_qty") is not None:
                entry["filled_qty"] = payload.get("filled_qty")
            if payload.get("filled_avg_price") is not None:
                entry["filled_avg_price"] = payload.get("filled_avg_price")
    if fills_available:
        for key, entry in orders.items():
            oid = entry["order_id"]
            if oid is None:
                continue
            allowed = allowed_oids is None or str(oid) in allowed_oids
            matched = fills_by_order.get(str(oid)) if allowed else None
            if matched:
                entry["fills_matched"] = True
                entry["fills_qty"] = sum(float(f.get("qty") or 0.0) for f in matched)
                entry["fills_notional"] = round(
                    sum(float(f.get("notional") or 0.0) for f in matched), 2)
                if any((f.get("order_status") or "") == _FILL_COMPLETE_STATUS
                       for f in matched):
                    # The broker's own completing-fill marker — completion
                    # evidence (R5); a lone "partial_fill" activity is not.
                    entry["filled_evidence"] = True
                elif (entry["fills_qty"] or 0.0) > 0:
                    entry["partial_evidence"] = True
            elif str(oid) in fills_by_order:
                # The feed HAS activity for this order_id, but this chain
                # holds no unique provable ownership of it (conflicted /
                # identity-less submission — R4 ITERATION-4). Granting it
                # would credit one fill to two chains; the evidence stays
                # order-level in unlinked_fills with the reason.
                entry["fills_matched"] = None
                entry["fills_withheld"] = True
            else:
                # No match in a readable feed — that is absence of evidence,
                # not evidence of zero fills.
                entry["fills_matched"] = False
    for entry in orders.values():
        entry["phase"] = _order_phase(entry)
    return list(orders.values())


def _funnel_sizing_snapshot(events: list[dict]) -> dict | None:
    """The latest sizing snapshot carried by any event payload — echoed, never
    recomputed (PLAN §A-4: the number must come from the attempt that produced
    it, not from the moment the page was opened)."""
    snapshot = None
    for event in events:  # events arrive in id (append) order — last wins
        payload = event.get("payload") or {}
        sizing = payload.get("sizing")
        if isinstance(sizing, dict):
            # R8: the snapshot carries ITS OWN timestamp and attempt identity
            # (which attempt produced these numbers), not the page-open moment.
            snapshot = {
                "event_kind": event["kind"],
                "event_ts": event["ts"].isoformat() if event.get("ts") else None,
                "attempt_id": event.get("attempt_id"),
                "sizing": sizing,
            }
            for key in ("reason", "cash_available", "available_notional", "min_notional"):
                if key in payload:
                    snapshot[key] = payload[key]
    return snapshot


def _funnel_classify(orders: list[dict], last_kind: str | None) -> str:
    # R5: a real submission needs an order (order_id present) that was not
    # explicitly rejected. NULL-order_id rejected/unknown attempts get their
    # own buckets instead of inflating "submitted".
    real_submissions = [
        o for o in orders
        if o.get("submitted_at") and o.get("order_id")
        and o.get("submit_phase") != "submit_rejected"
    ]
    if real_submissions:
        return "submitted"
    if any(o.get("phase") == "submit_rejected" for o in orders):
        return "submit_rejected"
    if any(o.get("submitted_at") for o in orders):
        return "submit_unknown"
    if last_kind in _FUNNEL_WAIT_KINDS:
        return "waiting"
    if last_kind in _FUNNEL_DISCARD_KINDS:
        return "discarded"
    if last_kind == "intent_cleared":
        return "cleared"
    if last_kind in _FUNNEL_INTENT_KINDS:
        return "created_pending"
    return "other"


def funnel_summary(
    conn: sqlite3.Connection,
    *,
    mode: str = "paper",
    now: datetime | None = None,
    fills: list[dict] | None = None,
    fills_reason: str | None = None,
    fills_truncated: bool | None = None,
    session_day: str | None = None,
) -> dict:
    """Read-only Today execution-funnel aggregation (c2c_a7e2 PLAN §五).

    Cohort denominator: decision_buy events of THIS session — unified ET
    session day (the latest paper cycle's, matching today_payload), the given
    trading mode, and events at or before `now` — grouped by (run_id, symbol).
    Intents created on an earlier ET day that keep flushing today are listed
    separately as carryover and never enter the denominator. Every chain shows
    the latest known stage, its structured reason, attempt counts, order
    observations and the sizing snapshot carried by the events. Old schema /
    identity-less rows / failed or truncated fills degrade explicitly.
    """
    now = _aware(now or datetime.now(timezone.utc))
    day = session_day
    notes: list[str] = []
    if day is None:
        try:
            day = session_date(conn, now)
        except sqlite3.Error:
            day = et_date_of(now)
            notes.append("cycles 表不可读——会话日回退为 ET 今日")
    out = {
        "session_date": day,
        "mode": mode,
        "generated_at": now.isoformat(),
        "schema_degraded": False,
        "schema_reason": None,
        "summary": _funnel_zero_summary(),
        "chains": [],
        "carryover": [],
        "unlinked_fills": [],
        "run_skips": [],
        "degraded": _funnel_empty_degraded(),
    }

    fills_available = fills is not None
    out["degraded"]["fills"] = {
        "degraded": not fills_available,
        "reason": fills_reason if not fills_available else None,
        "truncated": fills_truncated if fills_available else None,
    }
    # R5 tail: the fill index applies the same `now` cutoff as everything
    # else and dedupes by activity id — executions after the cutoff and
    # duplicated feed rows are neither evidence nor double-counted.
    fills_by_order: dict[str, list[dict]] = {}
    seen_fill_ids: set[str] = set()
    if fills_available:
        for fill in fills:
            fid = fill.get("id")
            if fid is not None:
                if str(fid) in seen_fill_ids:
                    out["degraded"]["duplicate_fill_activities"] += 1
                    continue
                seen_fill_ids.add(str(fid))
            fts = parse_iso(fill.get("transaction_time"))
            if fts is not None and fts > now:
                out["degraded"]["post_cutoff_fills"] += 1
                continue
            oid = fill.get("order_id")
            if oid:
                fills_by_order.setdefault(str(oid), []).append(fill)

    # -- schema gate: an old journal degrades instead of breaking --------------
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(intent_events)")}
    except sqlite3.Error as exc:
        out["schema_degraded"] = True
        out["schema_reason"] = f"intent_events 不可读: {exc}"
        out["degraded"]["notes"] = notes + ["旧 schema：无法做身份级漏斗聚合"]
        return out
    if not columns:
        out["schema_degraded"] = True
        out["schema_reason"] = "intent_events 表不存在"
        out["degraded"]["notes"] = notes + ["旧 schema：无法做身份级漏斗聚合"]
        return out
    missing = [c for c in _FUNNEL_REQUIRED_COLUMNS if c not in columns]
    if missing:
        out["schema_degraded"] = True
        out["schema_reason"] = (
            "旧 schema——intent_events 缺少身份列: " + ", ".join(missing)
        )
        legacy: dict[str, int] = {}
        try:
            rows = conn.execute(
                "SELECT timestamp, kind FROM intent_events ORDER BY id"
            ).fetchall()
        except sqlite3.Error:
            rows = []
        for row in rows:
            ts = parse_iso(row[0])
            if ts is not None and et_date_of(ts) == day and ts <= now:
                legacy[row[1]] = legacy.get(row[1], 0) + 1
        out["legacy_event_counts"] = legacy
        out["degraded"]["notes"] = notes + [
            "旧行无身份列，只按 kind 计数；不按 symbol+时间强行配对",
        ]
        return out

    # -- read the whole ledger (explicit column list → works with any row factory)
    try:
        raw = conn.execute(
            "SELECT id, timestamp, symbol, kind, deferred, detail, event_id, run_id, "
            "mode, decision_key, intent_id, attempt_id, order_id, payload "
            "FROM intent_events ORDER BY id"
        ).fetchall()
    except sqlite3.Error as exc:
        out["schema_degraded"] = True
        out["schema_reason"] = f"intent_events 读取失败: {exc}"
        out["degraded"]["notes"] = notes
        return out

    degraded = out["degraded"]
    today: list[dict] = []                      # in-session, in-mode events
    by_intent_all: dict[str, list[dict]] = {}   # in-mode, any day, up to cutoff
    # Pass 1 — parse, plus the R4 (ITERATION-3) attribution index: which
    # intent(s) SUBMITTED each order_id (same book, same mode, identified
    # submissions only, up to the cutoff). Resolution happens after the scan
    # because a later row can turn a unique owner into a conflict.
    submit_owners: dict[tuple, set] = {}
    identityless_submit_keys: set[tuple] = set()
    parsed: list[dict] = []
    for row in raw:
        r = dict(zip(_FUNNEL_INTENT_EVENT_COLUMNS, row))
        ts = parse_iso(r["timestamp"])
        payload = None
        if r["payload"]:
            try:
                loaded = json.loads(r["payload"])
            except (ValueError, TypeError):
                degraded["unparseable_payloads"] += 1
                loaded = None
            payload = loaded if isinstance(loaded, dict) else None
        event = {
            "id": r["id"], "ts": ts, "symbol": r["symbol"], "kind": r["kind"],
            "detail": r["detail"], "run_id": r["run_id"], "mode": r["mode"],
            "decision_key": r["decision_key"], "intent_id": r["intent_id"],
            "attempt_id": r["attempt_id"], "order_id": r["order_id"],
            "payload": payload,
        }
        if ts is None:
            degraded["unparseable_timestamps"] += 1
        elif ts <= now and r["mode"] == mode and r["intent_id"]:
            # R4: the chain-history index keeps every in-mode identified
            # event up to the cutoff — any ET day. The creation date never
            # bounds order lookup. (Exactly one append per event.)
            by_intent_all.setdefault(r["intent_id"], []).append(event)
            if r["kind"] == "order_submitted" and r["order_id"]:
                submit_owners.setdefault(
                    (_book_of(r["run_id"]), mode, str(r["order_id"])),
                    set()).add(r["intent_id"])
        elif (ts is not None and ts <= now and r["mode"] == mode
                and r["kind"] == "order_submitted" and r["order_id"]
                and not r["intent_id"]):
            # A submission with no identity: its observations can never be
            # attributed to a version — the owner identity never existed.
            identityless_submit_keys.add(
                (_book_of(r["run_id"]), mode, str(r["order_id"])))
        parsed.append({"r": r, "event": event})

    unique_owner: dict[tuple, str] = {}
    conflict_keys: set[tuple] = set()
    for key, owners in submit_owners.items():
        if len(owners) == 1:
            unique_owner[key] = next(iter(owners))
        else:
            conflict_keys.add(key)

    # R4 (ITERATION-4): the same ownership facts, keyed by order_id alone.
    # Broker fills carry no book/mode, so an order is fill-attributable only
    # when its identified-submission owner set across the FULL in-mode
    # history is EXACTLY ONE intent. submit_owners is mode-scoped, so another
    # mode's same-order_id history never enters this set — a dry_run twin can
    # neither steal the paper identity nor suppress the paper lookback. A
    # conflicted order (or one whose only submission is identity-less, or one
    # with no submission at all) stays OUT of the index: its fills are
    # withheld from every chain and listed order-level with the reason.
    owners_by_oid: dict[str, set] = {}
    for (_book, _mode, oid), owners in submit_owners.items():
        owners_by_oid.setdefault(oid, set()).update(owners)
    order_to_intent: dict[str, str] = {}
    conflict_oids: set[str] = set()
    for oid, owners in owners_by_oid.items():
        if len(owners) == 1:
            order_to_intent[oid] = next(iter(owners))
        else:
            conflict_oids.add(oid)
    identityless_submit_oids = {key[2] for key in identityless_submit_keys}

    # Per-intent view of the fill index (R4 ITERATION-4): a chain is handed
    # only the fills of orders it uniquely owns; everything else is withheld
    # inside _funnel_orders and surfaces in unlinked_fills.
    fills_by_intent: dict[str, dict[str, list[dict]]] = {}
    if fills_available:
        for oid, matched in fills_by_order.items():
            iid = order_to_intent.get(oid)
            if iid is not None:
                fills_by_intent.setdefault(iid, {})[oid] = matched

    # Pass 2 — R4 identity-less attachment, the identity-less degraded gate,
    # and the in-session cohort intake.
    for row in parsed:
        r, event = row["r"], row["event"]
        ts = event["ts"]
        in_day = ts is not None and et_date_of(ts) == day and ts <= now
        # R4 (ITERATION-3): an identity-less order observation joins a chain
        # only through a UNIQUE identified submission of the same order_id in
        # the same book and mode. Conflicts and identity-less/no submissions
        # are listed as unattributed — never paired by symbol. A recovered
        # event is NOT degraded accounting: its identity was established.
        attached_to = None
        if (ts is not None and ts <= now and r["mode"] == mode
                and r["kind"] in _FUNNEL_OBSERVATION_KINDS
                and not r["intent_id"] and r["order_id"]):
            key = (_book_of(r["run_id"]), mode, str(r["order_id"]))
            if key in unique_owner:
                attached_to = unique_owner[key]
                # RECOVERED identity from the unique submission — not invented.
                event["intent_id"] = attached_to
                by_intent_all.setdefault(attached_to, []).append(event)
            elif in_day:
                if key in conflict_keys:
                    note = ("同一 order_id 归属多个意图（同书同 mode）——"
                            "不按 symbol 猜测，显式列为无法归因")
                elif key in identityless_submit_keys:
                    note = ("原提交事件无意图身份（intent_id 为空）——"
                            "不按 symbol 猜测，显式列为无法归因")
                else:
                    note = ("台账中无该订单（同书同 mode）的提交记录——"
                            "不按 symbol 猜测，显式列为无法归因")
                degraded["unattributed_order_events"].append({
                    "kind": r["kind"],
                    "order_id": str(r["order_id"]),
                    "symbol": r["symbol"],
                    "mode": r["mode"],
                    "timestamp": r["timestamp"],
                    "note": note,
                })
        if not in_day:
            continue
        if attached_to is not None:
            today.append(event)
            continue
        # Identity-less rows: countable, not chainable. R6 (ITERATION-3): the
        # gate is the MISSING IDENTITY — not the kind list, and not the mode —
        # so it covers the pre-A-2 flush five AND the modern flush_wait /
        # intent_cleared / order_submitted / order_* / clear_failed events
        # produced for a version-NULL intent. Entries stay mode-distinguishable
        # (paper and dry_run separable; mode-NULL history lands in the
        # "unsplit" reference bucket, never folded into this page's own mode).
        # No version is invented, so they stay out of every chain and
        # denominator.
        if r["kind"] in _FUNNEL_IDENTITY_KINDS and not r["intent_id"]:
            degraded["legacy_flush_events"] += 1
            mode_key = r["mode"] if r["mode"] else "unsplit"
            degraded["identityless_by_mode"][mode_key] = (
                degraded["identityless_by_mode"].get(mode_key, 0) + 1)
            degraded["identityless_observed_today"].append({
                "kind": r["kind"],
                "symbol": r["symbol"],
                "timestamp": r["timestamp"],
                "mode": r["mode"],
                "note": "本轮已观察、意图身份缺失（intent_id 为空）——"
                        "不补造版本，不成链，不入分母；按 mode 区分，"
                        "mode=NULL 归未分模式参考",
            })
        # A decision_buy with no run_id, or no mode at all, can never join any
        # mode's cohort — count it as unattributed instead of fabricating one.
        if r["kind"] == "decision_buy" and (not r["run_id"] or r["mode"] is None):
            degraded["unattributed_decision_events"] += 1
            continue
        if r["mode"] != mode:
            continue
        today.append(event)

    # -- cohort: this session's BUY decisions, keyed by (run_id, symbol) --------
    decisions: dict[tuple[str, str], dict] = {}
    for event in today:
        if event["kind"] != "decision_buy":
            continue
        if not event["run_id"]:
            degraded["unattributed_decision_events"] += 1
            continue
        decisions.setdefault((event["run_id"], event["symbol"]), event)

    created_by_decision: dict[str, list[dict]] = {}
    created_by_fallback: dict[tuple[str, str], list[dict]] = {}
    not_created_by_decision: dict[str, list[dict]] = {}
    not_created_by_fallback: dict[tuple[str, str], list[dict]] = {}
    today_by_intent: dict[str, list[dict]] = {}
    for event in today:
        kind = event["kind"]
        if kind in _FUNNEL_INTENT_KINDS and event["intent_id"]:
            if event["decision_key"]:
                created_by_decision.setdefault(event["decision_key"], []).append(event)
            elif event["run_id"]:
                created_by_fallback.setdefault(
                    (event["run_id"], event["symbol"]), []).append(event)
            today_by_intent.setdefault(event["intent_id"], []).append(event)
        elif kind == "intent_not_created":
            if event["decision_key"]:
                not_created_by_decision.setdefault(event["decision_key"], []).append(event)
            elif event["run_id"]:
                not_created_by_fallback.setdefault(
                    (event["run_id"], event["symbol"]), []).append(event)
        elif event["intent_id"]:
            today_by_intent.setdefault(event["intent_id"], []).append(event)

    # -- cross-day indexes (R4) ---------------------------------------------------
    # order_id → intent_id over the full in-mode history — built ABOVE from the
    # unique-submission ownership facts (R4 ITERATION-4: the fill-attribution
    # twin of the observation attachment) — so today's fills can find the chain
    # an order belongs to even when nothing else happened today. Conflicted and
    # identity-less orders are absent from it by construction.
    superseded_by: dict[str, str] = {}
    for iid, events in by_intent_all.items():
        for event in events:
            if event["kind"] == "intent_replaced":
                prev = (event.get("payload") or {}).get("previous_version")
                if prev:
                    superseded_by.setdefault(str(prev), iid)
    # Intents shown outside the cohort denominator: any in-session activity,
    # plus orders whose TODAY fills matched history (fills-only days), plus
    # intents that a today replacement superseded.
    carry_ids: set[str] = set(today_by_intent)
    if fills_available:
        for oid, matched in fills_by_order.items():
            if any(_fill_session_date(f) == day for f in matched):
                iid = order_to_intent.get(oid)
                if iid:
                    carry_ids.add(iid)
    for event in today:
        if event["kind"] == "intent_replaced":
            prev = (event.get("payload") or {}).get("previous_version")
            if prev and prev in by_intent_all:
                carry_ids.add(prev)

    def _intent_chain(intent_id: str) -> dict:
        # R4: the chain detail is the FULL history up to the cutoff —
        # yesterday's creation, order_id and sizing snapshot join today's
        # observations. The today cohort denominator is untouched (it is
        # decided by today's decision_buy events alone).
        events = sorted(by_intent_all.get(intent_id, []), key=lambda e: e["id"])
        last = events[-1] if events else None
        # R4 (ITERATION-4): the chain sees only fills of orders it UNIQUELY
        # owns; feed activity outside the grant is withheld (see
        # _funnel_orders) and stays order-level in unlinked_fills.
        allowed = set(fills_by_intent.get(intent_id, {}))
        orders = _funnel_orders(events, fills_by_order, fills_available,
                                allowed_oids=allowed)
        # R6 (ITERATION-3): attempts = flush ATTEMPTS — distinct runs whose
        # events were flush-stage WORK (wait/veto/discard/submit). Submitting
        # IS flush work; status observations are re-queries of an existing
        # order, not attempts (1 submit + 10 requeries = 1 attempt and 10
        # observed_runs, never 11). Creation and replacement runs are not
        # attempts; open_missing waits are a skipped check, not a wait, and
        # do not enter wait_events.
        attempts = len({e["run_id"] for e in events
                        if e["run_id"] and e["kind"] in _FUNNEL_ATTEMPT_KINDS})
        created_runs = len({e["run_id"] for e in events
                            if e["run_id"] and e["kind"] in _FUNNEL_INTENT_KINDS})
        observed_runs = len({e["run_id"] for e in events
                             if e["run_id"] and e["kind"] in _FUNNEL_OBSERVATION_KINDS})
        wait_events = sum(
            1 for e in events
            if e["kind"] == "flush_wait" and _event_reason(e) != "open_missing")
        return {
            "intent_id": intent_id,
            "attempts": attempts,
            "created_runs": created_runs,
            "observed_runs": observed_runs,
            "wait_events": wait_events,
            "stage": last["kind"] if last else None,
            "reason": _event_reason(last) if last else None,
            "orders": orders,
            "sizing": _funnel_sizing_snapshot(events),
            "phase": _chain_phase(orders),
        }

    # -- per-decision chains ------------------------------------------------------
    summary = out["summary"]
    linked_versions: set[str] = set()
    for (run_id, symbol), decision_event in sorted(
            decisions.items(), key=lambda kv: kv[1]["id"]):
        key = f"{run_id}:{symbol}"
        not_created = (not_created_by_decision.get(key)
                       or not_created_by_fallback.get((run_id, symbol)))
        creations = (created_by_decision.get(key)
                     or created_by_fallback.get((run_id, symbol)))
        chain = {
            "decision_key": key,
            "run_id": run_id,
            "symbol": symbol,
            "first_event_at": decision_event["ts"].isoformat(),
            "last_event_at": decision_event["ts"].isoformat(),
            "stage": None,
            "reason": None,
            "intent_id": None,
            "replaced_previous_version": None,
            "superseded_by": None,
            "attempts": 0,
            "created_runs": 0,
            "observed_runs": 0,
            "wait_events": 0,
            "orders": [],
            "sizing": None,
            "classification": None,
            "phase": None,
        }
        if not_created:
            last = max(not_created, key=lambda e: e["id"])
            chain["stage"] = "intent_not_created"
            chain["reason"] = _event_reason(last)
            chain["last_event_at"] = last["ts"].isoformat()
            chain["classification"] = "not_created"
            reason = chain["reason"] or "unknown"
            summary["not_created_reasons"][reason] = (
                summary["not_created_reasons"].get(reason, 0) + 1)
        elif creations:
            newest = max(creations, key=lambda e: e["id"])
            version = newest["intent_id"]
            linked_versions.add(version)
            intent_chain = _intent_chain(version)
            payload = newest.get("payload") or {}
            chain.update({
                "stage": intent_chain["stage"] or newest["kind"],
                "reason": intent_chain["reason"] or _event_reason(newest),
                "intent_id": version,
                "replaced_previous_version": payload.get("previous_version"),
                "attempts": intent_chain["attempts"],
                "created_runs": intent_chain["created_runs"],
                "observed_runs": intent_chain["observed_runs"],
                "wait_events": intent_chain["wait_events"],
                "orders": intent_chain["orders"],
                "sizing": intent_chain["sizing"],
                "last_event_at": max(
                    [chain["first_event_at"]]
                    + [e["ts"].isoformat() for e in by_intent_all.get(version, [])]
                ),
            })
            chain["classification"] = _funnel_classify(chain["orders"], chain["stage"])
            chain["phase"] = _chain_phase(chain["orders"])
            # R5 display fix: a replaced intent is not "waiting" — say what
            # replaced it, while keeping its own failure history intact. If
            # the old version already acted (reached an order), the order
            # buckets stay and only superseded_by is attached.
            replaced_by = superseded_by.get(version)
            if replaced_by:
                chain["superseded_by"] = replaced_by
                if chain["classification"] not in ("submitted", "submit_rejected"):
                    chain["classification"] = "superseded"
        else:
            chain["stage"] = "evidence_missing"
            chain["reason"] = None
            chain["classification"] = "evidence_missing"
        out["chains"].append(chain)
        summary["decisions"] += 1
        bucket = chain["classification"]
        if bucket == "not_created":
            summary["not_created"] += 1
        elif bucket == "evidence_missing":
            summary["evidence_missing"] += 1
        else:
            summary["with_intent"] += 1
            summary[bucket] = summary.get(bucket, 0) + 1
        # R5 fill accounting: completion = an order_filled observation or a
        # fill activity the broker marked "fill" (see _funnel_orders); a
        # partial quantity is a partial, never a completion; and a later
        # unknown observation cannot un-verify earlier completion evidence
        # (evidence accumulates in the order flags, not the latest kind).
        chain_phase = chain["phase"]
        if chain_phase == "complete":
            summary["filled_verified"] += 1
        elif chain_phase == "partial":
            summary["partial"] += 1

    # -- carryover / orphan / unknown-origin intents -------------------------------
    first_creation: dict[str, dict] = {}
    for intent_id, events in by_intent_all.items():
        for event in sorted(events, key=lambda e: e["id"]):
            if event["kind"] in _FUNNEL_INTENT_KINDS:
                first_creation[intent_id] = event
                break
    for intent_id in sorted(carry_ids):
        if intent_id in linked_versions:
            continue
        events = sorted(by_intent_all.get(intent_id, []), key=lambda e: e["id"])
        creation = first_creation.get(intent_id)
        if creation is None:
            degraded["unknown_origin_intents"].append({
                "intent_id": intent_id,
                "symbol": events[0]["symbol"] if events else None,
                "note": "今日有活动但找不到创建事件——来源不明，不并入任何分母",
            })
            continue
        chain = _intent_chain(intent_id)
        entry = {
            "intent_id": intent_id,
            "symbol": creation["symbol"],
            "created_on": et_date_of(creation["ts"]),
            "created_by_decision": creation.get("decision_key"),
            **{k: chain[k] for k in
               ("stage", "reason", "attempts", "created_runs", "observed_runs",
                "wait_events", "orders", "sizing")},
        }
        entry["phase"] = _chain_phase(entry["orders"])
        entry["superseded_by"] = None
        entry["classification"] = _funnel_classify(entry["orders"], entry["stage"])
        replaced_by = superseded_by.get(intent_id)
        if replaced_by:
            entry["superseded_by"] = replaced_by
            if entry["classification"] not in ("submitted", "submit_rejected"):
                entry["classification"] = "superseded"
        if et_date_of(creation["ts"]) < day:
            out["carryover"].append(entry)
            summary["carryover_intents"] += 1
        else:
            degraded["orphan_intents"].append({
                "intent_id": intent_id,
                "symbol": entry["symbol"],
                "created_by_decision": entry.get("created_by_decision"),
                "note": "今日创建但对应的 decision_buy 事件缺失——不计入分母，不丢弃",
                **{k: chain[k] for k in
                   ("stage", "reason", "attempts", "created_runs", "observed_runs",
                    "wait_events", "orders", "sizing", "phase")},
            })

    # -- fills that cannot be attributed stay visible, with the reason ------------
    # R4 (ITERATION-4): attribution goes through the SAME unique-submission
    # index as the observation attachment. A feed order outside the index —
    # no ledger order at all, an identity-less-only submission, or one claimed
    # by several intents — keeps its fill evidence listed HERE with the
    # specific reason: never credited to a chain, never counted twice.
    if fills_available:
        for oid, matched in fills_by_order.items():
            if oid in order_to_intent:
                continue          # uniquely owned — the evidence lives on its chain
            if oid in conflict_oids:
                note = ("同一 order_id 归属多个意图（同书同 mode）——"
                        "成交不归功任一意图，不重复计完成")
            elif oid in identityless_submit_oids:
                note = ("原提交事件无意图身份（intent_id 为空）——"
                        "成交不归功任一意图，不并入任何链")
            else:
                note = ("order_id 在台账中无任何订单事件——无法归因；"
                        "正面证据单独列出，不并入分母，不造链")
            for fill in matched:
                if _fill_session_date(fill) != day:
                    continue
                out["unlinked_fills"].append({
                    "activity_id": fill.get("id"),
                    "order_id": oid,
                    "symbol": fill.get("symbol"),
                    "side": fill.get("side"),
                    "qty": fill.get("qty"),
                    "notional": fill.get("notional"),
                    "transaction_time": fill.get("transaction_time"),
                    "fill_status": fill.get("order_status"),
                    "note": note,
                })

    # -- run-level flush skips -------------------------------------------------------
    for event in today:
        if event["kind"] == "flush_skipped":
            payload = event.get("payload") or {}
            remainder = payload.get("unprocessed")
            out["run_skips"].append({
                "run_id": event["run_id"],
                "reason": _event_reason(event),
                "at": event["ts"].isoformat(),
                # R6 (ITERATION-3): the detail prose is preserved VERBATIM —
                # the aggregation never parses text. The structured remainder
                # is forwarded AS DATA when the recorder's payload carries it
                # (whitelisted key); a missing key is None, never re-derived.
                "detail": event["detail"],
                "unprocessed": remainder if isinstance(remainder, list) else None,
            })

    summary["submitted_frac"] = f"{summary['submitted']}/{summary['decisions']}"
    degraded["notes"] = notes
    return out


def session_date(conn: sqlite3.Connection, now: datetime) -> str:
    """ET calendar date of the latest paper cycle, else ET today.

    Weekend visitors still see Friday's story instead of an empty 'today'.
    """
    row = conn.execute(
        "SELECT timestamp FROM cycles WHERE mode = 'paper' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if row and row[0]:
        ts = parse_iso(row[0])
        if ts is not None:
            return et_date_of(ts)
    return et_date_of(now)


def _fill_session_date(fill: dict) -> str | None:
    ts = parse_iso(fill.get("transaction_time"))
    return et_date_of(ts) if ts is not None else None


def latest_scores(conn: sqlite3.Connection) -> dict[str, dict]:
    rows = conn.execute(
        "SELECT d.symbol, d.quant_score, d.combined_score, d.action, d.reasoning, c.timestamp "
        "FROM decisions d JOIN cycles c ON c.id = d.cycle_id "
        "WHERE c.id = (SELECT MAX(id) FROM cycles WHERE mode = 'paper') "
        "ORDER BY d.symbol"
    ).fetchall()
    return {row["symbol"]: dict(row) for row in rows}


def fail_closed_count(conn: sqlite3.Connection, day: str) -> int:
    rows = conn.execute(
        "SELECT c.timestamp FROM decisions d JOIN cycles c ON c.id = d.cycle_id "
        "WHERE d.reasoning LIKE '%new entries fail closed%'"
    ).fetchall()
    n = 0
    for row in rows:
        ts = parse_iso(row["timestamp"])
        if ts is not None and et_date_of(ts) == day:
            n += 1
    return n


def today_payload(
    conn: sqlite3.Connection,
    *,
    heartbeat_path: Path,
    watchlist: list[str],
    fills: list[dict] | None,
    fill_reason: str | None,
    positions: list[dict] | None,
    now: datetime | None = None,
    deep_only: bool = False,
) -> dict:
    now = _aware(now or datetime.now(timezone.utc))
    day = session_date(conn, now)
    calendar_today = et_date_of(now)
    pool = {s.upper() for s in watchlist}
    scores = latest_scores(conn)
    session_fills = [f for f in (fills or []) if _fill_session_date(f) == day]
    holdings = []
    for pos in positions or []:
        symbol = str(pos.get("symbol") or "").upper()
        score = scores.get(symbol) or scores.get(pos.get("symbol")) or {}
        in_pool = symbol in pool
        holdings.append({
            **pos,
            "symbol": symbol,
            "in_pool": in_pool,
            "bucket": "pool" if in_pool else "legacy",
            "quant_score": score.get("quant_score"),
            "combined_score": score.get("combined_score"),
            "last_action": score.get("action"),
        })
    return {
        "session_date": day,
        "calendar_today": calendar_today,
        "weekend": datetime.fromisoformat(calendar_today).weekday() >= 5,
        "health": book_health(heartbeat_path, now=now, deep_only=deep_only),
        "fills_today": session_fills,
        "fills_degraded": fills is None,
        "fills_reason": fill_reason,
        "intents": list_intents(conn, now=now),
        "intent_events": recent_intent_events(conn),
        "vetoes": veto_counts(conn, now=now),
        "holdings": holdings,
        "fail_closed_today": fail_closed_count(conn, day),
        "outcomes": outcomes_summary(conn),
        "notes": {
            "fills": "成交来自券商 fills，不是 journal.fill_price（该列故意为空）。",
            "intents": "intent_events 自 2026-08-30 才落库；更早的拦截只在 logs/ 里。",
            "legacy": "不在当前 watchlist.symbols 里的持仓是遗留仓，走 legacy_sell_threshold。",
        },
    }
