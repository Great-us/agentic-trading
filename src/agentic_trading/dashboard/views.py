"""Read-only SQL views over a book's journal.db.

Everything here opens the database with mode=ro — the dashboard can never
corrupt a live journal, and WAL lets it read while the trading system writes.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path


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


def outcomes_summary(conn: sqlite3.Connection, min_n: int = 5) -> list[dict]:
    """Forward-return averages per action bucket, evaluate-style. Descriptive
    only — small n is shown as-is, never as a finding."""
    rows = conn.execute(
        "SELECT d.action, COUNT(*) AS n,"
        " AVG(o.ret_1d)*100 AS ret_1d, AVG(o.ret_5d)*100 AS ret_5d,"
        " AVG(o.ret_20d)*100 AS ret_20d,"
        " AVG(o.mfe_20d)*100 AS mfe_20d, AVG(o.mae_20d)*100 AS mae_20d"
        " FROM signal_outcomes o JOIN decisions d ON d.id = o.decision_id"
        " GROUP BY d.action ORDER BY n DESC"
    ).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        for key in ("ret_1d", "ret_5d", "ret_20d", "mfe_20d", "mae_20d"):
            value = item[key]
            item[key] = round(value, 2) if value is not None else None
        item["small_sample"] = bool(item["n"] < min_n)
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
