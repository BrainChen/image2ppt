from __future__ import annotations

import json
import re
from typing import Any


def strip_json_fence(text: str) -> str:
    value = text.strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", value, flags=re.DOTALL | re.IGNORECASE)
    if fence:
        return fence.group(1).strip()
    return value


def extract_json_object(text: str) -> dict[str, Any]:
    value = strip_json_fence(text)
    try:
        parsed = _loads_model_json(value)
    except json.JSONDecodeError:
        parsed = _loads_model_json(_first_balanced_json(value))

    if not isinstance(parsed, dict):
        raise ValueError("Expected model output to be a JSON object")
    return parsed


def _loads_model_json(value: str) -> Any:
    return json.loads(value, strict=False)


def _first_balanced_json(text: str) -> str:
    start = text.find("{")
    if start < 0:
        raise ValueError("No JSON object found in model output")

    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]

    raise ValueError("Unclosed JSON object in model output")
