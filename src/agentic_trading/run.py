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
import time
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from .cycle_lock import DEEP_LOCK_WAIT_SECONDS, CycleLock
from .config import Settings, load_settings
from .data.feed import DataFeed, YFinanceFeed
from .data.market_data import average_dollar_volume, fetch_last_price
from .decision.engine import Action, Decision, decide
from .execution.broker import AlpacaBroker, Broker, DryRunBroker, OrderIdMinter, Position
from .heartbeat import _notify_toast, write_heartbeat
from .live_events import emit as _emit_live_event
from .journal.logger import (
    DecisionRow, TradeIntent, bump_intraday_confirmation, clear_intraday_confirmation,
    clear_position_peak, clear_position_state, clear_trade_intent, connect,
    get_last_escalation, load_position_peaks, load_position_state, load_trade_intents,
    record_cycle, record_escalation, record_intent_event, record_position_state,
    record_regime_dwell, reset_position_peak, save_trade_intent, update_position_peak,
)
from .llm.analyst import analyze
from .llm.grok_provider import check_sentiment
from .risk.manager import (
    average_corr_to_holdings, check_exit, effective_max_exposure_pct,
    plan_trims, portfolio_stop_risk, protective_stop_price,
    sector_invested, sector_of, sector_room_dollars, size_position,
    stop_distance_pct, theme_room_dollars,
)
from .signals.macro import MacroRegime, assess_regime
from .signals.technical import QuantSignal, compute_signal

ROOT = Path(__file__).resolve().parents[2]
LOG_DIR = ROOT / "logs"

# An intent is meant to be executed at the NEXT open, and flushes only run
# during market hours — so the longest legitimate wait is a weekend (~65h from
# a Friday 16:15 intent to Monday's open). Anything older than that is stale
# analysis, not a pending order, and must not execute after a long outage.
TRADE_INTENT_TTL = timedelta(hours=72)

# Share-count multiples consistent with a forward stock split. Reverse splits
# are deliberately not handled: they leave the recorded peak too LOW (which is
# harmless for a ratcheting trail), and the above-market stop guard fails safe
# on anything else that goes stale.
CLEAN_SPLIT_FACTORS = (2.0, 3.0, 4.0, 5.0, 10.0)

# When a protective stop submit fails twice in a row on a live broker, wait
# this long and try once more with a fresh id. The classic case is Alpaca's
# wash-trade rejection (403) when the stop lands seconds after the entry buy —
# the buy is still settling, and by the time the retry runs it usually isn't.
STOP_REJECTION_RETRY_SECONDS = 20.0

# Same-cycle analyst circuit: after this many consecutive CLI/API failures
# (None from analyze()), skip remaining LLM calls this cycle. Exits, stops,
# and fail-closed WAIT on new entries are unchanged. Stops a 14 × timeout
# hang from blowing the 30-minute Task Scheduler limit and holding cycle.lock
# (2026-09-04 09:45: Codex stdin wait, journal never written, next cycle 12:15).
LLM_FAIL_FAST_STREAK = 3


def _detect_and_reset_splits(conn, peaks, positions, log, cycle_timestamp: str) -> None:
    """Detects a forward split via the broker's share count and re-bases the
    absolute-price state.

    A 2-for-1 split makes Alpaca double qty while halving avg entry and current
    price. The high-water mark is an absolute price tracked in this journal, so
    without this check a perfectly healthy position suddenly looks ~50% below
    its peak — a false trailing-stop exit at exactly the wrong moment."""
    state = load_position_state(conn)
    for symbol, pos in positions.items():
        qty = float(getattr(pos, "qty", 0.0) or 0.0)
        value = float(getattr(pos, "market_value", 0.0) or 0.0)
        _journal_safe(log, record_position_state, conn, symbol, qty, value, cycle_timestamp)
        last_qty, last_value = state.get(symbol, (None, None))
        if not last_qty or last_qty <= 0 or qty <= 0:
            continue
        ratio = qty / last_qty
        factor = next((f for f in CLEAN_SPLIT_FACTORS if abs(ratio - f) <= 0.03 * f), None)
        # A split preserves value; a crash that halved the price would also
        # roughly halve market_value, so the value guard is what keeps a real
        # -50% day from being misread as a split.
        value_kept = bool(last_value) and value > 0 and 0.7 <= value / float(last_value) <= 1.4
        if factor and value_kept:
            price = float(getattr(pos, "current_price", 0.0) or 0.0)
            log.warning(
                "%s: shares %.4f -> %.4f (%.1fx, value kept %.0f%%) — treating as a "
                "%g-for-1 split; resetting the peak to the post-split price %.2f.",
                symbol, last_qty, qty, ratio,
                (value / float(last_value) * 100.0) if last_value else 0.0,
                factor, price,
            )
            _journal_safe(log, reset_position_peak, conn, symbol, price, cycle_timestamp)
            peaks[symbol] = price


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


def _reconcile_protective_stops(broker, positions, peaks, atrs, risk, log, *, live: bool = False,
                                ids: OrderIdMinter | None = None) -> tuple[int, int] | None:
    """Make sure every open position is sitting behind a broker-side stop at the
    right level, and that the level ratchets up as a position runs.

    This is what closes the gap between cycles: the client-side exit check only
    looks twice a day, so without a resting order a position is unprotected for
    hours at a time. Fractional stops are DAY-only, so they are re-placed every
    cycle rather than left resting indefinitely.

    Returns (covered, total) positions, or None when open orders were unreadable
    and coverage is therefore unknown. The caller stamps this into the heartbeat
    so `heartbeat --check` can alert on a naked position; every branch below that
    leaves one uncovered also logs, but nothing was watching those logs.
    """
    ids = ids or OrderIdMinter(datetime.now(timezone.utc).isoformat())
    try:
        open_orders = broker.get_open_orders()
    except Exception:
        log.exception("Could not read open orders; skipping stop reconciliation this cycle.")
        return None

    existing = {o.symbol: o for o in open_orders if o.side == "sell" and o.order_type == "stop"}
    covered: set[str] = set()

    for symbol, position in positions.items():
        atr = atrs.get(symbol)
        if atr is None:
            log.warning("No ATR for %s; leaving its protective stop untouched.", symbol)
            if symbol in existing:
                covered.add(symbol)
            continue

        wanted = protective_stop_price(
            entry_price=position.avg_entry_price,
            high_water_mark=peaks.get(symbol, position.avg_entry_price),
            atr14=atr, risk=risk,
        )
        current = existing.get(symbol)
        market = getattr(position, "current_price", 0.0) or 0.0
        if market > 0 and wanted >= market:
            # A stop at/above the market is rejected by the broker outright.
            # A position truly this far off its peak would already have been
            # exited by the client-side trailing check, so reaching this branch
            # means the peak or price data is wrong — refuse rather than loop
            # on a guaranteed rejection every cycle. Placing one just under the
            # market instead would sell the position on that same bad data.
            #
            # Whether that is survivable depends entirely on the resting order:
            # an earlier stop is left untouched here and still covers the
            # position, but with nothing resting this leaves it naked for as
            # long as the bad data persists, which is why the two cases are not
            # logged at the same level. `heartbeat --check` asserts coverage
            # independently.
            if current is not None:
                covered.add(symbol)
                log.warning(
                    "%s: computed protective stop %.2f is not below the market (%.2f) — "
                    "keeping the resting stop at %.2f; position_peaks or the quote looks wrong.",
                    symbol, wanted, market, current.stop_price or 0.0,
                )
            else:
                log.error(
                    "%s: NO PROTECTIVE STOP IN PLACE — computed stop %.2f is not below the "
                    "market (%.2f) and nothing is resting; position_peaks or the quote looks "
                    "wrong. Relying on the client-side check until this clears.",
                    symbol, wanted, market,
                )
            continue

        # Only replace when the level actually moves up — cancel/replace churn
        # briefly leaves the position naked, so it should not happen for noise.
        if current is not None:
            if current.stop_price is not None and wanted <= current.stop_price + 0.01:
                covered.add(symbol)
                continue
            if not broker.cancel_order(current.order_id):
                # The old stop is still resting, so the position stays covered.
                log.error("%s: could not cancel stale stop; not placing a replacement.", symbol)
                covered.add(symbol)
                continue
            log.info("%s: raising protective stop %.2f -> %.2f", symbol, current.stop_price or 0.0, wanted)
            if live:
                time.sleep(0.35)

        stop_id = ids.mint("stop", symbol)
        result = broker.submit_stop_sell(symbol, position.qty, wanted, client_order_id=stop_id)
        if result is None and live:
            time.sleep(0.35)
            # Deliberately the SAME id: if the first submit actually reached
            # Alpaca despite the failure, this retry is rejected as a duplicate
            # instead of doubling the protection.
            result = broker.submit_stop_sell(symbol, position.qty, wanted, client_order_id=stop_id)
        if result is None and live:
            # An explicit rejection — the wash-trade rule when the stop lands
            # while the entry buy is still settling, or an id burned by a
            # cancel/replace — leaves no order behind, so a fresh id is safe
            # here. Give the fill a moment to land, then try once more before
            # giving up until the next cycle's reconciliation.
            time.sleep(STOP_REJECTION_RETRY_SECONDS)
            result = broker.submit_stop_sell(
                symbol, position.qty, wanted, client_order_id=ids.mint("stop", symbol),
            )
        if result is None:
            log.error("%s: NO PROTECTIVE STOP IN PLACE — relying on the twice-daily client-side check.", symbol)
        else:
            covered.add(symbol)

    return len(covered), len(positions)


