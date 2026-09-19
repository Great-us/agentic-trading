"""round_trips.py — average-cost walk over fill history.

Fixed, offline sample data only (no network). Covers the avg_entry=0 bug
(bought_notional was reusing a mutated `cost` variable — see AGENTS.md F3)
and the fills-vs-broker reconciliation added alongside it (see F1's real
VEEV example: a fills-derived open lot with no matching broker position).
"""
from __future__ import annotations

import pytest

from agentic_trading.round_trips import reconcile_positions, realized_pnl_timeline, round_trips


def _fill(symbol: str, side: str, qty: float, price: float, ts: str, **extra) -> dict:
    row = {
        "symbol": symbol,
        "side": side,
        "qty": qty,
        "price": price,
        "notional": round(qty * price, 2),
        "transaction_time": ts,
        "order_status": "filled",
    }
    row.update(extra)
    return row


class TestClosedTrip:
    def test_simple_buy_then_sell(self):
        fills = [
            _fill("X", "buy", 1.0, 100.0, "2026-01-02T15:00:00Z"),
            _fill("X", "sell", 1.0, 110.0, "2026-01-03T15:00:00Z"),
        ]
        trips = round_trips(fills)
        assert len(trips) == 1
        trip = trips[0]
        assert trip["open"] is False
        assert trip["avg_entry"] == 100.0
        assert trip["avg_exit"] == 110.0
        assert trip["realized_pnl"] == 10.0

    def test_batched_buys_then_single_sell(self):
        fills = [
            _fill("Y", "buy", 1.0, 100.0, "2026-01-02T15:00:00Z"),
            _fill("Y", "buy", 1.0, 120.0, "2026-01-02T16:00:00Z"),
            _fill("Y", "sell", 2.0, 130.0, "2026-01-03T15:00:00Z"),
        ]
        trips = round_trips(fills)
        assert len(trips) == 1
        trip = trips[0]
        assert trip["avg_entry"] == 110.0  # (100+120)/2
        assert trip["avg_exit"] == 130.0
        assert trip["realized_pnl"] == 40.0  # (130-110)*2

    def test_sell_twice_after_two_buys_averages_exit(self):
        fills = [
            _fill("Z", "buy", 1.0, 100.0, "2026-01-02T15:00:00Z"),
            _fill("Z", "buy", 1.0, 120.0, "2026-01-02T16:00:00Z"),
            _fill("Z", "sell", 1.0, 130.0, "2026-01-03T15:00:00Z"),
            _fill("Z", "sell", 1.0, 150.0, "2026-01-03T16:00:00Z"),
        ]
        trips = round_trips(fills)
        assert len(trips) == 1
        trip = trips[0]
        assert trip["avg_entry"] == 110.0
        assert trip["avg_exit"] == 140.0  # (130+150)/2
        assert trip["realized_pnl"] == 60.0  # 20 + 40


class TestOpenTrip:
    def test_partial_sell_keeps_lot_open_with_correct_avg_entry(self):
        fills = [
            _fill("Y", "buy", 2.0, 100.0, "2026-01-02T15:00:00Z"),
            _fill("Y", "sell", 1.0, 110.0, "2026-01-03T15:00:00Z"),
        ]
        trips = round_trips(fills)
        assert len(trips) == 1
        trip = trips[0]
        assert trip["open"] is True
        assert trip["qty"] == 1.0
        assert trip["avg_entry"] == 100.0
        assert trip["realized_pnl"] == 10.0


class TestNewTripAfterFullClose:
    def test_sell_then_buy_opens_a_second_independent_trip(self):
        fills = [
            _fill("X", "buy", 1.0, 100.0, "2026-01-02T15:00:00Z"),
            _fill("X", "sell", 1.0, 110.0, "2026-01-03T15:00:00Z"),
            _fill("X", "buy", 1.0, 120.0, "2026-01-04T15:00:00Z"),
            _fill("X", "sell", 1.0, 130.0, "2026-01-05T15:00:00Z"),
        ]
        trips = round_trips(fills)
        closed = sorted((t for t in trips if not t["open"]), key=lambda t: t["opened_at"])
        assert len(closed) == 2
        first, second = closed
        assert first["avg_entry"] == 100.0 and first["avg_exit"] == 110.0 and first["realized_pnl"] == 10.0
        assert second["avg_entry"] == 120.0 and second["avg_exit"] == 130.0 and second["realized_pnl"] == 10.0


