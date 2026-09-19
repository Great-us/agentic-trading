"""Read-only diagnostics for the P1 growth-pool paper-forward observation window.

Answers, from nothing but the journal / heartbeat / watchlist files already on
disk: how far into the pre-registered 6-month observation window
(research/ACTIVE-BOOK-VALIDATION-PLAN.md, pool switched 2026-08-22) the book
is, whether the paper cycles that are supposed to accumulate evidence actually
ran on every expected trading day, how many forward-outcome samples have
matured per horizon, and how the book's capital splits between pool names and
pre-pool legacy holdings.

Strictly read-only: the journal is opened with SQLite URI mode=ro, the
heartbeat and watchlist files are only read, and no data/ path is ever
written. It imports nothing from run/decision/risk/signals/dashboard, so the
same module runs unchanged on Trading-P2 (which has no dashboard package).
"""
from __future__ import annotations

import argparse
import calendar
import json
import sqlite3
import statistics
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_JOURNAL_PATH = ROOT / "data" / "journal.db"
DEFAULT_HEARTBEAT_PATH = ROOT / "data" / "heartbeat.json"
DEFAULT_WATCHLIST_PATH = ROOT / "config" / "watchlist.yaml"

# Pre-registered protocol start: the day the growth pool was switched to the
# 14-name list in config/watchlist.yaml (research/ACTIVE-BOOK-VALIDATION-PLAN.md,
# "Growth pool 2026-08-22"). First formal review is 6 calendar months later.
PROTOCOL_START = date(2026, 8, 22)

# Simplified 2026 US market holiday calendar (weekday closures only). This is
# NOT an exchange calendar: Good Friday (2026-04-03) predates the window, but
# any 2026 holidays later than Christmas are ignored, and no 2027 holidays are
# subtracted (they don't matter while now < first_review anyway). Recheck
# before reusing this module in 2027.
HOLIDAYS_2026 = {
    date(2026, 9, 7),    # Labor Day
    date(2026, 11, 26),  # Thanksgiving
    date(2026, 12, 25),  # Christmas
}

EXPECTED_CYCLE_MODE = "paper"


