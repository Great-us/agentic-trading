"""One-page daily report over the journal, plus optional cross-book monitoring.

Read-only by construction: journals are opened through a `mode=ro` SQLite URI,
so generating a report can never migrate or mutate a live database. Usage:

    python -m agentic_trading.daily_report                     # main book only
    python -m agentic_trading.daily_report --db PATH --p2-db PATH
    python -m agentic_trading.daily_report --out-dir logs/daily

Writes <out-dir>/YYYY-MM-DD.md (today's local date) and, when matplotlib is
installed, an equity-curve PNG next to it.

Fills and closed round-trip P&L come from a GET-only Alpaca reader (never from
journal.fill_price, which stays NULL by design). Missing credentials degrade
the fills section instead of failing the rest of the report.
"""
from __future__ import annotations

import argparse
import math
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

from .broker_read import BookBrokerReader, BrokerError
from .journal.evaluate import DEFAULT_MODE as OUTCOMES_DEFAULT_MODE
from .journal.evaluate import MIN_REPORT_N
from .journal.logger import DEFAULT_DB_PATH
from .round_trips import reconcile_positions, round_trips

ET = ZoneInfo("America/New_York")

CYCLE_WINDOW = 20          # equity/cash timeline length
# 60 daily-return observations, per the pre-registered incremental-information
# test in research/PAPER2-THEME-ROTATION.md §5. That section is frozen, so the
# window, the use of returns (not equity levels) and the max() overlap
# denominator below all follow it literally rather than what is convenient.
CORR_WINDOW = 60           # rolling correlation window (daily returns)
RECENT_DAYS = 7            # veto-stats recency window


def _connect_ro(db_path: Path | str) -> sqlite3.Connection:
    """Open a journal read-only; refuses to touch (or create) the file."""
    db_path = Path(db_path)
    if not db_path.exists():
        raise FileNotFoundError(f"journal not found: {db_path}")
    # POSIX separators + percent-quoting keep Windows paths valid inside a URI.
    uri = "file:" + quote(str(db_path.resolve()).replace("\\", "/")) + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


# --- main-book views ----------------------------------------------------------

