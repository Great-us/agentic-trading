"""R7 (c2c_a7e2 ITERATION 2, review §R7): one recursive safe-cleaning step for
intent-event payloads, shared by both destinations.

The review's named leak: payload={"sizing": {"api_key": "SENTINEL"},
"prompt": "SENTINEL"} reached SQLite with the nested api_key intact, because
the top-level whitelist never looked inside nested dicts. Contract (leader
dispatch, ITERATION 2):
- top level: INTENT_EVENT_PAYLOAD_KEYS whitelist (unknown keys dropped);
- nested dicts / lists: recursed, INTENT_EVENT_SECRET_KEYS blacklist applied
  at every depth (vocabulary mirrors live_events._SECRET_KEYS — equality is
  pinned below so the two cannot drift silently);
- non-finite floats -> None everywhere;
- non-JSON-native objects -> str() everywhere;
- sqlite text (via _encode_payload) and the dict handed to the JSONL
  destination (funnel wiring, leader) both come from the same function, so
  their content must agree.
"""
from __future__ import annotations

import json
import sqlite3
from decimal import Decimal

import pytest

from agentic_trading import live_events
from agentic_trading.journal import logger as logger_mod
from agentic_trading.journal.logger import connect, record_intent_event


def _no_nan_constants(const):
    """json.loads hook: any NaN/Infinity literal in stored text fails hard."""
    raise ValueError(f"non-finite JSON literal reached storage: {const}")


def _assert_safe_json(raw: str) -> dict:
    assert raw is not None
    assert "SENTINEL" not in raw, f"secret sentinel leaked into storage: {raw!r}"
    return json.loads(raw, parse_constant=_no_nan_constants)


# ---------------------------------------------------------------------------
# SQLite path (via record_intent_event -> _encode_payload)
# ---------------------------------------------------------------------------

def test_review_sentinel_scenario_never_reaches_sqlite(tmp_path):
    """The exact scenario the review named: nested api_key + top-level prompt."""
    conn = connect(tmp_path / "n.db")
    record_intent_event(
        conn, "VEEV", "sizing", True, "x",
        payload={"sizing": {"api_key": "SENTINEL"}, "prompt": "SENTINEL"},
    )
    raw = conn.execute("SELECT payload FROM intent_events").fetchone()[0]
    data = _assert_safe_json(raw)
    assert "prompt" not in data                    # top-level whitelist
    assert "api_key" not in data.get("sizing", {})  # nested blacklist
    conn.close()


def test_secret_keys_dropped_in_nested_dicts_lists_and_top(tmp_path):
    conn = connect(tmp_path / "n.db")
    record_intent_event(
        conn, "VEEV", "order_submitted", True, "x",
        payload={
            "api_key": "SENTINEL",                          # top level
            "secret_key": "SENTINEL",                       # top level
            "reason": "ok",
            "sizing": {"cash_available": 12.5,
                       "auth": {"token": "SENTINEL"}},      # two levels deep
            "limits": ["exposure", {"password": "SENTINEL"}],  # list element
            "note": ({"moonshot_api_key": "SENTINEL"},),    # tuple element
        },
    )
    data = _assert_safe_json(conn.execute(
        "SELECT payload FROM intent_events").fetchone()[0])
    assert data["reason"] == "ok"
    assert data["sizing"]["cash_available"] == 12.5
    # "auth" is not itself a secret key NAME, so it survives — but its secret
    # subtree ("token") is gone, leaving an empty dict. Safety is content-level.
    assert data["sizing"]["auth"] == {}
    assert data["limits"] == ["exposure", {}]           # list position kept
    assert data["note"] == [{}]
    conn.close()


