"""One full decision cycle: pull signals for the watchlist, get LLM reads,
combine into decisions, size and place paper orders, journal everything.

Usage:
    python -m agentic_trading.run                # normal run
    python -m agentic_trading.run --dry-run       # force DryRunBroker even if Alpaca creds are set
    python -m agentic_trading.run --skip-llm      # quant-only, skips Anthropic calls (cheap smoke test)
"""
from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .config import Settings, load_settings
from .data.feed import DataFeed, YFinanceFeed
from .data.market_data import average_dollar_volume
from .decision.engine import Action, Decision, decide
from .execution.broker import AlpacaBroker, Broker, DryRunBroker, Position
from .journal.logger import (
    DecisionRow, bump_intraday_confirmation, clear_intraday_confirmation, clear_position_peak,
    connect, get_last_escalation, load_position_peaks, record_cycle, record_escalation,
    update_position_peak,
)
from .llm.analyst import analyze
from .llm.grok_provider import check_sentiment
from .risk.manager import check_exit, protective_stop_price, size_position, stop_distance_pct
from .signals.macro import MacroRegime, assess_regime
from .signals.technical import QuantSignal, compute_signal

ROOT = Path(__file__).resolve().parents[2]
LOG_DIR = ROOT / "logs"


def _setup_logging() -> None:
    # Windows consoles default to a legacy codepage that mangles the em dashes
    # used in log messages below; force UTF-8 so `python -m agentic_trading.run`
    # is readable directly in PowerShell/cmd, not just in the log file.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    LOG_DIR.mkdir(exist_ok=True)
    log_file = LOG_DIR / f"{datetime.now():%Y-%m-%d}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.FileHandler(log_file, encoding="utf-8"), logging.StreamHandler()],
    )


def _release_shares_for_sale(broker, symbol: str, log) -> None:
    """Cancel any resting stop on this symbol before selling it.

    A stop order reserves the shares it covers, so a market sell submitted while
    one is open is rejected for insufficient quantity. Any exit path has to
    release the shares first.
    """
    try:
        for order in broker.get_open_orders():
            if order.symbol == symbol and order.side == "sell":
                broker.cancel_order(order.order_id)
                log.info("%s: cancelled resting %s order to free shares for sale.", symbol, order.order_type)
    except Exception:
        log.exception("%s: could not clear resting orders; the sell may be rejected.", symbol)


def _reconcile_protective_stops(broker, positions, peaks, atrs, risk, log) -> None:
    """Make sure every open position is sitting behind a broker-side stop at the
    right level, and that the level ratchets up as a position runs.

    This is what closes the gap between cycles: the client-side exit check only
    looks twice a day, so without a resting order a position is unprotected for
    hours at a time. Fractional stops are DAY-only, so they are re-placed every
    cycle rather than left resting indefinitely.
    """
    try:
        open_orders = broker.get_open_orders()
    except Exception:
        log.exception("Could not read open orders; skipping stop reconciliation this cycle.")
        return

    existing = {o.symbol: o for o in open_orders if o.side == "sell" and o.order_type == "stop"}

    for symbol, position in positions.items():
        atr = atrs.get(symbol)
        if atr is None:
            log.warning("No ATR for %s; leaving its protective stop untouched.", symbol)
            continue

        wanted = protective_stop_price(
            entry_price=position.avg_entry_price,
            high_water_mark=peaks.get(symbol, position.avg_entry_price),
            atr14=atr, risk=risk,
        )
        current = existing.get(symbol)

        # Only replace when the level actually moves up — cancel/replace churn
        # briefly leaves the position naked, so it should not happen for noise.
        if current is not None:
            if current.stop_price is not None and wanted <= current.stop_price + 0.01:
                continue
            if not broker.cancel_order(current.order_id):
                log.error("%s: could not cancel stale stop; not placing a replacement.", symbol)
                continue
            log.info("%s: raising protective stop %.2f -> %.2f", symbol, current.stop_price or 0.0, wanted)

        result = broker.submit_stop_sell(symbol, position.qty, wanted)
        if result is None:
            log.error("%s: NO PROTECTIVE STOP IN PLACE — relying on the twice-daily client-side check.", symbol)


