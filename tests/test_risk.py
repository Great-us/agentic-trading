from agentic_trading.config import RiskConfig
from agentic_trading.risk.manager import (
    check_exit, protective_stop_price, size_position, stop_distance_pct,
)

RISK = RiskConfig(
    risk_per_trade_pct=0.015,
    max_position_pct=0.18,
    min_position_pct=0.04,
    max_total_exposure_pct=0.95,
    max_open_positions=6,
    max_new_orders_per_cycle=5,
    min_quant_score_to_consider=0.15,
    atr_stop_multiple=2.5,
    min_stop_pct=0.06,
    max_stop_pct=0.20,
    trailing_stop_pct=0.12,
    risk_off_size_multiplier=0.5,
    risk_off_score_penalty=0.15,
    escalation_cooldown_minutes=60,
    escalation_cooldown_score_delta=0.15,
    take_profit_pct=None,
)


# --- position sizing (equal-risk) ------------------------------------------

def _size(stop_pct, equity=100_000, cash=100_000, invested=0, held=0, mult=1.0):
    return size_position("TEST", 100.0, equity=equity, cash=cash, invested_value=invested,
                         open_position_count=held, risk=RISK, stop_pct=stop_pct, regime_multiplier=mult)


def test_wide_stop_gets_a_smaller_position_than_a_tight_one():
    tight = _size(0.06)   # quiet name
    wide = _size(0.20)    # 90%-volatility name
    assert wide.notional < tight.notional


def test_dollar_risk_is_equalised_across_volatility():
    # The point of the formula: hitting the stop costs about the same either way.
    for stop in (0.10, 0.15, 0.20):
        r = _size(stop)
        assert abs(r.notional * stop - 100_000 * RISK.risk_per_trade_pct) < 1.0


def test_size_is_capped_by_max_position_pct():
    # 1.5% risk / 6% stop = 25% of equity, which the 18% cap must clamp.
    r = _size(0.06)
    assert r.notional == 18_000
    assert "capped" in r.reason


def test_size_position_respects_cash_constraint():
    r = _size(0.10, cash=500)
    assert r.approved is False  # $500 is below the 4% minimum on a 100k account


def test_size_position_respects_exposure_budget():
    # 93% already invested; the 95% cap leaves 2% of equity of room, under the
    # 4% minimum position, so the trade is declined rather than sized to dust.
    r = _size(0.10, cash=10_000, invested=93_000, held=1)
    assert r.approved is False
    assert "minimum position" in r.reason


def test_size_position_vetoes_at_max_open_positions():
    r = _size(0.10, held=RISK.max_open_positions)
    assert r.approved is False
    assert "max_open_positions" in r.reason


def test_risk_off_regime_halves_position_size():
    full = _size(0.12)
    cut = _size(0.12, mult=0.5)
    assert cut.notional == full.notional * 0.5
    assert "risk-off" in cut.reason


def test_size_position_rejects_an_invalid_stop():
    assert _size(0.0).approved is False


# --- ATR-derived stop distance ---------------------------------------------

def test_stop_distance_scales_with_atr():
    quiet = stop_distance_pct(atr14=1.0, last_price=100.0, risk=RISK)   # 2.5% raw -> floor
    wild = stop_distance_pct(atr14=5.0, last_price=100.0, risk=RISK)    # 12.5% raw
    assert quiet == RISK.min_stop_pct
    assert wild == 0.125
    assert wild > quiet


def test_stop_distance_respects_bounds():
    assert stop_distance_pct(atr14=50.0, last_price=100.0, risk=RISK) == RISK.max_stop_pct
    assert stop_distance_pct(atr14=0.0, last_price=100.0, risk=RISK) == RISK.min_stop_pct
    assert stop_distance_pct(atr14=1.0, last_price=0.0, risk=RISK) == RISK.min_stop_pct


# --- exit checks -----------------------------------------------------------

