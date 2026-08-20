import pandas as pd

from agentic_trading.signals.macro import MacroRegime, _ratio_score, _vix_adjustment


def _series(values: list[float]) -> pd.DataFrame:
    return pd.DataFrame({"Close": values})


def test_rising_ratio_scores_positive():
    # Numerator steadily outpacing denominator = risk-on.
    num = _series([100 + i * 0.5 for i in range(80)])
    den = _series([100.0] * 80)
    assert _ratio_score(num, den) > 0.2


def test_falling_ratio_scores_negative():
    num = _series([100 - i * 0.5 for i in range(80)])
    den = _series([100.0] * 80)
    assert _ratio_score(num, den) < -0.2


def test_flat_ratio_scores_near_zero():
    flat = _series([100.0] * 80)
    assert abs(_ratio_score(flat, _series([100.0] * 80))) < 0.05


def test_ratio_score_needs_enough_history():
    short = _series([100.0] * 30)
    assert _ratio_score(short, short) is None


def test_ratio_score_handles_empty_input():
    assert _ratio_score(pd.DataFrame(), _series([100.0] * 80)) is None


def test_calm_vix_is_not_a_bonus():
    adjustment, _ = _vix_adjustment(12.0)
    assert adjustment == 0.0


def test_stressed_vix_penalises():
    adjustment, note = _vix_adjustment(35.0)
    assert adjustment == -0.5
    assert "stressed" in note


def test_elevated_vix_scales_between():
    adjustment, _ = _vix_adjustment(24.0)
    assert -0.5 < adjustment < 0.0


def test_regime_label_helpers():
    assert MacroRegime(score=-0.4, label="risk_off").is_risk_off is True
    assert MacroRegime(score=0.4, label="risk_on").is_risk_off is False
