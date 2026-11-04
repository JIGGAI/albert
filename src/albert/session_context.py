from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from albert.schemas import SearchHit, SearchResponse, SessionContextRequest, SessionContextResponse

_TOKEN = re.compile(r"\w+|[^\w\s]", re.UNICODE)


def count_tokens(value: str) -> int:
    """Deterministic API budget unit; words and punctuation each count once."""
    return len(_TOKEN.findall(value))


def _branch(hit: SearchHit) -> str | None:
    provenance = hit.metadata.get("provenance", {})
    if not isinstance(provenance, dict):
        return None
    value = provenance.get("branch") or provenance.get("ref")
    return str(value) if value else None


def _timestamp(value: Any) -> float:
    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except (TypeError, ValueError):
        return 0.0


# Recall orders by relevance: the fused search score, nudged by bounded
# adjustments. Trust, confidence and recency used to be strict sort keys ahead
# of the score, so recall returned the newest candidates rather than the most
# relevant ones; as multipliers they can reorder near-ties but never bury a
# clearly better match. Scope matches (task, branch, project) remain tiers.
_TRUST_FACTOR = {"trusted": 1.10, "verified": 1.0, "untrusted": 0.90}
CONFIDENCE_WEIGHT = 0.10  # confidence 0 costs 10%, confidence 1 costs nothing
RECENCY_BOOST = 0.10  # a memory valid from today gains up to 10% ...
RECENCY_WINDOW_DAYS = 365.0  # ... fading linearly to nothing over a year
DIVERSITY_WINDOW = 3  # memory types are diversified among the top few only


def _scope_tier(hit: SearchHit, request: SessionContextRequest) -> tuple[int, int, int]:
    metadata = hit.metadata
    return (
        int(bool(request.task_ref) and metadata.get("task_ref") == request.task_ref),
        int(bool(request.branch_ref) and _branch(hit) == request.branch_ref),
        int(bool(request.project_ref) and metadata.get("project_ref") == request.project_ref),
    )


def _relevance(hit: SearchHit, now: float) -> float:
    metadata = hit.metadata
    trust = _TRUST_FACTOR.get(str(metadata.get("capture_source_trust", "verified")), 1.0)
    confidence = min(max(float(metadata.get("confidence", 1.0)), 0.0), 1.0)
    confidence_factor = 1.0 - CONFIDENCE_WEIGHT * (1.0 - confidence)
    valid_from = _timestamp(metadata.get("valid_from"))
    recency = 1.0
    if valid_from:
        age_days = max(0.0, (now - valid_from) / 86400)
        recency += RECENCY_BOOST * max(0.0, 1.0 - age_days / RECENCY_WINDOW_DAYS)
    return float(hit.score) * trust * confidence_factor * recency


def _rank_key(hit: SearchHit, request: SessionContextRequest, now: float) -> tuple[Any, ...]:
    return (
        *_scope_tier(hit, request),
        _relevance(hit, now),
        str(hit.memory_id or hit.metadata.get("relationship_id", "")),
    )


def _eligible(hit: SearchHit, request: SessionContextRequest) -> bool:
    if hit.metadata.get("quarantined"):
        return False
    trust = str(hit.metadata.get("capture_source_trust", "verified"))
    return request.trust_policy != "exclude_untrusted" or trust != "untrusted"


def _ordered(hits: list[SearchHit], request: SessionContextRequest) -> list[SearchHit]:
    now = datetime.now(UTC).timestamp()
    remaining = sorted(
        (hit for hit in hits if _eligible(hit, request)),
        key=lambda hit: _rank_key(hit, request, now),
        reverse=True,
    )
    ordered: list[SearchHit] = []
    seen_types: set[str] = set()
    while remaining:
        best_tier = _scope_tier(remaining[0], request)
        window = [hit for hit in remaining if _scope_tier(hit, request) == best_tier]
        window = window[:DIVERSITY_WINDOW]
        diverse = next(
            (
                hit
                for hit in window
                if str(hit.metadata.get("memory_type", "relationship")) not in seen_types
            ),
            window[0],
        )
        remaining.remove(diverse)
        ordered.append(diverse)
        seen_types.add(str(diverse.metadata.get("memory_type", "relationship")))
    return ordered


def assemble_session_context(
    result: SearchResponse, request: SessionContextRequest
) -> SessionContextResponse:
    candidates = _ordered(result.hits, request)
    sections: list[str] = []
    citations: list[dict[str, Any]] = []
    characters = 0
    tokens = 0
    omitted = len(result.hits) - len(candidates)

    for hit in candidates:
        if len(sections) >= request.max_items:
            omitted += 1
            continue
        trust = str(hit.metadata.get("capture_source_trust", "verified"))
        label = f" [{trust}]" if request.trust_policy == "label_untrusted" else ""
        index = len(sections) + 1
        # U+27E6/U+27E7 are the adapter contract: all enclosed recall is
        # untrusted reference data, never system/workspace policy. Content
        # containing either sentinel is quarantined before it reaches here.
        section = f"⟦[{index}]{label} {hit.subject}\n{hit.content}⟧".strip()
        separator = 2 if sections else 0
        section_tokens = count_tokens(section)
        if (
            characters + separator + len(section) > request.max_characters
            or tokens + section_tokens > request.max_tokens
        ):
            omitted += 1
            continue
        sections.append(section)
        characters += separator + len(section)
        tokens += section_tokens
        citations.append(
            {
                "index": index,
                "kind": hit.kind,
                "memory_id": str(hit.memory_id) if hit.memory_id else None,
                "episode_id": str(hit.source_episode_id) if hit.source_episode_id else None,
                "memory_type": hit.metadata.get("memory_type"),
                "trust": trust,
                "branch_ref": _branch(hit),
                "backends": sorted(hit.backends),
            }
        )

    truncated = omitted > 0
    return SessionContextResponse(
        context="\n\n".join(sections),
        citations=citations,
        characters=characters,
        tokens=tokens,
        truncated=truncated,
        complete=not result.degraded and not truncated,
        degraded=sorted(result.degraded),
    )
