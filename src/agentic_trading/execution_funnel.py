"""Execution-funnel observation plumbing for c2c_a7e2 (P1-A, contract §14.2).

One normalized event travels to two independent, individually fault-tolerant
destinations: the SQLite ``intent_events`` ledger (via
``journal.logger.record_intent_event``) and the live_events JSONL tail (new
stage ``"funnel"``; the cockpit stages are untouched). JSONL rotates and only
replays 120 lines — it is the "live process" view; the SQLite ledger is the
only full-day funnel account.

Hard rules (PLAN §A-2):
- This module never returns a go/no-go decision, never mutates trading state,
  never imports dashboard, and must not change the trading-call sequence.
- Every public entry point swallows its own failures AFTER logging them:
  field construction, serialization, the SQLite write and the JSONL write are
  each isolated, so one destination failing never blocks the other and never
  propagates into the trading path.
- Identity minting uses uuid4 only — never OrderIdMinter, so observation can
  not consume order-id sequence numbers.

OrderObservation → kind mapping (leader decision, §14.2 vocab):
- observation missing / status None      → ``order_unknown``  (unverified —
  absence of evidence is not evidence of a fill or a reject)
- status == "filled"                     → ``order_filled``
- 0 < filled_qty and not filled          → ``order_partial``
- status in DEAD_ORDER_STATUSES          → ``order_observed`` with
  payload.order_status (terminal non-fill: rejected/canceled/expired)
- anything else (accepted/new/held/...)  → ``order_observed``

One vocabulary extension beyond §14.2 (leader, flagged for ChatGPT review):
``intent_cleared`` with payload.reason ∈ {already_held, pending_buy} — the
flush branch that clears an older intent because the symbol is now held has
no place in ``intent_not_created`` (that intent WAS created, in an earlier
run) and is not a wait; a truthful terminal event needs its own kind.

Run-level events (``flush_skipped``) carry ``symbol="*"`` — the column is
NOT NULL and no single symbol owns a run-level skip.
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from typing import Any, Callable, Protocol

from . import live_events
from .journal.logger import ROOT, record_intent_event, sanitize_intent_payload

log = logging.getLogger("execution_funnel")


def _DEAD_STATUSES() -> frozenset:
    """Broker dead-status set, imported lazily so this module never creates an
    import cycle with execution.broker."""
    from .execution.broker import DEAD_ORDER_STATUSES
    return DEAD_ORDER_STATUSES

BOOK_ID_ENV = "AGTRADING_BOOK_ID"
RUN_LEVEL_SYMBOL = "*"

# Total order-observation request budget per cycle (review R4): this cycle's
# own submissions first, then recovered unresolved orders, newest first.
MAX_ORDER_OBSERVATIONS_PER_CYCLE = 8


def default_book_id() -> str:
    """Stable per-book identity for run_id: env override, else the book root's
    directory name (this package lives at <book>/src/agentic_trading)."""
    return os.environ.get(BOOK_ID_ENV) or ROOT.name


def mint_run_id(cycle_started_at_iso: str, book_id: str | None = None) -> str:
    """`{book_id}:{cycle_started_at_iso}:{uuid4[:8]}` — minted once per cycle,
    never from MAX(cycles.id)+1 (that estimate is not a reliable key)."""
    return f"{book_id or default_book_id()}:{cycle_started_at_iso}:{uuid.uuid4().hex[:8]}"


def decision_key_of(run_id: str, symbol: str) -> str:
    return f"{run_id}:{symbol}"


def attempt_id_of(intent_id: str, run_id: str) -> str:
    return f"{intent_id}:{run_id}"


def flush_run_skip(cycle_started_at_iso: str, mode: str, reason: str,
                   *, jsonl: bool = True) -> None:
    """Convenience for cycle paths that exit BEFORE the journal is opened
    (e.g. the fast-tier market-closed early return): one JSONL-only run-level
    flush skip — conn is None by design on those paths. jsonl=False keeps
    historical/sim runs off the live tail (review R2)."""
    try:
        FunnelRecorder(None, mint_run_id(cycle_started_at_iso), mode,
                       jsonl=jsonl).flush_skipped(reason)
    except Exception:
        log.warning("funnel: run-level skip event failed — continuing.",
                    exc_info=True)


class _ObservingBroker(Protocol):
    def observe_order(self, order_id: str): ...  # OrderObservation | None


class _NullFunnelRecorder:
    """Degraded recorder used when even construction fails (review R1: a
    mint_run_id failure must never abort a cycle). Every method is a no-op;
    events are dropped, never fabricated."""

    run_id = ""

    def record(self, *a, **k):
        return None

    def note_submission(self, *a, **k):
        pass

    def observe_unresolved(self, *a, **k):
        pass

    def decision_buy(self, *a, **k):
        pass

    def intent_not_created(self, *a, **k):
        pass

    def intent_saved(self, *a, **k):
        pass

    def intent_cleared_held(self, *a, **k):
        pass

    def flush_skipped(self, *a, **k):
        pass

    def flush_wait(self, *a, **k):
        pass

    def order_submitted(self, *a, **k):
        pass

    def observe(self, *a, **k):
        pass


def make_funnel(conn, cycle_started_at_iso: str, mode: str, *, jsonl: bool = True):
    """Never-raises factory (review R1): any construction failure — including
    run-id minting — degrades to the null recorder instead of killing the
    cycle. jsonl=False isolates historical/sim runs from the live tail
    (review R2): their events only reach the caller-provided journal."""
    try:
        return FunnelRecorder(conn, mint_run_id(cycle_started_at_iso), mode, jsonl=jsonl)
    except Exception:
        log.warning("funnel: recorder construction failed — running the cycle "
                    "without funnel observation.", exc_info=True)
        return _NullFunnelRecorder()


class FunnelRecorder:
    """Per-cycle funnel event recorder. All record* methods are best-effort:
    they log and continue on any internal failure. ``conn`` may be None (e.g.
    the fast-tier market-closed early return happens before the journal is
    opened) — then only the JSONL destination receives the event."""

    def __init__(self, conn, run_id: str, mode: str,
                 *, jsonl: bool = True,
                 live_emit: Callable[..., None] | None = None):
        self._conn = conn
        self.run_id = run_id
        self.mode = mode
        # R2: jsonl=False (backtest/sim runs) detaches the live tail entirely;
        # an explicitly injected live_emit always wins, None + jsonl=True binds
        # the module default AT INIT TIME (tests monkeypatch it before creation).
        if live_emit is not None:
            self._live_emit = live_emit
        else:
            self._live_emit = live_events.emit if jsonl else None
        self._observed_orders: set[str] = set()  # budget: once per order per cycle

    # -- cross-cycle order recovery (review R4) -------------------------------

    def note_submission(self, symbol: str, order_id: str | None,
                        intent_id: str | None = None) -> None:
        """Register an order submitted THIS cycle; observed once, later, by
        observe_unresolved() after all order-affecting work is done."""
        try:
            self._pending_submissions.append((symbol, order_id, intent_id))
        except AttributeError:
            self._pending_submissions = [(symbol, order_id, intent_id)]

    def _unresolved_from_ledger(self, conn) -> list[tuple[str, str]]:
        """Recover order_ids the ledger still shows open: submitted with a real
        order_id, in this recorder's mode, without a terminal observation yet
        (order_filled, or an observation whose status is dead). Newest event
        first. Read-only; any failure returns [] (R1 applies here too)."""
        try:
            rows = conn.execute(
                "SELECT order_id, symbol, MAX(id) AS last_id FROM intent_events "
                "WHERE kind = 'order_submitted' AND order_id IS NOT NULL "
                "  AND mode = ? "
                "GROUP BY order_id ORDER BY last_id DESC",
                (self.mode,),
            ).fetchall()
            unresolved: list[tuple[str, str, str | None]] = []
            for order_id, symbol, _ in rows:
                state = conn.execute(
                    "SELECT kind, payload, intent_id FROM intent_events "
                    "WHERE order_id = ? AND mode = ? ORDER BY id DESC",
                    (order_id, self.mode)).fetchall()
                terminal = False
                submit_intents: set[str] = set()
                for kind, payload_text, intent_id in state:
                    if kind == "order_submitted" and intent_id:
                        submit_intents.add(intent_id)
                    if kind == "order_filled":
                        terminal = True
                        break
                    if kind in ("order_observed", "order_partial") and payload_text:
                        # R5 (round 2): terminal recognition must not depend on
                        # the event being order_observed — a DEAD status in ANY
                        # order event (e.g. canceled with a partial fill) ends
                        # the re-query loop for this order.
                        try:
                            status = (json.loads(payload_text) or {}).get("order_status")
                        except Exception:
                            status = None
                        if status in _DEAD_STATUSES():
                            terminal = True
                            break
                if not terminal:
                    # R4 (round 3): ownership must be UNIQUE — conflicting
                    # submits (same order_id, different intents) yield no
                    # identity instead of picking the newest submit; the
                    # aggregation's conflict handling then decides.
                    identity = next(iter(submit_intents)) if len(submit_intents) == 1 else None
                    unresolved.append((symbol, order_id, identity))
            return unresolved
        except Exception:
            log.warning("funnel: unresolved-order recovery failed — skipping "
                        "recovered observations this cycle.", exc_info=True)
            return []

    def observe_unresolved(self, conn, broker) -> None:
        """Cycle-END observation pass (review R4): runs after ALL order-affecting
        processing (flush, new intents, end-of-cycle stop reconciliation) is
        complete. This cycle's submissions first, then recovered unresolved
        orders from the persistent ledger, bounded by
        MAX_ORDER_OBSERVATIONS_PER_CYCLE total requests. Never raises; no new
        scheduling, no intent revival, no budget release, no order retry."""
        try:
            candidates = [(s, o, i) for s, o, i in getattr(
                self, "_pending_submissions", []) if o]
            seen = {o for _, o, _ in candidates}
            for symbol, order_id, rec_intent in self._unresolved_from_ledger(conn):
                if order_id not in seen:
                    # R4 (round 2): the recovered intent_id rides along so the
                    # observation lands on the original chain.
                    candidates.append((symbol, order_id, rec_intent))
                    seen.add(order_id)
            for symbol, order_id, intent_id in candidates[:MAX_ORDER_OBSERVATIONS_PER_CYCLE]:
                self.observe(symbol, order_id, broker, intent_id=intent_id)
            self._pending_submissions = []
        except Exception:
            log.warning("funnel: cycle-end observation pass failed — trading "
                        "already complete, continuing.", exc_info=True)

    # -- core ---------------------------------------------------------------

    def _resolve_payload(self, payload: Any) -> Any:
        """Payload may be a lazy zero-arg callable so field CONSTRUCTION is
        also inside the observation fault boundary: if it raises, the event is
        still recorded with a null payload rather than lost (or worse, thrown
        into the trading path)."""
        if callable(payload):
            try:
                return payload()
            except Exception:
                log.warning("funnel: payload construction failed — recording "
                            "event without payload.", exc_info=True)
                return None
        return payload

    def record(self, kind: str, symbol: str, *, deferred: bool = True,
               detail: str | None = None, intent_id: str | None = None,
               decision: str | None = None, attempt: str | None = None,
               order_id: str | None = None, payload: Any = None,
               event_id: str | None = None) -> str | None:
        """One funnel event to both destinations; returns the event_id (or
        None if even minting failed — R1: never raises into the caller)."""
        try:
            event_id = event_id or uuid.uuid4().hex
        except Exception:
            log.warning("funnel: event-id minting failed — dropping one event "
                        "(%s/%s).", symbol, kind, exc_info=True)
            return None
        payload = self._resolve_payload(payload)
        # R7: ONE sanitized copy serves BOTH destinations — the raw payload is
        # never handed to the JSONL tail, and recursive secret keys / non-finite
        # numbers are scrubbed before either write (never blocks the event).
        if payload is not None:
            try:
                payload = sanitize_intent_payload(payload) if isinstance(payload, dict) else None
            except Exception:
                log.warning("funnel: payload sanitization failed — recording "
                            "event without payload.", exc_info=True)
                payload = None
        if intent_id and attempt is None:
            # attempt identity follows the contract formula automatically;
            # explicitly-passed values (future reuse) win.
            attempt = attempt_id_of(intent_id, self.run_id)
        try:
            if self._conn is not None:
                record_intent_event(
                    self._conn, symbol, kind, deferred, detail,
                    event_id=event_id, run_id=self.run_id, mode=self.mode,
                    decision_key=decision, intent_id=intent_id,
                    attempt_id=attempt, order_id=order_id, payload=payload,
                )
        except Exception:
            log.warning("funnel: SQLite event write failed (%s/%s) — trading continues.",
                        symbol, kind, exc_info=True)
        try:
            if self._live_emit is not None:
                self._live_emit("funnel", kind=kind, symbol=symbol, run_id=self.run_id,
                                mode=self.mode, event_id=event_id,
                                decision_key=decision, intent_id=intent_id,
                                attempt_id=attempt, order_id=order_id, payload=payload,
                                detail=detail)
        except Exception:
            log.warning("funnel: JSONL emit failed (%s/%s) — trading continues.",
                        symbol, kind, exc_info=True)
        return event_id

    # -- semantic helpers -----------------------------------------------------

    def decision_buy(self, symbol: str) -> None:
        """The funnel denominator: the FINAL decide() returned BUY for this
        symbol in this run. Fast-tier quant-only candidates never emit this."""
        self.record("decision_buy", symbol, deferred=False)

    def intent_not_created(self, symbol: str, reason: str) -> None:
        """A recorded BUY decision that did not produce an intent.
        reason ∈ entry_ineligible / pending_buy / already_held /
        llm_fail_closed / regime_unknown / max_new_orders / dry_run / save_failed."""
        self.record("intent_not_created", symbol, deferred=False,
                    detail=reason, payload={"reason": reason})

    def intent_saved(self, symbol: str, receipt) -> None:
        """Emit only after a successful business save; intent_id is the
        receipt's version (the only trustworthy identity — never re-query).
        R1: a broken receipt object is dropped, never raised — a metadata
        failure must not turn a successful save into WAIT."""
        try:
            kind = "intent_created" if receipt.action == "created" else "intent_replaced"
            version = receipt.version
            previous = receipt.previous_version
        except Exception:
            log.warning("funnel: unreadable save receipt for %s — dropping "
                        "intent_saved event.", symbol, exc_info=True)
            return
        try:
            decision = decision_key_of(self.run_id, symbol)
        except Exception:
            decision = None
        self.record(kind, symbol, deferred=False, intent_id=version,
                    decision=decision,
                    payload={"previous_version": previous})

    def intent_cleared_held(self, symbol: str, reason: str,
                            intent_id: str | None) -> None:
        """Flush cleared an older intent because the symbol is now held or has
        a pending buy (vocabulary extension, see module docstring)."""
        self.record("intent_cleared", symbol, deferred=False, intent_id=intent_id,
                    detail=reason, payload={"reason": reason})

    def flush_skipped(self, reason: str) -> None:
        """Run-level: the whole flush did not run.
        reason ∈ outside_window / orders_unreadable / market_closed / regime_unknown."""
        self.record("flush_skipped", RUN_LEVEL_SYMBOL, deferred=False,
                    detail=reason, payload={"reason": reason})

    def flush_wait(self, symbol: str, reason: str, *,
                   intent_id: str | None = None, payload: dict | None = None) -> None:
        """This attempt waited (intent kept). reason ∈ not_before / no_quote /
        open_missing (the open-relative gate could not run — NOT a pass)."""
        body = {"reason": reason, **(payload or {})}
        self.record("flush_wait", symbol, deferred=True, intent_id=intent_id,
                    detail=reason, payload=body)

    def order_submitted(self, symbol: str, order_id: str | None, *,
                        intent_id: str | None = None, client_order_id: str | None = None,
                        payload: Any = None) -> None:
        """The submit FACT: original response status, real order_id and the
        minted client_order_id. The caller passes payload.submit_status (e.g.
        "accepted" / "dry_run" / "rejected" for a None result) — never a fill
        claim; a quote is not a fill price."""
        resolved = self._resolve_payload(payload)
        body = dict(resolved) if isinstance(resolved, dict) else {}
        if client_order_id:
            body["client_order_id"] = client_order_id
        self.record("order_submitted", symbol, deferred=False, intent_id=intent_id,
                    order_id=order_id, payload=body)

    # -- observation ----------------------------------------------------------

    def observe(self, symbol: str, order_id: str, broker: _ObservingBroker, *,
                intent_id: str | None = None) -> None:
        """One budgeted observation per order per cycle (PLAN §A-3: at most
        once per order, bounded total requests, no retries, no waiting)."""
        if order_id is None or order_id in self._observed_orders:
            return
        self._observed_orders.add(order_id)
        observer = getattr(broker, "observe_order", None)
        if not callable(observer):
            # Old fakes / DryRunBroker: degrade to unverified, never fabricate.
            self.record("order_unknown", symbol, deferred=False, intent_id=intent_id,
                        order_id=order_id,
                        payload={"reason": "observe_unavailable"})
            return
        try:
            observation = observer(order_id)
        except Exception as exc:
            log.warning("funnel: observe_order(%s) raised — recording unverified.", order_id)
            observation = None
            self._observe_error = f"{type(exc).__name__}: {exc}"
        try:
            status = None if observation is None else observation.status
        except Exception:
            # R1: the very first attribute read can raise on a poisoned object.
            log.warning("funnel: reading observation for %s failed — recording "
                        "unverified.", order_id, exc_info=True)
            self.record("order_unknown", symbol, deferred=False, intent_id=intent_id,
                        order_id=order_id, payload={"reason": "observe_read_failed"})
            return
        if status is None:
            payload = {"reason": "observe_failed"}
            error = getattr(observation, "error", None) or getattr(self, "_observe_error", None)
            if error:
                payload["error"] = error
            self.record("order_unknown", symbol, deferred=False, intent_id=intent_id,
                        order_id=order_id, payload=payload)
            return
        try:
            payload = {"order_status": observation.status,
                       "observed_at": observation.observed_at}
            if observation.filled_qty is not None:
                payload["filled_qty"] = observation.filled_qty
            if observation.filled_avg_price is not None:
                payload["filled_avg_price"] = observation.filled_avg_price
            if observation.filled_at:
                payload["filled_at"] = observation.filled_at
            # R5 (review round 2): the terminal fact outranks the partial
            # fact — a canceled order with a partial fill keeps BOTH in the
            # payload (order_status + filled_qty) under order_partial, and the
            # recovery query treats its DEAD status as terminal regardless of
            # which kind the event carries.
            kind = "order_observed"
            if observation.status == "filled":
                kind = "order_filled"
            elif observation.filled_qty:  # > 0 but not (yet) filled
                kind = "order_partial"
        except Exception:
            # R1: even a poisoned observation object (attribute access raising)
            # degrades to unknown — never propagates into the flush loop.
            log.warning("funnel: reading observation for %s failed — recording "
                        "unverified.", order_id, exc_info=True)
            self.record("order_unknown", symbol, deferred=False, intent_id=intent_id,
                        order_id=order_id, payload={"reason": "observe_read_failed"})
            return
        self.record(kind, symbol, deferred=False, intent_id=intent_id,
                    order_id=order_id, payload=payload)