def _journal_safe(log, fn, *args, **kwargs) -> bool:
    """Best-effort journal write. Bookkeeping rows (peaks, confirmations,
    intents) must not abort order handling when SQLite hiccups — a lost audit
    row is recoverable, an unhandled exception between cancelling a protective
    stop and re-placing it is not."""
    try:
        fn(*args, **kwargs)
        return True
    except sqlite3.Error:
        log.exception("Journal write %s failed — continuing without it.",
                      getattr(fn, "__name__", fn))
        return False


def _emit_live(asof, stage: str, **fields) -> None:
    """Best-effort cockpit JSONL. Backtests (asof set) stay silent; never raises."""
    if asof is not None:
        return
    try:
        _emit_live_event(stage, **fields)
    except Exception:
        logging.getLogger("run_cycle").exception(
            "live_events emit %s failed — continuing without it.", stage,
        )


def _stamp_cycle_progress(
    *,
    asof,
    fast_mode: bool,
    cycle_no: int | None,
    account,
    positions,
    rows: list | None = None,
    orders_this_cycle: int = 0,
    stop_coverage: tuple[int, int] | None = None,
    status: str = "ok",
    did: list[str] | None = None,
    did_not: list[str] | None = None,
    unresolved: list | None = None,
    skipped: str | None = None,
) -> None:
    """Live wakeups only — same gate as write_heartbeat. Never raises."""
    if asof is not None:
        return
    from .progress import emit_progress, infer_book_id

    did = list(did or [])
    did_not = list(did_not or [])
    unresolved = list(unresolved or [])
    if skipped:
        did_not.append(skipped)
    if orders_this_cycle:
        did.append(f"{orders_this_cycle} orders placed")
    elif not did and not skipped:
        did.append("no orders")
    for row in rows or []:
        reason = getattr(row, "sizing_reason", None) or ""
        if "exposure cap" in reason.lower() or "exposure" in reason.lower() and "ceiling" in reason.lower():
            unresolved.append({"kind": "exposure_cap", "detail": reason})
        reasoning = getattr(row, "reasoning", None) or ""
        if "fail closed" in reasoning.lower() or "LLM failed" in reasoning:
            unresolved.append({"kind": "llm_fail_closed", "detail": getattr(row, "symbol", "")})
    equity = float(getattr(account, "equity", 0) or 0) if account is not None else None
    cash = float(getattr(account, "cash", 0) or 0) if account is not None else None
    exposure = None
    if equity and equity > 0 and cash is not None:
        exposure = round(max(0.0, 1.0 - cash / equity), 4)
    covered = total = None
    if stop_coverage is not None:
        covered, total = stop_coverage
        if total and covered is not None and int(total) > int(covered):
            unresolved.append({
                "kind": "naked_stops",
                "detail": f"{int(total) - int(covered)}/{int(total)} uncovered",
            })
            if status == "ok":
                status = "incomplete"
    locks = []
    for item in unresolved:
        kind = item.get("kind") if isinstance(item, dict) else None
        if kind and kind not in locks and kind in {"exposure_cap", "stop_risk"}:
            locks.append(kind)
    emit_progress(
        book_id=infer_book_id(),
        round_name="fast" if fast_mode else "deep",
        round_id="" if cycle_no is None else str(cycle_no),
        status=status,
        did=did,
        did_not=did_not,
        unresolved=unresolved,
        broker={
            "equity": equity,
            "cash": cash,
            "n_positions": len(positions or {}),
            "degraded": account is None,
        },
        risk={
            "exposure_pct": exposure,
            "stops_covered": covered,
            "stops_total": total,
            "locks": locks,
        },
    )


def _restore_protective_stop(broker, log, *, symbol: str, qty: float,
                             entry_price: float, peak: float,
                             atr14: float, risk, cycle_timestamp: str,
                             ids: OrderIdMinter | None = None) -> None:
    """Put a stop back right after a failed sell. The exit path has just
    cancelled the resting stop to free the shares; if the sell is rejected the
    position would otherwise sit bare until the end-of-cycle reconciliation,
    which a mid-cycle crash could skip entirely."""
    ids = ids or OrderIdMinter(cycle_timestamp)
    wanted = protective_stop_price(
        entry_price=entry_price, high_water_mark=peak, atr14=atr14, risk=risk,
    )
    if broker.submit_stop_sell(symbol, qty, wanted, client_order_id=ids.mint("stop", symbol)) is not None:
        log.info("%s: restored a protective stop at %.2f after the failed sell.", symbol, wanted)


def _submit_protected_sell(broker, log, *, symbol: str, qty: float, purpose: str,
                           peaks: dict, entry_price: float, atr14: float | None,
                           risk, cycle_timestamp: str, ids):
    """Release reserved shares, submit the market sell and — if it fails — put
    the protective stop straight back so the position is never left bare.
    The one invariant every exit path must keep, kept in exactly one place.
    Returns the broker result, or None when the sell failed."""
    _release_shares_for_sale(broker, symbol, log)
    order = broker.submit_market_order(
        symbol, qty, "sell", client_order_id=ids.mint(purpose, symbol),
    )
    if order is None and atr14 is not None:
        _restore_protective_stop(
            broker, log, symbol=symbol, qty=qty, entry_price=entry_price,
            peak=peaks.get(symbol, entry_price), atr14=atr14, risk=risk,
            cycle_timestamp=cycle_timestamp, ids=ids,
        )
    return order


def _intent_created_at(created_at: str) -> datetime | None:
    """Parses an intent timestamp; naive values are read as UTC (that is how
    cycle_timestamp is written). None means unparseable garbage → discard."""
    try:
        ts = datetime.fromisoformat(created_at)
    except (TypeError, ValueError):
        return None
    return ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts


_ENTRY_WINDOW_ET = ZoneInfo("America/New_York")
ET = _ENTRY_WINDOW_ET

# 09:45 rather than pre-market, deliberately. Notional orders only fill during
# regular hours, so a pre-market cycle leaves its buys queued — and the
# protective-stop reconciliation at the end of that cycle would then see no
# position to protect, leaving the day's new entries naked until the next run.
# Running after the open lets a buy fill and get its stop in the same cycle.
# The 16:15 run reacts to the close; its orders queue for the next session.
# Defined here (not in scheduler.py) so both the daemon and the fast scan's
# yield window read one definition — two copies would drift.
RUN_TIMES_ET = [(9, 45), (16, 15)]

# How close to a deep slot a fast scan refuses to start. Wide enough to cover
# a deep cycle that starts a minute late and runs a couple of minutes long.
DEEP_CYCLE_YIELD_MINUTES = 5


def _near_deep_cycle(now_et: datetime, yield_minutes: int = DEEP_CYCLE_YIELD_MINUTES) -> bool:
    """True when `now_et` is within the yield window of a scheduled deep run."""
    now_minutes = now_et.hour * 60 + now_et.minute
    return any(
        abs(now_minutes - (hour * 60 + minute)) <= yield_minutes
        for hour, minute in RUN_TIMES_ET
    )


