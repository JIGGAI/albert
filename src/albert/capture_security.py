from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ScreeningResult:
    content: str
    metadata: dict[str, Any]
    reasons: tuple[str, ...]


_PATTERNS = (
    (
        "private_key",
        re.compile(
            r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----[\s\S]*?"
            r"-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"
        ),
    ),
    (
        "github_token",
        re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"),
    ),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("bearer_token", re.compile(r"(?i)(?<=bearer )[A-Za-z0-9._~+/=-]{16,}")),
    (
        "assigned_credential",
        re.compile(
            r"(?i)(?:api[_ -]?key|password|passwd|secret|token)\s*[:=]\s*[A-Za-z0-9._~+/=-]{8,}"
        ),
    ),
)


def _screen_text(value: str) -> tuple[str, set[str]]:
    reasons: set[str] = set()
    result = value
    for reason, pattern in _PATTERNS:
        if pattern.search(result):
            reasons.add(reason)
            result = pattern.sub(f"[REDACTED:{reason}]", result)
    return result, reasons


def _screen_value(value: Any) -> tuple[Any, set[str]]:
    if isinstance(value, str):
        return _screen_text(value)
    if isinstance(value, dict):
        output: dict[str, Any] = {}
        reasons: set[str] = set()
        for key, item in value.items():
            screened, found = _screen_value(item)
            output[key] = screened
            reasons.update(found)
        return output, reasons
    if isinstance(value, list):
        output = []
        reasons: set[str] = set()
        for item in value:
            screened, found = _screen_value(item)
            output.append(screened)
            reasons.update(found)
        return output, reasons
    return value, set()


def screen_capture(content: str, metadata: dict[str, Any]) -> ScreeningResult:
    screened_content, content_reasons = _screen_text(content)
    screened_metadata, metadata_reasons = _screen_value(metadata)
    return ScreeningResult(
        content=screened_content,
        metadata=screened_metadata,
        reasons=tuple(sorted(content_reasons | metadata_reasons)),
    )
