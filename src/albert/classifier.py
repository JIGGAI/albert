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


def _parse_model_classification(payload: dict) -> Classification:
    allowed_types = {
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
    }
    allowed_sensitivity = {"public", "internal", "confidential", "restricted"}
    memory_type = str(payload.get("memory_type", "fact"))
    sensitivity = str(payload.get("sensitivity", "internal"))
    if memory_type not in allowed_types:
        raise ValueError("Classifier returned an invalid memory type")
    if sensitivity not in allowed_sensitivity:
        raise ValueError("Classifier returned an invalid sensitivity")

    entities: dict[str, ExtractedEntity] = {}
    for raw in payload.get("entities", []):
        name = _clean_entity(str(raw.get("name", "")))
        if name:
            entities[name.casefold()] = ExtractedEntity(
                name=name,
                entity_type=str(raw.get("entity_type", "concept"))[:100],
                description=str(raw.get("description", ""))[:10_000],
                confidence=min(1.0, max(0.0, float(raw.get("confidence", 0.7)))),
            )
    relationships: list[ExtractedRelationship] = []
    for raw in payload.get("relationships", []):
        source = entities.get(str(raw.get("source", "")).casefold())
        target = entities.get(str(raw.get("target", "")).casefold())
        relation = re.sub(r"[^a-z0-9_]+", "_", str(raw.get("relation_type", "" )).lower())
        if source and target and relation:
            relationships.append(
                ExtractedRelationship(
                    source,
                    relation[:100],
                    target,
                    min(1.0, max(0.0, float(raw.get("confidence", 0.7)))),
                )
            )
    return Classification(
        memory_type=memory_type,
        sensitivity=sensitivity,
        confidence=min(1.0, max(0.0, float(payload.get("confidence", 0.7)))),
        entities=list(entities.values()),
        relationships=relationships,
    )


def classify(text: str) -> Classification:
    settings = get_settings()
    if settings.llm_provider != "openai-compatible":
        return heuristic_classify(text)
    if settings.openai_api_key is None or not settings.classification_model:
        raise ValueError("LLM classification requires an API key and classification model")
    schema_instruction = """Return only JSON with keys memory_type, sensitivity, confidence,
entities, relationships. entities is a list of {name, entity_type, description, confidence}.
relationships is a list of {source, relation_type, target, confidence}; source and target must
exactly match entity names. Never include credentials or secrets."""
    response = httpx.post(
        f"{settings.openai_base_url.rstrip('/')}/chat/completions",
        headers={"Authorization": f"Bearer {settings.openai_api_key.get_secret_value()}"},
        json={
            "model": settings.classification_model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": schema_instruction},
                {"role": "user", "content": text[:100_000]},
            ],
        },
        timeout=120,
    )
    response.raise_for_status()
    content = response.json()["choices"][0]["message"]["content"]
    return _parse_model_classification(json.loads(content))