def _entry_not_before_utc(now_utc: datetime, risk) -> str:
    """ISO-UTC moment a freshly queued intent may first execute: today's window
    start when queued before it (the 9:45 cycle queues for ~10:00 the same
    day), immediately when queued inside the window, tomorrow's window start
    when queued after the close (the 16:15 cycle queues for the next open).
    Weekends resolve naturally: the flush only runs while the market is open."""
    now_et = now_utc.astimezone(_ENTRY_WINDOW_ET)
    start = datetime.strptime(risk.entry_window_start_et, "%H:%M").time()
    end = datetime.strptime(risk.entry_window_end_et, "%H:%M").time()
    today_start = now_et.replace(hour=start.hour, minute=start.minute, second=0, microsecond=0)
    if now_et.time() < start:
        target = today_start
    elif now_et.time() <= end:
        target = now_et
    else:
        target = today_start + timedelta(days=1)
    return target.astimezone(timezone.utc).isoformat()


def _within_entry_window(now_utc: datetime, risk) -> bool:
    now_et = now_utc.astimezone(_ENTRY_WINDOW_ET).time()
    start = datetime.strptime(risk.entry_window_start_et, "%H:%M").time()
    end = datetime.strptime(risk.entry_window_end_et, "%H:%M").time()
    return start <= now_et <= end


def _bar_high(df, last_price: float) -> float:
    """The last bar's high, for the trailing-stop peak update.

    A bad print (feed glitch, split misprint) can produce a nonsense High, and
    the peak table only ever ratchets up — one bad value would pin the
    high-water mark above reality forever and push computed stops above the
    market. A High implausible against its own bar's Close is therefore
    discarded in favour of the close-based price."""
    if df is None or df.empty or "High" not in df.columns:
        return last_price
    try:
        high = float(df["High"].iloc[-1])
        close = float(df["Close"].iloc[-1])
        if close > 0 and close * 0.95 <= high <= close * 1.75:
            return high
        logging.getLogger("run_cycle").warning(
            "Implausible bar High %.2f vs Close %.2f — ignoring it for the peak update.",
            high, close,
        )
    except Exception:
        pass
    return last_price


def _refresh_peak_and_check_exit(conn, peaks, cycle_timestamp, risk, *, df, signal, held, log):
    """Ratchet the high-water mark from the latest bar, then run the hard exit
    overrides against it. Shared by watchlist symbols and by holdings that have
    dropped off every list but are still owned."""
    bar_high = _bar_high(df, signal.last_price)
    _journal_safe(log, update_position_peak, conn, signal.symbol, bar_high, cycle_timestamp)
    peak = max(peaks.get(signal.symbol, held.avg_entry_price), bar_high)
    peaks[signal.symbol] = peak
    return check_exit(
        entry_price=held.avg_entry_price, last_price=signal.last_price,
        high_water_mark=peak, atr14=signal.atr14, risk=risk,
    )


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


def _corr_haircut(
    symbol: str,
    positions: dict,
    feed: DataFeed | None,
    asof,
    risk,
    extra_held: list[str] | None = None,
    correlation_model: Callable[[str, list[str], object, int], float | None] | None = None,
) -> tuple[float, float | None]:
    mult = getattr(risk, "corr_size_multiplier", 1.0) or 1.0
    names = [n for n in dict.fromkeys([*positions, *(extra_held or [])]) if n != symbol]
    if not names or feed is None or mult >= 1.0:
        return 1.0, None
    window = getattr(risk, "corr_lookback", 60) or 60
    if correlation_model is not None:
        corr = correlation_model(symbol, names, asof, window)
        if corr is not None and corr > getattr(risk, "corr_penalty_threshold", 0.75):
            return mult, corr
        return 1.0, corr
    cand = feed.price_history(symbol, asof=asof)
    if cand.empty or "Close" not in cand.columns:
        return 1.0, None
    holdings: dict[str, object] = {}
    for held in names:
        hdf = feed.price_history(held, asof=asof)
        if not hdf.empty and "Close" in hdf.columns:
            holdings[held] = hdf["Close"]
    corr = average_corr_to_holdings(
        cand["Close"], holdings, window=window,
    )
    if corr is not None and corr > getattr(risk, "corr_penalty_threshold", 0.75):
        return mult, corr
    return 1.0, corr


def _fill_holding_atrs(positions: dict, atrs: dict[str, float], feed: DataFeed, asof, weights) -> None:
    """Count leftover / off-watchlist names in portfolio stop-risk (e.g. ACET)."""
    for symbol in positions:
        if symbol in atrs:
            continue
        df = feed.price_history(symbol, asof=asof)
        signal = compute_signal(symbol, df, weights=weights)
        if signal is not None:
            atrs[symbol] = signal.atr14


def _resolve_sectors(settings: Settings, feed: DataFeed | None, asof, symbols: list[str]) -> dict[str, str]:
    mapping = dict(getattr(settings, "sectors", None) or {})
    if asof is not None or feed is None:
        return mapping
    for symbol in symbols:
        if symbol in mapping:
            continue
        try:
            info = feed.fundamentals(symbol) or {}
        except Exception:
            info = {}
        sector = info.get("sector")
        if sector:
            mapping[symbol] = str(sector)
    return mapping


def _update_trim_dwell(conn, regime, risk, log, cycle_timestamp: str) -> bool:
    """Double-buffered sell trigger for the existing book.

    TRIM arms only when the regime sits in a trim label AND beyond
    `trim_trigger_score` (depth buffer) for `trim_confirm_cycles` consecutive
    deep-cycle readings (time buffer — 9:45/16:15, so 2 ≈ one full trading
    day). A shallow dip past the risk_off line, or a one-cycle VIX spike that
    reverts by the afternoon, sells nothing. The counter is journaled so it
    survives restarts; any reading outside the trigger resets it."""
    triggered = (
        regime.score <= risk.trim_trigger_score
        and regime.label in (list(getattr(risk, "trim_regimes", None) or ["risk_off"]))
    )
    count = record_regime_dwell(conn, triggered, regime.score, regime.label, cycle_timestamp)
    if triggered and count < risk.trim_confirm_cycles:
        log.info(
            "TRIM held back: regime %s (%.2f) beyond trigger %.2f for %d/%d consecutive readings.",
            regime.label, regime.score, risk.trim_trigger_score, count, risk.trim_confirm_cycles,
        )
    return triggered and count >= risk.trim_confirm_cycles


def _apply_trims(
    *,
    work: list[_Work],
    positions: dict,
    account,
    settings: Settings,
    broker: Broker,
    conn: sqlite3.Connection,
    invested_value: float,
    open_position_count: int,
    regime,
    peaks: dict,
    atrs: dict[str, float],
    log,
    cycle_timestamp: str = "",
    ids: OrderIdMinter | None = None,
) -> tuple[float, int]:
    """Partial (or dust-full) sells to bring the book under the regime exposure cap."""
    ids = ids or OrderIdMinter(cycle_timestamp)
    target_pct = effective_max_exposure_pct(settings.risk, regime.label)
    skip = {
        item.symbol for item in work
        if item.decision is not None
        and item.decision.action == Action.SELL
        and item.order_status not in (None, "rejected")
    }
    scores = {
        item.symbol: item.decision.combined_score
        for item in work if item.decision is not None
    }
    prices = {
        symbol: getattr(pos, "current_price", 0.0) or 0.0
        for symbol, pos in positions.items()
    }
    for item in work:
        if item.signal is not None:
            prices[item.symbol] = item.signal.last_price
    plans = plan_trims(
        positions, scores, account.equity, invested_value, prices,
        settings.risk, target_pct, skip=skip,
    )
    if not plans:
        return invested_value, open_position_count

    start_invested = invested_value
    by_symbol = {item.symbol: item for item in work}
    for plan in plans:
        pos = positions.get(plan.symbol)
        if pos is None:
            continue
        try:
            order = _submit_protected_sell(
                broker, log, symbol=plan.symbol, qty=plan.qty, purpose="trim",
                peaks=peaks, entry_price=pos.avg_entry_price,
                atr14=atrs.get(plan.symbol), risk=settings.risk,
                cycle_timestamp=cycle_timestamp, ids=ids,
            )
            kind = "full exit" if plan.full_exit else f"{plan.qty:.4f} shares"
            reasoning = (
                f"TRIM ({kind}): book at {start_invested / account.equity:.0%} of equity, "
                f"cap {target_pct:.0%}; selling ${plan.notional:,.0f} of {plan.symbol}."
            )
            item = by_symbol.get(plan.symbol)
            if order is None:
                log.error("%s: TRIM order failed — position unchanged.", plan.symbol)
                if item is not None and item.decision is not None:
                    item.decision.reasoning += " TRIM ORDER FAILED."
                continue
            invested_value = max(0.0, invested_value - plan.notional)
            if plan.full_exit:
                open_position_count = max(0, open_position_count - 1)
                _journal_safe(log, clear_position_peak, conn, plan.symbol)
                _journal_safe(log, clear_position_state, conn, plan.symbol)
            log.info("%s: %s", plan.symbol, reasoning)
            if item is None:
                continue
            item.order_status, item.order_qty = order.status, order.qty
            item.notional = plan.notional
            item.decision = Decision(
                symbol=plan.symbol, action=Action.TRIM,
                combined_score=item.decision.combined_score if item.decision else 0.0,
                quant_score=item.decision.quant_score if item.decision else item.signal.score,
                llm_score=item.decision.llm_score if item.decision else None,
                conflicting_signals=False,
                reasoning=reasoning if item.decision is None else (item.decision.reasoning + " " + reasoning),
            )
        except Exception:
            log.exception("%s: TRIM handling failed mid-way.", plan.symbol)
    return invested_value, open_position_count