class TestDuplicateAndSameTimestampFills:
    def test_duplicate_fill_id_is_not_deduped_by_round_trips_itself(self):
        # De-duplication is broker_read.fills()'s job (by activity id, across
        # pages); round_trips just walks whatever series it is handed. This
        # documents that a genuinely duplicated row would double-count, so a
        # caller must dedupe before calling in.
        fills = [
            _fill("X", "buy", 1.0, 100.0, "2026-01-02T15:00:00Z", id="a1"),
            _fill("X", "sell", 1.0, 110.0, "2026-01-03T15:00:00Z", id="a2"),
        ]
        deduped = list({f["id"]: f for f in fills + [fills[0]]}.values())
        trips = round_trips(deduped)
        assert len(trips) == 1
        assert trips[0]["realized_pnl"] == 10.0

    def test_same_timestamp_fills_without_id_tie_break_are_flagged_ambiguous(self):
        # No usable ids: whatever order this happens to walk in (here, the
        # input's own [buy, sell] order, since a stable sort can't reorder
        # equal keys) is a guess, not a known fact — the numbers can still
        # come out sensible, but callers must be told they're unverified.
        fills = [
            _fill("X", "buy", 1.0, 100.0, "2026-01-02T15:00:00Z"),
            _fill("X", "sell", 1.0, 110.0, "2026-01-02T15:00:00Z"),
        ]
        trips = round_trips(fills)
        assert len(trips) == 1
        assert trips[0]["avg_entry"] == 100.0
        assert trips[0]["realized_pnl"] == 10.0
        assert trips[0]["ambiguous"] is True

    def test_same_timestamp_reversed_input_without_tie_break_produces_orphan_and_open(self):
        # Same pair, but the sell is listed BEFORE the buy in the input and
        # there is no id to determine the true order — a stable sort leaves
        # them exactly as given, so the walk (wrongly, but unavoidably)
        # treats the sell as having no lot to sell against. Both resulting
        # records must be marked ambiguous so this doesn't read as fact.
        fills = [
            _fill("X", "sell", 1.0, 110.0, "2026-01-02T15:00:00Z"),
            _fill("X", "buy", 1.0, 100.0, "2026-01-02T15:00:00Z"),
        ]
        trips = round_trips(fills)
        orphan = next(t for t in trips if t.get("incomplete"))
        opened = next(t for t in trips if t["open"])
        assert orphan["ambiguous"] is True
        assert opened["ambiguous"] is True
        assert opened["avg_entry"] == 100.0

    def test_same_timestamp_with_id_prefix_tie_break_resolves_correct_order(self):
        # Alpaca-style ids ("<numeric timestamp>::<uuid>") let the walk sort
        # correctly even when the input list has the sell listed first and
        # transaction_time alone can't distinguish them.
        fills = [
            _fill("X", "sell", 1.0, 110.0, "2026-01-02T15:00:00Z",
                  id="20260102150000002::deadbeef-0000-0000-0000-000000000002"),
            _fill("X", "buy", 1.0, 100.0, "2026-01-02T15:00:00Z",
                  id="20260102150000001::deadbeef-0000-0000-0000-000000000001"),
        ]
        trips = round_trips(fills)
        assert len(trips) == 1
        assert trips[0]["avg_entry"] == 100.0
        assert trips[0]["realized_pnl"] == 10.0
        assert trips[0]["ambiguous"] is False

    def test_same_side_tie_at_same_timestamp_is_never_ambiguous(self):
        # Two buys at the same instant, no ids: order doesn't affect the
        # totals, so this must not be flagged even without a tie-break.
        fills = [
            _fill("X", "buy", 1.0, 100.0, "2026-01-02T15:00:00Z"),
            _fill("X", "buy", 1.0, 120.0, "2026-01-02T15:00:00Z"),
            _fill("X", "sell", 2.0, 130.0, "2026-01-03T15:00:00Z"),
        ]
        trips = round_trips(fills)
        assert len(trips) == 1
        assert trips[0]["avg_entry"] == 110.0
        assert trips[0]["ambiguous"] is False

    def test_same_timestamp_and_same_id_prefix_is_still_ambiguous(self):
        # R5 (ChatGPT review, PROGRESS.md §10): both fills have a parseable
        # id prefix, but it's the SAME prefix (same millisecond) — only the
        # trailing UUID differs. That is not a real tie-break: the full sort
        # key `(transaction_time, prefix)` still ties, so [sell, buy] input
        # order must not be trusted as "buy first" just because ids exist.
        fills_sell_first = [
            _fill("X", "sell", 1.0, 110.0, "2026-01-02T15:00:00Z",
                  id="20260102150000000::aaaaaaaa-0000-0000-0000-000000000001"),
            _fill("X", "buy", 1.0, 100.0, "2026-01-02T15:00:00Z",
                  id="20260102150000000::bbbbbbbb-0000-0000-0000-000000000002"),
        ]
        trips = round_trips(fills_sell_first)
        orphan = next(t for t in trips if t.get("incomplete"))
        opened = next(t for t in trips if t["open"])
        assert orphan["ambiguous"] is True
        assert opened["ambiguous"] is True

        # Same pair, buy listed first — a real tie-break would not care
        # about input order, but this IS a tie, so the result (and the
        # ambiguous flag) legitimately depends on it; both directions must
        # still come out flagged.
        fills_buy_first = [
            _fill("X", "buy", 1.0, 100.0, "2026-01-02T15:00:00Z",
                  id="20260102150000000::bbbbbbbb-0000-0000-0000-000000000002"),
            _fill("X", "sell", 1.0, 110.0, "2026-01-02T15:00:00Z",
                  id="20260102150000000::aaaaaaaa-0000-0000-0000-000000000001"),
        ]
        trips2 = round_trips(fills_buy_first)
        assert len(trips2) == 1
        assert trips2[0]["ambiguous"] is True

    @pytest.mark.parametrize("input_order", ["as_listed", "reversed"])
    def test_only_sell_missing_prefix_is_still_ambiguous(self, input_order):
        # R5 follow-up (ChatGPT review, PROGRESS.md §11): buy has a
        # parseable prefix, sell doesn't. A missing prefix sorts as "",
        # which is lower than any real numeric prefix, so the walk puts the
        # prefix-less sell BEFORE the buy — sort() reorders by key value
        # regardless of input order once the keys genuinely differ, so both
        # orderings below produce the identical (wrong-looking) walk: an
        # orphan sell with no lot to match, then a fresh open buy. That is
        # exactly the "缺前缀的卖出被硬排前 → 假孤儿 + 假 open" case from the
        # review, and both resulting trips must be flagged ambiguous.
        buy = _fill("X", "buy", 1.0, 100.0, "2026-01-02T15:00:00Z",
                    id="20260102150000000::cccccccc-0000-0000-0000-000000000003")
        sell = _fill("X", "sell", 1.0, 110.0, "2026-01-02T15:00:00Z")  # no id
        fills = [buy, sell] if input_order == "as_listed" else [sell, buy]

        trips = round_trips(fills)
        orphan = next(t for t in trips if t.get("incomplete"))
        opened = next(t for t in trips if t["open"])
        assert orphan["ambiguous"] is True
        assert opened["ambiguous"] is True
        assert realized_pnl_timeline(trips) == []  # nothing confident to sum

    @pytest.mark.parametrize("input_order", ["as_listed", "reversed"])
    def test_only_buy_missing_prefix_is_still_ambiguous(self, input_order):
        # Mirror case: sell has a prefix, buy doesn't — the prefix-less buy
        # sorts first (by the same "" artifact), which this time happens to
        # produce a normal-looking closed trip with a real $10 realized_pnl.
        # That number must still be flagged ambiguous and excluded from the
        # confident cumulative total — a missing prefix on the OTHER side
        # would have produced a completely different (orphan) result, so
        # this coincidentally-plausible-looking number is not evidence the
        # order was actually resolved.
        buy = _fill("X", "buy", 1.0, 100.0, "2026-01-02T15:00:00Z")  # no id
        sell = _fill("X", "sell", 1.0, 110.0, "2026-01-02T15:00:00Z",
                     id="20260102150000000::cccccccc-0000-0000-0000-000000000003")
        fills = [buy, sell] if input_order == "as_listed" else [sell, buy]

        trips = round_trips(fills)
        assert len(trips) == 1
        closed = trips[0]
        assert closed["open"] is False
        assert not closed.get("incomplete")
        assert closed["realized_pnl"] == 10.0  # a real, non-None number...
        assert closed["ambiguous"] is True
        assert realized_pnl_timeline(trips) == []  # ...that must not be summed