def _recent_cycles(conn: sqlite3.Connection, limit: int = CYCLE_WINDOW) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT id, timestamp, mode, equity, cash, regime_score, regime_label
           FROM cycles ORDER BY id DESC LIMIT ?""",
        (limit,),
    ).fetchall()


def _exposure_pct(equity: float | None, cash: float | None) -> float | None:
    if not equity or cash is None or equity <= 0:
        return None
    return (1.0 - cash / equity) * 100.0


def _regime_dwell(conn: sqlite3.Connection) -> tuple[str, float, int, str] | None:
    row = conn.execute(
        "SELECT label, score, consecutive, updated_at FROM regime_dwell WHERE id = 1"
    ).fetchone()
    return tuple(row) if row else None


def _position_state(conn: sqlite3.Connection) -> dict[str, tuple[float, float]]:
    """symbol -> (qty, market value) from the last recorded snapshot."""
    return {
        sym: (qty, val)
        for sym, qty, val in conn.execute(
            "SELECT symbol, last_qty, last_value FROM position_state"
        )
    }


def _price_basis(conn: sqlite3.Connection, symbol: str) -> tuple[float | None, str]:
    """Latest journal-recorded price for a held symbol, without live quotes.

    The journal stores no close prices, so the reference price falls back to
    whatever the pipeline itself wrote: the position snapshot's implied mark
    (value/qty, refreshed every cycle), then the most recent fill price, then
    notional/qty. The returned note is shown in the report so the basis is
    always explicit."""
    snap = conn.execute(
        "SELECT last_qty, last_value FROM position_state WHERE symbol = ?", (symbol,)
    ).fetchone()
    if snap and snap[0] and snap[1] is not None:
        return snap[1] / snap[0], "position_state 市值/股数推算"
    row = conn.execute(
        """SELECT fill_price FROM decisions d
           JOIN cycles c ON c.id = d.cycle_id
           WHERE d.symbol = ? AND d.fill_price IS NOT NULL
           ORDER BY c.id DESC LIMIT 1""",
        (symbol,),
    ).fetchone()
    if row and row[0]:
        return row[0], "最近一次成交价 fill_price"
    row = conn.execute(
        """SELECT notional, order_qty FROM decisions d
           JOIN cycles c ON c.id = d.cycle_id
           WHERE d.symbol = ? AND d.notional IS NOT NULL AND d.order_qty IS NOT NULL AND d.order_qty > 0
           ORDER BY c.id DESC LIMIT 1""",
        (symbol,),
    ).fetchone()
    if row:
        return row[0] / row[1], "notional/股数推算"
    return None, "无可用价格"


def _peak_drawdowns(conn: sqlite3.Connection) -> list[dict]:
    """Per-held-symbol distance to the recorded high-water mark (percent)."""
    holdings = _position_state(conn)
    peaks = {sym: hwm for sym, hwm in conn.execute(
        "SELECT symbol, high_water_mark FROM position_peaks")}
    rows = []
    for sym in sorted(holdings):
        price, basis = _price_basis(conn, sym)
        peak = peaks.get(sym)
        dd = (price / peak - 1.0) * 100.0 if (price is not None and peak) else None
        rows.append({
            "symbol": sym, "peak": peak, "price": price,
            "basis": basis, "dd_pct": dd,
        })
    return rows


# --- veto statistics -----------------------------------------------------------
# Categories below mirror run.py verbatim: each SQL predicate matches the exact
# string run.py appends to decisions.reasoning (or the order_status it sets),
# so every count is auditable against the journal. Anything run.py only sends
# to the log file is listed in NOT_JOURNALED instead of being guessed at.
VETO_CATEGORIES: list[tuple[str, str, str]] = [
    ("风控否决买入（size/sector/exposure 等）",
     "d.action='buy' AND d.order_status IS NULL "
     "AND d.reasoning LIKE '%Risk manager vetoed buy:%'",
     "decisions.reasoning"),
    ("买单被券商拒绝", "d.action='buy' AND d.order_status='rejected'", "decisions.order_status"),
    ("卖出失败/被拒（仓位仍持有）",
     "d.action='sell' AND d.order_status='rejected'", "decisions.order_status"),
    ("每周期新单上限跳过",
     "d.reasoning LIKE '%max_new_orders_per_cycle reached%'", "decisions.reasoning"),
    ("macro regime UNKNOWN 禁止开仓",
     "d.reasoning LIKE '%macro regime UNKNOWN%'", "decisions.reasoning"),
    ("缺 LLM 结论，BUY 降级 WAIT（fail closed）",
     "d.reasoning LIKE '%new entries fail closed%'", "decisions.reasoning"),
]

REFERENCE_CATEGORIES: list[tuple[str, str, str]] = [
    ("BUY 转 TradeIntent 排队（非否决）", "d.order_status='intent'", "decisions.order_status"),
    ("收盘后卖出排队次日执行（非否决）", "d.order_status='queued_closed'", "decisions.order_status"),
]

# Intent-flush outcomes now land in intent_events (journal/logger.py), so they
# are counted below rather than declared uncountable. `kind` -> label.
INTENT_EVENT_KINDS: list[tuple[str, str]] = [
    ("gap", "跳空拦截（live 价超出 signal 价 max_entry_gap_atr 个 ATR）— intent 丢弃"),
    ("chase_signal", "Chase 拦截（对比信号价）— intent 保留待下次扫描"),
    ("chase_open", "Chase 拦截（对比今日开盘）— intent 保留待下次扫描"),
    ("sizing", "Flush 阶段 sizing 否决（敞口 / book 风险 / sector cap）— intent 保留"),
    ("ttl", "TradeIntent TTL 过期丢弃"),
]

NOT_JOURNALED: list[str] = []


def _count_category(conn: sqlite3.Connection, condition: str, cutoff_iso: str) -> tuple[int, int]:
    total = conn.execute(
        f"SELECT COUNT(*) FROM decisions d WHERE {condition}").fetchone()[0]
    recent = conn.execute(
        f"""SELECT COUNT(*) FROM decisions d JOIN cycles c ON c.id = d.cycle_id
            WHERE {condition} AND c.timestamp >= ?""",
        (cutoff_iso,),
    ).fetchone()[0]
    return recent, total


# --- cross-book comparison ------------------------------------------------------

def _daily_equity(conn: sqlite3.Connection) -> dict[str, float]:
    """date -> last recorded equity that day (UTC calendar day of cycle ts).

    Paper cycles only. DryRunBroker reports a fixed $100,000 account
    (execution/broker.py) and dry runs share the live journal, so an unfiltered
    query lets a rehearsal become the day's equity — 2026-08-23 read $100,000
    against a ~$10k book until this filter existed.
    """
    rows = conn.execute(
        """SELECT substr(timestamp, 1, 10) AS day, equity FROM cycles
           WHERE mode = 'paper'
             AND id IN (SELECT MAX(id) FROM cycles WHERE mode = 'paper'
                        GROUP BY substr(timestamp, 1, 10))
           ORDER BY timestamp"""
    ).fetchall()
    return {day: eq for day, eq in rows if eq is not None}


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n < 2:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    var_x = sum((x - mx) ** 2 for x in xs)
    var_y = sum((y - my) ** 2 for y in ys)
    if var_x <= 0 or var_y <= 0:
        return None
    return cov / math.sqrt(var_x * var_y)


def equity_correlation(
    main_conn: sqlite3.Connection, p2_conn: sqlite3.Connection,
    window: int = CORR_WINDOW,
) -> tuple[float | None, int]:
    """Rolling-window Pearson of the two books' daily equity *returns*.

    Returns (correlation-or-None, number_of_aligned_returns); None means fewer
    than `window` aligned returns exist and the sample must be called out as
    insufficient rather than reported.

    Returns, not equity levels: two books that both drift upward correlate at
    almost 1.0 on levels whatever they hold, which would trip the >0.95 "the
    rotation adds no information" line in PAPER2-THEME-ROTATION.md §5 on
    arithmetic alone. Day-over-day returns are what that test is about.
    """
    a, b = _daily_equity(main_conn), _daily_equity(p2_conn)
    common = sorted(set(a) & set(b))
    # A return needs the previous aligned day, so n days yield n-1 returns.
    rets_a, rets_b = [], []
    for prev, day in zip(common, common[1:]):
        if a[prev] and b[prev]:
            rets_a.append(a[day] / a[prev] - 1.0)
            rets_b.append(b[day] / b[prev] - 1.0)
    rets_a, rets_b = rets_a[-window:], rets_b[-window:]
    if len(rets_a) < window:
        return None, len(rets_a)
    return _pearson(rets_a, rets_b), len(rets_a)


def position_symbols(conn: sqlite3.Connection) -> set[str]:
    return set(_position_state(conn))


def overlap_stats(
    main_conn: sqlite3.Connection, p2_conn: sqlite3.Connection,
) -> tuple[set[str], set[str], set[str], float | None]:
    """Symbol-set overlap between the two books' open positions.

    Ratio is |intersection| / max(|A|, |B|), the denominator fixed by
    PAPER2-THEME-ROTATION.md §5. Dividing by the smaller book instead inflates
    the number — one shared name out of {1} and {8} would read 100% — and the
    >70% line it feeds is a kill criterion, so it has to be the strict form.
    None when both books hold nothing."""
    a, b = position_symbols(main_conn), position_symbols(p2_conn)
    inter = a & b
    larger = max(len(a), len(b))
    ratio = (len(inter) / larger) if larger else None
    return a, b, inter, ratio


# --- rendering -------------------------------------------------------------------

def _money(v: float | None) -> str:
    return "${:,.2f}".format(v) if v is not None else "n/a"


def _pct(v: float | None, nd: int = 1) -> str:
    return f"{v:.{nd}f}%" if v is not None else "n/a"


def _short_ts(ts: str) -> str:
    return ts.replace("T", " ")[:16]


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        ts = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


def _et_date(ts: datetime) -> str:
    return ts.astimezone(ET).date().isoformat()


def _short_et(ts_raw: str | None) -> str:
    ts = _parse_iso(ts_raw)
    if ts is None:
        return (ts_raw or "")[:16]
    return ts.astimezone(ET).strftime("%Y-%m-%d %H:%M")


def _infer_book_root(db_path: Path | str) -> Path:
    """<root>/data/journal.db → <root>; otherwise the db's parent directory."""
    path = Path(db_path).resolve()
    if path.parent.name.lower() == "data":
        return path.parent.parent
    return path.parent