def _as_et_date(timestamp: str) -> date | None:
    """The ET calendar date a cycle timestamp belongs to.

    Cycle timestamps are stamped in UTC (run.py: datetime.now(timezone.utc)),
    and a deep cycle can run late enough in the ET evening to cross UTC
    midnight while still being the same ET trading session (AGENTS.md F5: a
    16:15 ET slot ran at 19:19 ET). Bucketing by the UTC date would then move
    that cycle to the next calendar day, so convert to America/New_York first.
    Returns None for timestamps that cannot be parsed at all.
    """
    text = (timestamp or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(ET).date()


def _expected_trading_days(start: date, end: date) -> list[date]:
    """Weekdays from start through end inclusive, minus the simplified
    2026 holiday table. Heuristic calendar — see HOLIDAYS_2026."""
    days: list[date] = []
    current = start
    while current <= end:
        if current.weekday() < 5 and current not in HOLIDAYS_2026:
            days.append(current)
        current += timedelta(days=1)
    return days


def _add_months(day: date, months: int) -> date:
    """Calendar-month arithmetic clamped to the month's last day
    (2026-08-22 + 6 months = 2027-02-22; 2026-08-31 + 6 months clamps to
    2027-02-28 rather than raising, since February has no 31st)."""
    month_index = day.month - 1 + months
    year = day.year + month_index // 12
    month = month_index % 12 + 1
    last_day_of_month = calendar.monthrange(year, month)[1]
    return date(year, month, min(day.day, last_day_of_month))


def _load_watchlist_symbols(path: Path) -> list[str]:
    """Parse the top-level `symbols:` list from config/watchlist.yaml.

    Hand-rolled instead of importing the config loader (which pulls in risk
    settings and their schema) — this file only needs the symbol list. Lines
    look like `  - MU     # HBM / DRAM`; inline comments are stripped."""
    symbols: list[str] = []
    in_symbols = False
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        if not line.startswith(" ") and not line.startswith("\t"):
            in_symbols = line.strip() == "symbols:"
            continue
        if not in_symbols:
            continue
        stripped = line.strip()
        if stripped.startswith("- "):
            symbol = stripped[2:].strip()
            if symbol:
                symbols.append(symbol.upper())
    return symbols


def _load_heartbeat(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _open_readonly(path: Path) -> sqlite3.Connection:
    uri = f"file:{path.as_posix()}?mode=ro"
    return sqlite3.connect(uri, uri=True)


def _protocol_section(now: datetime) -> dict:
    et_now = now.astimezone(ET)
    today = et_now.date()
    first_review = _add_months(PROTOCOL_START, 6)
    days_elapsed = (today - PROTOCOL_START).days
    days_remaining = (first_review - today).days
    return {
        "start": PROTOCOL_START.isoformat(),
        "first_review": first_review.isoformat(),
        "days_elapsed": days_elapsed,
        "days_remaining": days_remaining,
        "status": "observing" if today < first_review else "review_due",
    }


def _sessions_section(conn: sqlite3.Connection, now: datetime) -> dict:
    """Expected vs observed trading days since the protocol start.

    "Observed" = an ET date with at least one cycles row of mode='paper'.
    days_missing only flags ET dates with zero rows; a holiday that the
    simplified calendar fails to subtract would be reported as missing even
    though the market was closed (see caveats)."""
    end = now.astimezone(ET).date()
    expected = _expected_trading_days(PROTOCOL_START, end)
    expected_iso = {d.isoformat() for d in expected}
    per_day: dict[str, int] = {iso: 0 for iso in expected_iso}
    rows = conn.execute(
        "SELECT timestamp FROM cycles WHERE mode = ?", (EXPECTED_CYCLE_MODE,)
    )
    for (ts,) in rows:
        day = _as_et_date(ts)
        if day is None:
            continue
        iso = day.isoformat()
        if iso in per_day:
            per_day[iso] += 1
    counts = [per_day[iso] for iso in sorted(per_day)]
    days_missing = [iso for iso in sorted(per_day) if per_day[iso] == 0]
    return {
        "expected_trading_days": len(expected),
        "observed_trading_days": len(expected) - len(days_missing),
        "days_missing": days_missing,
        "cycles_per_day": {
            "min": min(counts) if counts else None,
            "median": statistics.median(counts) if counts else None,
            "max": max(counts) if counts else None,
        },
    }


def _outcomes_bucket(
    outcome_rows: list[tuple],
    decision_rows: list[tuple],
    *,
    min_day: date | None,
    max_day: date | None,
) -> dict:
    """Matured forward-outcome sample counts per horizon, plus the number of
    distinct (symbol, ET date) decision pairs, restricted to the closed range
    [`min_day`, `max_day`] (either bound None means unrestricted on that side).

    An empty range (e.g. `max_day` before `min_day`, which happens when `now`
    predates PROTOCOL_START) simply matches nothing — every row is filtered
    out, all counts come back 0, and nothing raises."""
    n1 = n5 = n20 = 0
    for ts, r1, r5, r20 in outcome_rows:
        day = _as_et_date(ts)
        if day is None:
            continue
        if min_day is not None and day < min_day:
            continue
        if max_day is not None and day > max_day:
            continue
        n1 += r1 is not None
        n5 += r5 is not None
        n20 += r20 is not None
    distinct_pairs: set[tuple[str, date]] = set()
    for symbol, ts in decision_rows:
        day = _as_et_date(ts)
        if day is None:
            continue
        if min_day is not None and day < min_day:
            continue
        if max_day is not None and day > max_day:
            continue
        distinct_pairs.add((symbol, day))
    return {
        "ret_1d_mature": n1,
        "ret_5d_mature": n5,
        "ret_20d_mature": n20,
        "distinct_symbol_days": len(distinct_pairs),
    }


def _outcomes_section(conn: sqlite3.Connection, now: datetime) -> dict:
    """Matured forward-outcome sample counts, split into the pre-registered
    protocol observation window and the full journal history.

    `sessions` counts trading days over [PROTOCOL_START, `now`'s ET date] —
    an outcomes count with no upper bound would include decisions made AFTER
    that same `now`, describing a different (wider) time range than `sessions`
    under the same report. `protocol_window` closes that gap: it carries its
    own `start`/`end` (ET dates, inclusive) and only counts decisions whose ET
    date falls in that closed range. `all_history` stays unfiltered by design,
    for context/debugging only — see caveats.

    signal_outcomes is keyed by decision_id; the mode='paper' filter comes
    from joining back to cycles. Every journaled action (buy/sell/trim/wait/
    hold/avoid) counts — evaluate.py deliberately scores them all."""
    end = now.astimezone(ET).date()
    outcome_rows = conn.execute(
        """SELECT c.timestamp, so.ret_1d, so.ret_5d, so.ret_20d
           FROM signal_outcomes so
           JOIN decisions d ON d.id = so.decision_id
           JOIN cycles c ON c.id = d.cycle_id
           WHERE c.mode = ?""",
        (EXPECTED_CYCLE_MODE,),
    ).fetchall()
    # The ET date cannot be derived in SQL (DST-aware conversion), so compute
    # the pair set in Python.
    decision_rows = conn.execute(
        """SELECT d.symbol, c.timestamp
           FROM decisions d JOIN cycles c ON c.id = d.cycle_id
           WHERE c.mode = ?""",
        (EXPECTED_CYCLE_MODE,),
    ).fetchall()
    window = _outcomes_bucket(
        outcome_rows, decision_rows, min_day=PROTOCOL_START, max_day=end
    )
    window = {"start": PROTOCOL_START.isoformat(), "end": end.isoformat(), **window}
    return {
        "protocol_window": window,
        "all_history": _outcomes_bucket(
            outcome_rows, decision_rows, min_day=None, max_day=None
        ),
    }


def _holdings_section(conn: sqlite3.Connection, watchlist_path: Path) -> dict:
    """Split the latest position_state snapshot into pool vs legacy capital.

    position_state holds one row per currently-open position (symbol ->
    last_qty / last_value / updated_at, from the most recent cycle). The
    pool/legacy split follows ACTIVE-BOOK-VALIDATION-PLAN.md §7: legacy =
    holdings not in config/watchlist.yaml `symbols`."""
    pool_symbols = set(_load_watchlist_symbols(watchlist_path))
    rows = conn.execute(
        "SELECT symbol, last_qty, last_value, updated_at FROM position_state"
    ).fetchall()
    pool_value = legacy_value = 0.0
    pool_symbols_held: list[str] = []
    legacy_symbols_held: list[str] = []
    for symbol, qty, value, updated_at in rows:
        if symbol.upper() in pool_symbols:
            pool_value += float(value or 0.0)
            pool_symbols_held.append(symbol)
        else:
            legacy_value += float(value or 0.0)
            legacy_symbols_held.append(symbol)
    total = pool_value + legacy_value
    total = max(total, 0.0)
    return {
        "pool": {
            "market_value": pool_value,
            "share_of_positions": pool_value / total if total > 0 else None,
            "symbols": sorted(pool_symbols_held),
        },
        "legacy": {
            "market_value": legacy_value,
            "share_of_positions": legacy_value / total if total > 0 else None,
            "symbols": sorted(legacy_symbols_held),
        },
        "total_market_value": pool_value + legacy_value,
        "watchlist_symbols": sorted(pool_symbols),
        "snapshot_updated_at": max((r[3] for r in rows), default=None),
    }


def _llm_section(heartbeat_path: Path) -> dict:
    """LLM analyst status straight from heartbeat.json's deep stamp.

    The llm_status field is written by P0-B-1 (run.py -> write_heartbeat);
    older stamps don't carry it, in which case this reports null with a note
    rather than guessing."""
    heartbeat = _load_heartbeat(heartbeat_path)
    if not isinstance(heartbeat, dict):
        return {
            "llm_status": None,
            "note": "heartbeat missing or unreadable; llm_status unknown",
        }
    deep = heartbeat.get("deep")
    if not isinstance(deep, dict) or "llm_status" not in deep:
        return {
            "llm_status": None,
            "note": "llm_status 字段由 P0-B-1 提供，当前 heartbeat 没有",
        }
    return {"llm_status": deep["llm_status"], "note": None}


def _caveats() -> list[str]:
    return [
        "交易日历是简化的：仅周六日 + 2026 年三个硬编码假日（09-07 Labor Day / "
        "11-26 Thanksgiving / 12-25 Christmas）。真实交易所还有其他休市日；"
        "被漏掉的假日会被误报成 days_missing，min/median/max 的分母也包含它。",
        "周期按 America/New_York 时区取日期归属（时间戳为 UTC，深周期晚跑跨 "
        "UTC 零点时仍归属同一 ET 交易日，见 AGENTS.md F5）；非 ISO 时间戳"
        "无法解析的行被静默跳过。",
        "position_state 只保留每只持仓最近一次周期的快照（最后一次写入值），"
        "不是历史序列；市值/占比反映的是最新快照那一刻，且不含现金，"
        "share_of_positions 的分母只是持仓市值合计而非净值。",
        "outcomes 计数包含全部动作（buy/sell/trim/wait/hold/avoid），与 "
        "evaluate.py 的口径一致；ret_20d 需要 20 个交易日才成熟，观察期开始"
        "不足一个月时它必然为 0。outcomes 拆成两组：protocol_window（ET 日期 "
        "落在 [PROTOCOL_START, now 的 ET 日期] 闭区间内，start/end 随本次结果附"
        "带输出，本协议观察期证据，判读用这组）与 all_history（不限起止的全部 "
        "paper 记录，仅供参考/排障，不得当观察期证据引用）。",
        "outcomes 的成熟标记（ret_1d/5d/20d 是否非空）反映的是 signal_outcomes "
        "表**当前**的内容——即上一次 `journal.evaluate` 实际跑到、写进库里的状态，"
        "不是把 `now` 参数当成一个历史时间点、反推'如果在 now 那天评估会看到什么'。"
        "传入一个过去的 `now` 只会收紧 protocol_window 的 [start, end] 过滤范围，"
        "不会让已经成熟的样本变回当时尚未成熟的状态。",
        "llm_status 原样透传自 heartbeat.json 的 deep 戳（P0-B-1 写入），"
        "本模块不解析、不验证其内容。",
        "position_state 是 journal 侧快照，不与券商 positions 对账（对账在 "
        "P0-A-3 的 reconcile 路径做）；VEEV 类账实差异不会在这里出现。",
        "预期交易日从 PROTOCOL_START 当天算起（2026-08-22 是周六，本身不是 "
        "交易日，不影响计数）。",
    ]


def diagnose(
    journal_path: Path | str,
    heartbeat_path: Path | str,
    watchlist_path: Path | str,
    now: datetime | None = None,
) -> dict:
    """Read-only snapshot of the P1 paper-forward observation protocol.

    All paths default to this book's data/config files; `now` defaults to the
    real UTC clock (tests pass a fixed value). Returns a JSON-serializable
    dict; never writes anywhere."""
    journal_path = Path(journal_path)
    heartbeat_path = Path(heartbeat_path)
    watchlist_path = Path(watchlist_path)
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    result: dict = {
        "protocol": _protocol_section(now),
        "sessions": None,
        "outcomes": None,
        "holdings": None,
        "llm": _llm_section(heartbeat_path),
        "caveats": _caveats(),
    }

    if not journal_path.exists():
        result["caveats"] = list(result["caveats"]) + [
            f"journal not found at {journal_path}; sessions/outcomes/holdings omitted"
        ]
        return result
    conn = _open_readonly(journal_path)
    try:
        result["sessions"] = _sessions_section(conn, now)
        result["outcomes"] = _outcomes_section(conn, now)
        result["holdings"] = _holdings_section(conn, watchlist_path)
    finally:
        conn.close()
    return result


def _fmt_json(data: dict) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False, default=str)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--json", action="store_true", dest="as_json",
        help="print the raw JSON dict instead of a short summary",
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_JOURNAL_PATH)
    parser.add_argument("--heartbeat", type=Path, default=DEFAULT_HEARTBEAT_PATH)
    parser.add_argument("--watchlist", type=Path, default=DEFAULT_WATCHLIST_PATH)
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    report = diagnose(args.db, args.heartbeat, args.watchlist)
    if args.as_json:
        print(_fmt_json(report))
        return 0

    proto = report["protocol"]
    print(
        f"Protocol: start {proto['start']}, first review {proto['first_review']} "
        f"({proto['days_elapsed']}d elapsed / {proto['days_remaining']}d remaining, "
        f"{proto['status']})"
    )
    sessions = report["sessions"]
    if sessions:
        print(
            f"Sessions: {sessions['observed_trading_days']}/"
            f"{sessions['expected_trading_days']} trading days observed; "
            f"missing: {', '.join(sessions['days_missing']) or 'none'}"
        )
        print(
            "cycles/day min/median/max: "
            f"{sessions['cycles_per_day']['min']}/"
            f"{sessions['cycles_per_day']['median']}/"
            f"{sessions['cycles_per_day']['max']}"
        )
    outcomes = report["outcomes"]
    if outcomes:
        window = outcomes["protocol_window"]
        print(
            f"Outcomes (protocol window {window['start']}..{window['end']}): "
            f"n1d={window['ret_1d_mature']} n5d={window['ret_5d_mature']} "
            f"n20d={window['ret_20d_mature']} "
            f"distinct symbol-days={window['distinct_symbol_days']}"
        )
        h = outcomes["all_history"]
        print(
            f"Outcomes (all history, context only): n1d={h['ret_1d_mature']} "
            f"n5d={h['ret_5d_mature']} n20d={h['ret_20d_mature']} "
            f"distinct symbol-days={h['distinct_symbol_days']}"
        )
    holdings = report["holdings"]
    if holdings:
        pool, legacy = holdings["pool"], holdings["legacy"]
        print(
            f"Holdings: pool ${pool['market_value']:.2f} "
            f"({pool['share_of_positions'] if pool['share_of_positions'] is not None else 0:.1%}), "
            f"legacy ${legacy['market_value']:.2f} "
            f"({legacy['share_of_positions'] if legacy['share_of_positions'] is not None else 0:.1%})"
        )
    llm = report["llm"]
    if llm["llm_status"] is not None:
        print(f"LLM: {json.dumps(llm['llm_status'], ensure_ascii=False)}")
    else:
        print(f"LLM: null ({llm['note']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
