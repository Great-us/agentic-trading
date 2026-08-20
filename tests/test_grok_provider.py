"""Grok sentiment gate is informational-only, but a malformed verdict must
still never be treated as a directional signal — these lean on the same
'return None on anything ambiguous' contract as every other LLM provider."""
import json

from agentic_trading.llm.grok_provider import SentimentVerdict, _parse_verdict


def test_parses_valid_payload():
    v = _parse_verdict({"stance": "bullish", "confidence": 0.7, "summary": "s"}, "NVDA")
    assert v == SentimentVerdict(symbol="NVDA", stance="bullish", confidence=0.7, summary="s")


def test_normalizes_stance_case():
    v = _parse_verdict({"stance": "Bullish", "confidence": 0.5, "summary": "s"}, "T")
    assert v.stance == "bullish"


def test_rejects_invalid_stance():
    assert _parse_verdict({"stance": "very bullish", "confidence": 0.5, "summary": "s"}, "T") is None


def test_rejects_out_of_range_confidence():
    assert _parse_verdict({"stance": "bullish", "confidence": 1.5, "summary": "s"}, "T") is None


def test_rejects_missing_field():
    assert _parse_verdict({"stance": "bullish", "confidence": 0.5}, "T") is None


def test_directional_score_signs():
    assert _parse_verdict({"stance": "bullish", "confidence": 0.8, "summary": "s"}, "T").directional_score == 0.8
    assert _parse_verdict({"stance": "bearish", "confidence": 0.8, "summary": "s"}, "T").directional_score == -0.8
    assert _parse_verdict({"stance": "neutral", "confidence": 0.8, "summary": "s"}, "T").directional_score == 0.0


def test_two_concatenated_json_objects_extracts_the_first():
    # Observed live: --json-schema can emit a stub object then the real one,
    # back to back with no separator. extract_json_object must not choke.
    from agentic_trading.llm.json_extract import extract_json_object

    blob = (
        json.dumps({"stance": "neutral", "confidence": 0.0, "summary": "gathering..."})
        + json.dumps({"stance": "bullish", "confidence": 0.6, "summary": "real answer"})
    )
    result = extract_json_object(blob)
    assert result == {"stance": "neutral", "confidence": 0.0, "summary": "gathering..."}
