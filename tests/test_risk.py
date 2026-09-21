from dataclasses import replace

import pandas as pd

from agentic_trading.config import RiskConfig
from agentic_trading.execution.broker import Position
from agentic_trading.risk.manager import (
    SizingDiagnostics, average_corr_to_holdings, check_exit,
    effective_max_exposure_pct, plan_trims, portfolio_stop_risk,
    protective_stop_price, sector_of, sector_room_dollars, size_position,
    stop_distance_pct,
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


def test_portfolio_stop_cap_shrinks_a_new_entry():
    # 5.8% of equity already at risk; 6% cap leaves 0.2% = $200, so a 10% stop
    # can only be $2,000 — below the 4% minimum, so the trade is declined.
    r = size_position(
        "TEST", 100.0, equity=100_000, cash=100_000, invested_value=0,
        open_position_count=1, risk=RISK, stop_pct=0.10, existing_stop_risk=5_800,
    )
    assert r.approved is False
    assert "minimum position" in r.reason


def test_portfolio_stop_cap_leaves_room_when_book_is_empty():
    r = _size(0.10)
    assert r.approved is True
    assert abs(r.notional * 0.10 - 1_500) < 1.0


def test_correlation_haircut_halves_size():
    full = _size(0.12)
    cut = size_position(
        "TEST", 100.0, equity=100_000, cash=100_000, invested_value=0,
        open_position_count=0, risk=RISK, stop_pct=0.12, corr_multiplier=0.5,
    )
    assert cut.approved is True
    assert cut.notional == round(full.notional * 0.5, 2)
    assert "correlation" in cut.reason


def test_average_corr_is_one_for_identical_series():
    idx = pd.bdate_range("2020-01-02", periods=80)
    close = pd.Series(100 + pd.RangeIndex(80) * 0.3, index=idx)
    assert average_corr_to_holdings(close, {"A": close}) == 1.0


def test_average_corr_none_without_holdings():
    idx = pd.bdate_range("2020-01-02", periods=80)
    close = pd.Series(100.0, index=idx)
    assert average_corr_to_holdings(close, {}) is None


def test_portfolio_stop_risk_sums_open_positions():
    positions = {
        "A": Position("A", qty=100, avg_entry_price=100.0, current_price=100.0, market_value=10_000),
        "B": Position("B", qty=200, avg_entry_price=100.0, current_price=100.0, market_value=20_000),
    }
    # ATR 4 on a $100 name → 10% stop under 2.5× ATR.
    total = portfolio_stop_risk(positions, {"A": 4.0, "B": 4.0}, RISK)
    assert abs(total - 3_000.0) < 1e-6


def test_portfolio_stop_risk_skips_names_without_atr():
    positions = {
        "A": Position("A", qty=100, avg_entry_price=100.0, current_price=100.0, market_value=10_000),
    }
    assert portfolio_stop_risk(positions, {}, RISK) == 0.0


def test_regime_exposure_uses_the_label_table():
    assert effective_max_exposure_pct(RISK, "neutral") == 0.80
    assert effective_max_exposure_pct(RISK, "risk_off") == 0.55
    assert effective_max_exposure_pct(RISK, None) == RISK.max_total_exposure_pct


def test_sector_room_is_zero_when_the_cap_is_full():
    mapping = {"AAPL": "Technology", "MSFT": "Technology", "JNJ": "Healthcare"}
    occupancy = {"Technology": 35_000}
    room = sector_room_dollars("NVDA", 100_000, occupancy, mapping | {"NVDA": "Technology"}, RISK)
    assert room == 0.0
    room_jnj = sector_room_dollars("JNJ", 100_000, occupancy, mapping, RISK)
    assert room_jnj == 30_000


def test_unknown_sector_uses_the_default_cap():
    assert sector_of("ZZZ", {}) == "Unknown"
    room = sector_room_dollars("ZZZ", 100_000, {}, {}, RISK)
    assert room == 100_000 * RISK.default_max_sector_pct


def test_sector_room_shrinks_a_new_entry():
    full = _size(0.12)
    cut = size_position(
        "TEST", 100.0, equity=100_000, cash=100_000, invested_value=0,
        open_position_count=0, risk=RISK, stop_pct=0.12, sector_room=5_000,
    )
    assert cut.approved is True
    assert cut.notional == 5_000
    assert cut.notional < full.notional
    assert "sector" in cut.reason


def test_sector_room_zero_vetoes_below_minimum():
    r = size_position(
        "TEST", 100.0, equity=100_000, cash=100_000, invested_value=0,
        open_position_count=0, risk=RISK, stop_pct=0.10, sector_room=0.0,
    )
    assert r.approved is False


def test_plan_trims_weakest_first_down_to_the_cap():
    positions = {
        "WEAK": Position("WEAK", 400, 100, 100, 40_000),
        "STRONG": Position("STRONG", 600, 100, 100, 60_000),
    }
    plans = plan_trims(
        positions, {"WEAK": -0.2, "STRONG": 0.8},
        equity=100_000, invested_value=100_000,
        prices={"WEAK": 100.0, "STRONG": 100.0},
        risk=RISK, target_exposure_pct=0.55,
    )
    assert plans
    assert plans[0].symbol == "WEAK"
    sold = sum(p.notional for p in plans)
    assert sold >= 44_000  # 100k → 55k, excess 45k
    remaining = 100_000 - sold
    assert remaining <= 55_000 + 1.0


def test_plan_trims_skips_names_already_exiting():
    positions = {
        "WEAK": Position("WEAK", 400, 100, 100, 40_000),
        "STRONG": Position("STRONG", 600, 100, 100, 60_000),
    }
    plans = plan_trims(
        positions, {"WEAK": -0.2, "STRONG": 0.8},
        equity=100_000, invested_value=100_000,
        prices={"WEAK": 100.0, "STRONG": 100.0},
        risk=RISK, target_exposure_pct=0.55, skip={"WEAK"},
    )
    assert all(p.symbol != "WEAK" for p in plans)


def test_plan_trims_full_exits_dust():
    # 50k position, need to cut 48k → remainder 2k < 4% min → full exit.
    positions = {"A": Position("A", 500, 100, 100, 50_000)}
    plans = plan_trims(
        positions, {"A": 0.0},
        equity=100_000, invested_value=50_000,
        prices={"A": 100.0},
        risk=RISK, target_exposure_pct=0.02,
    )
    assert len(plans) == 1
    assert plans[0].full_exit is True
    assert plans[0].qty == 500


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


# --- veto attribution ------------------------------------------------------
# `notional` is the minimum of five separate caps. Reporting only the surviving
# number made a $0 that meant "the exposure ceiling is already breached" read as
# "the account is out of cash" — the 2026-08-29 weekly review chased a
# non-existent broker-cash bug on exactly that wording. The reason string must
# name the cap that actually bound.

def test_veto_names_the_exposure_cap_when_it_is_the_binding_limit():
    # Fully invested against the cap, but plenty of cash on hand.
    result = _size(0.06, equity=100_000, cash=50_000, invested=95_000)
    assert not result.approved
    assert "exposure cap" in result.reason
    assert "cash" not in result.reason


def test_veto_names_cash_when_cash_is_the_binding_limit():
    result = _size(0.06, equity=100_000, cash=100.0, invested=0)
    assert not result.approved
    assert "limited by cash" in result.reason


def test_veto_names_the_book_stop_risk_budget_when_it_binds():
    # Exposure and cash are both wide open; the book's stop-risk budget is not.
    risk = replace(RISK, max_portfolio_stop_risk_pct=0.06)
    result = size_position("TEST", 100.0, equity=100_000, cash=100_000,
                           invested_value=0, open_position_count=1, risk=risk,
                           stop_pct=0.06, existing_stop_risk=5_990.0)
    assert not result.approved
    assert "book stop-risk budget" in result.reason


def test_approved_sizing_also_reports_what_set_the_number():
    result = _size(0.06, equity=100_000, cash=100_000, invested=0)
    assert result.approved
    assert "set by " in result.reason


# --- sizing diagnostics (c2c_a7e2 contract §14.4) ---------------------------
# The baseline fixtures below pin approved / notional (float.hex) / reason
# bit-for-bit as recorded on the pre-diagnostics code. They must pass unchanged
# after the diagnostics snapshot is wired in — that is the evidence that
# attaching it did not alter sizing results.

_SIZING_BASELINE = {
    "approved_risk_budget": (True, "0x1.d4c0000000000p+13",
        "sized to $15,000 (15% of equity, 10.0% stop ≈ $1,500 at risk); "
        "set by max_position_pct 18%"),
    "approved_position_cap": (True, "0x1.1940000000000p+14",
        "sized to $18,000 (18% of equity, 6.0% stop ≈ $1,080 at risk); "
        "set by max_position_pct 18%; capped by max_position_pct"),
    "approved_sector_cap": (True, "0x1.3880000000000p+12",
        "sized to $5,000 (5% of equity, 12.0% stop ≈ $600 at risk); "
        "set by sector cap; capped by sector limit"),
    "approved_corr_haircut": (True, "0x1.86a0000000000p+12",
        "sized to $6,250 (6% of equity, 12.0% stop ≈ $750 at risk); "
        "set by max_position_pct 18% then the 50% correlation haircut; "
        "cut to 50% for correlation vs holdings"),
    "reject_exposure_cap": (False, "0x0.0p+0",
        "only $0 available (limited by the exposure cap — invested 95.0% "
        "of a 95% ceiling), below the 4% minimum position"),
    "reject_cash": (False, "0x0.0p+0",
        "only $100 available (limited by cash), below the 4% minimum position"),
    "reject_stop_risk_budget": (False, "0x0.0p+0",
        "only $167 available (limited by the book stop-risk budget — 5.99% "
        "of 6% already committed), below the 4% minimum position"),
    "reject_sector_room_zero": (False, "0x0.0p+0",
        "only $0 available (limited by sector cap), below the 4% minimum position"),
    "reject_below_min_cash": (False, "0x0.0p+0",
        "only $500 available (limited by cash), below the 4% minimum position"),
    "reject_invalid_price": (False, "0x0.0p+0", "invalid price"),
    "reject_max_open_positions": (False, "0x0.0p+0", "at max_open_positions (6)"),
    "reject_invalid_stop": (False, "0x0.0p+0", "invalid stop distance"),
    "tie_cash_exposure": (True, "0x1.3880000000000p+12",
        "sized to $5,000 (5% of equity, 10.0% stop ≈ $500 at risk); "
        "set by the exposure cap — invested 90.0% of a 95% ceiling"),
}


def _baseline_results():
    port_risk = replace(RISK, max_portfolio_stop_risk_pct=0.06)

    def run(**kw):
        args = dict(symbol="TEST", last_price=100.0, equity=100_000, cash=100_000,
                    invested_value=0, open_position_count=0, risk=RISK, stop_pct=0.10)
        args.update(kw)
        return size_position(**args)

    return {
        "approved_risk_budget": run(stop_pct=0.10),
        "approved_position_cap": run(stop_pct=0.06),
        "approved_sector_cap": run(stop_pct=0.12, sector_room=5_000.0),
        "approved_corr_haircut": run(stop_pct=0.12, corr_multiplier=0.5),
        "reject_exposure_cap": run(stop_pct=0.06, cash=50_000.0, invested_value=95_000.0),
        "reject_cash": run(stop_pct=0.06, cash=100.0),
        "reject_stop_risk_budget": run(risk=port_risk, stop_pct=0.06,
                                       open_position_count=1, existing_stop_risk=5_990.0),
        "reject_sector_room_zero": run(stop_pct=0.10, sector_room=0.0),
        "reject_below_min_cash": run(stop_pct=0.10, cash=500.0),
        "reject_invalid_price": run(last_price=0.0),
        "reject_max_open_positions": run(open_position_count=RISK.max_open_positions),
        "reject_invalid_stop": run(stop_pct=0.0),
        "tie_cash_exposure": run(cash=100_000 * RISK.max_total_exposure_pct - 90_000.0,
                                 invested_value=90_000.0),
    }


def test_sizing_baseline_bit_exact():
    results = _baseline_results()
    assert set(results) == set(_SIZING_BASELINE)
    for name, expected in _SIZING_BASELINE.items():
        r = results[name]
        assert (r.approved, r.notional.hex(), r.reason) == expected, name


def _diag(**kw):
    args = dict(symbol="TEST", last_price=100.0, equity=100_000, cash=100_000,
                invested_value=0, open_position_count=0, risk=RISK, stop_pct=0.10)
    args.update(kw)
    result = size_position(**args)
    assert isinstance(result.diagnostics, SizingDiagnostics)
    return result


def test_diagnostics_full_path_nothing_clamps():
    d = _diag(stop_pct=0.10).diagnostics
    assert d.cash_available == 100_000.0
    assert d.exposure_room == 95_000.0
    assert d.position_cap == 18_000.0
    # RISK carries the RiskConfig default max_portfolio_stop_risk_pct=0.06, so
    # the stop-risk branch runs and reports its room even when nothing binds.
    remaining = max(0.0, 100_000 * RISK.max_portfolio_stop_risk_pct - 0.0)
    assert d.stop_risk_room_pct == remaining / 100_000
    assert d.stop_risk_room_notional == remaining / 0.10
    assert d.sector_theme_room is None
    target = (100_000 * RISK.risk_per_trade_pct * 1.0) / 0.10
    assert d.pre_haircut_notional == target
    assert d.post_haircut_notional == target
    assert d.available_notional == 15_000.0
    assert d.min_position_notional == 4_000.0
    assert d.binding_constraints == ("risk_budget",)
    assert d.reject_code is None


def test_diagnostics_stop_risk_fields_none_when_cap_not_configured():
    risk = replace(RISK, max_portfolio_stop_risk_pct=0.0)
    d = _diag(risk=risk, stop_pct=0.10).diagnostics
    assert d.stop_risk_room_pct is None
    assert d.stop_risk_room_notional is None
    assert d.binding_constraints == ("risk_budget",)


def test_diagnostics_position_cap_binds():
    d = _diag(stop_pct=0.06).diagnostics
    assert d.pre_haircut_notional == 18_000.0
    assert d.available_notional == 18_000.0
    assert d.binding_constraints == ("position_cap",)
    assert d.reject_code is None


def test_diagnostics_exposure_cap_reject():
    r = _diag(stop_pct=0.06, cash=50_000.0, invested_value=95_000.0)
    d = r.diagnostics
    assert r.notional == 0.0
    assert d.cash_available == 50_000.0
    assert d.exposure_room == 0.0
    assert d.available_notional == 0.0
    assert d.binding_constraints == ("exposure_cap",)
    assert d.reject_code == "below_min_position"


def test_diagnostics_cash_reject():
    d = _diag(stop_pct=0.10, cash=500.0).diagnostics
    assert d.cash_available == 500.0
    assert d.available_notional == 500.0
    assert d.binding_constraints == ("cash",)
    assert d.reject_code == "below_min_position"


def test_diagnostics_stop_risk_budget_reject():
    risk = replace(RISK, max_portfolio_stop_risk_pct=0.06)
    r = _diag(risk=risk, stop_pct=0.06, open_position_count=1, existing_stop_risk=5_990.0)
    d = r.diagnostics
    remaining = max(0.0, 100_000 * 0.06 - 5_990.0)
    assert d.stop_risk_room_pct == remaining / 100_000
    assert d.stop_risk_room_notional == remaining / 0.06
    assert d.available_notional == round(remaining / 0.06, 2)
    assert d.binding_constraints == ("stop_risk_budget",)
    assert d.reject_code == "below_min_position"


def test_diagnostics_sector_cap_binds_and_zero_room_rejects():
    approved = _diag(stop_pct=0.12, sector_room=5_000.0)
    d = approved.diagnostics
    target = (100_000 * RISK.risk_per_trade_pct * 1.0) / 0.12
    assert d.sector_theme_room == 5_000.0
    assert d.pre_haircut_notional == target
    assert d.post_haircut_notional == target
    assert d.available_notional == 5_000.0
    assert d.binding_constraints == ("sector_cap",)
    assert d.reject_code is None

    rejected = _diag(stop_pct=0.10, sector_room=0.0)
    d = rejected.diagnostics
    assert rejected.notional == 0.0
    assert d.sector_theme_room == 0.0
    assert d.available_notional == 0.0
    assert d.binding_constraints == ("sector_cap",)
    assert d.reject_code == "below_min_position"


def test_diagnostics_tie_reports_every_binding_constraint():
    room = 100_000 * RISK.max_total_exposure_pct - 90_000.0
    d = _diag(cash=room, invested_value=90_000.0).diagnostics
    assert d.exposure_room == room
    assert d.binding_constraints == ("exposure_cap", "cash")


def test_diagnostics_haircut_before_sector_before_round_before_minimum():
    # Haircut first: pre-haircut 12.5k halves to 6.25k, under the 7k sector
    # room, so the sector cap must NOT clamp (sector-first would give 3.5k).
    r = _diag(stop_pct=0.12, corr_multiplier=0.5, sector_room=7_000.0)
    d = r.diagnostics
    target = (100_000 * RISK.risk_per_trade_pct * 1.0) / 0.12
    assert d.pre_haircut_notional == target
    assert d.post_haircut_notional == target * 0.5
    assert d.available_notional == 6_250.0
    assert d.binding_constraints == ("risk_budget", "correlation_haircut")

    # Sector clamp applies to the post-haircut number.
    r = _diag(stop_pct=0.12, corr_multiplier=0.5, sector_room=3_000.0)
    d = r.diagnostics
    assert d.post_haircut_notional == target * 0.5
    assert d.available_notional == 3_000.0
    assert d.binding_constraints == ("sector_cap",)
    assert d.reject_code == "below_min_position"

    # Round before the minimum check: 3,999.999 rounds up to 4,000.00, which
    # passes the 4% ($4,000) minimum; comparing before rounding would reject.
    r = _diag(stop_pct=0.10, cash=3_999.999)
    d = r.diagnostics
    assert r.approved is True
    assert r.notional == 4_000.0
    assert d.pre_haircut_notional == 3_999.999
    assert d.available_notional == 4_000.0
    assert d.min_position_notional == 4_000.0
    assert d.binding_constraints == ("cash",)


def test_diagnostics_early_exits_leave_uncomputed_fields_none():
    cases = [
        (dict(last_price=0.0), "invalid_price"),
        (dict(last_price=-5.0), "invalid_price"),
        (dict(open_position_count=RISK.max_open_positions), "max_open_positions"),
        (dict(stop_pct=0.0), "invalid_stop_distance"),
        (dict(stop_pct=-0.05), "invalid_stop_distance"),
    ]
    for kwargs, code in cases:
        r = _diag(**kwargs)
        d = r.diagnostics
        assert r.approved is False and r.notional == 0.0
        assert d.reject_code == code
        assert d.binding_constraints == ()
        for field in ("cash_available", "exposure_room", "position_cap",
                      "stop_risk_room_pct", "stop_risk_room_notional",
                      "sector_theme_room", "pre_haircut_notional",
                      "post_haircut_notional", "available_notional",
                      "min_position_notional"):
            assert getattr(d, field) is None, (code, field)


def test_diagnostics_to_dict_shape():
    d = _diag(stop_pct=0.10).diagnostics.to_dict()
    assert set(d) == {
        "cash_available", "exposure_room", "position_cap", "stop_risk_room_pct",
        "stop_risk_room_notional", "sector_theme_room", "pre_haircut_notional",
        "post_haircut_notional", "available_notional", "min_position_notional",
        "binding_constraints", "reject_code",
    }
    assert d["binding_constraints"] == ["risk_budget"]
    assert d["reject_code"] is None