def test_atr_stop_triggers_on_volatile_name():
    # ATR 5 on a 100 stock -> 12.5% stop; -13% breaches it.
    exit_signal = check_exit(entry_price=100.0, last_price=87.0, high_water_mark=100.0, atr14=5.0, risk=RISK)
    assert exit_signal is not None and exit_signal.trigger == "stop_loss"


def test_volatile_name_survives_noise_that_a_fixed_5pct_stop_would_cut():
    # -8% on a 12.5%-stop name is normal noise, not an exit. Under the old fixed
    # 5% stop this position would have been closed.
    assert check_exit(entry_price=100.0, last_price=92.0, high_water_mark=100.0, atr14=5.0, risk=RISK) is None


def test_trailing_stop_triggers_after_giving_back_gains():
    # Ran to 150, fell to 130 = -13.3% from peak, still +30% on the trade.
    exit_signal = check_exit(entry_price=100.0, last_price=130.0, high_water_mark=150.0, atr14=2.0, risk=RISK)
    assert exit_signal is not None and exit_signal.trigger == "trailing_stop"


def test_trailing_stop_lets_winners_run():
    # +50% and holding near the high: no exit, which is the whole point of
    # dropping the fixed take-profit.
    assert check_exit(entry_price=100.0, last_price=148.0, high_water_mark=150.0, atr14=2.0, risk=RISK) is None


def test_trailing_stop_does_not_fire_below_entry():
    # Never rose above entry, so the fixed stop governs, not the trailing one.
    exit_signal = check_exit(entry_price=100.0, last_price=93.0, high_water_mark=100.0, atr14=1.0, risk=RISK)
    assert exit_signal is not None and exit_signal.trigger == "stop_loss"


def test_take_profit_disabled_by_default():
    assert check_exit(entry_price=100.0, last_price=140.0, high_water_mark=140.0, atr14=2.0, risk=RISK) is None


def test_take_profit_fires_when_configured():
    risk = RiskConfig(**{**RISK.__dict__, "take_profit_pct": 0.15})
    exit_signal = check_exit(entry_price=100.0, last_price=120.0, high_water_mark=120.0, atr14=2.0, risk=risk)
    assert exit_signal is not None and exit_signal.trigger == "take_profit"


def test_no_exit_within_normal_range():
    assert check_exit(entry_price=100.0, last_price=103.0, high_water_mark=104.0, atr14=2.0, risk=RISK) is None


# --- broker-side protective stop level -------------------------------------

def test_stop_price_uses_atr_level_early_in_a_trade():
    # No gain yet, so the ATR stop (5% below entry with ATR 2) governs; the
    # trailing level (12% below entry) sits lower and is ignored.
    price = protective_stop_price(entry_price=100.0, high_water_mark=100.0, atr14=2.0, risk=RISK)
    assert price == 94.0  # ATR 2 x 2.5 = 5 -> but floor is 6% -> 94.00


def test_stop_price_ratchets_up_as_the_position_runs():
    early = protective_stop_price(entry_price=100.0, high_water_mark=100.0, atr14=2.0, risk=RISK)
    later = protective_stop_price(entry_price=100.0, high_water_mark=150.0, atr14=2.0, risk=RISK)
    assert later > early
    assert later == round(150.0 * 0.88, 2)  # 12% below the peak


def test_stop_price_never_drops_below_the_atr_floor():
    # Peak barely above entry: the trailing level would sit under the ATR stop,
    # so the tighter ATR level must win rather than loosening protection.
    price = protective_stop_price(entry_price=100.0, high_water_mark=101.0, atr14=2.0, risk=RISK)
    assert price == 94.0


def test_stop_price_widens_for_a_volatile_name():
    quiet = protective_stop_price(entry_price=100.0, high_water_mark=100.0, atr14=1.0, risk=RISK)
    wild = protective_stop_price(entry_price=100.0, high_water_mark=100.0, atr14=6.0, risk=RISK)
    assert wild < quiet  # a wider stop sits further below entry


def test_stop_price_is_penny_rounded():
    price = protective_stop_price(entry_price=173.33, high_water_mark=191.77, atr14=3.1, risk=RISK)
    assert price == round(price, 2)
