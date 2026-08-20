"""Generic "find the JSON object in this blob of text" parser, shared by every
CLI-backed provider (Kimi, Grok, ...). CLIs return prose around/instead of
clean JSON regardless of what the prompt asks for or what `--json-schema`-style
flags promise — Grok's own schema-constrained mode has been observed emitting
two concatenated JSON objects in one response rather than one clean object, so
even "structured output" flags still need this on the receiving end.
"""
from __future__ import annotations

import json


def extract_json_object(text: str) -> dict | None:
    """Finds the first balanced {...} span that decodes to a dict. Handles bare
    JSON, fenced JSON, JSON embedded in stray commentary, and multiple
    concatenated JSON objects (returns the first)."""
    text = text.strip()
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass

    depth = 0
    start = -1
    in_string = False
    escaped = False
    for i, ch in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                try:
                    obj = json.loads(text[start : i + 1])
                    if isinstance(obj, dict):
                        return obj
                except json.JSONDecodeError:
                    pass
                start = -1
    return None