def _bar_high(df, last_price: float) -> float:
    if df is None or df.empty or "High" not in df.columns:
        return last_price
    try:
        return float(df["High"].iloc[-1])
    except Exception:
        return last_price


@dataclass
class _Work:
    symbol: str
    signal: QuantSignal
    held: Position | None
    adv: float
    verdict: object = None
    decision: Decision | None = None
    forced_exit: object = None
    order_status: str | None = None
    order_qty: float | None = None
    fill_price: float | None = None
    notional: float | None = None
    stop_price: float | None = None
    sizing_reason: str | None = None
    grok_stance: str | None = None
    grok_confidence: float | None = None
    grok_summary: str | None = None


def _grok_confirm(settings: Settings, item: "_Work", pending_action: str, log) -> None:
    """Final X/web sentiment check right before an order actually goes out —
    informational only, folded into `item.decision.reasoning`, never a veto.
    Only called for decision-driven BUY/SELL, never for a forced risk exit
    (stop/trailing-stop), and only in the full cycle: even optimized, Grok's
    latency (~12-40s, see llm/grok_provider.py) doesn't fit the 20-minute
    fast-tier cadence, and each call costs real USD, not subscription quota.
    """
    grok_ok, grok_problem = settings.grok_available
    if not grok_ok:
        log.debug("%s: Grok sentiment check skipped — %s", item.symbol, grok_problem)
        return

    verdict = check_sentiment(
        symbol=item.symbol,
        pending_action=pending_action,
        existing_reasoning=item.decision.reasoning,
        grok_path=settings.grok_cli_path,
        timeout=settings.grok_timeout_seconds,
        reasoning_effort=settings.grok_reasoning_effort,
    )
    if verdict is None:
        log.info("%s: Grok sentiment check unavailable this cycle — proceeding on quant+LLM alone.", item.symbol)
        return

    item.grok_stance = verdict.stance
    item.grok_confidence = verdict.confidence
    item.grok_summary = verdict.summary

    action_sign = 1 if pending_action == "buy" else -1
    contradicts = (verdict.directional_score * action_sign) < -0.1
    label = "CONTRADICTS" if contradicts else "supports/neutral on"
    item.decision.reasoning += (
        f" Grok X/web sentiment {label} this {pending_action}: "
        f"{verdict.stance} ({verdict.confidence:.0%}) — {verdict.summary}"
    )
    if contradicts:
        log.warning("%s: Grok sentiment CONTRADICTS the %s decision (%s, %.0f%% confidence) — "
                    "proceeding anyway, this is informational only.",
                    item.symbol, pending_action, verdict.stance, verdict.confidence * 100)


def _decide_kwargs(settings: Settings, regime: MacroRegime | None) -> dict:
    return dict(
        min_quant_score_to_consider=settings.risk.min_quant_score_to_consider,
        regime=regime,
        risk_off_score_penalty=settings.risk.risk_off_score_penalty,
        buy_threshold=settings.risk.buy_threshold,
        sell_threshold=settings.risk.sell_threshold,
    )