def _entry_sizing_inputs(
    *,
    symbol: str,
    atr14: float,
    live_price: float,
    positions: dict,
    atrs: dict[str, float],
    feed: DataFeed | None,
    asof,
    settings: Settings,
    occupancy: dict[str, float],
    sector_map: dict[str, str],
    account_equity: float,
    extra_held: list[str],
    cycle_book_risk: float = 0.0,
    correlation_model=None,
    reserved_notional: dict[str, float] | None = None,
) -> tuple[float, float, float, float]:
    """The sizing-input sequence shared by the live TradeIntent flush and the
    backtest buy path. This MUST stay one implementation: a gate that exists
    only on one path would make the backtest lie about the live system
    (the theme-room gate used to be missing on the flush path).

    Returns (stop_pct, book_risk, corr_multiplier, sector_room_including_theme)."""
    stop_pct = stop_distance_pct(atr14, live_price, settings.risk)
    book_risk = portfolio_stop_risk(positions, atrs, settings.risk) + cycle_book_risk
    corr_mult, _ = _corr_haircut(
        symbol, positions, feed, asof, settings.risk,
        extra_held=extra_held, correlation_model=correlation_model,
    )
    room = sector_room_dollars(
        symbol, account_equity, occupancy, sector_map, settings.risk,
    )
    theme_room = theme_room_dollars(
        symbol, account_equity, positions, settings.risk,
        reserved=reserved_notional or {},
    )
    if theme_room is not None:
        room = min(room, theme_room)
    return stop_pct, book_risk, corr_mult, room


