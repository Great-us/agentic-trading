from datetime import datetime, timezone

import numpy as np
import pandas as pd

from agentic_trading.journal.evaluate import evaluate_journal, format_report, forward_stats
from agentic_trading.journal.logger import DecisionRow, connect, record_cycle
from agentic_trading.llm.schema import news_age_hours


def _bars(n=40, start="2020-01-02"):
    idx = pd.bdate_range(start, periods=n)
    close = 100.0 + np.arange(n, dtype=float)
    return pd.DataFrame(
        {"Open": close - 0.1, "High": close + 2.0, "Low": close - 1.0,
         "Close": close, "Volume": 1e6},
        index=idx,
    )


def test_forward_stats_match_a_known_path():
    df = _bars(35)
    asof = df.index[10]
    stats = forward_stats(df, asof)
    c0 = float(df["Close"].iloc[10])
    assert abs(stats["ret_1d"] - (float(df["Close"].iloc[11]) / c0 - 1)) < 1e-9
    assert abs(stats["ret_5d"] - (float(df["Close"].iloc[15]) / c0 - 1)) < 1e-9
    assert abs(stats["ret_20d"] - (float(df["Close"].iloc[30]) / c0 - 1)) < 1e-9
    future = df.iloc[11:31]
    assert abs(stats["mfe_20d"] - (float(future["High"].max()) / c0 - 1)) < 1e-9
    assert abs(stats["mae_20d"] - (float(future["Low"].min()) / c0 - 1)) < 1e-9


def test_forward_stats_none_when_horizon_missing():
    df = _bars(12)
    stats = forward_stats(df, df.index[10])
    assert stats["ret_1d"] is not None
    assert stats["ret_20d"] is None


def test_evaluate_journal_writes_outcomes_for_a_buy():
    df = _bars(30)
    conn = connect(":memory:")
    asof = df.index[10]
    record_cycle(
        conn, asof.isoformat(), "backtest", 100_000, 100_000,
        [DecisionRow(symbol="AAA", quant_score=0.5, llm_stance="bullish",
                     llm_confidence=0.8, llm_rationale="t", combined_score=0.5,
                     action="buy", reasoning="t", llm_risk_flags="earnings in 3 days")],
    )
    rows = evaluate_journal(conn, bars={"AAA": df})
    assert len(rows) == 1
    assert rows[0].ret_1d is not None
    stored = conn.execute("SELECT ret_1d FROM signal_outcomes").fetchone()
    assert stored is not None and stored[0] == rows[0].ret_1d
    text = format_report(rows)
    assert "n<5" in text or "Forward outcomes" in text


def test_evaluate_empty_journal_is_quiet():
    conn = connect(":memory:")
    rows = evaluate_journal(conn, bars={})
    assert rows == []
    assert "No buy/sell/trim/wait/hold/avoid" in format_report(rows)


def _decision(symbol, action, **kwargs):
    defaults = dict(
        quant_score=0.2, llm_stance=None, llm_confidence=None,
        llm_rationale="t", combined_score=0.2, reasoning="t",
    )
    defaults.update(kwargs)
    return DecisionRow(symbol=symbol, action=action, **defaults)


def _falling_bars(n=40, start="2020-01-02"):
    idx = pd.bdate_range(start, periods=n)
    close = 100.0 - np.arange(n, dtype=float)
    return pd.DataFrame(
        {"Open": close + 0.1, "High": close + 1.0, "Low": close - 2.0,
         "Close": close, "Volume": 1e6},
        index=idx,
    )


def test_avoid_and_hold_decisions_get_the_same_forward_outcomes():
    up, down = _bars(40), _falling_bars(40)
    asof = up.index[10]
    conn = connect(":memory:")
    record_cycle(
        conn, asof.isoformat(), "live", 100_000, 100_000,
        [
            _decision("ROSE", "avoid"),    # avoided a name that ran: bad avoid
            _decision("FELL", "avoid"),    # avoided a name that fell: good avoid
            _decision("KEPT", "hold"),     # held a name that fell: bad hold
        ],
    )
    rows = evaluate_journal(conn, bars={"ROSE": up, "FELL": down, "KEPT": down})
    assert {r.symbol: r.action for r in rows} == {
        "ROSE": "avoid", "FELL": "avoid", "KEPT": "hold",
    }
    by_symbol = {r.symbol: r for r in rows}
    assert by_symbol["ROSE"].ret_20d is not None and by_symbol["ROSE"].ret_20d > 0
    assert by_symbol["FELL"].ret_20d is not None and by_symbol["FELL"].ret_20d < 0
    assert by_symbol["KEPT"].ret_20d is not None and by_symbol["KEPT"].ret_20d < 0
    stored = conn.execute(
        "SELECT d.action, COUNT(*) FROM signal_outcomes s"
        " JOIN decisions d ON d.id = s.decision_id GROUP BY d.action"
    ).fetchall()
    assert dict(stored) == {"avoid": 2, "hold": 1}


def _outcome(action, ret_20d):
    from agentic_trading.journal.evaluate import OutcomeRow
    return OutcomeRow(
        decision_id=1, symbol="AAA", action=action, quant_score=0.2,
        llm_stance=None, llm_confidence=None, llm_risk_flags=None,
        asof="2026-01-05", ret_1d=ret_20d / 3, ret_5d=ret_20d / 2,
        ret_20d=ret_20d, mfe_20d=abs(ret_20d), mae_20d=-abs(ret_20d),
    )


def test_format_report_shows_avoid_and_hold_buckets():
    rows = [_outcome("avoid", -0.02) for _ in range(6)]
    rows += [_outcome("hold", 0.01) for _ in range(6)]
    text = format_report(rows)
    assert "action=avoid" in text and "action=hold" in text
    assert "avoid was right" in text  # the reading note ships with the numbers
    # Small n stays honest: 4 avoids is below MIN_REPORT_N, no averages shown.
    small = format_report([_outcome("avoid", -0.02) for _ in range(4)])
    assert "action=avoid" in small and "n<5" in small


def test_news_age_hours_iso():
    now = datetime(2026, 8, 21, 16, 0, tzinfo=timezone.utc)
    assert abs(news_age_hours("2026-08-21T14:00:00+00:00", now) - 2.0) < 1e-6
    assert news_age_hours("", now) is None


def test_format_report_source_buckets():
    from agentic_trading.journal.evaluate import OutcomeRow, format_report

    def row(symbol):
        return OutcomeRow(
            decision_id=1, symbol=symbol, action="buy", quant_score=0.4,
            llm_stance=None, llm_confidence=None, llm_risk_flags=None,
            asof="2026-01-05", ret_1d=0.01, ret_5d=0.02, ret_20d=0.03,
            mfe_20d=0.05, mae_20d=-0.01,
        )

    rows = [row("MU"), row("NVDA"), row("BOTZ")]
    text = format_report(rows, sources={"MU": "core", "NVDA": "core", "BOTZ": "vault"})
    assert "source=core" in text and "source=vault" in text
    # n<MIN_REPORT_N discipline still applies per bucket
    assert "n<5" in text
    # No sources provided -> no breakdown section at all
    plain = format_report(rows)
    assert "source=" not in plain