def run_cycle(
    force_dry_run: bool = False,
    skip_llm: bool = False,
    fast_mode: bool = False,
    *,
    settings: Settings | None = None,
    broker: Broker | None = None,
    feed: DataFeed | None = None,
    conn: sqlite3.Connection | None = None,
    asof=None,
) -> None:
    log = logging.getLogger("run_cycle")
    settings = settings or load_settings()
    cycle_timestamp = datetime.now(timezone.utc).isoformat()
    owns_conn = conn is None

    if broker is None:
        use_dry_run = force_dry_run or not settings.has_alpaca_credentials
        if use_dry_run:
            broker = DryRunBroker()
            log.warning("Running with DryRunBroker (%s) — no real orders will be placed.",
                        "forced" if force_dry_run else "no Alpaca credentials found in .env")
        else:
            broker = AlpacaBroker(settings.alpaca_api_key, settings.alpaca_secret_key, paper=True)
            log.info("Connected to Alpaca paper trading.")
    else:
        use_dry_run = force_dry_run or isinstance(broker, DryRunBroker)

    if feed is None:
        # The deep cycle scores settled bars (a 9:45 run must not compute
        # SMA/RSI on 15 minutes of tape), so it drops the forming bar. The fast
        # tier must NOT: yfinance + drop_forming meant every 20-minute scan
        # re-scored yesterday's close and could never see intraday movement.
        # Alpaca's free tier returns today's in-progress bar and batch-fetches
        # the universe in ~1s, so the fast tier uses it when credentials exist.
        if fast_mode and asof is None and settings.has_alpaca_credentials:
            from .data.alpaca_feed import AlpacaFeed

            alpaca_feed = AlpacaFeed(
                settings.alpaca_api_key, settings.alpaca_secret_key, drop_forming=False,
            )
            alpaca_feed.prefetch(list(settings.watchlist))
            feed = alpaca_feed
            log.info("Fast tier using Alpaca live bars (today's forming bar included).")
        else:
            if fast_mode and asof is None:
                log.warning("Fast tier falling back to yfinance settled bars — no Alpaca "
                            "credentials, so this scan cannot see intraday movement.")
            feed = YFinanceFeed(drop_forming=(asof is None))

    analyst_ok, analyst_problem = settings.analyst_available
    use_llm = analyst_ok and not skip_llm
    if use_llm:
        if settings.analyst_provider == "cli":
            log.info("Analyst backend: cli %s (model=%s, home=%s)", settings.analyst_cli_path,
                     settings.analyst_cli_model or "<CLI default>", settings.analyst_cli_home or "<global>")
        else:
            log.info("Analyst backend: api (model=%s)", settings.analyst_model)
    else:
        log.warning("Running quant-only — %s", "skipped via --skip-llm" if skip_llm else analyst_problem)

    account = broker.get_account()
    positions = broker.get_positions()
    market_open = broker.is_market_open()
    log.info(
        "Account: equity=$%.2f cash=$%.2f open_positions=%d market_open=%s",
        account.equity, account.cash, len(positions), market_open,
    )
    core = settings.core_watchlist or settings.watchlist
    added = [s for s in settings.watchlist if s not in core]
    if added:
        log.info("Universe: %d symbols (%d core + %d from research vault: %s)",
                 len(settings.watchlist), len(core), len(added), ", ".join(added))
    elif settings.research_symbols:
        log.info("Universe: %d symbols (%d core; research vault overlapped the core list)",
                 len(settings.watchlist), len(core))

    if fast_mode and not market_open:
        log.info("Fast-tier scan skipped — market is closed.")
        return

    # Market regime is assessed once per cycle and applies to every symbol: it
    # decides how much size each entry gets and how high the entry bar sits.
    regime = assess_regime(feed=feed, asof=asof)
    log.info("Market regime: %s (%.2f)%s", regime.label.upper(), regime.score,
             f", VIX {regime.vix:.1f}" if regime.vix else "")
    for note in regime.notes:
        log.info("  regime | %s", note)
    regime_multiplier = settings.risk.risk_off_size_multiplier if regime.is_risk_off else 1.0

    if owns_conn:
        conn = connect()
    peaks = load_position_peaks(conn)
    # Positions closed outside this loop (manual sale, broker-side fill) should
    # not leave a stale peak behind to poison a later re-entry.
    for stale in set(peaks) - set(positions):
        clear_position_peak(conn, stale)
        peaks.pop(stale, None)

    # A buy submitted outside regular hours sits queued until the next open, so
    # a cycle running before it fills still sees a flat book. Without this the
    # same signal would be bought twice and the position would end up double the
    # intended size.
    try:
        pending_buys = {o.symbol for o in broker.get_open_orders() if o.side == "buy"}
    except Exception:
        log.exception("Could not read open orders; skipping new buys this cycle to avoid duplicates.")
        pending_buys = set(settings.watchlist)
    if pending_buys:
        log.info("Pending unfilled buys, no new entries for: %s", ", ".join(sorted(pending_buys)))

    invested_value = sum(p.market_value for p in positions.values())
    cash_remaining = account.cash
    open_position_count = len(positions)
    orders_this_cycle = 0

    rows: list[DecisionRow] = []
    atrs: dict[str, float] = {}  # fed to the protective-stop reconciliation below
    work: list[_Work] = []
    decide_kw = _decide_kwargs(settings, regime)

    for symbol in settings.watchlist:
        df = feed.price_history(symbol, asof=asof)
        signal = compute_signal(symbol, df)
        if signal is None:
            log.warning("Skipping %s — insufficient price history.", symbol)
            continue
        atrs[symbol] = signal.atr14
        adv = average_dollar_volume(df)
        held = positions.get(symbol)
        item = _Work(symbol=symbol, signal=signal, held=held, adv=adv)

        # Hard risk override: stop / trailing stop / take-profit beats any model
        # output. Refresh the peak first so the trailing stop sees today's high.
        if held is not None:
            bar_high = _bar_high(df, signal.last_price)
            update_position_peak(conn, symbol, bar_high, cycle_timestamp)
            peak = max(peaks.get(symbol, held.avg_entry_price), bar_high)
            peaks[symbol] = peak

            exit_signal = check_exit(
                entry_price=held.avg_entry_price, last_price=signal.last_price,
                high_water_mark=peak, atr14=signal.atr14, risk=settings.risk,
            )
            if exit_signal:
                item.forced_exit = exit_signal
                work.append(item)
                continue

        if held is None and adv < settings.risk.min_adv_usd:
            if fast_mode:
                continue
            item.decision = Decision(
                symbol=symbol, action=Action.AVOID, combined_score=signal.score,
                quant_score=signal.score, llm_score=None, conflicting_signals=False,
                reasoning=(
                    f"20-day average dollar volume ${adv:,.0f} is below the "
                    f"${settings.risk.min_adv_usd:,.0f} liquidity floor; not a new-entry candidate."
                ),
            )
            work.append(item)
            continue

        if fast_mode:
            # Cheap gate: a quant-only decide() first, and only spend an LLM
            # call + journal row on symbols it would actually act on. This is
            # what makes frequent fast-tier scans affordable — the full
            # analysis+news+fundamentals pass still only happens for symbols
            # that crossed a threshold, not the whole universe every time.
            quant_only = decide(
                signal=signal, verdict=None, has_open_position=held is not None, **decide_kw,
            )
            wants_entry = quant_only.action == Action.BUY and held is None
            actionable = wants_entry or (quant_only.action == Action.SELL and held is not None)
            if not actionable:
                # A name that stopped qualifying loses its streak — otherwise an
                # on-again/off-again symbol would bank confirmations across
                # unrelated crossings and enter on the strength of neither.
                clear_intraday_confirmation(conn, symbol)
                continue

            # Debounce new entries only. Intraday indicators ride the forming
            # bar, so a single crossing is not evidence; N consecutive scans is.
            # Exits are deliberately exempt — a risk exit never waits.
            if wants_entry and settings.risk.min_intraday_confirm_scans > 1:
                seen = bump_intraday_confirmation(conn, symbol, cycle_timestamp)
                if seen < settings.risk.min_intraday_confirm_scans:
                    log.info("%s: quant-only score %+.2f qualifies, but only %d/%d consecutive "
                             "scans — waiting for confirmation.",
                             symbol, quant_only.quant_score, seen,
                             settings.risk.min_intraday_confirm_scans)
                    continue

            # Cooldown: fundamentals and news don't move within an hour, so a
            # symbol whose score hasn't moved much since its last full analysis
            # doesn't need another LLM call to reach the same verdict. A bigger
            # move than the configured delta always overrides the cooldown.
            last = get_last_escalation(conn, symbol)
            if last is not None:
                last_at, last_score = last
                age_minutes = (datetime.now(timezone.utc) - datetime.fromisoformat(last_at)).total_seconds() / 60
                score_delta = abs(quant_only.quant_score - last_score)
                if age_minutes < settings.risk.escalation_cooldown_minutes and score_delta < settings.risk.escalation_cooldown_score_delta:
                    log.info("%s: quant-only score %+.2f crossed a threshold, but analyzed %.0fm ago "
                             "(score moved %.2f) — cooldown, skipping.",
                             symbol, quant_only.quant_score, age_minutes, score_delta)
                    continue

            log.info("%s: quant-only score %+.2f crossed a threshold — escalating to full analysis.",
                     symbol, quant_only.quant_score)

        verdict = None
        if use_llm:
            news = feed.news(symbol)
            fundamentals = feed.fundamentals(symbol)
            verdict = analyze(signal, news, fundamentals, settings)
            # Recorded for BOTH modes: a symbol the twice-daily deep cycle just
            # analyzed shouldn't be immediately re-analyzed by the next
            # fast-tier scan a few minutes later either.
            record_escalation(conn, symbol, cycle_timestamp, signal.score)
        item.verdict = verdict

        item.decision = decide(
            signal=signal,
            verdict=verdict,
            has_open_position=held is not None,
            **decide_kw,
        )
        work.append(item)

    # Exits first so they free shares (and, on a sim broker, cash) before buys.
    for item in work:
        if item.forced_exit is None:
            continue
        _release_shares_for_sale(broker, item.symbol, log)
        order = broker.submit_market_order(item.symbol, item.held.qty, "sell")
        reasoning = f"Risk override: {item.forced_exit.trigger} — {item.forced_exit.detail}."
        if order is None:
            log.error("%s: exit order failed — position REMAINS OPEN and unprotected.", item.symbol)
            reasoning += " EXIT ORDER FAILED — position still open."
        else:
            orders_this_cycle += 1
            clear_position_peak(conn, item.symbol)
            log.info("%s: forced SELL (%s) — %s", item.symbol, item.forced_exit.trigger, item.forced_exit.detail)
        item.order_status = order.status if order else "rejected"
        item.order_qty = order.qty if order else None
        item.decision = Decision(
            symbol=item.symbol, action=Action.SELL, combined_score=item.signal.score,
            quant_score=item.signal.score, llm_score=None, conflicting_signals=False,
            reasoning=reasoning,
        )

    for item in work:
        if item.forced_exit is not None or item.decision is None:
            continue
        if item.decision.action != Action.SELL or item.held is None:
            continue
        if not fast_mode:
            _grok_confirm(settings, item, "sell", log)
        _release_shares_for_sale(broker, item.symbol, log)
        order = broker.submit_market_order(item.symbol, item.held.qty, "sell")
        if order is None:
            item.order_status = "rejected"
            item.decision.reasoning += " SELL ORDER FAILED — position still open."
            log.error("%s: signal exit failed — position REMAINS OPEN.", item.symbol)
        else:
            item.order_status, item.order_qty = order.status, order.qty
            orders_this_cycle += 1
            clear_position_peak(conn, item.symbol)

    buy_items = [
        item for item in work
        if item.decision is not None and item.decision.action == Action.BUY and item.forced_exit is None
    ]
    buy_items.sort(key=lambda it: it.decision.combined_score, reverse=True)

    for item in buy_items:
        decision = item.decision
        signal = item.signal
        if item.symbol in pending_buys:
            decision.reasoning += " Skipped: an earlier buy for this symbol is still unfilled."
            continue
        if orders_this_cycle >= settings.risk.max_new_orders_per_cycle:
            decision.reasoning += " Skipped: max_new_orders_per_cycle reached for this run."
            continue
        stop_pct = stop_distance_pct(signal.atr14, signal.last_price, settings.risk)
        sizing = size_position(
            symbol=item.symbol, last_price=signal.last_price, equity=account.equity,
            cash=cash_remaining, invested_value=invested_value,
            open_position_count=open_position_count, risk=settings.risk,
            stop_pct=stop_pct, regime_multiplier=regime_multiplier,
        )
        item.sizing_reason = sizing.reason
        if sizing.approved:
            if not fast_mode:
                _grok_confirm(settings, item, "buy", log)
            order = broker.submit_notional_buy(item.symbol, sizing.notional, atr14=signal.atr14)
            if order is None:
                item.order_status = "rejected"
                decision.reasoning += f" {sizing.reason}, but the order was REJECTED (notional orders need regular market hours)."
            else:
                item.order_status, item.order_qty = order.status, order.qty
                item.notional = sizing.notional
                item.stop_price = protective_stop_price(
                    signal.last_price, signal.last_price, signal.atr14, settings.risk,
                )
                cash_remaining -= sizing.notional
                invested_value += sizing.notional
                open_position_count += 1
                orders_this_cycle += 1
                update_position_peak(conn, item.symbol, signal.last_price, cycle_timestamp)
                clear_intraday_confirmation(conn, item.symbol)
                decision.reasoning += f" {sizing.reason}."
        else:
            decision.reasoning += f" Risk manager vetoed buy: {sizing.reason}."

    for item in work:
        decision = item.decision
        if decision is None:
            continue
        log.info("%s: %s (combined=%+.2f) — %s", item.symbol, decision.action.value.upper(),
                 decision.combined_score, decision.reasoning)
        verdict = item.verdict
        rows.append(DecisionRow(
            symbol=item.symbol,
            quant_score=decision.quant_score,
            llm_stance=verdict.stance if verdict else None,
            llm_confidence=verdict.confidence if verdict else None,
            llm_rationale=verdict.rationale if verdict else None,
            combined_score=decision.combined_score,
            action=decision.action.value,
            reasoning=decision.reasoning,
            order_status=item.order_status,
            order_qty=item.order_qty,
            fill_price=item.fill_price,
            notional=item.notional,
            atr14=item.signal.atr14,
            stop_price=item.stop_price,
            sizing_reason=item.sizing_reason,
            grok_stance=item.grok_stance,
            grok_confidence=item.grok_confidence,
            grok_summary=item.grok_summary,
        ))

    # Re-read positions so newly filled buys are protected too, then make sure
    # everything held is sitting behind a stop at the current level.
    try:
        final_positions = broker.get_positions()
    except Exception:
        log.exception("Could not re-read positions; reconciling against the opening snapshot instead.")
        final_positions = positions
    _reconcile_protective_stops(broker, final_positions, peaks, atrs, settings.risk, log)

    if isinstance(broker, DryRunBroker):
        mode = "dry_run"
    elif asof is not None:
        mode = "backtest"
    else:
        mode = "paper"
    record_cycle(conn, cycle_timestamp, mode, account.equity, account.cash, rows,
                 regime_score=regime.score, regime_label=regime.label)
    if owns_conn:
        conn.close()
    if fast_mode:
        log.info("Fast-tier scan complete: %d/%d symbols escalated to full analysis, %d orders placed.",
                 len(rows), len(settings.watchlist), orders_this_cycle)
    else:
        log.info("Cycle complete: %d symbols evaluated, %d orders placed.", len(rows), orders_this_cycle)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Force DryRunBroker even if Alpaca credentials are configured.")
    parser.add_argument("--skip-llm", action="store_true", help="Skip Anthropic calls; quant-only decisions.")
    parser.add_argument("--fast", action="store_true",
                        help="Fast-tier mode: cheap quant-only pass over the whole universe, only spending an "
                             "LLM call on symbols that actually crossed a buy/sell threshold. Meant for frequent "
                             "intraday runs; the full twice-daily cycle should still run for deep news/fundamentals coverage.")
    args = parser.parse_args()

    _setup_logging()
    run_cycle(force_dry_run=args.dry_run, skip_llm=args.skip_llm, fast_mode=args.fast)


if __name__ == "__main__":
    main()