def _flush_trade_intents(
    *,
    broker: Broker,
    conn: sqlite3.Connection,
    settings: Settings,
    account,
    positions: dict,
    pending_buys: set[str],
    peaks: dict,
    cash_remaining: float,
    invested_value: float,
    open_position_count: int,
    orders_this_cycle: int,
    regime_multiplier: float,
    cycle_timestamp: str,
    log,
    atrs: dict[str, float],
    feed: DataFeed | None,
    asof,
    sizing_risk,
    sector_map: dict[str, str],
    occupancy: dict[str, float],
    orders_readable: bool = True,
    ids: OrderIdMinter | None = None,
    now_utc: datetime | None = None,
) -> tuple[set[str], float, float, int, int]:
    """Execute queued BUY intents inside the entry window, with a gap check and
    chase guards. Intents whose time has not come (not_before) or that would
    chase the price stay queued for a later scan; the TTL eventually discards
    the ones that never get a sane entry."""
    ids = ids or OrderIdMinter(cycle_timestamp)
    now_utc = now_utc or datetime.now(timezone.utc)
    filled: set[str] = set()
    extra_risk = 0.0
    extra_held: list[str] = []
    filled_notional: dict[str, float] = {}  # this scan's buys, for the theme-room reserved count
    if not orders_readable:
        # pending_buys is untrustworthy this cycle, so flushing could double a
        # buy that genuinely is pending. Keep every intent for the next run.
        log.warning("TradeIntent flush skipped — open orders were unreadable; "
                    "intents preserved instead of cleared.")
        return filled, cash_remaining, invested_value, open_position_count, orders_this_cycle
    if not _within_entry_window(now_utc, settings.risk):
        # No order is ever placed in the open's first half hour (the widest
        # spread, most chaotic tape of the day) or after the window closes.
        log.info("TradeIntent flush skipped — outside the entry window (%s–%s ET).",
                 settings.risk.entry_window_start_et, settings.risk.entry_window_end_et)
        return filled, cash_remaining, invested_value, open_position_count, orders_this_cycle
    for intent in load_trade_intents(conn):
        created = _intent_created_at(intent.created_at)
        if created is None or now_utc - created > TRADE_INTENT_TTL:
            age = ("unreadable timestamp" if created is None
                   else f"{(now_utc - created).total_seconds() / 3600:.0f}h old")
            log.warning("%s: TradeIntent discarded — %s exceeds the %.0fh TTL; "
                        "stale analysis must not execute.", intent.symbol, age,
                        TRADE_INTENT_TTL.total_seconds() / 3600)
            _journal_safe(log, record_intent_event, conn, now_utc.isoformat(),
                          intent.symbol, "ttl", deferred=False, detail=age)
            _journal_safe(log, clear_trade_intent, conn, intent.symbol)
            _emit_live(asof, "order_or_veto", symbol=intent.symbol, status="veto",
                       kind="ttl", reason=age)
            continue
        not_before = _intent_created_at(intent.not_before) if intent.not_before else None
        if not_before is not None and now_utc < not_before:
            continue  # queued for a later window — keep it
        if intent.symbol in positions or intent.symbol in pending_buys:
            _journal_safe(log, clear_trade_intent, conn, intent.symbol)
            continue
        if orders_this_cycle >= settings.risk.max_new_orders_per_cycle:
            break
        live_price = fetch_last_price(intent.symbol)
        if live_price is None:
            log.warning("%s: TradeIntent skipped — no live quote for gap check.", intent.symbol)
            continue
        if intent.atr14 > 0:
            gap_atr = (live_price - intent.signal_price) / intent.atr14
            if gap_atr > settings.risk.max_entry_gap_atr:
                # Discarded, not deferred: the price the analysis was written
                # against no longer exists. Say so — "WAIT" read like the chase
                # guards, which do keep their intent.
                log.info("%s: TradeIntent discarded — live %.2f is %.2f ATR above signal %.2f; "
                         "the setup this was queued for is gone.",
                         intent.symbol, live_price, gap_atr, intent.signal_price)
                _journal_safe(log, record_intent_event, conn, now_utc.isoformat(),
                              intent.symbol, "gap", deferred=False,
                              detail=f"live {live_price:.2f} is {gap_atr:.2f} ATR above "
                                     f"signal {intent.signal_price:.2f} "
                                     f"(cap {settings.risk.max_entry_gap_atr})")
                _journal_safe(log, clear_trade_intent, conn, intent.symbol)
                _emit_live(asof, "order_or_veto", symbol=intent.symbol, status="veto",
                           kind="gap")
                continue
        # Chase guards: the signal-relative gate always runs; the open-relative
        # gate runs when today's open is available. An intent that would buy
        # the top of the morning's move waits for a later scan instead.
        vs_signal = live_price / intent.signal_price - 1 if intent.signal_price > 0 else 0.0
        if vs_signal > settings.risk.max_chase_vs_signal_pct:
            log.info("%s: TradeIntent WAIT — live %.2f is %+.1f%% above the signal price "
                     "(chase cap %.1f%%); keeping the intent for a later scan.",
                     intent.symbol, live_price, vs_signal * 100,
                     settings.risk.max_chase_vs_signal_pct * 100)
            _journal_safe(log, record_intent_event, conn, now_utc.isoformat(),
                          intent.symbol, "chase_signal", deferred=True,
                          detail=f"live {live_price:.2f} is {vs_signal * 100:+.1f}% above signal "
                                 f"(cap {settings.risk.max_chase_vs_signal_pct * 100:.1f}%)")
            _emit_live(asof, "order_or_veto", symbol=intent.symbol, status="deferred",
                       kind="chase_signal")
            continue
        today_open = None
        open_price_of = getattr(broker, "get_today_open", None)
        if callable(open_price_of):
            try:
                today_open = open_price_of(intent.symbol)
            except Exception:
                log.exception("%s: get_today_open failed — open-relative chase gate skipped.", intent.symbol)
        if today_open and live_price / today_open - 1 > settings.risk.max_chase_vs_open_pct:
            log.info("%s: TradeIntent WAIT — live %.2f is %+.1f%% above today's open %.2f "
                     "(chase cap %.1f%%); keeping the intent for a later scan.",
                     intent.symbol, live_price, (live_price / today_open - 1) * 100, today_open,
                     settings.risk.max_chase_vs_open_pct * 100)
            _journal_safe(log, record_intent_event, conn, now_utc.isoformat(),
                          intent.symbol, "chase_open", deferred=True,
                          detail=f"live {live_price:.2f} is "
                                 f"{(live_price / today_open - 1) * 100:+.1f}% above open "
                                 f"(cap {settings.risk.max_chase_vs_open_pct * 100:.1f}%)")
            _emit_live(asof, "order_or_veto", symbol=intent.symbol, status="deferred",
                       kind="chase_open")
            continue
        stop_pct, book_risk, corr_mult, room = _entry_sizing_inputs(
            symbol=intent.symbol, atr14=intent.atr14, live_price=live_price,
            positions=positions, atrs=atrs, feed=feed, asof=asof,
            settings=settings, occupancy=occupancy, sector_map=sector_map,
            account_equity=account.equity,
            extra_held=extra_held, cycle_book_risk=extra_risk,
            reserved_notional=dict(filled_notional),
        )
        sizing = size_position(
            symbol=intent.symbol, last_price=live_price, equity=account.equity,
            cash=cash_remaining, invested_value=invested_value,
            open_position_count=open_position_count, risk=sizing_risk,
            stop_pct=stop_pct, regime_multiplier=regime_multiplier,
            existing_stop_risk=book_risk, corr_multiplier=corr_mult,
            sector_room=room,
        )
        _emit_live(asof, "sizing", symbol=intent.symbol, approved=sizing.approved,
                   notional=round(sizing.notional, 2), reason=sizing.reason)
        if not sizing.approved:
            # Book-level and transient: exposure, book stop-risk and sector room
            # all move as positions come and go, so the same intent can size
            # fine on the next scan. Keep it (the chase guards above do the
            # same) and let the TTL be the only thing that discards analysis.
            log.info("%s: TradeIntent vetoed — %s; keeping the intent for a later scan.",
                     intent.symbol, sizing.reason)
            _journal_safe(log, record_intent_event, conn, now_utc.isoformat(),
                          intent.symbol, "sizing", deferred=True, detail=sizing.reason)
            _emit_live(asof, "order_or_veto", symbol=intent.symbol, status="veto",
                       kind="sizing", reason=sizing.reason)
            continue
        order = broker.submit_notional_buy(
            intent.symbol, sizing.notional, atr14=intent.atr14,
            client_order_id=ids.mint("intent-buy", intent.symbol),
        )
        if order is None:
            log.warning("%s: TradeIntent order rejected.", intent.symbol)
            _emit_live(asof, "order_or_veto", symbol=intent.symbol, status="rejected",
                       kind="intent-buy")
            continue
        cash_remaining -= sizing.notional
        invested_value += sizing.notional
        extra_risk += sizing.notional * stop_pct
        extra_held.append(intent.symbol)
        sec = sector_of(intent.symbol, sector_map)
        occupancy[sec] = occupancy.get(sec, 0.0) + sizing.notional
        open_position_count += 1
        orders_this_cycle += 1
        filled.add(intent.symbol)
        pending_buys.add(intent.symbol)
        filled_notional[intent.symbol] = sizing.notional
        _journal_safe(log, update_position_peak, conn, intent.symbol, live_price, cycle_timestamp)
        _journal_safe(log, clear_intraday_confirmation, conn, intent.symbol)
        _journal_safe(log, clear_trade_intent, conn, intent.symbol)
        log.info("%s: TradeIntent filled $%.0f @ ~%.2f — %s",
                 intent.symbol, sizing.notional, live_price, sizing.reason)
        _emit_live(asof, "order_or_veto", symbol=intent.symbol, status="filled",
                   kind="intent-buy", notional=round(sizing.notional, 2))
    return filled, cash_remaining, invested_value, open_position_count, orders_this_cycle


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
    signal_weights: dict[str, float] | None = None,
    disable_macro: bool = False,
    signal_model: Callable[..., QuantSignal | None] | None = None,
    regime_model: Callable[[object], MacroRegime] | None = None,
    scan_symbols: list[str] | None = None,
    entry_symbols: set[str] | None = None,
    correlation_model: Callable[[str, list[str], object, int], float | None] | None = None,
) -> None:
    log = logging.getLogger("run_cycle")
    settings = settings or load_settings()
    cycle_symbols = list(dict.fromkeys(scan_symbols if scan_symbols is not None else settings.watchlist))
    compute = signal_model or compute_signal
    cycle_timestamp = datetime.now(timezone.utc).isoformat()
    ids = OrderIdMinter(cycle_timestamp)
    live_session = asof is None
    owns_conn = conn is None
    cycle_no: int | None = None

    def _live(stage: str, **fields) -> None:
        if cycle_no is not None:
            fields.setdefault("cycle", cycle_no)
        _emit_live(asof, stage, **fields)

    if broker is None:
        use_dry_run = force_dry_run or not settings.has_alpaca_credentials
        if use_dry_run:
            broker = DryRunBroker()
            log.warning("Running with DryRunBroker (%s) — no real orders will be placed.",
                        "forced" if force_dry_run else "no Alpaca credentials found in .env")
        else:
            # Second layer of the paper-only guarantee (README "Safety"):
            # AlpacaBroker refuses paper=False internally, and this check makes
            # a live base URL in .env refuse here as well.
            if not settings.is_paper:
                raise ValueError(
                    f"ALPACA_BASE_URL {settings.alpaca_base_url!r} is not a paper endpoint — refusing to run."
                )
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
            alpaca_feed.prefetch(cycle_symbols)
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
            log.info("Analyst backend: cli %s (model=%s, home=%s, extra_args=%s)", settings.analyst_cli_path,
                     settings.analyst_cli_model or "<CLI default>", settings.analyst_cli_home or "<global>",
                     settings.analyst_cli_extra_args or "<none>")
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
        # Observability: prove the scheduler woke up even though there was
        # nothing to scan. Live runs only — backtests/tests (asof set, often
        # with throwaway journals) must not freshen the watchdog stamp.
        _live("cycle_start", n_symbols=len(cycle_symbols), fast=True,
              skip_llm=skip_llm, dry_run=use_dry_run, skipped="market_closed")
        _live("cycle_end", skipped="market_closed", n_evaluated=0, n_orders=0)
        if asof is None:
            write_heartbeat(conn, "fast")
        _stamp_cycle_progress(
            asof=asof, fast_mode=True, cycle_no=cycle_no, account=account,
            positions=positions, status="no_trade", skipped="market closed",
        )
        return

    if owns_conn:
        conn = connect()
    if conn is not None:
        try:
            cycle_no = int(conn.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM cycles").fetchone()[0])
        except Exception:
            cycle_no = None
    _live("cycle_start", n_symbols=len(cycle_symbols), fast=fast_mode,
          skip_llm=skip_llm, dry_run=use_dry_run, market_open=market_open)

    # Market regime is assessed once per cycle and applies to every symbol: it
    # decides how much size each entry gets and how high the entry bar sits.
    regime = regime_model(asof) if regime_model is not None else assess_regime(feed=feed, asof=asof)
    if disable_macro:
        regime = MacroRegime(score=0.0, label="neutral",
                             notes=["macro disabled (ablation) — treated as neutral"])
    log.info("Market regime: %s (%.2f)%s", regime.label.upper(), regime.score,
             f", VIX {regime.vix:.1f}" if regime.vix else "")
    for note in regime.notes:
        log.info("  regime | %s", note)
    _live("regime", label=regime.label, score=round(regime.score, 4), vix=regime.vix)
    regime_multiplier = settings.risk.risk_off_size_multiplier if regime.is_risk_off else 1.0

    peaks = load_position_peaks(conn)
    # Positions closed outside this loop (manual sale, broker-side fill) should
    # not leave a stale peak behind to poison a later re-entry.
    for stale in set(peaks) - set(positions):
        clear_position_peak(conn, stale)
        _journal_safe(log, clear_position_state, conn, stale)
        peaks.pop(stale, None)

    # Before anything reads a peak: a split detected here re-bases it, so the
    # exit checks below never see a phantom -50% from the split itself.
    _detect_and_reset_splits(conn, peaks, positions, log, cycle_timestamp)

    # A buy submitted outside regular hours sits queued until the next open, so
    # a cycle running before it fills still sees a flat book. Without this the
    # same signal would be bought twice and the position would end up double the
    # intended size.
    orders_readable = True
    try:
        pending_buys = {o.symbol for o in broker.get_open_orders() if o.side == "buy"}
    except Exception:
        log.exception("Could not read open orders; skipping new buys this cycle to avoid duplicates.")
        pending_buys = set(cycle_symbols)
        orders_readable = False
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
    llm_fail_streak = 0

    # Re-protect everything FIRST. A previous cycle can have died between
    # cancelling a protective stop and re-placing it (Task Scheduler execution
    # timeouts have killed cycles mid-flight), leaving positions bare — and
    # buys filled late in such a cycle have no stop either. Reconciling here
    # bounds that window to one cycle start instead of one full cycle; it runs
    # again at the end once today's fills are visible.
    _fill_holding_atrs(positions, atrs, feed, asof, signal_weights)
    _reconcile_protective_stops(broker, positions, peaks, atrs, settings.risk, log,
                                live=live_session, ids=ids)

    for symbol in cycle_symbols:
        _live("symbol_enter", symbol=symbol)
        df = feed.price_history(symbol, asof=asof)
        signal = compute(symbol, df, weights=signal_weights)
        if signal is None:
            log.warning("Skipping %s — insufficient price history.", symbol)
            _live("quant", symbol=symbol, error="insufficient_history")
            continue
        atrs[symbol] = signal.atr14
        _live("quant", symbol=symbol, score=round(signal.score, 4),
              rsi=round(signal.rsi14, 2), extended=bool(signal.extended))
        cached_adv = getattr(signal_model, "average_dollar_volume", None)
        adv = cached_adv(symbol, df) if callable(cached_adv) else average_dollar_volume(df)
        held = positions.get(symbol)
        item = _Work(symbol=symbol, signal=signal, held=held, adv=adv)

        # Hard risk override: stop / trailing stop / take-profit beats any model
        # output. Refresh the peak first so the trailing stop sees today's high.
        if held is not None:
            exit_signal = _refresh_peak_and_check_exit(
                conn, peaks, cycle_timestamp, settings.risk,
                df=df, signal=signal, held=held, log=log,
            )
            if exit_signal:
                item.forced_exit = exit_signal
                _live("decide", symbol=symbol, action="sell", forced=True,
                      trigger=exit_signal.trigger)
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
            _live("decide", symbol=symbol, action="avoid", combined=round(signal.score, 4),
                  reason="adv_floor")
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
            if wants_entry and not settings.risk.allow_intraday_entries:
                # Risk-watching only: the fast tier still runs exits, stop
                # reconciliation, and trailing ratchets on live prices — it just
                # leaves new positions to the twice-daily cycle, which scores
                # settled bars. See allow_intraday_entries in config/risk.yaml.
                _journal_safe(log, clear_intraday_confirmation, conn, symbol)
                continue
            actionable = wants_entry or (quant_only.action == Action.SELL and held is not None)
            if not actionable:
                # A name that stopped qualifying loses its streak — otherwise an
                # on-again/off-again symbol would bank confirmations across
                # unrelated crossings and enter on the strength of neither.
                _journal_safe(log, clear_intraday_confirmation, conn, symbol)
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
            if llm_fail_streak >= LLM_FAIL_FAST_STREAK:
                log.info("%s: analyst circuit open — skipping LLM this cycle.", symbol)
            else:
                news = feed.news(symbol)
                fundamentals = feed.fundamentals(symbol)
                _live("llm_call_start", symbol=symbol)
                _t0 = time.perf_counter()
                verdict = analyze(signal, news, fundamentals, settings)
                _ms = int((time.perf_counter() - _t0) * 1000)
                if verdict is None:
                    llm_fail_streak += 1
                    _live("llm_call_done", symbol=symbol, ms=_ms, error="none")
                    if llm_fail_streak >= LLM_FAIL_FAST_STREAK:
                        log.error(
                            "Analyst CLI/API failed %d symbols in a row — skipping remaining "
                            "LLM calls this cycle (fail-closed: new entries WAIT). Exits and "
                            "stops still run.",
                            LLM_FAIL_FAST_STREAK,
                        )
                        _live("llm_circuit_open", streak=LLM_FAIL_FAST_STREAK)
                else:
                    llm_fail_streak = 0
                    _live("llm_call_done", symbol=symbol, ms=_ms, stance=verdict.stance)
                # Recorded for BOTH modes: a symbol the twice-daily deep cycle just
                # analyzed shouldn't be immediately re-analyzed by the next
                # fast-tier scan a few minutes later either. Skipped-circuit
                # names are not recorded — they were never analyzed.
                _journal_safe(log, record_escalation, conn, symbol, cycle_timestamp, signal.score)
        item.verdict = verdict

        item.decision = decide(
            signal=signal,
            verdict=verdict,
            has_open_position=held is not None,
            **decide_kw,
        )
        _live("decide", symbol=symbol, action=item.decision.action.value,
              combined=round(item.decision.combined_score, 4),
              quant=round(item.decision.quant_score, 4))
        work.append(item)

    # Holdings no longer on any list still get full protection: their peaks
    # ratchet and the hard exits see them. Dropping a held name from the
    # watchlist otherwise silently downgrades its trailing stop to a frozen
    # ATR-from-entry stop and blinds every client-side exit check.
    for symbol, held in positions.items():
        if symbol in cycle_symbols:
            continue
        _live("symbol_enter", symbol=symbol, off_watchlist=True)
        df = feed.price_history(symbol, asof=asof)
        signal = compute(symbol, df, weights=signal_weights)
        if signal is None:
            log.warning("Held %s is off-watchlist with no usable history — "
                        "cannot refresh its peak or exit checks.", symbol)
            _live("quant", symbol=symbol, error="insufficient_history", off_watchlist=True)
            continue
        atrs[symbol] = signal.atr14
        _live("quant", symbol=symbol, score=round(signal.score, 4),
              rsi=round(signal.rsi14, 2), extended=bool(signal.extended),
              off_watchlist=True)
        item = _Work(symbol=symbol, signal=signal, held=held, adv=average_dollar_volume(df))
        item.forced_exit = _refresh_peak_and_check_exit(
            conn, peaks, cycle_timestamp, settings.risk,
            df=df, signal=signal, held=held, log=log,
        )
        # Hard stops alone are not the whole exit contract: README step 4 gives
        # an open position a signal SELL once quant falls through
        # sell_threshold. Without a decision the signal-exit loop below skips
        # this item entirely, so a decayed off-watchlist name could only ever
        # leave via a 12% trailing giveback — and nothing about it reaches the
        # journal, making it invisible to evaluate/scorecard/daily_report.
        # verdict=None is deliberate: the LLM can neither originate nor block a
        # SELL (decision/engine.py), so an off-watchlist holding costs no call.
        # This cannot open new risk — the buy path skips anything already held.
        #
        # These names are also held to legacy_sell_threshold rather than
        # sell_threshold: they are leftovers from a pool swap, outside the book
        # under validation, and they occupy exposure and book stop-risk that the
        # names being tested cannot then use. A stricter exit bar returns that
        # budget instead of parking it. Books whose watchlist always retains its
        # holdings (P2's roster does) never reach this path.
        item.decision = decide(
            signal=signal, verdict=None, has_open_position=True,
            **{**decide_kw, "sell_threshold": settings.risk.legacy_sell_threshold},
        )
        _live("decide", symbol=symbol, action=item.decision.action.value,
              combined=round(item.decision.combined_score, 4),
              quant=round(item.decision.quant_score, 4), off_watchlist=True)
        work.append(item)

    # Exits first so they free shares (and, on a sim broker, cash) before buys.
    # Each exit is isolated: one symbol's broker/journal failure must not skip
    # another's protection.
    for item in work:
        if item.forced_exit is None:
            continue
        reasoning = f"Risk override: {item.forced_exit.trigger} — {item.forced_exit.detail}."
        try:
            if live_session and not market_open:
                # A market sell after the close is guaranteed rejected. Leave
                # the position (and its resting stop) untouched; tomorrow's
                # cycle re-runs this check against the fresh open.
                item.order_status = "queued_closed"
                item.decision = Decision(
                    symbol=item.symbol, action=Action.SELL, combined_score=item.signal.score,
                    quant_score=item.signal.score, llm_score=None, conflicting_signals=False,
                    reasoning=reasoning + " Queued — market closed; the next open re-checks.",
                )
                log.info("%s: %s detected after close — queued for the next session.",
                         item.symbol, item.forced_exit.trigger)
                continue
            order = _submit_protected_sell(
                broker, log, symbol=item.symbol, qty=item.held.qty, purpose="exit",
                peaks=peaks, entry_price=item.held.avg_entry_price,
                atr14=item.signal.atr14, risk=settings.risk,
                cycle_timestamp=cycle_timestamp, ids=ids,
            )
            if order is None:
                log.error("%s: exit order failed — position REMAINS OPEN and unprotected.", item.symbol)
                reasoning += " EXIT ORDER FAILED — position still open."
            else:
                orders_this_cycle += 1
                _journal_safe(log, clear_position_peak, conn, item.symbol)
                _journal_safe(log, clear_position_state, conn, item.symbol)
                log.info("%s: forced SELL (%s) — %s", item.symbol, item.forced_exit.trigger, item.forced_exit.detail)
                item.notional = item.held.market_value
            item.order_status = order.status if order else "rejected"
            item.order_qty = order.qty if order else None
            item.decision = Decision(
                symbol=item.symbol, action=Action.SELL, combined_score=item.signal.score,
                quant_score=item.signal.score, llm_score=None, conflicting_signals=False,
                reasoning=reasoning,
            )
        except Exception:
            log.exception("%s: forced-exit handling failed mid-way — "
                          "retried from scratch next cycle.", item.symbol)

    for item in work:
        if item.forced_exit is not None or item.decision is None:
            continue
        if item.decision.action != Action.SELL or item.held is None:
            continue
        try:
            if live_session and not market_open:
                # Same after-close policy as forced exits: don't burn a Grok
                # call on a guaranteed-rejected market order.
                item.order_status = "queued_closed"
                item.decision.reasoning += " Queued — market closed; the next open re-checks."
                continue
            if not fast_mode:
                _grok_confirm(settings, item, "sell", log)
            order = _submit_protected_sell(
                broker, log, symbol=item.symbol, qty=item.held.qty, purpose="sell",
                peaks=peaks, entry_price=item.held.avg_entry_price,
                atr14=item.signal.atr14, risk=settings.risk,
                cycle_timestamp=cycle_timestamp, ids=ids,
            )
            if order is None:
                item.order_status = "rejected"
                item.decision.reasoning += " SELL ORDER FAILED — position still open."
                log.error("%s: signal exit failed — position REMAINS OPEN.", item.symbol)
            else:
                item.order_status, item.order_qty = order.status, order.qty
                item.notional = item.held.market_value
                orders_this_cycle += 1
                _journal_safe(log, clear_position_peak, conn, item.symbol)
                _journal_safe(log, clear_position_state, conn, item.symbol)
        except Exception:
            log.exception("%s: signal-exit handling failed mid-way.", item.symbol)

    unknown_regime = regime.is_unknown

    trim_armed = False
    if not fast_mode and not use_dry_run:
        # Selling the existing book is double-buffered (depth + dwell, see
        # _update_trim_dwell); the risk-off tightening on NEW entries above is
        # immediate. Dry runs never advance the live sell counter.
        trim_armed = _update_trim_dwell(conn, regime, settings.risk, log, cycle_timestamp)
    if trim_armed and (not live_session or market_open):
        invested_value, open_position_count = _apply_trims(
            work=work, positions=positions, account=account, settings=settings,
            broker=broker, conn=conn, invested_value=invested_value,
            open_position_count=open_position_count, regime=regime,
            peaks=peaks, atrs=atrs, log=log, cycle_timestamp=cycle_timestamp,
            ids=ids,
        )

    exposure_pct = effective_max_exposure_pct(settings.risk, regime.label)
    sizing_risk = replace(settings.risk, max_total_exposure_pct=exposure_pct)
    sector_map = _resolve_sectors(
        settings, feed, asof, list({*cycle_symbols, *positions}),
    )
    occupancy = sector_invested(positions, sector_map)
    for item in work:
        if (
            item.notional
            and item.decision is not None
            and item.decision.action in (Action.SELL, Action.TRIM)
            and item.order_status not in (None, "rejected")
        ):
            sec = sector_of(item.symbol, sector_map)
            occupancy[sec] = max(0.0, occupancy.get(sec, 0.0) - item.notional)

    if live_session and market_open and not unknown_regime:
        flushed, cash_remaining, invested_value, open_position_count, orders_this_cycle = _flush_trade_intents(
            broker=broker, conn=conn, settings=settings, account=account,
            positions=positions, pending_buys=pending_buys, peaks=peaks,
            cash_remaining=cash_remaining, invested_value=invested_value,
            open_position_count=open_position_count, orders_this_cycle=orders_this_cycle,
            regime_multiplier=regime_multiplier, cycle_timestamp=cycle_timestamp, log=log,
            atrs=atrs, feed=feed, asof=asof,
            sizing_risk=sizing_risk, sector_map=sector_map, occupancy=occupancy,
            orders_readable=orders_readable, ids=ids,
        )
        pending_buys.update(flushed)

    buy_items = [
        item for item in work
        if item.decision is not None and item.decision.action == Action.BUY and item.forced_exit is None
    ]
    buy_items.sort(key=lambda it: it.decision.combined_score, reverse=True)

    for item in buy_items:
        decision = item.decision
        signal = item.signal
        if entry_symbols is not None and item.symbol not in entry_symbols:
            decision.action = Action.WAIT
            decision.reasoning += " Skipped: symbol is not entry-eligible on this session."
            continue
        if item.symbol in pending_buys:
            decision.reasoning += " Skipped: an earlier buy for this symbol is still unfilled."
            continue
        if item.symbol in positions:
            decision.reasoning += " Skipped: already held."
            continue
        if (
            settings.risk.require_llm_for_entry
            and use_llm
            and item.verdict is None
        ):
            # The analyst was supposed to read this symbol and produced nothing
            # (CLI/API crashed mid-cycle). A decision chain missing a layer
            # must not open new risk — fail closed. (--skip-llm is an explicit
            # operator override and does not hit this guard.)
            decision.action = Action.WAIT
            decision.reasoning += (
                " Skipped: the analyst produced no verdict for this symbol "
                "(LLM failed mid-cycle) and require_llm_for_entry is on — "
                "new entries fail closed."
            )
            log.info("%s: BUY downgraded to WAIT — no LLM verdict available.", item.symbol)
            _live("order_or_veto", symbol=item.symbol, status="veto", kind="fail_closed")
            continue
        if unknown_regime:
            decision.reasoning += " Skipped: macro regime UNKNOWN — no new entries."
            continue
        if orders_this_cycle >= settings.risk.max_new_orders_per_cycle:
            decision.reasoning += " Skipped: max_new_orders_per_cycle reached for this run."
            continue
        if live_session:
            # Every live entry queues as a TradeIntent and executes inside the
            # entry window (default 10:00–15:30 ET), never as an immediate
            # market order: the open's first half hour is the most expensive
            # tape of the day to cross, and the flush re-runs the gap check
            # plus the chase guards against the live price first.
            if use_dry_run:
                # A dry run shares this journal; queuing intents here would
                # have the next REAL cycle execute them on the paper account.
                decision.reasoning += (
                    " Skipped: a dry run never queues TradeIntents — re-decide in a real cycle."
                )
                log.info("%s: dry run — BUY not queued as TradeIntent.", item.symbol)
                _live("order_or_veto", symbol=item.symbol, status="dry_run", kind="buy")
                continue
            intent = TradeIntent(
                symbol=item.symbol, created_at=cycle_timestamp,
                signal_price=signal.last_price, atr14=signal.atr14,
                quant_score=decision.quant_score, combined_score=decision.combined_score,
                reasoning=decision.reasoning,
                not_before=_entry_not_before_utc(datetime.now(timezone.utc), settings.risk),
            )
            if not _journal_safe(log, save_trade_intent, conn, intent):
                # An unpersisted intent must not be reported as queued — it
                # would silently never execute.
                decision.action = Action.WAIT
                decision.reasoning += (
                    " Skipped: could not persist the TradeIntent — will re-decide next cycle."
                )
                continue
            item.order_status = "intent"
            decision.reasoning += (
                " Recorded as TradeIntent — executes in the entry window "
                f"({settings.risk.entry_window_start_et}–{settings.risk.entry_window_end_et} ET) "
                "after gap and chase revalidation."
            )
            log.info("%s: BUY queued as TradeIntent @ %.2f, not before %s.",
                     item.symbol, signal.last_price, intent.not_before)
            _live("order_or_veto", symbol=item.symbol, status="intent", kind="buy",
                  price=round(signal.last_price, 4))
            continue

        # Backtest/sim path only (asof set): live entries never reach here —
        # they go through the TradeIntent queue and its revalidation gates.
        live_price = signal.last_price

        stop_pct, book_risk, corr_mult, room = _entry_sizing_inputs(
            symbol=item.symbol, atr14=signal.atr14, live_price=live_price,
            positions=positions, atrs=atrs, feed=feed, asof=asof,
            settings=settings, occupancy=occupancy, sector_map=sector_map,
            account_equity=account.equity,
            # Count buys already sized this cycle as if they were on the book.
            extra_held=[*pending_buys, *(it.symbol for it in buy_items if it.notional)],
            cycle_book_risk=sum(
                (it.notional or 0.0) * stop_distance_pct(it.signal.atr14, it.signal.last_price, settings.risk)
                for it in buy_items
                if it.notional
            ),
            correlation_model=correlation_model,
            reserved_notional={
                it.symbol: float(it.notional or 0.0)
                for it in buy_items if it.notional
            },
        )
        sizing = size_position(
            symbol=item.symbol, last_price=live_price, equity=account.equity,
            cash=cash_remaining, invested_value=invested_value,
            open_position_count=open_position_count, risk=sizing_risk,
            stop_pct=stop_pct, regime_multiplier=regime_multiplier,
            existing_stop_risk=book_risk, corr_multiplier=corr_mult,
            sector_room=room,
        )
        item.sizing_reason = sizing.reason
        _live("sizing", symbol=item.symbol, approved=sizing.approved,
              notional=round(sizing.notional, 2), reason=sizing.reason)
        if sizing.approved:
            if not fast_mode:
                _grok_confirm(settings, item, "buy", log)
            order = broker.submit_notional_buy(
                item.symbol, sizing.notional, atr14=signal.atr14,
                client_order_id=ids.mint("buy", item.symbol),
            )
            if order is None:
                item.order_status = "rejected"
                decision.reasoning += f" {sizing.reason}, but the order was REJECTED (notional orders need regular market hours)."
                _live("order_or_veto", symbol=item.symbol, status="rejected", kind="buy")
            else:
                item.order_status, item.order_qty = order.status, order.qty
                item.notional = sizing.notional
                item.stop_price = protective_stop_price(
                    live_price, live_price, signal.atr14, settings.risk,
                )
                cash_remaining -= sizing.notional
                invested_value += sizing.notional
                sec = sector_of(item.symbol, sector_map)
                occupancy[sec] = occupancy.get(sec, 0.0) + sizing.notional
                open_position_count += 1
                orders_this_cycle += 1
                _journal_safe(log, update_position_peak, conn, item.symbol, live_price, cycle_timestamp)
                _journal_safe(log, clear_intraday_confirmation, conn, item.symbol)
                _journal_safe(log, clear_trade_intent, conn, item.symbol)
                decision.reasoning += f" {sizing.reason}."
                _live("order_or_veto", symbol=item.symbol, status=item.order_status or "filled",
                      kind="buy", notional=round(sizing.notional, 2))
        else:
            decision.reasoning += f" Risk manager vetoed buy: {sizing.reason}."
            _live("order_or_veto", symbol=item.symbol, status="veto", kind="sizing",
                  reason=sizing.reason)

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
            llm_evidence_quality=getattr(verdict, "evidence_quality", None) if verdict else None,
            llm_risk_flags=", ".join(verdict.risk_flags) if verdict and verdict.risk_flags else None,
        ))

    # Re-read positions so newly filled buys are protected too, then make sure
    # everything held is sitting behind a stop at the current level.
    try:
        final_positions = broker.get_positions()
    except Exception:
        log.exception("Could not re-read positions; reconciling against the opening snapshot instead.")
        final_positions = positions
    stop_coverage = _reconcile_protective_stops(
        broker, final_positions, peaks, atrs, settings.risk, log,
        live=live_session, ids=ids,
    )

    if isinstance(broker, DryRunBroker):
        mode = "dry_run"
    elif asof is not None:
        mode = "backtest"
    else:
        mode = "paper"
    if not _journal_safe(log, record_cycle, conn, cycle_timestamp, mode,
                         account.equity, account.cash, rows,
                         regime_score=regime.score, regime_label=regime.label):
        log.error("This cycle's decisions were NOT journaled — audit trail has a gap.")
    # Observability: stamp the heartbeat while conn is still open. Live runs
    # only — a backtest/test replay carries a throwaway journal whose ids and
    # timestamps would falsely freshen the watchdog stamp.
    if asof is None:
        write_heartbeat(conn, "fast" if fast_mode else "deep", stop_coverage=stop_coverage)
    _stamp_cycle_progress(
        asof=asof, fast_mode=fast_mode, cycle_no=cycle_no, account=account,
        positions=final_positions, rows=rows, orders_this_cycle=orders_this_cycle,
        stop_coverage=stop_coverage, status="ok",
    )
    if owns_conn:
        conn.close()
    if fast_mode:
        log.info("Fast-tier scan complete: %d/%d symbols escalated to full analysis, %d orders placed.",
                 len(rows), len(settings.watchlist), orders_this_cycle)
    else:
        log.info("Cycle complete: %d symbols evaluated, %d orders placed.", len(rows), orders_this_cycle)
    _live("cycle_end", n_evaluated=len(rows), n_orders=orders_this_cycle,
          fast=fast_mode, mode="fast" if fast_mode else "deep")


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
    # The deep task and the fast scan are independent scheduled tasks that can
    # overlap; the OS-level lock keeps two cycles from trading one account at
    # the same time. The kernel releases it if this process dies, so a killed
    # cycle cannot leave a stale lock behind.
    log = logging.getLogger("run_cycle")

    # A fast scan fires every 20 minutes and, with allow_intraday_entries off,
    # can place no entries at all. The deep cycle runs twice a day and is the
    # only path that opens a position. When the two collide the deep cycle used
    # to lose and exit, silently dropping that decision point. Two guards:
    # the fast scan steps aside near a deep slot, and the deep cycle waits
    # instead of giving up.
    if args.fast and _near_deep_cycle(datetime.now(ET)):
        log.info("Fast-tier scan yielding — within %d min of a deep cycle (%s ET); "
                 "the deep cycle takes the lock.", DEEP_CYCLE_YIELD_MINUTES, RUN_TIMES_ET)
        _stamp_cycle_progress(
            asof=None, fast_mode=True, cycle_no=None, account=None, positions=None,
            status="no_trade", skipped="yielded to deep cycle",
        )
        return

    lock = CycleLock()
    wait = 0.0 if args.fast else DEEP_LOCK_WAIT_SECONDS
    if not lock.acquire(timeout=wait):
        if args.fast:
            log.warning(
                "Could not acquire the cycle lock (%s) — another cycle is running; exiting.",
                lock.path,
            )
        else:
            # The deep cycle waited and still lost: this is a dropped decision
            # point, not routine contention. Make it loud — it used to be one
            # WARNING in a log nobody reads.
            log.error(
                "DEEP CYCLE SKIPPED — waited %.0fs for the cycle lock (%s) and never got it. "
                "No entry or exit decisions were made this session.",
                wait, lock.path,
            )
            _notify_toast("Deep cycle skipped: could not acquire the cycle lock.")
        _stamp_cycle_progress(
            asof=None, fast_mode=args.fast, cycle_no=None, account=None, positions=None,
            status="failed", skipped="cycle lock not acquired",
        )
        return
    try:
        run_cycle(force_dry_run=args.dry_run, skip_llm=args.skip_llm, fast_mode=args.fast)
    except Exception:
        _stamp_cycle_progress(
            asof=None, fast_mode=args.fast, cycle_no=None, account=None, positions=None,
            status="failed", skipped="cycle raised",
        )
        raise
    finally:
        lock.release()


if __name__ == "__main__":
    main()
