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