class TestIncompleteOrphanSell:
    def test_sell_with_no_prior_buy_is_flagged_incomplete_not_dropped(self):
        fills = [_fill("AVGO", "sell", 3.0, 100.0, "2026-06-15T15:00:00Z")]
        trips = round_trips(fills)
        assert len(trips) == 1
        trip = trips[0]
        assert trip["open"] is False
        assert trip["incomplete"] is True
        assert trip["symbol"] == "AVGO"
        assert trip["qty"] == 3.0
        assert trip["closed_at"] == "2026-06-15T15:00:00Z"
        assert trip["avg_entry"] is None
        assert trip["realized_pnl"] is None

    def test_orphan_sells_accumulate_until_a_buy_resets_the_run(self):
        fills = [
            _fill("AVGO", "sell", 1.0, 100.0, "2026-06-10T15:00:00Z"),
            _fill("AVGO", "sell", 2.0, 105.0, "2026-06-15T15:00:00Z"),
            _fill("AVGO", "buy", 1.0, 110.0, "2026-06-20T15:00:00Z"),
        ]
        trips = round_trips(fills)
        incomplete = [t for t in trips if t.get("incomplete")]
        assert len(incomplete) == 1
        assert incomplete[0]["qty"] == 3.0
        open_trip = [t for t in trips if t["open"]]
        assert len(open_trip) == 1
        assert open_trip[0]["qty"] == 1.0
        assert open_trip[0]["avg_entry"] == 110.0

    def test_oversell_beyond_the_tracked_lot_folds_into_orphan_not_dropped(self):
        # Window saw a 1-share buy, but the sell is for 2 shares — the extra
        # share must have come from a position this window never saw
        # opened. The matched share closes a normal trip; the excess must
        # not silently vanish (previously: qty went negative, cost floored
        # to 0, and the second share was never accounted for anywhere).
        fills = [
            _fill("X", "buy", 1.0, 100.0, "2026-01-02T15:00:00Z"),
            _fill("X", "sell", 2.0, 110.0, "2026-01-03T15:00:00Z"),
        ]
        trips = round_trips(fills)
        closed = next(t for t in trips if not t["open"] and not t.get("incomplete"))
        orphan = next(t for t in trips if t.get("incomplete"))
        assert closed["qty"] == 1.0
        assert closed["avg_entry"] == 100.0
        assert closed["avg_exit"] == 110.0
        assert closed["realized_pnl"] == 10.0
        assert orphan["qty"] == 1.0
        assert orphan["avg_exit"] == 110.0
        assert orphan["avg_entry"] is None
        assert orphan["realized_pnl"] is None

    def test_oversell_remainder_accumulates_with_a_later_true_orphan_sell(self):
        fills = [
            _fill("X", "buy", 1.0, 100.0, "2026-01-02T15:00:00Z"),
            _fill("X", "sell", 3.0, 110.0, "2026-01-03T15:00:00Z"),  # 2 shares oversold
            _fill("X", "sell", 1.0, 111.0, "2026-01-04T15:00:00Z"),  # fully orphaned
        ]
        trips = round_trips(fills)
        orphan = next(t for t in trips if t.get("incomplete"))
        assert orphan["qty"] == 3.0  # 2 (oversold remainder) + 1 (no lot at all)
        assert orphan["closed_at"] == "2026-01-04T15:00:00Z"  # flushed at series end

    def test_incomplete_trips_excluded_from_realized_pnl_timeline(self):
        fills = [
            _fill("A", "buy", 1.0, 10.0, "2026-01-02T15:00:00Z"),
            _fill("A", "sell", 1.0, 12.0, "2026-01-03T15:00:00Z"),
            _fill("B", "sell", 1.0, 50.0, "2026-01-04T15:00:00Z"),  # orphan
        ]
        trips = round_trips(fills)
        timeline = realized_pnl_timeline(trips)
        assert len(timeline) == 1
        assert timeline[0]["symbol"] == "A"
        assert timeline[0]["cum_realized_pnl"] == 2.0

    def test_ambiguous_trips_excluded_from_realized_pnl_timeline(self):
        """R5: a P&L number computed from an order the walk had to guess at
        must not enter a confident cumulative total, same treatment as
        incomplete trips."""
        fills = [
            _fill("A", "buy", 1.0, 10.0, "2026-01-02T15:00:00Z"),
            _fill("A", "sell", 1.0, 12.0, "2026-01-03T15:00:00Z"),
            # Same timestamp, no id — ambiguous, has a non-None realized_pnl
            # (unlike an orphan/incomplete trip), so it needs its own guard.
            _fill("B", "buy", 1.0, 50.0, "2026-01-04T15:00:00Z"),
            _fill("B", "sell", 1.0, 55.0, "2026-01-04T15:00:00Z"),
        ]
        trips = round_trips(fills)
        b_trip = next(t for t in trips if t["symbol"] == "B" and not t["open"])
        assert b_trip["ambiguous"] is True
        assert b_trip["realized_pnl"] is not None  # it has a number...
        timeline = realized_pnl_timeline(trips)
        assert len(timeline) == 1  # ...but that number must not reach the timeline
        assert timeline[0]["symbol"] == "A"
        assert timeline[0]["cum_realized_pnl"] == 2.0


