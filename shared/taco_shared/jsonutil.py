"""Strict JSON parsing for model output."""

from __future__ import annotations

import json
import re
from typing import Any


def strict_json_loads(text: str) -> Any:
    def reject_constant(value: str) -> Any:
        raise ValueError(f"Invalid JSON constant: {value}")

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"Duplicate JSON key: {key}")
            result[key] = value
        return result

    return json.loads(text, parse_constant=reject_constant, object_pairs_hook=reject_duplicates)


def parse_json_output(text: str) -> tuple[Any, bool]:
    """Return (parsed, strict). strict is False when JSON had to be recovered from surrounding text."""
    cleaned = text.strip()
    # Some reasoning models leak <think> blocks into content when no reasoning parser is configured.
    cleaned = re.sub(r"<think>.*?</think>", "", cleaned, flags=re.DOTALL).strip()
    try:
        return strict_json_loads(cleaned), True
    except ValueError:
        pass
    if cleaned.startswith("```"):
        cleaned = "\n".join(line for line in cleaned.splitlines() if not line.strip().startswith("```"))
    try:
        return strict_json_loads(cleaned), False
    except ValueError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start == -1 or end <= start:
            raise ValueError("No JSON object found in model output.") from None
        return strict_json_loads(cleaned[start : end + 1]), False
