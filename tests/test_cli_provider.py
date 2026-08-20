"""The CLI provider parses free-form agent output, so its extraction path
carries more risk than the API provider's structured tool calls. These cover
the shapes real CLIs actually emit."""
import json

from agentic_trading.llm.cli_provider import _extract_json_object, _extract_text
from agentic_trading.llm.schema import parse_verdict

VERDICT_JSON = {
    "stance": "bullish",
    "confidence": 0.7,
    "rationale": "Solid growth.",
    "risk_flags": ["stretched valuation"],
}


def test_extract_text_from_stream_json():
    lines = [
        json.dumps({"role": "meta", "type": "system.version", "version": "0.36.1"}),
        json.dumps({"role": "assistant", "content": '{"ok": true}'}),
        json.dumps({"role": "meta", "type": "session.resume_hint", "content": "To resume: kimi -r x"}),
    ]
    assert _extract_text("\n".join(lines)) == '{"ok": true}'


def test_extract_text_handles_content_blocks():
    line = json.dumps({"role": "assistant", "content": [{"type": "text", "text": "hello"}]})
    assert _extract_text(line) == "hello"


def test_extract_text_falls_back_to_raw_output():
    # A CLI in plain-text mode emits no JSON envelopes at all.
    assert _extract_text("just prose") == "just prose"


def test_extract_text_ignores_meta_only_output():
    # Envelopes present but no assistant content -> fall back to raw, which the
    # JSON extractor will then reject.
    raw = json.dumps({"role": "meta", "type": "system.version"})
    assert _extract_text(raw) == raw


def test_extract_json_from_bare_object():
    assert _extract_json_object(json.dumps(VERDICT_JSON)) == VERDICT_JSON


def test_extract_json_from_markdown_fence():
    text = f"Here you go:\n```json\n{json.dumps(VERDICT_JSON)}\n```\nHope that helps."
    assert _extract_json_object(text) == VERDICT_JSON


def test_extract_json_ignores_braces_inside_strings():
    payload = {"stance": "neutral", "rationale": "uses { and } literally", "confidence": 0.5, "risk_flags": []}
    text = f"prose {json.dumps(payload)} trailing"
    assert _extract_json_object(text) == payload


def test_extract_json_returns_none_when_absent():
    assert _extract_json_object("no json here at all") is None


def test_extract_json_skips_malformed_leading_object():
    text = '{not valid json} then {"stance": "bearish", "confidence": 0.4, "rationale": "r", "risk_flags": []}'
    result = _extract_json_object(text)
    assert result is not None
    assert result["stance"] == "bearish"


def test_parse_verdict_rejects_bad_stance():
    assert parse_verdict({**VERDICT_JSON, "stance": "very bullish"}, "TEST") is None


def test_parse_verdict_rejects_out_of_range_confidence():
    assert parse_verdict({**VERDICT_JSON, "confidence": 1.7}, "TEST") is None


def test_parse_verdict_rejects_missing_field():
    assert parse_verdict({"stance": "bullish", "confidence": 0.5}, "TEST") is None


def test_parse_verdict_normalizes_stance_case():
    v = parse_verdict({**VERDICT_JSON, "stance": "Bullish"}, "TEST")
    assert v is not None and v.stance == "bullish"


def test_parse_verdict_tolerates_missing_risk_flags():
    v = parse_verdict({"stance": "neutral", "confidence": 0.5, "rationale": "r"}, "TEST")
    assert v is not None and v.risk_flags == []


def test_directional_score_signs():
    assert parse_verdict(VERDICT_JSON, "T").directional_score == 0.7
    assert parse_verdict({**VERDICT_JSON, "stance": "bearish"}, "T").directional_score == -0.7
    assert parse_verdict({**VERDICT_JSON, "stance": "neutral"}, "T").directional_score == 0.0