class TestReconcilePositions:
    def test_agrees_everywhere_yields_no_diffs(self):
        trips = [{"symbol": "MSFT", "open": True, "qty": 3.0}]
        positions = [{"symbol": "MSFT", "qty": 3.0}]
        assert reconcile_positions(trips, positions) == []

    def test_missing_from_broker_is_reported(self):
        # The real VEEV case: fills say the lot is still open, but the
        # broker's positions list doesn't have it.
        trips = [{"symbol": "VEEV", "open": True, "qty": 5.831337}]
        diffs = reconcile_positions(trips, [])
        assert len(diffs) == 1
        assert diffs[0]["symbol"] == "VEEV"
        assert diffs[0]["fills_qty"] == 5.831337
        assert diffs[0]["broker_qty"] is None

    def test_missing_from_fills_is_reported(self):
        trips: list[dict] = []
        positions = [{"symbol": "NVDA", "qty": 8.0}]
        diffs = reconcile_positions(trips, positions)
        assert len(diffs) == 1
        assert diffs[0]["symbol"] == "NVDA"
        assert diffs[0]["fills_qty"] is None
        assert diffs[0]["broker_qty"] == 8.0

    def test_qty_mismatch_is_reported(self):
        trips = [{"symbol": "IQV", "open": True, "qty": 2.0}]
        positions = [{"symbol": "IQV", "qty": 2.5}]
        diffs = reconcile_positions(trips, positions)
        assert len(diffs) == 1
        assert diffs[0]["fills_qty"] == 2.0
        assert diffs[0]["broker_qty"] == 2.5

    def test_closed_trips_are_ignored_not_compared(self):
        trips = [{"symbol": "X", "open": False, "qty": 1.0}]
        positions: list[dict] = []
        assert reconcile_positions(trips, positions) == []

    def test_ambiguous_symbol_reported_as_undetermined_even_when_qty_matches(self):
        """R5: an ambiguous trip means the fills order was never actually
        verified — the symbol must surface as undetermined even if its
        computed qty happens to equal the broker's, so "agrees" never
        silently covers for "we don't actually know"."""
        trips = [{"symbol": "X", "open": True, "qty": 3.0, "ambiguous": True}]
        positions = [{"symbol": "X", "qty": 3.0}]
        diffs = reconcile_positions(trips, positions)
        assert len(diffs) == 1
        assert diffs[0]["symbol"] == "X"
        assert diffs[0]["undetermined"] is True
        assert "无法确定" in diffs[0]["detail"]

    def test_ambiguous_symbol_reported_as_undetermined_not_a_concrete_diff(self):
        trips = [{"symbol": "X", "open": True, "qty": 3.0, "ambiguous": True}]
        positions = [{"symbol": "X", "qty": 9.0}]  # would otherwise read as a qty mismatch
        diffs = reconcile_positions(trips, positions)
        assert len(diffs) == 1
        assert diffs[0]["undetermined"] is True
        assert "数量不一致" not in diffs[0]["detail"]

    def test_non_ambiguous_symbols_unaffected_by_an_ambiguous_one_elsewhere(self):
        trips = [
            {"symbol": "MSFT", "open": True, "qty": 3.0},
            {"symbol": "X", "open": True, "qty": 3.0, "ambiguous": True},
        ]
        positions = [{"symbol": "MSFT", "qty": 3.0}, {"symbol": "X", "qty": 3.0}]
        diffs = reconcile_positions(trips, positions)
        assert len(diffs) == 1  # MSFT still agrees quietly; only X is flagged
        assert diffs[0]["symbol"] == "X"
        assert diffs[0]["undetermined"] is True