def test_nonfinite_floats_nested_become_null(tmp_path):
    conn = connect(tmp_path / "n.db")
    record_intent_event(
        conn, "VEEV", "sizing", True, "x",
        payload={"reason": "r",
                 "limit": float("-inf"),
                 "sizing": {"min_notional": float("nan"),
                            "nested": {"deep": float("inf")}}},
    )
    data = _assert_safe_json(conn.execute(
        "SELECT payload FROM intent_events").fetchone()[0])
    assert data["limit"] is None
    assert data["sizing"]["min_notional"] is None
    assert data["sizing"]["nested"]["deep"] is None
    conn.close()


# ---------------------------------------------------------------------------
# sanitize_intent_payload contract (the function the funnel wiring will reuse)
# ---------------------------------------------------------------------------

def test_sanitize_intent_payload_top_level_contract():
    sanitize = logger_mod.sanitize_intent_payload
    assert sanitize(None) is None
    assert sanitize({"reason": "ok", "unknown_key": "drop me"}) == {"reason": "ok"}
    # non-dict payloads wrap, same as the SQLite encoder's historical contract
    assert sanitize(42.5) == {"value": 42.5}
    out = sanitize({"sizing": {"api_key": "s", "cash_available": 1.0}})
    assert out == {"sizing": {"cash_available": 1.0}}
    json.dumps(out, allow_nan=False)  # plain JSON types, directly dumps-able


def test_unserializable_objects_become_str():
    sentinel_obj = object()
    cleaned = logger_mod.sanitize_intent_payload(
        {"reason": "x", "limit": Decimal("1.5"),
         "sizing": {"note": sentinel_obj, "deep": [sentinel_obj]}})
    assert cleaned["limit"] == str(Decimal("1.5"))
    assert isinstance(cleaned["sizing"]["note"], str)
    assert isinstance(cleaned["sizing"]["deep"][0], str)


def test_secret_vocabulary_lockstep_with_live_events():
    assert logger_mod.INTENT_EVENT_SECRET_KEYS == live_events._SECRET_KEYS


def test_sanitized_dict_matches_sqlite_text_content(tmp_path):
    """The dict sanitize_intent_payload returns is what the JSONL destination
    receives (funnel wiring, leader): it must round-trip to the same content
    the SQLite text stores — one cleaning step, two destinations."""
    payload = {"sizing": {"api_key": "SENTINEL"}, "prompt": "SENTINEL",
               "reason": "r", "limit": float("nan")}
    cleaned = logger_mod.sanitize_intent_payload(payload)
    as_jsonl = json.dumps(cleaned, ensure_ascii=False, allow_nan=False)
    conn = connect(tmp_path / "n.db")
    record_intent_event(conn, "VEEV", "sizing", True, "x", payload=payload)
    as_sqlite = conn.execute(
        "SELECT payload FROM intent_events").fetchone()[0]
    assert json.loads(as_jsonl, parse_constant=_no_nan_constants) == \
        json.loads(as_sqlite, parse_constant=_no_nan_constants)
    conn.close()


def test_mixed_payload_with_unknown_and_secret_and_object(tmp_path):
    """Mixed shapes in one event: unknown top-level key, secret at every
    depth, non-finite, non-serializable, tuple, deep nesting."""
    payload = {
        "not_in_whitelist": {"api_key": "SENTINEL"},
        "reason": {"detail": "mixed", "token": "SENTINEL"},
        "sizing": {"limits": ({"secret": "SENTINEL"}, "book_risk"),
                   "when": "2026-09-20T14:00:00+00:00",
                   "junk": object(),
                   "ratio": float("nan")},
        "order_id": "ord-1",
    }
    conn = connect(tmp_path / "n.db")
    record_intent_event(conn, "VEEV", "order_submitted", True, "x",
                        payload=payload)
    data = _assert_safe_json(conn.execute(
        "SELECT payload FROM intent_events").fetchone()[0])
    assert "not_in_whitelist" not in data
    assert data["reason"] == {"detail": "mixed"}          # token dropped
    assert data["sizing"]["limits"] == [{}, "book_risk"]
    assert isinstance(data["sizing"]["junk"], str)
    assert data["sizing"]["ratio"] is None
    assert data["order_id"] == "ord-1"
    conn.close()
