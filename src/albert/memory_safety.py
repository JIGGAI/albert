from __future__ import annotations

import re
from typing import Any

TRUST_LEVELS = ("untrusted", "verified", "trusted")
CONFIDENCE_CEILINGS = {"untrusted": 0.45, "verified": 0.80, "trusted": 1.0}
SAFETY_METADATA_KEYS = frozenset(
    {
        "capture_source_trust",
        "confidence_ceiling",
        "confidence_corroborated",
        "contradiction_memory_ids",
        "contradiction_flagged",
        "quarantine_reason_codes",
        "quarantined",
        "suspicious_instruction",
    }
)

_INSTRUCTION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "instruction_override",
        re.compile(
            r"\b(?:ignore|disregard|override|replace)\b.{0,60}"
            r"\b(?:previous|prior|system|developer|workspace|instruction|policy|rules?)\b",
            re.I | re.S,
        ),
    ),
    (
        "privileged_role_impersonation",
        re.compile(
            r"(?:<\/?(?:system|developer|assistant)>|\[(?:system|developer)\]|"
            r"\b(?:system prompt|developer message)\b)",
            re.I,
        ),
    ),
    (
        "agent_control_language",
        re.compile(
            r"\b(?:you (?:must|shall|are required to)|do not follow|never reveal|"
            r"execute (?:this|the following) command)\b",
            re.I,
        ),
    ),
    (
        "reference_delimiter_escape",
        re.compile(r"[\u27e6\u27e7]|ALBERT[_ -]REFERENCE|END[_ -]REFERENCE", re.I),
    ),
)


def suspicious_instruction_reasons(content: str) -> list[str]:
    """Return stable reason codes only; callers must not log the body."""
    return [name for name, pattern in _INSTRUCTION_PATTERNS if pattern.search(content)]


def trust_level(metadata: dict[str, Any] | None, *, default: str = "verified") -> str:
    value = str((metadata or {}).get("capture_source_trust", default))
    return value if value in TRUST_LEVELS else default


def sanitize_client_metadata(metadata: dict[str, Any] | None) -> dict[str, Any]:
    """Remove review-owned assertions; clients may only downgrade their own trust."""
    supplied = dict(metadata or {})
    requested_trust = trust_level(supplied)
    safe = {key: value for key, value in supplied.items() if key not in SAFETY_METADATA_KEYS}
    safe["capture_source_trust"] = (
        "untrusted" if requested_trust == "untrusted" else "verified"
    )
    if supplied.get("quarantined") is True:
        safe["quarantined"] = True
    return safe


def apply_memory_safety(
    content: str,
    confidence: float,
    metadata: dict[str, Any] | None,
    *,
    default_trust: str = "verified",
) -> tuple[float, dict[str, Any]]:
    """Apply deterministic quarantine and confidence controls to untrusted data."""
    safe = dict(metadata or {})
    trust = trust_level(safe, default=default_trust)
    reasons = suspicious_instruction_reasons(content)
    ceiling = CONFIDENCE_CEILINGS[trust]
    corroborated = bool(safe.get("confidence_corroborated"))
    effective = confidence if corroborated else min(confidence, ceiling)
    safe.update(
        {
            "capture_source_trust": trust,
            "confidence_ceiling": ceiling,
            "confidence_corroborated": corroborated,
            "suspicious_instruction": bool(reasons),
            "quarantined": bool(reasons) or bool(safe.get("quarantined", False)),
            "quarantine_reason_codes": reasons or list(safe.get("quarantine_reason_codes", [])),
        }
    )
    return effective, safe


def preserve_safety_metadata(
    existing: dict[str, Any] | None, replacement: dict[str, Any] | None
) -> dict[str, Any]:
    """Prevent ordinary memory updates from editing review-owned safety state."""
    merged = dict(replacement or {})
    for key in SAFETY_METADATA_KEYS:
        if key in (existing or {}):
            merged[key] = existing[key]  # type: ignore[index]
    return merged