def _progress_lines(book_root: Path) -> list[str]:
    """Five-line 下一班 block from progress.json; missing file is honest."""
    from .progress import read_progress

    lines = ["", "### 下一班（交班卡）", ""]
    card = None
    for candidate in (book_root / "data" / "progress.json", book_root / "progress.json"):
        card = read_progress(candidate)
        if card:
            break
    if not card:
        lines.append("还没有交班卡（`data/progress.json`）。")
        return lines
    rnd = card.get("round") or {}
    job = card.get("next_job") or {}
    lines.append(f"- 上一轮：**{rnd.get('name') or '—'}** · {rnd.get('status') or '—'} @ {rnd.get('asof') or '—'}")
    when = job.get("when_et") or ""
    lines.append(f"- 下一班：**{job.get('slot') or '—'}** {when}".rstrip())
    if job.get("instruction"):
        lines.append(f"- 指令：{job['instruction']}")
    unresolved = card.get("unresolved") or []
    if unresolved:
        bits = []
        for item in unresolved:
            if isinstance(item, dict):
                bits.append(f"{item.get('kind')}{(' — ' + item['detail']) if item.get('detail') else ''}")
            else:
                bits.append(str(item))
        lines.append("- 未决：" + "；".join(bits))
    else:
        lines.append("- 未决：无")
    return lines


