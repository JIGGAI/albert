from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from albert.schemas import SearchHit, SearchResponse, SessionContextRequest, SessionContextResponse

_TOKEN = re.compile(r"\w+|[^\w\s]", re.UNICODE)
_TRUST_SCORE = {"trusted": 2, "verified": 1, "untrusted": 0}


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


def _rank_key(hit: SearchHit, request: SessionContextRequest) -> tuple[Any, ...]:
    metadata = hit.metadata
    trust = str(metadata.get("capture_source_trust", "verified"))
    return (
        int(bool(request.task_ref) and metadata.get("task_ref") == request.task_ref),
        int(bool(request.branch_ref) and _branch(hit) == request.branch_ref),
        int(bool(request.project_ref) and metadata.get("project_ref") == request.project_ref),
        _TRUST_SCORE.get(trust, 0),
        float(metadata.get("confidence", 0.0)),
        _timestamp(metadata.get("valid_from")),
        float(hit.score),
        str(hit.memory_id or metadata.get("relationship_id", "")),
    )


def _eligible(hit: SearchHit, request: SessionContextRequest) -> bool:
    if hit.metadata.get("quarantined"):
        return False
    trust = str(hit.metadata.get("capture_source_trust", "verified"))
    return request.trust_policy != "exclude_untrusted" or trust != "untrusted"


def _ordered(hits: list[SearchHit], request: SessionContextRequest) -> list[SearchHit]:
    remaining = sorted(
        (hit for hit in hits if _eligible(hit, request)),
        key=lambda hit: _rank_key(hit, request),
        reverse=True,
    )
    ordered: list[SearchHit] = []
    seen_types: set[str] = set()
    while remaining:
        best_tier = _rank_key(remaining[0], request)[:3]
        tier = [hit for hit in remaining if _rank_key(hit, request)[:3] == best_tier]
        diverse = next(
            (
                hit
                for hit in tier
                if str(hit.metadata.get("memory_type", "relationship")) not in seen_types
            ),
            tier[0],
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
        section = f"[{index}]{label} {hit.subject}\n{hit.content}".strip()
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
