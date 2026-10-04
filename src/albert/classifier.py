from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

import httpx

from albert.config import get_settings


@dataclass(frozen=True)
class ExtractedEntity:
    name: str
    entity_type: str = "concept"
    description: str = ""
    confidence: float = 0.7


@dataclass(frozen=True)
class ExtractedRelationship:
    source: ExtractedEntity
    relation_type: str
    target: ExtractedEntity
    confidence: float = 0.7


@dataclass(frozen=True)
class Classification:
    memory_type: str
    sensitivity: str
    confidence: float
    entities: list[ExtractedEntity] = field(default_factory=list)
    relationships: list[ExtractedRelationship] = field(default_factory=list)


_SECRET_PATTERNS = (
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"\b(?:sk|ghp|github_pat|xox[baprs])-[_A-Za-z0-9-]{16,}\b"),
    re.compile(r"\bAKIA[A-Z0-9]{16}\b"),
    re.compile(r"(?i)\b(?:password|passwd|api[_ -]?key|secret)\s*[:=]\s*[^\s]{8,}"),
)


def contains_likely_secret(text: str) -> bool:
    return any(pattern.search(text) for pattern in _SECRET_PATTERNS)


_RELATION_PHRASES = (
    ("depends on", "depends_on"),
    ("integrates with", "integrates_with"),
    ("runs on", "runs_on"),
    ("belongs to", "belongs_to"),
    ("is owned by", "owned_by"),
    ("uses", "uses"),
    ("owns", "owns"),
    ("manages", "manages"),
    ("supports", "supports"),
    ("prefers", "prefers"),
    ("works on", "works_on"),
)
# An entity is one to four bare tokens. Clauses are split on punctuation and
# coordinating conjunctions first so one clause carries at most one relation and
# the captured names cannot swallow neighbouring clauses.
_ENTITY = r"((?:[\w.-]+\s+){0,3}[\w.-]+?)"
_DETERMINER = r"(?:(?:the|a|an|our|their|its)\s+)?"
_TRAILER = r"(?:\s+(?:for|to|in|on|with|as|at|by|since|because|when|via)\b.*)?"
_RELATION_PATTERNS = tuple(
    (
        re.compile(
            rf"^{_DETERMINER}{_ENTITY}\s+{phrase}\s+{_DETERMINER}{_ENTITY}{_TRAILER}$", re.I
        ),
        relation_type,
    )
    for phrase, relation_type in _RELATION_PHRASES
)
_CLAUSE_SPLIT = re.compile(r"[.;:!?\n]+|,\s+|\s+(?:and|but|while|whereas)\s+", re.I)
_MAX_ENTITY_WORDS = 4


def _clean_entity(value: str) -> str:
    value = re.sub(r"\s+", " ", value).strip(" \t\n\r,:()[]{}\"'")
    words = value.split()
    return " ".join(words[:_MAX_ENTITY_WORDS])[:500]


def _clauses(text: str) -> list[str]:
    return [clause.strip() for clause in _CLAUSE_SPLIT.split(text) if clause and clause.strip()]


def heuristic_classify(text: str) -> Classification:
    lowered = text.lower()
    if any(word in lowered for word in ("must not", "danger", "warning", "avoid ")):
        memory_type = "warning"
    elif any(word in lowered for word in ("steps:", "procedure", "runbook", "how to")):
        memory_type = "procedure"
    elif any(word in lowered for word in ("decided", "decision", "we chose")):
        memory_type = "decision"
    elif any(word in lowered for word in ("prefers", "preference", "likes ")):
        memory_type = "preference"
    else:
        memory_type = "fact"

    relationships: list[ExtractedRelationship] = []
    entities: dict[str, ExtractedEntity] = {}
    for clause in _clauses(text):
        for pattern, relation_type in _RELATION_PATTERNS:
            match = pattern.match(clause)
            if match is None:
                continue
            source_name, target_name = (_clean_entity(value) for value in match.groups())
            if not source_name or not target_name:
                continue
            if source_name.casefold() == target_name.casefold():
                continue
            source = entities.setdefault(source_name.casefold(), ExtractedEntity(source_name))
            target = entities.setdefault(target_name.casefold(), ExtractedEntity(target_name))
            relationships.append(
                ExtractedRelationship(source, relation_type, target, confidence=0.65)
            )
            break
    return Classification(
        memory_type=memory_type,
        sensitivity="internal",
        confidence=0.65,
        entities=list(entities.values()),
        relationships=relationships,
    )


