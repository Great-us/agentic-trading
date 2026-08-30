"""Tests for the LLM shadow scorecard:

- bucket classification (veto / rescue) including threshold boundaries
- LLM signed-score mapping and sign-agreement tallies by confidence band
- post-hoc forward returns against monkeypatched price history
- single-symbol vs overall price-data failure handling
- small-sample discipline (<5 shows a count and no interpretation)
- threshold provenance (CLI override > config/risk.yaml > fallback)
- end-to-end read from a real journal file and CLI output writing
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pandas as pd
import pytest

from agentic_trading import scorecard
from agentic_trading.journal.logger import DecisionRow, connect, record_cycle
from agentic_trading.scorecard import (
    Case, agreement_stats, attach_returns, classify, forward_returns,
    llm_signed_score, load_cases, render_report, resolve_threshold, sign,
)

THRESHOLD = 0.35


def _row(symbol, *, quant, stance, conf, combined, action):
    return DecisionRow(
        symbol=symbol, quant_score=quant, llm_stance=stance, llm_confidence=conf,
        llm_rationale="why", combined_score=combined, action=action, reasoning="test",
    )


def _case(*, symbol="AAA", ts="2026-03-02T15:00:00+00:00", quant=0.0, stance=None,
          conf=None, combined=0.0, action="hold"):
    return Case(
        decided_at=datetime.fromisoformat(ts), symbol=symbol, quant_score=quant,
        llm_stance=stance, llm_confidence=conf, combined_score=combined, action=action,
    )


# --- bucket classification ------------------------------------------------------

def test_veto_requires_bearish_stance_and_non_buy_action():
    assert classify(_case(quant=0.50, stance="bearish", conf=0.7, combined=0.20,
                          action="hold"), THRESHOLD) == "veto"
    # Final action buy means the LLM did not actually veto.
    assert classify(_case(quant=0.50, stance="bearish", conf=0.7, combined=0.20,
                          action="buy"), THRESHOLD) is None
    assert classify(_case(quant=0.50, stance="bullish", conf=0.7, combined=0.55,
                          action="hold"), THRESHOLD) is None


def test_rescue_needs_combined_above_and_quant_below():
    assert classify(_case(quant=0.30, stance="bullish", conf=0.9, combined=0.40,
                          action="buy"), THRESHOLD) == "rescue"
    assert classify(_case(quant=0.30, stance="bullish", conf=0.9, combined=0.30,
                          action="buy"), THRESHOLD) is None


def test_threshold_boundaries_are_inclusive_on_the_upper_sides():
    # Veto uses >= : quant exactly at the threshold still qualifies.
    assert classify(_case(quant=0.35, stance="bearish", conf=0.7, combined=0.10,
                          action="wait"), THRESHOLD) == "veto"
    # Rescue's quant leg is strict <: quant at the threshold cannot be "below".
    assert classify(_case(quant=0.35, stance="bullish", conf=0.9, combined=0.36,
                          action="buy"), THRESHOLD) is None
    assert classify(_case(quant=0.3499, stance="bullish", conf=0.9, combined=0.36,
                          action="buy"), THRESHOLD) == "rescue"


# --- agreement baseline -----------------------------------------------------------

def test_llm_signed_score_maps_stance_times_confidence():
    assert llm_signed_score("bullish", 0.8) == pytest.approx(0.8)
    assert llm_signed_score("bearish", 0.4) == pytest.approx(-0.4)
    assert llm_signed_score("neutral", 0.9) == 0.0
    assert llm_signed_score(None, 0.9) is None
    assert llm_signed_score("unknown", 0.9) is None


def test_sign_is_three_valued():
    assert sign(0.5) == 1
    assert sign(-0.1) == -1
    assert sign(0.0) == 0


def test_agreement_stats_split_by_the_confidence_boundary():
    cases = [
        _case(symbol="A", quant=0.5, stance="bullish", conf=0.80),   # agree, high band
        _case(symbol="B", quant=-0.5, stance="bullish", conf=0.60),  # boundary: 0.6 is high band; disagree
        _case(symbol="C", quant=-0.5, stance="bearish", conf=0.59),  # agree, low band
        _case(symbol="D", quant=0.5, stance="neutral", conf=0.90),   # llm 0.0 vs +0.5: disagree
        _case(symbol="E", quant=0.5, stance=None, conf=None),        # unrated: skipped
    ]
    stats = agreement_stats(cases)
    assert stats["all"] == (2, 4)
    # High band holds A, B and D (its confidence is 0.90); only C falls below 0.6.
    assert stats["high"] == (1, 3)
    assert stats["low"] == (1, 1)


# --- forward returns ----------------------------------------------------------------

def test_forward_returns_baseline_is_the_first_session_after_the_decision():
    # 2026-03-01 is a Sunday; bars start Monday 2026-03-02. Closes rise by 1/day.
    idx = pd.bdate_range("2026-03-02", periods=7)
    close = pd.Series([10.0, 11.0, 12.0, 13.0, 14.0, 15.0, 16.0], index=idx)
    decided = datetime(2026, 3, 1, 22, 0, tzinfo=timezone.utc)
    r5, r20 = forward_returns(close, decided)
    assert r5 == pytest.approx(15.0 / 10.0 - 1.0)   # 5 sessions after the baseline close
    assert r20 is None                               # not enough future sessions


def test_forward_returns_all_na_when_decision_postdates_every_bar():
    idx = pd.bdate_range("2026-03-02", periods=3)
    close = pd.Series([10.0, 11.0, 12.0], index=idx)
    decided = datetime(2026, 3, 6, 22, 0, tzinfo=timezone.utc)
    assert forward_returns(close, decided) == (None, None)


# --- price attachment -----------------------------------------------------------------

def _frame(days):
    """Linear closes (100 + i) indexed by consecutive business days."""
    idx = pd.bdate_range("2026-01-05", periods=days)
    return pd.DataFrame({"Close": [100.0 + i for i in range(days)]}, index=idx)


def test_attach_returns_fills_horizons_and_survives_one_bad_symbol(monkeypatch):
    good = _frame(60)

    def fake_loader(symbol):
        if symbol == "BAD":
            raise RuntimeError("network down")
        return good

    monkeypatch.setattr(scorecard, "_load_price_history", fake_loader)
    # Decision Friday 2026-01-09 21:00 UTC -> baseline is Monday's close (105).
    cases = [
        _case(symbol="AAA", ts="2026-01-09T21:00:00+00:00"),
        _case(symbol="BAD", ts="2026-01-09T21:00:00+00:00"),
    ]
    assert attach_returns(cases) is True   # partial failure, not an outage
    assert cases[0].ret_5d == pytest.approx(110.0 / 105.0 - 1.0)
    assert cases[0].ret_20d == pytest.approx(125.0 / 105.0 - 1.0)
    assert cases[1].ret_5d is None and cases[1].ret_20d is None
    assert cases[1].price_error is not None


def test_total_price_failure_is_flagged_and_marked_in_the_report(monkeypatch, tmp_path):
    def boom(_symbol):
        raise RuntimeError("offline")

    monkeypatch.setattr(scorecard, "_load_price_history", boom)
    cases = [_case(symbol="AAA"), _case(symbol="BBB")]
    assert attach_returns(cases) is False
    text = render_report(cases, THRESHOLD, "test-source", tmp_path / "j.db", prices_ok=False)
    assert "价格数据不可用" in text


# --- small-sample discipline ------------------------------------------------------------

def test_small_bucket_shows_count_only(monkeypatch, tmp_path):
    monkeypatch.setattr(scorecard, "_load_price_history", lambda _symbol: _frame(60))
    cases = [
        _case(symbol="AAA", quant=0.50, stance="bearish", conf=0.7, combined=0.20, action="hold"),
        _case(symbol="BBB", quant=0.45, stance="bearish", conf=0.8, combined=0.15, action="wait"),
    ]
    attach_returns(cases)
    text = render_report(cases, THRESHOLD, "test-source", tmp_path / "j.db", prices_ok=True)
    veto_section = text.split("## 桶一")[1].split("\n## ")[0]
    assert "样本不足，不解读" in veto_section
    assert "5日" not in veto_section, "a sub-minimum bucket must not show returns to interpret"


def test_disclosure_and_threshold_source_always_present(tmp_path):
    text = render_report([], THRESHOLD, "config/risk.yaml: buy_threshold",
                         tmp_path / "j.db", prices_ok=False)
    assert "本报告是相关性观察，不是调参依据；两本书均处观察期，6 个月判读协议不变。" in text
    assert "buy_threshold" in text


# --- threshold provenance ------------------------------------------------------------------

def test_resolve_threshold_prefers_cli_then_config_then_fallback(monkeypatch):
    value, source = resolve_threshold(0.40)
    assert value == 0.40 and "--threshold" in source

    fake = SimpleNamespace(risk=SimpleNamespace(buy_threshold=0.33))
    monkeypatch.setattr(scorecard, "load_settings", lambda **_kwargs: fake)
    value, source = resolve_threshold(None)
    assert value == 0.33 and "risk.yaml" in source

    def broken(**_kwargs):
        raise RuntimeError("no config dir")

    monkeypatch.setattr(scorecard, "load_settings", broken)
    value, source = resolve_threshold(None)
    assert value == 0.35 and "默认值" in source


# --- end-to-end through a real journal file ---------------------------------------------------

def test_load_cases_classifies_rows_from_a_real_journal(tmp_path):
    db_path = tmp_path / "journal.db"
    conn = connect(db_path)
    try:
        record_cycle(conn, "2026-03-02T15:00:00+00:00", "deep", 100000.0, 50000.0, [
            _row("VETO", quant=0.50, stance="bearish", conf=0.70, combined=0.20, action="hold"),
            _row("RESQ", quant=0.30, stance="bullish", conf=0.90, combined=0.40, action="buy"),
        ])
        record_cycle(conn, "2026-03-03T15:00:00+00:00", "deep", 101000.0, 49000.0, [
            _row("VETO", quant=0.52, stance="bearish", conf=0.60, combined=0.18, action="wait"),
        ])
    finally:
        conn.close()
    kinds = [classify(case, THRESHOLD) for case in load_cases(db_path)]
    assert kinds.count("veto") == 2
    assert kinds.count("rescue") == 1
    assert len(kinds) == 3


def test_main_writes_a_report_file(tmp_path, monkeypatch):
    db_path = tmp_path / "journal.db"
    conn = connect(db_path)
    try:
        record_cycle(conn, "2026-03-02T15:00:00+00:00", "deep", 100000.0, 50000.0, [
            _row(f"S{i}", quant=0.40 + i * 0.01, stance="bearish", conf=0.70,
                 combined=0.20, action="hold") for i in range(6)
        ])
    finally:
        conn.close()
    monkeypatch.setattr(scorecard, "_load_price_history", lambda _symbol: _frame(60))
    out_path = tmp_path / "research" / "scorecard.md"
    scorecard.main(["--db", str(db_path), "--out", str(out_path), "--threshold", str(THRESHOLD)])
    text = out_path.read_text(encoding="utf-8")
    assert "LLM 影子计分报告" in text
    assert "样本数：6" in text          # bucket large enough to be interpreted
    assert "| 决策时间 (UTC)" in text   # case table rendered with returns columns
