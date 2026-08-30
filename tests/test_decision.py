from agentic_trading.decision.engine import Action, decide
from agentic_trading.llm.analyst import AnalystVerdict
from agentic_trading.signals.macro import MacroRegime
from agentic_trading.signals.technical import QuantSignal

MIN_SCORE = 0.15


def make_signal(score: float, extended: bool = False) -> QuantSignal:
    return QuantSignal(
        symbol="TEST", score=score, last_price=100.0, sma20=101.0, sma50=99.0,
        rsi14=55.0, momentum_20d_pct=2.0, atr14=1.5, volatility_annualized_pct=20.0,
        extended=extended,
    )


def make_verdict(stance: str, confidence: float) -> AnalystVerdict:
    return AnalystVerdict(symbol="TEST", stance=stance, confidence=confidence, rationale="test", risk_flags=[])


def test_buy_on_strong_quant_alone_no_position():
    d = decide(make_signal(0.8), None, has_open_position=False, min_quant_score_to_consider=MIN_SCORE)
    assert d.action == Action.BUY


def test_avoid_below_noise_floor():
    d = decide(make_signal(0.1), None, has_open_position=False, min_quant_score_to_consider=MIN_SCORE)
    assert d.action == Action.AVOID


def test_avoid_when_combined_score_below_buy_threshold():
    # Quant clears the noise floor but not the (higher) buy threshold.
    d = decide(make_signal(0.20), None, has_open_position=False, min_quant_score_to_consider=MIN_SCORE)
    assert d.action == Action.AVOID


def test_conflicting_signals_block_new_buy():
    d = decide(make_signal(0.8), make_verdict("bearish", 0.9), has_open_position=False, min_quant_score_to_consider=MIN_SCORE)
    assert d.action == Action.AVOID
    assert d.conflicting_signals is True


def test_conflicting_signals_hold_existing_position():
    d = decide(make_signal(0.8), make_verdict("bearish", 0.9), has_open_position=True, min_quant_score_to_consider=MIN_SCORE)
    assert d.action == Action.HOLD
    assert d.conflicting_signals is True


def test_llm_cannot_originate_an_entry():
    # Quant 0.20 clears the noise floor and a bullish LLM would lift combined
    # over 0.25 — that used to be a BUY. LLM is a brake, not ignition.
    d = decide(make_signal(0.20), make_verdict("bullish", 0.9),
               has_open_position=False, min_quant_score_to_consider=MIN_SCORE)
    assert d.action == Action.AVOID
    assert "cannot originate" in d.reasoning


def test_unknown_regime_blocks_new_entries():
    unknown = MacroRegime(score=0.0, label="unknown")
    d = decide(make_signal(0.8), None, has_open_position=False,
               min_quant_score_to_consider=MIN_SCORE, regime=unknown)
    assert d.action == Action.AVOID
    assert "UNKNOWN" in d.reasoning


def test_unknown_regime_does_not_block_an_exit():
    unknown = MacroRegime(score=0.0, label="unknown")
    d = decide(make_signal(-0.6), None, has_open_position=True,
               min_quant_score_to_consider=MIN_SCORE, regime=unknown)
    assert d.action == Action.SELL


def test_agreement_raises_conviction_vs_quant_alone():
    quant_only = decide(make_signal(0.4), None, has_open_position=False, min_quant_score_to_consider=MIN_SCORE)
    with_llm = decide(make_signal(0.4), make_verdict("bullish", 0.9), has_open_position=False, min_quant_score_to_consider=MIN_SCORE)
    assert with_llm.combined_score > quant_only.combined_score


def test_sell_on_weak_score_with_open_position():
    d = decide(make_signal(-0.6), None, has_open_position=True, min_quant_score_to_consider=MIN_SCORE)
    assert d.action == Action.SELL


def test_llm_cannot_originate_an_exit():
    # Quant is only mildly negative; a bearish LLM would drag combined under
    # -0.25. That used to be a SELL. LLM is a brake, not ignition, on exits too.
    d = decide(make_signal(-0.10), make_verdict("bearish", 0.9),
               has_open_position=True, min_quant_score_to_consider=MIN_SCORE)
    assert d.action == Action.HOLD
    assert "cannot originate an exit" in d.reasoning
    assert d.combined_score < -0.25


def test_quant_sell_still_fires_when_llm_is_cheerful():
    d = decide(make_signal(-0.6), make_verdict("bullish", 0.9),
               has_open_position=True, min_quant_score_to_consider=MIN_SCORE)
    assert d.action == Action.SELL


def test_hold_when_open_position_and_score_between_thresholds():
    d = decide(make_signal(-0.1), None, has_open_position=True, min_quant_score_to_consider=MIN_SCORE)
    assert d.action == Action.HOLD


def test_llm_unavailable_falls_back_to_quant_only():
    d = decide(make_signal(0.5), None, has_open_position=False, min_quant_score_to_consider=MIN_SCORE)
    assert d.llm_score is None
    assert d.combined_score == 0.5


# --- extended / "don't chase" ----------------------------------------------

def test_extended_signal_waits_instead_of_chasing():
    d = decide(make_signal(0.8, extended=True), None, has_open_position=False, min_quant_score_to_consider=MIN_SCORE)
    assert d.action == Action.WAIT
    assert "poor entry" in d.reasoning


def test_extended_does_not_block_holding_an_existing_position():
    d = decide(make_signal(0.8, extended=True), None, has_open_position=True, min_quant_score_to_consider=MIN_SCORE)
    assert d.action == Action.HOLD


# --- market regime ---------------------------------------------------------

RISK_OFF = MacroRegime(score=-0.45, label="risk_off")
RISK_ON = MacroRegime(score=0.45, label="risk_on")


def test_risk_off_regime_raises_the_entry_bar():
    # 0.30 clears the normal 0.25 threshold but not 0.25 + 0.15 penalty.
    d = decide(make_signal(0.30), None, has_open_position=False, min_quant_score_to_consider=MIN_SCORE,
               regime=RISK_OFF, risk_off_score_penalty=0.15)
    assert d.action == Action.AVOID
    assert "Risk-off" in d.reasoning


def test_strong_signal_still_buys_in_risk_off():
    d = decide(make_signal(0.8), None, has_open_position=False, min_quant_score_to_consider=MIN_SCORE,
               regime=RISK_OFF, risk_off_score_penalty=0.15)
    assert d.action == Action.BUY


def test_risk_on_regime_uses_the_normal_bar():
    d = decide(make_signal(0.30), None, has_open_position=False, min_quant_score_to_consider=MIN_SCORE,
               regime=RISK_ON, risk_off_score_penalty=0.15)
    assert d.action == Action.BUY


def test_risk_off_never_blocks_an_exit():
    d = decide(make_signal(-0.6), None, has_open_position=True, min_quant_score_to_consider=MIN_SCORE,
               regime=RISK_OFF, risk_off_score_penalty=0.15)
    assert d.action == Action.SELL
