"""broker_read.BookBrokerReader.fills() — pagination, dedup, truncation.

No network: monkeypatches `_get` directly. Credentials are irrelevant here
since `_get` never actually runs; a throwaway book_root is enough.
"""
from __future__ import annotations

from pathlib import Path

from agentic_trading.broker_read import BookBrokerReader


def _activity(i: int, order_id: str = "o1") -> dict:
    return {
        "id": f"act-{i}",
        "order_id": order_id,
        "symbol": "X",
        "side": "buy",
        "qty": "1",
        "price": "100",
        "transaction_time": f"2026-01-{i:02d}T15:00:00Z",
        "order_status": "filled",
    }


class TestFillsShape:
    def test_fills_carry_id_and_order_id(self, tmp_path: Path):
        reader = BookBrokerReader(tmp_path)
        reader._get = lambda path, params=None: [_activity(1)]
        fills, reason = reader.fills()
        assert reason is None
        assert fills[0]["id"] == "act-1"
        assert fills[0]["order_id"] == "o1"


class TestPaginationDedup:
    def test_pages_are_concatenated_and_deduped_by_id(self, tmp_path: Path):
        # Page 1 is a full page (100 items) so the loop asks for a second
        # page; simulate the last item of page 1 reappearing as the first
        # item of page 2, the way an off-by-one at the page boundary would.
        page1 = [_activity(i) for i in range(1, 101)]
        page2 = [page1[-1]] + [_activity(101)]

        calls = {"n": 0}

        def fake_get(path, params=None):
            calls["n"] += 1
            return page1 if calls["n"] == 1 else page2

        reader = BookBrokerReader(tmp_path)
        reader._get = fake_get
        fills, reason = reader.fills()
        assert reason is None
        ids = [f["id"] for f in fills]
        assert len(ids) == len(set(ids)) == 101
        assert reader.last_fills_truncated is False

    def test_truncation_flag_set_when_max_records_hit(self, tmp_path: Path):
        # Two full pages (unique ids) so the cap (150) is hit mid-second-page,
        # before the feed itself signals "no more data".
        pages = [[_activity(i) for i in range(1, 101)], [_activity(i) for i in range(101, 201)]]
        calls = {"n": 0}

        def fake_get(path, params=None):
            page = pages[calls["n"]]
            calls["n"] += 1
            return page

        reader = BookBrokerReader(tmp_path)
        reader._get = fake_get
        fills, reason = reader.fills(max_records=150)
        assert reason is None
        assert len(fills) == 150
        assert reader.last_fills_truncated is True

    def test_no_truncation_when_feed_ends_before_cap(self, tmp_path: Path):
        def fake_get(path, params=None):
            return [_activity(i) for i in range(1, 51)]  # < page_size, so it's the last page

        reader = BookBrokerReader(tmp_path)
        reader._get = fake_get
        fills, reason = reader.fills(max_records=1000)
        assert reason is None
        assert len(fills) == 50
        assert reader.last_fills_truncated is False


# ---- R4 (ChatGPT review, PROGRESS.md §10): an empty page is the feed's
# ---- normal "nothing more" signal, not a stuck-loop symptom — must never
# ---- be marked truncated, or downstream reconciliation silently skips its
# ---- per-symbol check on a perfectly complete fetch. ----------------------

class TestEmptyPageIsNotTruncation:
    def test_empty_history_first_page(self, tmp_path: Path):
        def fake_get(path, params=None):
            return []  # brand-new account, or a book with zero fills ever

        reader = BookBrokerReader(tmp_path)
        reader._get = fake_get
        fills, reason = reader.fills()
        assert reason is None
        assert fills == []
        assert reader.last_fills_truncated is False

    def test_empty_page_right_after_exactly_one_full_page(self, tmp_path: Path):
        # 100 fills, then the feed ends — the empty page confirming that is
        # a completely ordinary shape, not evidence of truncation.
        pages = [[_activity(i) for i in range(1, 101)], []]
        calls = {"n": 0}

        def fake_get(path, params=None):
            page = pages[calls["n"]]
            calls["n"] += 1
            return page

        reader = BookBrokerReader(tmp_path)
        reader._get = fake_get
        fills, reason = reader.fills()
        assert reason is None
        assert len(fills) == 100
        assert reader.last_fills_truncated is False
        assert calls["n"] == 2

    def test_empty_page_right_after_exactly_two_full_pages(self, tmp_path: Path):
        pages = [
            [_activity(i) for i in range(1, 101)],
            [_activity(i) for i in range(101, 201)],
            [],
        ]
        calls = {"n": 0}

        def fake_get(path, params=None):
            page = pages[calls["n"]]
            calls["n"] += 1
            return page

        reader = BookBrokerReader(tmp_path)
        reader._get = fake_get
        fills, reason = reader.fills()
        assert reason is None
        assert len(fills) == 200
        assert reader.last_fills_truncated is False
        assert calls["n"] == 3

    def test_repeated_full_page_is_still_truncated_and_bounded(self, tmp_path: Path):
        # Contrast case: the page is NOT empty, just entirely duplicate rows
        # — that's the real stuck-loop symptom this fix must not weaken.
        calls = {"n": 0}

        def fake_get(path, params=None):
            calls["n"] += 1
            return [_activity(i) for i in range(1, 101)]  # identical every time

        reader = BookBrokerReader(tmp_path)
        reader._get = fake_get
        fills, reason = reader.fills()
        assert reason is None
        assert len(fills) == 100
        assert reader.last_fills_truncated is True
        assert calls["n"] == 2


