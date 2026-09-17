"""Tests for the daily one-pager report and cross-book monitoring:

- report file lands at <out-dir>/YYYY-MM-DD.md with the key numbers correct
  (exposure, regime/dwell, peak drawdowns, veto counters)
- price bases resolve without live quotes (snapshot -> fill -> notional/qty)
- --p2-db overlap count/ratio and the 60-day equity-return correlation
- insufficient samples are labelled honestly; matplotlib is optional
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agentic_trading.daily_report import (
    CORR_WINDOW, _connect_ro, _price_basis, equity_correlation, generate_report,
    main, overlap_stats, position_symbols,
)
from agentic_trading.journal.logger import (
    DecisionRow, connect, record_cycle, record_position_state,
    record_regime_dwell, update_position_peak,
)

# Fixed clock so the "recent 7 days" veto window is deterministic.
NOW = datetime(2026, 8, 25, 13, 0, tzinfo=timezone.utc)
TODAY = NOW.date()


def _ts(day: int, hour: int = 14) -> str:
    return datetime(2026, 8, day, hour, 0, tzinfo=timezone.utc).isoformat()


def _row(symbol, *, action="buy", reasoning="", order_status=None,
         qty=None, fill_price=None, notional=None):
    return DecisionRow(
        symbol=symbol, quant_score=0.5, llm_stance="bullish", llm_confidence=0.8,
        llm_rationale=None, combined_score=0.6, action=action, reasoning=reasoning,
        order_status=order_status, order_qty=qty, fill_price=fill_price,
        notional=notional, atr14=2.0,
    )


@pytest.fixture()
def main_db(tmp_path):
    """Main book: 22 daily cycles (Aug 4–25), two holdings, veto history."""
    path = tmp_path / "main.db"
    conn = connect(path)  # journal's own schema + writers build everything
    try:
        for i in range(22):
            equity = 100_000 + i * 1_000
            rows = []
            if i == 21:  # latest cycle carries BBB's filled entry
                rows.append(_row("BBB", order_status="filled", qty=10.0,
                                 fill_price=500.0, notional=5_000.0))
            if i == 2:  # Aug 6 — older than the 7-day veto window
                rows.append(_row("HHH", reasoning=" Risk manager vetoed buy: exposure cap."))
            if i == 20:  # Aug 24 — inside the veto window
                rows += [
                    _row("CCC", reasoning=" Risk manager vetoed buy: sector cap."),
                    _row("DDD", order_status="rejected", reasoning=" broker said no"),
                    _row("EEE", reasoning=" Skipped: max_new_orders_per_cycle reached for this run."),
                    _row("FFF", order_status="intent", reasoning="Recorded as TradeIntent"),
                    _row("GGG", action="sell", order_status="rejected", reasoning=" SELL ORDER FAILED"),
                ]
            label = "risk_off" if i >= 20 else "neutral"
            score = -0.42 if i >= 20 else 0.10
            # "paper" is what a live cycle journals (run.py picks paper /
            # dry_run / backtest); the equity readers filter on it so a
            # rehearsal's fixed $100k account cannot become a day's equity.
            record_cycle(conn, _ts(4 + i), "paper",
                         equity, equity - 30_000, rows,
                         regime_score=score, regime_label=label)
        # Open book: AAA implied mark 1200 vs peak 1500 -> -20%; BBB 500 vs 600 -> -16.7%
        record_position_state(conn, "AAA", qty=10.0, market_value=12_000.0, timestamp=_ts(25))
        record_position_state(conn, "BBB", qty=10.0, market_value=5_000.0, timestamp=_ts(25))
        update_position_peak(conn, "AAA", 1_500.0, timestamp=_ts(20))
        update_position_peak(conn, "BBB", 600.0, timestamp=_ts(24))
        # Trim-dwell counter armed by two consecutive trigger readings.
        record_regime_dwell(conn, True, -0.42, "risk_off", _ts(24))
        record_regime_dwell(conn, True, -0.45, "risk_off", _ts(25))
    finally:
        conn.close()
    return path


@pytest.fixture()
def p2_db(tmp_path):
    """P2 clone: holds AAA (overlap) and DDD, but no cycles yet."""
    path = tmp_path / "p2.db"
    conn = connect(path)
    try:
        record_position_state(conn, "AAA", qty=5.0, market_value=6_000.0, timestamp=_ts(25))
        record_position_state(conn, "DDD", qty=8.0, market_value=4_000.0, timestamp=_ts(25))
    finally:
        conn.close()
    return path


def _report_path(out_dir):
    return out_dir / f"{TODAY.isoformat()}.md"


def test_report_generated_with_key_numbers(tmp_path, main_db, p2_db):
    out_dir = tmp_path / "daily"
    out = generate_report(main_db, p2_db, out_dir, today=TODAY, now_utc=NOW)
    assert out == _report_path(out_dir)
    text = out.read_text(encoding="utf-8")

    # headline: latest cycle equity/cash and exposure 30k/121k
    assert "$121,000.00" in text and "$91,000.00" in text
    assert "24.8%" in text
    # regime + dwell counter
    assert "RISK_OFF** (-0.42)" in text
    assert "连续触发 **2** 次" in text
    # peak drawdowns against the recorded high-water marks
    assert "| AAA | $1,500.00 | $1,200.00 | -20.0%" in text
    assert "| BBB | $600.00 | $500.00 | -16.7%" in text
    assert "口径说明" in text
    # veto counters: recent-7-days / total columns
    assert "| 风控否决买入（size/sector/exposure 等） | 1 | 2 |" in text
    assert "| 买单被券商拒绝 | 1 | 1 |" in text
    assert "| 卖出失败/被拒（仓位仍持有） | 1 | 1 |" in text
    assert "| 每周期新单上限跳过 | 1 | 1 |" in text
    assert "| BUY 转 TradeIntent 排队（非否决） | 1 | 1 |" in text
    # Intent-flush outcomes are journaled now, so they are counted rather than
    # declared uncountable — but the pre-2026-08-30 backlog is disclaimed.
    assert "未落库" not in text
    assert "| Chase 拦截（对比信号价）— intent 保留待下次扫描 | 0 | 0 | intent_events |" in text
    assert "intent_events 自 2026-08-30 起落库" in text

    # P2 section: {AAA,BBB} vs {AAA,DDD} -> 1 shared symbol, 1/max(2,2)=50%
    assert "持仓重叠：**1 个**（AAA）" in text
    assert "= **50.0%**" in text
    # P2 has no cycles: correlation must say insufficient, not invent a number
    assert "样本不足" in text
    assert "还没有交班卡" in text


def test_report_next_job_from_progress_card(tmp_path, main_db):
    from agentic_trading.progress import emit_progress

    emit_progress(
        book_id="p1", round_name="deep", status="ok", did=["cycle complete"],
        path=Path(main_db).parent / "progress.json",
        now=NOW,
    )
    out = generate_report(main_db, None, tmp_path / "daily", today=TODAY, now_utc=NOW)
    text = out.read_text(encoding="utf-8")
    assert "下一班（交班卡）" in text
    assert "上一轮：**deep**" in text
    assert "指令：" in text


def test_overlap_stats_values(main_db, p2_db):
    a, b = _connect_ro(main_db), _connect_ro(p2_db)
    try:
        syms_a, syms_b, inter, ratio = overlap_stats(a, b)
        assert syms_a == {"AAA", "BBB"}
        assert syms_b == {"AAA", "DDD"}
        assert inter == {"AAA"}
        assert ratio == pytest.approx(0.5)
        assert position_symbols(a) == {"AAA", "BBB"}
    finally:
        a.close()
        b.close()


def _seq_ts(n: int) -> str:
    """Nth calendar day from a fixed base — spans months, unlike _ts()."""
    return (datetime(2026, 5, 1, 14, 0, tzinfo=timezone.utc) + timedelta(days=n)).isoformat()


def test_correlation_insufficient_sample(tmp_path):
    # n aligned days yield n-1 returns, and the window is CORR_WINDOW returns.
    main_path, p2_path = tmp_path / "m.db", tmp_path / "p.db"
    mc, pc = connect(main_path), connect(p2_path)
    try:
        for day in range(CORR_WINDOW):  # 60 days -> only 59 returns (< 60)
            record_cycle(mc, _seq_ts(day), "paper", 100_000 + day, 50_000, [])
            record_cycle(pc, _seq_ts(day), "paper", 200_000 + day, 60_000, [])
    finally:
        mc.close()
        pc.close()
    ma, pa = _connect_ro(main_path), _connect_ro(p2_path)
    try:
        corr, n = equity_correlation(ma, pa)
        assert corr is None and n == CORR_WINDOW - 1
    finally:
        ma.close()
        pa.close()


def test_correlation_perfect(tmp_path):
    """P2 equity is a pure multiple of main's, so their daily returns are
    identical and the rolling Pearson must be exactly 1.0."""
    main_path, p2_path = tmp_path / "m.db", tmp_path / "p.db"
    mc, pc = connect(main_path), connect(p2_path)
    try:
        for day in range(CORR_WINDOW + 5):
            m_eq = 100_000 + day * 1_000
            record_cycle(mc, _seq_ts(day), "paper", m_eq, 40_000, [])
            record_cycle(pc, _seq_ts(day), "paper", 2 * m_eq, 80_000, [])
    finally:
        mc.close()
        pc.close()
    ma, pa = _connect_ro(main_path), _connect_ro(p2_path)
    try:
        corr, n = equity_correlation(ma, pa)
        assert n == CORR_WINDOW
        assert corr == pytest.approx(1.0)
    finally:
        ma.close()
        pa.close()


def test_correlation_is_on_returns_not_levels(tmp_path):
    """Two books drifting up on unrelated day-to-day paths must NOT read ~1.0.

    Correlating equity levels would: both series rise, so the shared trend alone
    pins the coefficient near 1 and would trip the >0.95 kill line in
    PAPER2-THEME-ROTATION.md §5 on arithmetic rather than on shared holdings."""
    main_path, p2_path = tmp_path / "m.db", tmp_path / "p.db"
    mc, pc = connect(main_path), connect(p2_path)
    try:
        m_eq = p_eq = 100_000.0
        for day in range(CORR_WINDOW + 5):
            record_cycle(mc, _seq_ts(day), "paper", m_eq, 40_000, [])
            record_cycle(pc, _seq_ts(day), "paper", p_eq, 80_000, [])
            # Same upward drift, opposite-phase wiggles around it.
            m_eq *= 1.001 + (0.004 if day % 2 else -0.004)
            p_eq *= 1.001 + (-0.004 if day % 2 else 0.004)
    finally:
        mc.close()
        pc.close()
    ma, pa = _connect_ro(main_path), _connect_ro(p2_path)
    try:
        corr, n = equity_correlation(ma, pa)
        assert n == CORR_WINDOW
        assert corr is not None and corr < -0.9  # anti-correlated returns
    finally:
        ma.close()
        pa.close()


def test_correlation_degenerate_flat_series(tmp_path):
    """A flat P2 book has zero return variance: no number may be fabricated."""
    main_path, p2_path = tmp_path / "m.db", tmp_path / "p.db"
    mc, pc = connect(main_path), connect(p2_path)
    try:
        for day in range(CORR_WINDOW + 5):
            record_cycle(mc, _seq_ts(day), "paper", 100_000 + day * 1_000, 40_000, [])
            record_cycle(pc, _seq_ts(day), "paper", 123_456.0, 60_000, [])  # flat
    finally:
        mc.close()
        pc.close()
    ma, pa = _connect_ro(main_path), _connect_ro(p2_path)
    try:
        corr, n = equity_correlation(ma, pa)
        assert corr is None and n == CORR_WINDOW
    finally:
        ma.close()
        pa.close()


def test_price_basis_fallbacks(tmp_path):
    """No live quotes: snapshot mark first, then fill price, then notional/qty."""
    path = tmp_path / "basis.db"
    conn = connect(path)
    try:
        record_cycle(conn, _ts(20), "paper", 100_000, 50_000, [
            _row("FILL", order_status="filled", qty=4.0, fill_price=250.0, notional=1_000.0),
            _row("NOTIONAL", order_status="accepted", qty=4.0, notional=1_000.0),
        ])
        record_position_state(conn, "SNAP", qty=2.0, market_value=300.0, timestamp=_ts(21))

        price, note = _price_basis(conn, "SNAP")
        assert price == 150.0 and "position_state" in note
        price, note = _price_basis(conn, "FILL")
        assert price == 250.0 and "fill_price" in note
        price, note = _price_basis(conn, "NOTIONAL")
        assert price == 250.0 and "notional" in note
        price, note = _price_basis(conn, "UNKNOWN")
        assert price is None and "无可用价格" in note
    finally:
        conn.close()


def test_png_written_or_skipped_gracefully(tmp_path, main_db):
    out_dir = tmp_path / "daily"
    out = generate_report(main_db, None, out_dir, today=TODAY, now_utc=NOW)
    text = out.read_text(encoding="utf-8")
    png = out_dir / f"{TODAY.isoformat()}.png"
    assert png.exists() or "matplotlib 不可用" in text


def test_missing_db_exits_cleanly(tmp_path, capsys):
    with pytest.raises(SystemExit) as excinfo:
        main(["--db", str(tmp_path / "nope.db"), "--out-dir", str(tmp_path / "out")])
    assert excinfo.value.code == 2
    assert "journal not found" in capsys.readouterr().out
    assert not (tmp_path / "out").exists() or not list((tmp_path / "out").iterdir())


def test_fills_section_degrades_without_inventing_fills():
    from agentic_trading.daily_report import _fills_section

    text = "\n".join(_fills_section("2026-09-04", None, "no ALPACA credentials"))
    assert "无法读取券商成交" in text
    assert "no ALPACA credentials" in text
    assert "`fill_price`" in text


def test_fills_section_lists_session_fills_and_closed_trips():
    from agentic_trading.daily_report import _fills_section

    fills = [
        {"symbol": "XOM", "side": "sell", "qty": 10.0, "price": 156.24,
         "notional": 1562.4, "transaction_time": "2026-08-27T13:44:00+00:00"},
        {"symbol": "XOM", "side": "buy", "qty": 10.0, "price": 166.25,
         "notional": 1662.5, "transaction_time": "2026-08-19T15:00:00+00:00"},
    ]
    text = "\n".join(_fills_section("2026-08-27", fills, None))
    assert "XOM" in text
    assert "SELL" in text
    assert "已实现盈亏" in text


def test_intent_events_are_counted(tmp_path):
    """Flush outcomes reach the veto table instead of only the run log."""
    from agentic_trading.journal.logger import record_intent_event

    path = tmp_path / "m.db"
    conn = connect(path)
    try:
        record_cycle(conn, _ts(25), "paper", 100_000, 50_000, [])
        # Two inside the 7-day window, one older than it.
        record_intent_event(conn, _ts(24), "VEEV", "sizing", deferred=True,
                            detail="only $0 available (limited by exposure budget)")
        record_intent_event(conn, _ts(24), "ANET", "sizing", deferred=True, detail="x")
        record_intent_event(conn, _ts(1), "HPE", "gap", deferred=False, detail="old")
    finally:
        conn.close()
    out = generate_report(path, None, tmp_path / "daily", today=TODAY, now_utc=NOW)
    text = out.read_text(encoding="utf-8")
    assert "| Flush 阶段 sizing 否决（敞口 / book 风险 / sector cap）— intent 保留 | 2 | 2 |" in text
    # The gap event is outside the 7-day window: recent 0, total 1.
    assert "intent 丢弃 | 0 | 1 |" in text