def _outcomes_summary(
    conn: sqlite3.Connection,
    *,
    mode: str | None = OUTCOMES_DEFAULT_MODE,
    min_n: int = MIN_REPORT_N,
) -> list[dict]:
    """Forward-return averages per action bucket, read-only off `signal_outcomes`
    (populated by `journal.evaluate`, which this report never calls — no writes).

    n_1d/n_5d/n_20d are the per-horizon MATURED sample count, not the bucket's
    decision count — a bucket can have 40 decisions but only 6 with a matured
    20d close. `n_1d_small`/`n_5d_small`/`n_20d_small` flag each horizon
    independently against `min_n` (evaluate.py's MIN_REPORT_N by default): a
    bucket with 40 decisions and 40 matured 1d returns but only 1 matured 20d
    return must not read as "big enough sample" just because the bucket-level
    decision count clears the bar.

    `mode='paper'` by default — dry_run and backtest cycles have no real
    fills and must not count as paper-forward evaluation evidence. Pass
    `mode=None` for the full journal across every mode (audit/debugging only).

    Duplicated rather than imported from `dashboard.views.outcomes_summary`:
    daily_report must keep working on P2, which has no `dashboard/` package
    (AGENTS.md).
    """
    query = (
        "SELECT d.action, COUNT(*) AS n,"
        " SUM(CASE WHEN o.ret_1d IS NOT NULL THEN 1 ELSE 0 END) AS n_1d,"
        " SUM(CASE WHEN o.ret_5d IS NOT NULL THEN 1 ELSE 0 END) AS n_5d,"
        " SUM(CASE WHEN o.ret_20d IS NOT NULL THEN 1 ELSE 0 END) AS n_20d,"
        " AVG(o.ret_1d)*100 AS ret_1d, AVG(o.ret_5d)*100 AS ret_5d,"
        " AVG(o.ret_20d)*100 AS ret_20d"
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
        for key in ("ret_1d", "ret_5d", "ret_20d"):
            value = item[key]
            item[key] = round(value, 2) if value is not None else None
        item["n_1d_small"] = item["n_1d"] < min_n
        item["n_5d_small"] = item["n_5d"] < min_n
        item["n_20d_small"] = item["n_20d"] < min_n
        out.append(item)
    return out


def _outcomes_table(rows: list[dict]) -> str:
    lines = [
        "| 决策 | n(决策数) | n1d | +1d | n5d | +5d | n20d | +20d |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for r in rows:
        def _fmt(pct: float | None) -> str:
            return "n/a" if pct is None else f"{pct:+.2f}%"

        def _n(count_key: str, small_key: str) -> str:
            mark = " ⚠️" if r.get(small_key) else ""
            return f"{r[count_key]}{mark}"

        lines.append(
            f"| {r['action']} | {r['n']} | {_n('n_1d', 'n_1d_small')} | {_fmt(r['ret_1d'])} "
            f"| {_n('n_5d', 'n_5d_small')} | {_fmt(r['ret_5d'])} "
            f"| {_n('n_20d', 'n_20d_small')} | {_fmt(r['ret_20d'])} |"
        )
    return "\n".join(lines)


def _evaluation_quality_lines(conn: sqlite3.Connection) -> list[str]:
    """Only-read summary of paper-forward outcome quality. Never triggers an
    evaluation run and never writes — purely a view over whatever
    `signal_outcomes` already has (populated by a separate `journal.evaluate`
    run, on its own schedule)."""
    lines = [
        "", "### 评估质量（paper-forward，只读展示）", "",
        "> `n1d`/`n5d`/`n20d` 是该 horizon **已成熟的样本数**，不是决策数——"
        "一个桶可能有 40 条决策但只有 6 条 20 日窗口已成熟，⚠️ 按每个 horizon "
        f"各自的成熟数（< {MIN_REPORT_N}）独立标注，不看决策总数；只统计 "
        f"`mode='{OUTCOMES_DEFAULT_MODE}'` 的周期，dry_run/backtest 不计入。"
        "本节不跑评估、不写库，数据来自上一次 `journal.evaluate` 的结果。",
    ]
    try:
        rows = _outcomes_summary(conn)
    except sqlite3.Error as exc:
        lines.append(f"无法读取 signal_outcomes：{exc}")
        return lines
    if not rows:
        lines.append("`signal_outcomes` 暂无数据（尚未跑过 `journal.evaluate`，或该 mode 无决策）。")
        return lines
    lines.append(_outcomes_table(rows))
    return lines


def _llm_status_lines(book_root: Path) -> list[str]:
    """Only-read summary of the analyst layer's last-known state, straight off
    heartbeat.json (P0-B-1's `llm_status` block) — no threshold logic, no
    alerting, just what's there for each mode that reported one."""
    from .progress import read_progress

    lines = ["", "### LLM 分析师状态（心跳，只读展示）", ""]
    payload = read_progress(book_root / "data" / "heartbeat.json")
    if not payload:
        lines.append("没有心跳文件，或心跳无法解析。")
        return lines
    shown = False
    for mode, label in (("deep", "深周期"), ("fast", "快扫")):
        entry = payload.get(mode)
        status = entry.get("llm_status") if isinstance(entry, dict) else None
        if not isinstance(status, dict):
            continue
        shown = True
        bits = [f"{label}：**{status.get('state') or 'unknown'}**"]
        if status.get("calls") is not None:
            bits.append(f"调用 {status['calls']} 次")
        if status.get("failures") is not None:
            bits.append(f"失败 {status['failures']} 次")
        if status.get("category"):
            bits.append(f"类别 {status['category']}")
        tail = status.get("retry_hint") or status.get("reason") or status.get("detail")
        if tail:
            bits.append(str(tail))
        lines.append("- " + "，".join(bits))
    if not shown:
        lines.append("心跳里没有 `llm_status` 字段（旧格式戳，或本盘未记录）。")
    return lines


def session_et_date(conn: sqlite3.Connection, now_utc: datetime) -> str:
    """ET calendar date of the latest paper cycle, else ET now.

    Weekend / after-close reports still show the last session instead of empty.
    """
    row = conn.execute(
        "SELECT timestamp FROM cycles WHERE mode = 'paper' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if row and row[0]:
        ts = _parse_iso(row[0])
        if ts is not None:
            return _et_date(ts)
    return _et_date(now_utc)


def load_broker_fills(book_root: Path | str) -> tuple[list[dict] | None, str | None, bool]:
    """GET-only fill history. Never writes. Degrades to (None, reason, False).

    Third element is True when the fetch hit broker_read's max_records cap,
    meaning older fills exist beyond what was returned (AGENTS.md F6).
    """
    reader = BookBrokerReader(Path(book_root))
    try:
        fills, reason = reader.fills()
    except BrokerError as exc:
        return None, str(exc), False
    except Exception as exc:  # noqa: BLE001 — network / parse / missing .env
        return None, f"{type(exc).__name__}: {exc}", False
    if fills is None:
        return None, reason or "broker unavailable", False
    return fills, None, reader.last_fills_truncated


def load_broker_positions(book_root: Path | str) -> tuple[list[dict] | None, str | None]:
    """GET-only broker positions. Never writes. Degrades to (None, reason)."""
    try:
        positions, reason = BookBrokerReader(Path(book_root)).positions()
    except BrokerError as exc:
        return None, str(exc)
    except Exception as exc:  # noqa: BLE001 — network / parse / missing .env
        return None, f"{type(exc).__name__}: {exc}"
    if positions is None:
        return None, reason or "broker unavailable"
    return positions, None


def _fills_on_session(fills: list[dict], session_day: str) -> list[dict]:
    matched = []
    for fill in fills:
        ts = _parse_iso(fill.get("transaction_time"))
        if ts is not None and _et_date(ts) == session_day:
            matched.append(fill)
    matched.sort(key=lambda f: f.get("transaction_time") or "")
    return matched


def _closed_trips_on_session(fills: list[dict], session_day: str) -> list[dict]:
    closed = []
    for trip in round_trips(fills):
        if trip.get("open"):
            continue
        ts = _parse_iso(trip.get("closed_at"))
        if ts is not None and _et_date(ts) == session_day:
            closed.append(trip)
    closed.sort(key=lambda t: t.get("closed_at") or "")
    return closed


def _fills_table(rows: list[dict]) -> str:
    lines = [
        "| 时间(ET) | 符号 | 方向 | 股数 | 价格 | 名义 |",
        "|---|---|---|---:|---:|---:|",
    ]
    for f in rows:
        side = str(f.get("side") or "").upper()
        lines.append(
            f"| {_short_et(f.get('transaction_time'))} | {f.get('symbol')} | {side} "
            f"| {float(f.get('qty') or 0.0):.4g} | {_money(f.get('price'))} "
            f"| {_money(f.get('notional'))} |"
        )
    return "\n".join(lines)


def _closed_trips_table(rows: list[dict]) -> str:
    lines = [
        "| 符号 | 开仓时间(ET) | 平仓时间(ET) | 股数 | 实现盈亏 | 备注 |",
        "|---|---|---|---:|---:|---|",
    ]
    for t in rows:
        notes = []
        if t.get("incomplete"):
            notes.append("⚠️ 不完整（缺期初买入记录）")
        if t.get("ambiguous"):
            notes.append("⚠️ 同一时刻多笔买卖，顺序无法确定")
        lines.append(
            f"| {t.get('symbol')} | {_short_et(t.get('opened_at'))} "
            f"| {_short_et(t.get('closed_at'))} | {float(t.get('qty') or 0.0):.4g} "
            f"| {_money(t.get('realized_pnl'))} | {'；'.join(notes)} |"
        )
    return "\n".join(lines)


def _reconciliation_table(diffs: list[dict]) -> str:
    lines = [
        "| 符号 | fills 派生持仓 | 券商持仓 | 说明 |",
        "|---|---:|---:|---|",
    ]
    for d in diffs:
        fills_qty = "—" if d.get("fills_qty") is None else f"{d['fills_qty']:g}"
        broker_qty = "—" if d.get("broker_qty") is None else f"{d['broker_qty']:g}"
        # An undetermined symbol (R5: order-ambiguous fills touched it) is
        # not a confirmed diff and must not read like one — same table,
        # different label, so it's still visible but not conflated with
        # "known to disagree".
        label = "❓ 无法确定" if d.get("undetermined") else "⚠️ 未解释"
        lines.append(f"| {d.get('symbol')} | {fills_qty} | {broker_qty} | {label}：{d.get('detail')} |")
    return "\n".join(lines)


def _reconciliation_section(
    fills: list[dict] | None,
    positions: list[dict] | None,
    positions_reason: str | None,
) -> list[str]:
    """fills 派生的未平仓 open lots vs 券商 positions，逐 symbol 比对（见 AGENTS.md F1）。
    只做观测展示，不影响任何决策。"""
    lines = ["", "### 账实对账（fills 派生持仓 vs 券商持仓）", ""]
    if fills is None:
        lines.append("无法核对：本会话未能读取券商成交。")
        return lines
    if positions is None:
        lines.append(f"无法核对：**{positions_reason or 'broker unavailable'}**。")
        return lines
    diffs = reconcile_positions(round_trips(fills), positions)
    if not diffs:
        lines.append("一致：fills 派生的未平仓持仓与券商持仓逐 symbol 匹配。")
    else:
        lines.append(_reconciliation_table(diffs))
    return lines


def _fills_section(
    session_day: str,
    fills: list[dict] | None,
    reason: str | None,
    truncated: bool = False,
    positions: list[dict] | None = None,
    positions_reason: str | None = None,
) -> list[str]:
    lines = [
        "",
        "## 三、成交与回合盈亏",
        "",
        (
            f"- 会话日期（ET）：**{session_day}**"
            "（最近一次 paper 周期的 ET 日；周末/盘后沿用上一交易日）"
        ),
        "",
        (
            "> 成交来自券商 fills（GET-only Alpaca paper）。**不是** journal"
            " `fill_price`——该列故意为空，本报告不把成交写回 journal。"
        ),
    ]
    if fills is None:
        lines += ["", f"无法读取券商成交，本栏降级：**{reason or 'broker unavailable'}**。"]
        return lines
    if truncated:
        lines += ["", "> ⚠️ 成交历史可能不完整：已触到读取上限，更早的成交未被拉取。"]

    session_fills = _fills_on_session(fills, session_day)
    closed = _closed_trips_on_session(fills, session_day)
    lines += ["", "### 本会话成交", ""]
    if session_fills:
        lines.append(_fills_table(session_fills))
    else:
        lines.append("本会话无成交。")
    lines += ["", "### 本会话已平仓回合", ""]
    if closed:
        lines.append(_closed_trips_table(closed))
        # Excluded from the confident total for two independent reasons — a
        # trip can be both (an orphan sell whose timestamp also happened to
        # tie), so they're counted and reported separately rather than as
        # one "not complete" bucket.
        complete = [t for t in closed if not t.get("incomplete") and not t.get("ambiguous")]
        total = sum(float(t.get("realized_pnl") or 0.0) for t in complete)
        incomplete_n = sum(1 for t in closed if t.get("incomplete"))
        ambiguous_n = sum(1 for t in closed if t.get("ambiguous"))
        notes = []
        if incomplete_n:
            notes.append(f"{incomplete_n} 笔不完整回合")
        if ambiguous_n:
            notes.append(f"{ambiguous_n} 笔顺序无法确定")
        suffix = f"（另有 {'、'.join(notes)}未计入）" if notes else ""
        lines += ["", f"- 本会话已实现盈亏合计：**{_money(total)}**{suffix}"]
    else:
        lines.append("本会话无已平仓回合。")
    lines += _reconciliation_section(fills, positions, positions_reason)
    return lines


def _timeline_table(rows: list[sqlite3.Row]) -> str:
    lines = [
        "| # | 时间(UTC) | mode | equity | cash | 敞口% | regime |",
        "|---:|---|---|---:|---:|---:|---|",
    ]
    for r in reversed(rows):  # chronological order reads better in a table
        label = f"{r['regime_label']} ({r['regime_score']:+.2f})" if r["regime_label"] else "—"
        lines.append(
            f"| {r['id']} | {_short_ts(r['timestamp'])} | {r['mode']} "
            f"| {_money(r['equity'])} | {_money(r['cash'])} "
            f"| {_pct(_exposure_pct(r['equity'], r['cash']))} | {label} |"
        )
    return "\n".join(lines)


def _drawdown_table(rows: list[dict]) -> str:
    lines = [
        "| 符号 | peak | 参考价 | 距峰值% | 价格口径 |",
        "|---|---:|---:|---:|---|",
    ]
    for r in rows:
        lines.append(
            f"| {r['symbol']} | {_money(r['peak'])} | {_money(r['price'])} "
            f"| {_pct(r['dd_pct'])} | {r['basis']} |"
        )
    return "\n".join(lines)


def _veto_tables(conn: sqlite3.Connection, cutoff_iso: str) -> str:
    lines = [
        "| 类别 | 近7天 | 累计 | 数据来源 |",
        "|---|---:|---:|---|",
    ]
    for label, condition, source in [*VETO_CATEGORIES, *REFERENCE_CATEGORIES]:
        recent, total = _count_category(conn, condition, cutoff_iso)
        lines.append(f"| {label} | {recent} | {total} | {source} |")
    for kind, label in INTENT_EVENT_KINDS:
        recent, total = _count_intent_events(conn, kind, cutoff_iso)
        lines.append(f"| {label} | {recent} | {total} | intent_events |")
    return "\n".join(lines)


def _count_intent_events(conn: sqlite3.Connection, kind: str,
                         cutoff_iso: str) -> tuple[int, int]:
    """(last 7 days, all time) flush outcomes of one kind.

    Counts events, not intents: a name held back by the chase guard on four
    consecutive scans counts four times, which is what makes "how often does
    this guard bind" answerable."""
    try:
        total = conn.execute(
            "SELECT COUNT(*) FROM intent_events WHERE kind = ?", (kind,)
        ).fetchone()[0]
        recent = conn.execute(
            "SELECT COUNT(*) FROM intent_events WHERE kind = ? AND timestamp >= ?",
            (kind, cutoff_iso),
        ).fetchone()[0]
    except sqlite3.Error:
        return 0, 0  # table predates this build (read-only journal, no migration)
    return recent, total


def _render_png(
    series: list[tuple[str, dict[str, float]]], out_path: Path, title: str,
) -> str | None:
    """Best-effort equity chart. Returns a skip-note, or None when written."""
    try:
        import matplotlib
        matplotlib.use("Agg")  # headless-safe backend
        import matplotlib.pyplot as plt
    except Exception as exc:  # ImportError covers missing matplotlib entirely
        return f"matplotlib 不可用，已跳过绘图（{type(exc).__name__}）。"
    try:
        fig, ax = plt.subplots(figsize=(9, 4.5))
        for label, daily in series:
            days = sorted(daily)
            xs = [datetime.strptime(d, "%Y-%m-%d") for d in days]
            ax.plot(xs, [daily[d] for d in days], marker=".", markersize=3, label=label)
        ax.set_title(title)
        ax.set_ylabel("Equity (USD)")
        ax.grid(True, alpha=0.3)
        ax.legend()
        fig.autofmt_xdate()
        fig.tight_layout()
        fig.savefig(out_path, dpi=120)
        plt.close(fig)
    except Exception as exc:
        return f"绘图失败，已跳过（{type(exc).__name__}: {exc}）。"
    return None


def generate_report(
    db_path: Path | str = DEFAULT_DB_PATH,
    p2_db_path: Path | str | None = None,
    out_dir: Path | str | None = None,
    *,
    today: date | None = None,
    now_utc: datetime | None = None,
) -> Path:
    """Build today's one-pager; returns the markdown path."""
    today = today or datetime.now().astimezone().date()
    now_utc = now_utc or datetime.now(timezone.utc)
    cutoff_iso = (now_utc - timedelta(days=RECENT_DAYS)).isoformat()
    out_dir = Path(out_dir) if out_dir else Path("logs", "daily")

    main_conn = _connect_ro(db_path)
    p2_conn = _connect_ro(p2_db_path) if p2_db_path else None
    png_note: str | None = None
    try:
        cycles = _recent_cycles(main_conn)  # newest first
        latest = cycles[0] if cycles else None
        dwell = _regime_dwell(main_conn)

        lines: list[str] = [
            f"# 每日一页纸报告 — {today.isoformat()}",
            "",
            f"- 生成时间：{_short_ts(now_utc.isoformat())}（UTC）",
            f"- 主盘数据库：`{Path(db_path)}`",
        ]
        if cycles:
            lines.append(
                f"- 周期覆盖：#{cycles[-1]['id']} ~ #{latest['id']}"
                f"（{_short_ts(cycles[-1]['timestamp'])} ~ {_short_ts(latest['timestamp'])}）"
            )
        else:
            lines.append("- 周期覆盖：暂无任何周期记录")

        # (a) main book -------------------------------------------------------
        lines += ["", "## 一、主盘概览", ""]
        if latest is None:
            lines.append("journal 中没有 cycles 记录，无法生成概览。")
        else:
            lines.append(
                f"- 最新周期：**#{latest['id']}** @ {_short_ts(latest['timestamp'])}"
                f" [{latest['mode']}]"
            )
            lines.append(
                f"- Equity / Cash：{_money(latest['equity'])} / {_money(latest['cash'])}"
            )
            lines.append(f"- 最新敞口：**{_pct(_exposure_pct(latest['equity'], latest['cash']))}**"
                         "（=1 − cash/equity）")
            if latest["regime_label"]:
                lines.append(
                    f"- Regime：**{latest['regime_label'].upper()}**"
                    f" ({latest['regime_score']:+.2f})"
                )
            else:
                lines.append("- Regime：本周期未记录")
            if dwell:
                lines.append(
                    f"- regime_dwell 当前状态：label={dwell[0] or '—'}，score="
                    f"{dwell[1]:+.2f}，连续触发 **{dwell[2]}** 次（更新于 {_short_ts(dwell[3])}）"
                )
            else:
                lines.append("- regime_dwell 当前状态：表中无记录")

            lines += ["", f"### 最近 {CYCLE_WINDOW} 个周期 equity/cash 时间线", "",
                      _timeline_table(cycles)]

            drawdowns = _peak_drawdowns(main_conn)
            lines += ["", "### 持仓距 peak 回撤", ""]
            if drawdowns:
                lines.append(_drawdown_table(drawdowns))
            else:
                lines.append("当前无持仓快照（position_state 为空）。")
            lines.append("")
            lines.append(
                "> 口径说明：journal 库不存收盘价，也不拉实时行情。参考价优先取"
                " `position_state` 最新市值÷股数（每周期刷新的 broker 快照），其次取最近一次"
                " `fill_price`，再次用 `notional÷order_qty` 推算；每行的口径已在表中注明。"
            )
        lines += _progress_lines(_infer_book_root(db_path))

        # (b) veto stats --------------------------------------------------------
        lines += ["", "## 二、veto / 拒单统计", "",
                  _veto_tables(main_conn, cutoff_iso), ""]
        if not NOT_JOURNALED:
            lines.append(
                "> intent_events 自 2026-08-30 起落库，此前的 flush 拦截只存在于 "
                "`logs/*.log`，累计列不含那段历史。"
            )
        else:
            lines.append("以下类别**未落库**（只写进 logs/*.log 运行日志），无法从 journal 统计：")
        for item in NOT_JOURNALED:
            lines.append(f"- {item}")
        lines += _llm_status_lines(_infer_book_root(db_path))
        lines += _evaluation_quality_lines(main_conn)

        # (b2) broker fills / closed round-trips --------------------------------
        session_day = session_et_date(main_conn, now_utc)
        book_root = _infer_book_root(db_path)
        fills, fill_reason, fills_truncated = load_broker_fills(book_root)
        positions, positions_reason = load_broker_positions(book_root)
        lines += _fills_section(
            session_day, fills, fill_reason, fills_truncated, positions, positions_reason
        )

        # (c) P2 comparison ------------------------------------------------------
        if p2_conn is not None:
            lines += ["", "## 四、双盘对照（--p2-db）", "",
                      f"- 二号盘数据库：`{Path(p2_db_path)}`"]
            a, b, inter, ratio = overlap_stats(main_conn, p2_conn)
            lines.append(
                f"- 持仓集合：主盘 {{{', '.join(sorted(a)) or '空'}}}"
                f"（{len(a)} 个），二号盘 {{{', '.join(sorted(b)) or '空'}}}（{len(b)} 个）"
            )
            if ratio is None:
                lines.append("- 持仓重叠：两盘均无持仓，比例不可计算")
            else:
                lines.append(
                    f"- 持仓重叠：**{len(inter)} 个**"
                    f"{('（' + ', '.join(sorted(inter)) + '）') if inter else ''}"
                    f"；重叠比例 = 重叠数 / max(两盘持仓数) = **{ratio * 100:.1f}%**（判负线 >70%）"
                )
            corr, n_aligned = equity_correlation(main_conn, p2_conn)
            if corr is None and n_aligned < CORR_WINDOW:
                lines.append(
                    f"- 日收益相关性（按日期对齐，{CORR_WINDOW} 日滚动）："
                    f"**样本不足**（仅 {n_aligned} 个对齐日收益，需 ≥{CORR_WINDOW}）"
                )
            elif corr is None:
                lines.append(
                    f"- 日收益相关性（按日期对齐，{CORR_WINDOW} 日滚动）："
                    "无法计算（某序列在窗口内方差为 0）"
                )
            else:
                lines.append(
                    f"- 日收益相关性（按日期对齐，{CORR_WINDOW} 日滚动）：**{corr:+.4f}**"
                    f"（基于最近 {n_aligned} 个对齐日收益；判负线 >0.95，需与持仓重叠 >70% 同时满足）"
                )

        # (d) equity chart ---------------------------------------------------------
        png_name = f"{today.isoformat()}.png"
        series: list[tuple[str, dict[str, float]]] = [("主盘", _daily_equity(main_conn))]
        if p2_conn is not None:
            series.append(("二号盘", _daily_equity(p2_conn)))
        has_any_point = any(pts for _, pts in series)
        # 一 overview, 二 veto, 三 fills, 四 p2 (optional), last = equity.
        chart_no = "五" if p2_conn is not None else "四"
        if not has_any_point:
            lines += ["", f"## {chart_no}、Equity 曲线", "", "无 equity 数据，跳过绘图。"]
        else:
            png_note = _render_png(series, out_dir / png_name, f"Equity — {today.isoformat()}")
            if png_note is None:
                lines += ["", f"## {chart_no}、Equity 曲线", "", f"![equity]({png_name})"]
            else:
                lines += ["", f"## {chart_no}、Equity 曲线", "", png_note]

        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{today.isoformat()}.md"
        out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return out_path
    finally:
        main_conn.close()
        if p2_conn is not None:
            p2_conn.close()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="main journal path")
    parser.add_argument("--p2-db", default=None, help="P2 clone journal path (enables the cross-book section)")
    parser.add_argument("--out-dir", default=str(Path("logs", "daily")), help="output directory")
    args = parser.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    try:
        out_path = generate_report(args.db, args.p2_db, args.out_dir)
    except (FileNotFoundError, sqlite3.Error) as exc:
        print(f"daily_report: {exc}")
        sys.exit(2)
    print(f"daily report written: {out_path}")


if __name__ == "__main__":
    main()