# ---- R4 (ChatGPT review, PROGRESS.md §9): fills() must never spin forever
# ---- on a broken/looping paginating feed — this runs before the cycle
# ---- lock is released, so a hang here would wedge the whole cycle. -------

class TestPaginationCannotHang:
    def test_repeated_full_page_stops_instead_of_looping_forever(self, tmp_path: Path):
        """The endpoint keeps handing back the exact same full page (ids and
        all) no matter what page_token is sent — a real server bug, not
        hypothetical. Dedup means the second such page adds zero new rows;
        that must stop the loop, not just shrink the growth rate to zero
        forever."""
        calls = {"n": 0}

        def fake_get(path, params=None):
            calls["n"] += 1
            return [_activity(i) for i in range(1, 101)]  # identical every time

        reader = BookBrokerReader(tmp_path)
        reader._get = fake_get
        fills, reason = reader.fills()
        assert reason is None
        assert len(fills) == 100  # only the first page's unique ids
        assert reader.last_fills_truncated is True
        # One page fetched, one more to discover it's a dead repeat, then stop.
        assert calls["n"] == 2

    def test_page_token_loop_is_detected_even_with_fresh_ids(self, tmp_path: Path):
        """A subtler failure than exact repetition: each page has brand-new
        ids (so the "zero new rows" guard alone would never fire), but the
        *token* the server hands back — derived from the last row's own id,
        the same way the real endpoint works — cycles back to one already
        used. Must stop on the token collision rather than re-requesting
        forever."""

        def _page(start: int, count: int, next_token: str) -> list[dict]:
            items = [_activity(start + i) for i in range(count - 1)]
            last = _activity(start + count - 1)
            last["id"] = next_token  # pagination cursor = last row's own id
            return items + [last]

        # Page 3's cursor loops back to "PAGE2", already used to fetch page 2.
        pages = {
            None: _page(1, 100, "PAGE2"),
            "PAGE2": _page(101, 100, "PAGE3"),
            "PAGE3": _page(201, 100, "PAGE2"),
        }
        calls = {"n": 0}

        def fake_get(path, params=None):
            calls["n"] += 1
            token = (params or {}).get("page_token")
            return pages[token]

        reader = BookBrokerReader(tmp_path)
        reader._get = fake_get
        fills, reason = reader.fills()
        assert reason is None
        # 100 + 100 + 99: the pagination cursor IS the last row's own id, so
        # page 3's cursor reusing "PAGE2" also makes its last row a genuine
        # duplicate of page 1's last row — id-dedup correctly drops that one
        # row on its own. The other 99 rows on page 3 are all fresh, so this
        # is still exercising the token-loop guard specifically, not the
        # "whole page added nothing" guard from the test above.
        assert len(fills) == 299
        assert reader.last_fills_truncated is True
        assert calls["n"] == 3  # stopped before re-requesting token "PAGE2"

    def test_page_budget_caps_total_requests(self, tmp_path: Path, monkeypatch):
        """Belt-and-suspenders: even if a feed keeps producing fresh ids AND
        never reuses a token, a hard page-count ceiling still bounds the
        total work per fills() call."""
        from agentic_trading.broker_read import BookBrokerReader as Reader

        monkeypatch.setattr(Reader, "MAX_FILLS_PAGES", 3)
        calls = {"n": 0}

        def fake_get(path, params=None):
            calls["n"] += 1
            start = calls["n"] * 1000
            return [_activity(start + i) for i in range(1, 101)]

        reader = Reader(tmp_path)
        reader._get = fake_get
        fills, reason = reader.fills(max_records=10_000)
        assert reason is None
        assert calls["n"] == 3
        assert len(fills) == 300
        assert reader.last_fills_truncated is True
