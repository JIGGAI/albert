"""Text helpers shared by the Responses-API LLM providers."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def strip_code_fences(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[1] if "\n" in stripped else ""
        if stripped.rstrip().endswith("```"):
            stripped = stripped.rstrip()[:-3]
    return stripped.strip()


def output_text(response: Mapping[str, Any]) -> str:
    """Concatenate the assistant text of a Responses-API ``response`` object."""
    parts: list[str] = []
    for item in response.get("output") or []:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for content in item.get("content") or []:
            if isinstance(content, dict) and content.get("type") == "output_text":
                parts.append(str(content.get("text", "")))
    return "".join(parts)


def first_json_object(text: str) -> str:
    """The JSON object a chatty model wrapped in prose, or ``text`` unchanged."""
    cleaned = strip_code_fences(text)
    if cleaned.startswith("{"):
        return cleaned
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if 0 <= start < end:
        return cleaned[start : end + 1]
    return cleaned