MEMORY_TYPES = (
    "fact",
    "preference",
    "procedure",
    "lesson",
    "warning",
    "project_state",
    "task_result",
    "decision",
    "relationship",
    "recurring_pattern",
    "reminder",
    "summary",
)
SENSITIVITIES = ("public", "internal", "confidential", "restricted")
MAX_ENTITIES = 40
MAX_RELATIONSHIPS = 60


def _score(value: object, default: float = 0.7) -> float:
    try:
        return min(1.0, max(0.0, float(value)))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _label(value: object) -> str:
    return re.sub(r"[^a-z0-9_]+", "_", str(value or "").strip().lower()).strip("_")


def _parse_model_classification(payload: dict) -> Classification:
    """Validate and bound a model's reply.

    A label outside the vocabulary falls back to a safe default instead of
    raising: a model's wording must never be able to fail a memory's indexing.
    """
    memory_type = _label(payload.get("memory_type"))
    if memory_type not in MEMORY_TYPES:
        memory_type = "fact"
    sensitivity = _label(payload.get("sensitivity"))
    if sensitivity not in SENSITIVITIES:
        sensitivity = "internal"

    entities: dict[str, ExtractedEntity] = {}
    for raw in payload.get("entities") or []:
        if not isinstance(raw, dict) or len(entities) >= MAX_ENTITIES:
            continue
        name = _clean_entity(str(raw.get("name") or ""))
        if name:
            entities.setdefault(
                name.casefold(),
                ExtractedEntity(
                    name=name,
                    entity_type=(_label(raw.get("entity_type")) or "concept")[:100],
                    description=str(raw.get("description") or "")[:10_000],
                    confidence=_score(raw.get("confidence")),
                ),
            )
    relationships: list[ExtractedRelationship] = []
    seen: set[tuple[str, str, str]] = set()
    for raw in payload.get("relationships") or []:
        if not isinstance(raw, dict) or len(relationships) >= MAX_RELATIONSHIPS:
            continue
        source = entities.get(_clean_entity(str(raw.get("source") or "")).casefold())
        target = entities.get(_clean_entity(str(raw.get("target") or "")).casefold())
        relation = _label(raw.get("relation_type"))[:100]
        if not (source and target and relation) or source is target:
            continue
        key = (source.name, relation, target.name)
        if key in seen:
            continue
        seen.add(key)
        relationships.append(
            ExtractedRelationship(source, relation, target, _score(raw.get("confidence")))
        )
    return Classification(
        memory_type=memory_type,
        sensitivity=sensitivity,
        confidence=_score(payload.get("confidence")),
        entities=list(entities.values()),
        relationships=relationships,
    )


CLASSIFIER_INSTRUCTION = f"""You label one memory for an agent memory system and extract a
small knowledge graph from it. Return only a JSON object with these keys:

- memory_type: exactly one of {", ".join(MEMORY_TYPES)}.
- sensitivity: exactly one of {", ".join(SENSITIVITIES)}. Use "internal" for ordinary
  working notes, plans, procedures and business detail. Use "confidential" only when the
  text contains personal data about named individuals, pay, health, legal or financial
  account detail. Use "restricted" only for material that would cause serious harm if
  shared inside the organization. When unsure, use "internal".
- confidence: a number from 0 to 1.
- entities: at most 15 objects {{name, entity_type, description, confidence}}. Include
  only specific, named things the memory is about: people, teams, roles, products,
  systems, tools, files, places, organizations, recurring processes. Use the name as
  written, without articles. entity_type is one lowercase word such as person, team,
  role, system, tool, file, location, organization, process, product, concept.
  Skip generic words, dates, numbers and one-off phrases.
- relationships: at most 20 objects {{source, relation_type, target, confidence}}.
  source and target must exactly match entity names from your entities list.
  relation_type is a short lowercase snake_case verb phrase such as uses, owns,
  depends_on, part_of, manages, reports_to, located_at, produces, replaces, blocks.
  State only relationships the text supports.

If the memory names nothing specific, return empty lists. Never include credentials or
secrets."""


def classify(text: str) -> Classification:
    settings = get_settings()
    if settings.llm_provider != "openai-compatible":
        return heuristic_classify(text)
    if settings.openai_api_key is None or not settings.classification_model:
        raise ValueError("LLM classification requires an API key and classification model")
    response = httpx.post(
        f"{settings.openai_base_url.rstrip('/')}/chat/completions",
        headers={"Authorization": f"Bearer {settings.openai_api_key.get_secret_value()}"},
        json={
            "model": settings.classification_model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": CLASSIFIER_INSTRUCTION},
                {"role": "user", "content": text[:100_000]},
            ],
        },
        timeout=120,
    )
    response.raise_for_status()
    content = response.json()["choices"][0]["message"]["content"]
    return _parse_model_classification(json.loads(content))
