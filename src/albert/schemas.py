from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Literal, get_args
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class ProfileAssertionRead(BaseModel):
    bucket: str
    value: str
    valid_from: datetime
    valid_until: datetime | None
    trust: Literal["untrusted", "verified", "trusted"]
    confidence: float
    sensitivity: Literal["public", "internal", "confidential", "restricted"]
    citations: list[UUID]
    decay_multiplier: float = 1.0
    decay_reason: str = "fresh"


class StandingProfileRead(BaseModel):
    principal_id: UUID
    workspace_id: UUID | None
    schema_version: str
    source_digest: str
    built_at: datetime
    assertions: list[ProfileAssertionRead]
    total: int
    truncated: bool


class ProfileMaterializationStatus(BaseModel):
    principal_id: UUID
    workspace_id: UUID | None
    state: Literal["never", "current"]
    schema_version: str | None = None
    source_digest: str | None = None
    assertion_count: int = 0
    built_at: datetime | None = None


class ProfileBucketPolicyWrite(BaseModel):
    expected_version: int = Field(ge=0)
    idempotency_key: str = Field(min_length=1, max_length=200)
    buckets: dict[str, list[str]] = Field(min_length=1, max_length=32)

    @field_validator("buckets")
    @classmethod
    def validate_buckets(cls, value: dict[str, list[str]]) -> dict[str, list[str]]:
        normalized: dict[str, list[str]] = {}
        for bucket, memory_types in value.items():
            if not bucket or len(bucket) > 64 or not bucket.replace("-", "").isalnum():
                raise ValueError("bucket names must be short alphanumeric slugs")
            cleaned = sorted({item.strip().lower() for item in memory_types if item.strip()})
            if not cleaned or any(len(item) > 64 for item in cleaned):
                raise ValueError("each bucket requires bounded memory types")
            normalized[bucket] = cleaned
        return normalized


class ProfileBucketPolicyRead(BaseModel):
    version: int
    buckets: dict[str, list[str]]
    actor_principal_id: UUID | None = None
    created_at: datetime | None = None


class ProfilePrincipalSummary(BaseModel):
    principal_id: UUID
    workspace_id: UUID | None
    name: str
    principal_type: str
    materialized: bool
    assertion_count: int
    built_at: datetime | None


class ProfilePrincipalPage(BaseModel):
    items: list[ProfilePrincipalSummary]
    total: int
    offset: int
    limit: int


class ProfileAssertionAdminRead(BaseModel):
    id: UUID
    principal_id: UUID
    bucket: str
    value: str
    valid_from: datetime
    valid_until: datetime | None
    trust: str
    confidence: float
    sensitivity: str
    citations: list[UUID]
    version: int
    decision_state: str
    suppressed: bool


class ProfileAssertionPage(BaseModel):
    items: list[ProfileAssertionAdminRead]
    total: int
    offset: int
    limit: int


class ProfileAssertionMutation(BaseModel):
    expected_version: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1, max_length=200)
    value: str | None = Field(default=None, min_length=1, max_length=50_000)


class ProfileAssertionHistoryRead(BaseModel):
    id: UUID
    assertion_id: UUID
    action: str
    version: int
    prior_value: str
    new_value: str
    valid_from: datetime
    valid_until: datetime | None
    trust: str
    confidence: float
    sensitivity: str
    decision_state: str
    citations: list[UUID]
    actor_principal_id: UUID
    created_at: datetime


class ProfileAssertionHistoryPage(BaseModel):
    items: list[ProfileAssertionHistoryRead]
    total: int
    offset: int
    limit: int


class FactEvolutionCreate(BaseModel):
    idempotency_key: str = Field(min_length=1, max_length=200)
    evidence_memory_id: UUID
    fact_memory_id: UUID | None = None
    relation: Literal["same", "adds", "supersedes", "conflicts", "ambiguous"]
    impact: Literal["low", "medium", "high"] = "low"
    effective_from: datetime | None = None
    effective_until: datetime | None = None

    @model_validator(mode="after")
    def validate_temporal_window(self):  # type: ignore[no-untyped-def]
        if (
            self.effective_from is not None
            and self.effective_until is not None
            and self.effective_until <= self.effective_from
        ):
            raise ValueError("effective_until must be after effective_from")
        return self


class FactEvolutionReview(BaseModel):
    action: Literal["approve", "reject"]
    relation: Literal["same", "adds", "supersedes", "conflicts"] | None = None

    @model_validator(mode="after")
    def validate_resolution(self):  # type: ignore[no-untyped-def]
        if self.action == "reject" and self.relation is not None:
            raise ValueError("A rejected evolution cannot override its relation")
        return self


class FactEvolutionRead(BaseModel):
    id: UUID
    evidence_memory_id: UUID | None
    fact_memory_id: UUID | None
    result_memory_id: UUID | None
    relation: Literal["same", "adds", "supersedes", "conflicts", "ambiguous"]
    decision: Literal["update", "extend", "derive", "contradict", "review"]
    state: Literal["applied", "pending_review", "rejected"]
    rationale_code: str
    review_reason: str | None
    impact: Literal["low", "medium", "high"]
    source_trust: Literal["untrusted", "verified", "trusted"]
    confidence_ceiling: float
    effective_confidence: float
    effective_from: datetime
    effective_until: datetime | None
    citations: list[UUID]
    reviewed_by_principal_id: UUID | None
    reviewed_at: datetime | None
    created_at: datetime


class FactEvolutionHistory(BaseModel):
    count: int
    history_digest: str
    decisions: list[FactEvolutionRead]


class DecayRule(BaseModel):
    freshness_days: float = Field(default=30, ge=0)
    half_life_days: float = Field(default=90, gt=0)
    retention_days: float = Field(default=365, ge=0)
    stale_state: Literal["mark", "demote"] = "demote"
    protected: bool = False

    @model_validator(mode="after")
    def validate_retention(self):  # type: ignore[no-untyped-def]
        if self.retention_days < self.freshness_days:
            raise ValueError("retention_days must be at least freshness_days")
        return self


class DecayPolicyWrite(BaseModel):
    rules: dict[str, DecayRule] = Field(min_length=1)
    expected_version: int | None = Field(default=None, ge=0)


class DecayPolicyRollback(BaseModel):
    version: int = Field(ge=1)


class DecayPolicyRead(BaseModel):
    id: UUID
    workspace_id: UUID | None
    version: int
    rules: dict[str, DecayRule]
    active: bool
    rollback_of_version: int | None
    created_by_principal_id: UUID
    created_at: datetime


class DecayRecompute(BaseModel):
    as_of: datetime | None = None
    policy_version: int | None = Field(default=None, ge=1)


class DecayStateRead(BaseModel):
    memory_id: UUID
    policy_version: int
    rule_class: str
    freshness_at: datetime
    evaluated_at: datetime
    age_days: float
    score_multiplier: float
    stale: bool
    protected: bool
    reason: str


class DecayRunRead(BaseModel):
    policy_version: int
    evaluated_at: datetime
    evaluated: int
    stale: int
    protected: int
    states: list[DecayStateRead]


def _require_visible_text(value: str | None) -> str | None:
    if value is not None and not value.strip():
        raise ValueError("content must contain visible text")
    return value


MAX_JSON_FIELD_CHARACTERS = 64_000


def _bounded_json(value: dict[str, Any] | None) -> dict[str, Any] | None:
    if value is not None and len(json.dumps(value, default=str)) > MAX_JSON_FIELD_CHARACTERS:
        raise ValueError(f"JSON field exceeds {MAX_JSON_FIELD_CHARACTERS} serialized characters")
    return value


class ScopeFields(BaseModel):
    workspace_id: UUID | None = None
    project_ref: str | None = Field(default=None, max_length=300)
    task_ref: str | None = Field(default=None, max_length=300)
    run_ref: str | None = Field(default=None, max_length=300)


class SharedWrite(BaseModel):
    """`scope: "organization"` stores a record every workspace in the organization shares.

    Without it a write lands in the requested workspace, else the key's own; a
    workspace-scoped key needs memory.write.organization to share.
    """

    workspace_id: UUID | None = None
    scope: Literal["organization"] | None = None

    @model_validator(mode="after")
    def _one_target(self):  # type: ignore[no-untyped-def]
        if self.scope == "organization" and self.workspace_id is not None:
            raise ValueError('Choose either workspace_id or scope="organization", not both')
        return self


class EpisodeCreate(SharedWrite, ScopeFields):
    content: str = Field(min_length=1, max_length=2_000_000)
    source_type: str = Field(default="explicit", max_length=64)
    source_uri: str = Field(default="", max_length=1000)
    sensitivity: Literal["public", "internal", "confidential", "restricted"] = "internal"
    occurred_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    _visible_content = field_validator("content")(_require_visible_text)
    _bounded_metadata = field_validator("metadata")(_bounded_json)


class IndexingStatus(BaseModel):
    """Where an episode is on its way to becoming searchable.

    queued: waiting for a worker. running: a worker has it now. retrying: an
    attempt failed and another is scheduled (see last_error, next_attempt_at).
    complete: searchable, as memory_id. failed: every attempt failed.
    """

    state: Literal["queued", "running", "retrying", "complete", "failed"]
    searchable: bool
    attempt: int
    max_attempts: int
    last_error: str | None = None
    next_attempt_at: datetime | None = None
    memory_id: UUID | None = None


class EpisodeRead(ORMModel):
    id: UUID
    organization_id: UUID
    workspace_id: UUID | None
    project_ref: str | None
    task_ref: str | None
    run_ref: str | None
    source_type: str
    source_uri: str
    content: str
    content_hash: str
    sensitivity: str
    occurred_at: datetime
    enrichment_status: str
    enrichment_error: str | None
    deleted_at: datetime | None
    extra: dict[str, Any]
    created_at: datetime
    indexing: IndexingStatus | None = None


CAPTURE_CONTRACT_VERSION = "1"
MAX_CAPTURE_BATCH_EVENTS = 100


class CaptureProfileWrite(BaseModel):
    profile_key: str = Field(min_length=1, max_length=100, pattern=r"^[a-z0-9][a-z0-9-]*$")
    version: int = Field(ge=1)
    workspace_id: UUID | None = None
    allowed_event_classes: list[str] = Field(min_length=1, max_length=64)
    metadata_allowlists: dict[str, list[str]] = Field(default_factory=dict)
    event_bounds: dict[str, dict[str, int]] = Field(default_factory=dict)
    sensitivity_floor: Literal["public", "internal", "confidential", "restricted"] = "internal"
    source_trust: Literal["untrusted", "verified", "trusted"] = "untrusted"
    # Per-event-class override of source_trust, e.g. {"user_goal": "verified"}.
    event_source_trust: dict[str, Literal["untrusted", "verified", "trusted"]] = Field(
        default_factory=dict
    )
    retention_days: int = Field(default=30, ge=1, le=3650)
    raw_body_policy: Literal["deny", "allow"] = "deny"
    secret_policy: Literal["reject", "redact"] = "reject"
    derivative_policy: Literal["cascade", "invalidate"] = "cascade"
    deny_reasons: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_profile(self):  # type: ignore[no-untyped-def]
        allowed = set(self.allowed_event_classes)
        if len(allowed) != len(self.allowed_event_classes):
            raise ValueError("allowed event classes must be unique")
        if not set(self.metadata_allowlists).issubset(allowed):
            raise ValueError("metadata allowlists must reference allowed event classes")
        if not set(self.event_bounds).issubset(allowed):
            raise ValueError("event bounds must reference allowed event classes")
        if not set(self.event_source_trust).issubset(allowed):
            raise ValueError("event source trust must reference allowed event classes")
        for event_class, bounds in self.event_bounds.items():
            if not set(bounds).issubset({"max_content_chars", "max_metadata_chars"}):
                raise ValueError(f"unsupported bound for {event_class}")
            if any(
                not isinstance(value, int) or value < 0 or value > 2_000_000
                for value in bounds.values()
            ):
                raise ValueError(f"invalid bound for {event_class}")
        return self


class CaptureProfileRead(CaptureProfileWrite):
    id: UUID
    created_at: datetime


class CaptureProvenance(BaseModel):
    client: str | None = Field(default=None, max_length=200)
    agent: str | None = Field(default=None, max_length=200)
    project: str | None = Field(default=None, max_length=300)
    remote_url: str | None = Field(default=None, max_length=1000)
    worktree_path: str | None = Field(default=None, max_length=2000)
    vcs: Literal["git", "none"] | None = None
    branch: str | None = Field(default=None, max_length=300)
    ref: str | None = Field(default=None, max_length=500)
    head: str | None = Field(default=None, min_length=1, max_length=128)
    dirty: bool | None = None
    task: str | None = Field(default=None, max_length=300)
    run: str | None = Field(default=None, max_length=300)
    event: str | None = Field(default=None, max_length=300)
    source_session: str | None = Field(default=None, max_length=300)
    adapter_version: str | None = Field(default=None, max_length=100)
    include_absolute_path: bool = False


class CaptureSessionStart(BaseModel):
    version: str = Field(default=CAPTURE_CONTRACT_VERSION, max_length=16)
    workspace_id: UUID | None = None
    client_session_id: str = Field(min_length=1, max_length=300)
    idempotency_key: str = Field(min_length=1, max_length=200)
    profile_key: str = Field(default="safe-default", min_length=1, max_length=100)
    local_event_classes: list[str] | None = Field(default=None, max_length=64)
    local_metadata_allowlists: dict[str, list[str]] | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    provenance: CaptureProvenance | None = None

    _bounded_metadata = field_validator("metadata")(_bounded_json)


class CaptureEventWrite(BaseModel):
    version: str = Field(default=CAPTURE_CONTRACT_VERSION, max_length=16)
    sequence: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1, max_length=200)
    event_class: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    content: str = Field(min_length=1, max_length=64_000)
    sensitivity: Literal["public", "internal", "confidential", "restricted"] = "internal"
    body_kind: Literal["summary", "raw"] = "summary"
    occurred_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    provenance: CaptureProvenance | None = None

    _visible_content = field_validator("content")(_require_visible_text)
    _bounded_metadata = field_validator("metadata")(_bounded_json)


class CaptureEventBatch(BaseModel):
    version: str = Field(default=CAPTURE_CONTRACT_VERSION, max_length=16)
    events: list[CaptureEventWrite] = Field(min_length=1, max_length=MAX_CAPTURE_BATCH_EVENTS)

    @model_validator(mode="after")
    def validate_batch(self):  # type: ignore[no-untyped-def]
        if any(event.version != self.version for event in self.events):
            raise ValueError("batch and event versions must match")
        if len({event.sequence for event in self.events}) != len(self.events):
            raise ValueError("batch event sequences must be unique")
        if len({event.idempotency_key for event in self.events}) != len(self.events):
            raise ValueError("batch idempotency keys must be unique")
        encoded = json.dumps(self.model_dump(mode="json"), separators=(",", ":"))
        if len(encoded) > 512_000:
            raise ValueError("capture batch exceeds 512000 serialized characters")
        return self


class CaptureCheckpointWrite(BaseModel):
    version: str = Field(default=CAPTURE_CONTRACT_VERSION, max_length=16)
    through_sequence: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1, max_length=200)
    metadata: dict[str, Any] = Field(default_factory=dict)

    _bounded_metadata = field_validator("metadata")(_bounded_json)


class CaptureSessionEnd(BaseModel):
    version: str = Field(default=CAPTURE_CONTRACT_VERSION, max_length=16)
    idempotency_key: str = Field(min_length=1, max_length=200)
    final_sequence: int = Field(ge=0)


class CaptureSessionRead(BaseModel):
    id: UUID
    version: str
    client_session_id: str
    workspace_id: UUID | None
    profile_key: str
    status: Literal["active", "ended"]
    last_sequence: int
    started_at: datetime
    ended_at: datetime | None = None
    provenance: dict[str, Any] = Field(default_factory=dict)


class CaptureEventRead(BaseModel):
    id: UUID
    session_id: UUID
    version: str
    sequence: int
    event_class: str
    profile_key: str
    profile_version: int
    sensitivity: str
    source_trust: str
    retention_days: int
    screening_decision: Literal["clean", "redacted"]
    screening_reasons: list[str]
    occurred_at: datetime
    episode_id: UUID
    derived_memory_id: UUID | None = None
    provenance: dict[str, Any] = Field(default_factory=dict)


class CaptureEventBatchRead(BaseModel):
    events: list[CaptureEventRead]


class CaptureCheckpointRead(BaseModel):
    id: UUID
    session_id: UUID
    version: str
    through_sequence: int
    episode_id: UUID
    derived_memory_id: UUID | None = None
    promotion_status: str
    promotion_error: str | None = None


class MemoryCreate(SharedWrite, ScopeFields):
    subject: str = Field(default="", max_length=500)
    content: str = Field(min_length=1, max_length=200_000)
    memory_type: str = Field(default="fact", max_length=64)
    sensitivity: Literal["public", "internal", "confidential", "restricted"] = "internal"
    confidence: float = Field(default=1.0, ge=0, le=1)
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    _visible_content = field_validator("content")(_require_visible_text)
    _bounded_metadata = field_validator("metadata")(_bounded_json)

    @field_validator("valid_until")
    @classmethod
    def validate_valid_until(cls, value: datetime | None, info):  # type: ignore[no-untyped-def]
        start = info.data.get("valid_from")
        if value is not None and start is not None and value <= start:
            raise ValueError("valid_until must be after valid_from")
        return value


class MemoryUpdate(BaseModel):
    subject: str | None = Field(default=None, max_length=500)
    content: str | None = Field(default=None, min_length=1, max_length=200_000)
    memory_type: str | None = Field(default=None, max_length=64)
    sensitivity: Literal["public", "internal", "confidential", "restricted"] | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)
    valid_until: datetime | None = None
    metadata: dict[str, Any] | None = None

    _visible_content = field_validator("content")(_require_visible_text)
    _bounded_metadata = field_validator("metadata")(_bounded_json)


class MemorySafetyReview(BaseModel):
    action: Literal["release", "reject"]
    corroborating_memory_ids: list[UUID] = Field(default_factory=list, max_length=20)
    confidence: float | None = Field(default=None, ge=0, le=1)


class MemoryRead(ORMModel):
    id: UUID
    episode_id: UUID | None
    organization_id: UUID
    workspace_id: UUID | None
    project_ref: str | None
    task_ref: str | None
    run_ref: str | None
    subject: str
    content: str
    memory_type: str
    sensitivity: str
    confidence: float
    status: str
    valid_from: datetime
    valid_until: datetime | None
    embedding_model: str | None
    metadata_: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class SearchRequest(ScopeFields):
    query: str = Field(min_length=1, max_length=10_000)
    limit: int = Field(default=10, ge=1, le=100)
    memory_types: list[str] = Field(default_factory=list)
    sensitivity: list[str] = Field(default_factory=list)
    temporal_as_of: datetime | None = None
    include_graph: bool = True
    include_content: bool = True
    branch_ref: str | None = Field(default=None, max_length=300)


class SearchHit(BaseModel):
    memory_id: UUID | None = None
    kind: Literal["memory", "relationship"]
    subject: str
    content: str
    score: float
    backends: list[str]
    source_episode_id: UUID | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class SearchResponse(BaseModel):
    hits: list[SearchHit]
    degraded: list[str] = Field(default_factory=list)


class ContextRequest(SearchRequest):
    max_characters: int = Field(default=12_000, ge=500, le=200_000)


class ContextResponse(BaseModel):
    context: str
    citations: list[dict[str, Any]]
    degraded: list[str] = Field(default_factory=list)


class SessionContextRequest(ScopeFields):
    query: str = Field(default="current task project context", min_length=1, max_length=10_000)
    branch_ref: str | None = Field(default=None, max_length=300)
    sensitivity: list[str] = Field(default_factory=list)
    memory_types: list[str] = Field(default_factory=list)
    max_characters: int = Field(default=12_000, ge=100, le=200_000)
    max_tokens: int = Field(default=3_000, ge=16, le=50_000)
    max_items: int = Field(default=20, ge=1, le=100)
    trust_policy: Literal["exclude_untrusted", "label_untrusted", "include"] = "exclude_untrusted"
    # Capture clients send the provenance they capture with, so recall resolves the
    # same server-normalized project identity the captured memories were stored under.
    provenance: CaptureProvenance | None = None


class SessionContextResponse(BaseModel):
    context: str
    citations: list[dict[str, Any]]
    characters: int
    tokens: int
    truncated: bool
    complete: bool
    degraded: list[str] = Field(default_factory=list)


class EntityInput(BaseModel):
    name: str = Field(min_length=1, max_length=500)
    entity_type: str = Field(default="concept", max_length=100)
    description: str = Field(default="", max_length=10_000)
    confidence: float = Field(default=1.0, ge=0, le=1)


class RelationshipCreate(SharedWrite):
    source: EntityInput
    relation_type: str = Field(min_length=1, max_length=100)
    target: EntityInput
    sensitivity: Literal["public", "internal", "confidential", "restricted"] = "internal"
    source_memory_id: UUID | None = None
    confidence: float = Field(default=1.0, ge=0, le=1)
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    _bounded_metadata = field_validator("metadata")(_bounded_json)

    @model_validator(mode="after")
    def validate_interval(self):  # type: ignore[no-untyped-def]
        if (
            self.valid_from is not None
            and self.valid_until is not None
            and self.valid_until <= self.valid_from
        ):
            raise ValueError("valid_until must be after valid_from")
        return self


class RelationshipRead(BaseModel):
    id: UUID
    source_entity_id: UUID
    source_name: str
    source_type: str
    relation_type: str
    sensitivity: str
    target_entity_id: UUID
    target_name: str
    target_type: str
    source_memory_id: UUID | None
    confidence: float
    valid_from: datetime
    valid_until: datetime | None


class GraphQuery(BaseModel):
    query: str = Field(min_length=1, max_length=1000)
    workspace_id: UUID | None = None
    hops: int = Field(default=2, ge=1, le=5)
    limit: int = Field(default=50, ge=1, le=200)
    temporal_as_of: datetime | None = None


class SubgraphQuery(BaseModel):
    hops: int = Field(default=2, ge=1, le=5)
    limit: int = Field(default=50, ge=1, le=200)
    temporal_as_of: datetime | None = None


class WorkingMemoryCreate(BaseModel):
    task_id: str = Field(min_length=1, max_length=300)
    description: str = Field(min_length=1, max_length=50_000)
    workspace_id: UUID | None = None
    resource_type: str | None = Field(default=None, max_length=100)
    resource_ref: str | None = Field(default=None, max_length=500)
    expires_in_seconds: int = Field(default=7200, ge=60, le=604800)
    acquire_lock: bool = False


class WorkingMemoryUpdate(BaseModel):
    progress: dict[str, Any] | None = None
    expires_in_seconds: int | None = Field(default=None, ge=60, le=604800)

    _bounded_progress = field_validator("progress")(_bounded_json)


class WorkingMemoryFinish(BaseModel):
    consolidation_notes: str = Field(default="", max_length=100_000)
    # Finishing a task writes a task-record episode (source_type "task-record",
    # source_uri "working-memory://<task_id>") so every task leaves a canonical,
    # indexed account of what was done. Set record=False to opt out.
    record: bool = True
    record_sensitivity: Literal["public", "internal", "confidential", "restricted"] = "internal"
    project_ref: str | None = Field(default=None, max_length=300)


class WorkingMemoryRead(ORMModel):
    id: UUID
    task_id: str
    description: str
    status: str
    progress: dict[str, Any]
    resource_type: str | None
    resource_ref: str | None
    expires_at: datetime
    completed_at: datetime | None
    consolidation_notes: str | None
    record_episode_id: UUID | None


class LockAcquire(BaseModel):
    workspace_id: UUID | None = None
    working_memory_id: UUID | None = None
    resource_type: str = Field(min_length=1, max_length=100)
    resource_ref: str = Field(min_length=1, max_length=500)
    ttl_seconds: int = Field(default=900, ge=10, le=86400)


class LockRenew(BaseModel):
    token: str = Field(min_length=20)
    fence: int = Field(ge=1)
    ttl_seconds: int = Field(default=900, ge=10, le=86400)


class LockRelease(BaseModel):
    token: str = Field(min_length=20)
    fence: int = Field(ge=1)


class LockRead(BaseModel):
    id: UUID
    resource_type: str
    resource_ref: str
    fence: int
    expires_at: datetime
    token: str | None = None


class WorkingMemoryCreateRead(WorkingMemoryRead):
    lock: LockRead | None = None


class WorkspaceExport(BaseModel):
    ref: UUID
    name: str


class MemoryExport(BaseModel):
    ref: UUID
    workspace_ref: UUID | None = None
    project_ref: str | None = None
    task_ref: str | None = None
    run_ref: str | None = None
    subject: str
    content: str
    memory_type: str
    sensitivity: Literal["public", "internal", "confidential", "restricted"]
    confidence: float
    valid_from: datetime
    valid_until: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    _bounded_metadata = field_validator("metadata")(_bounded_json)


class RelationshipExport(BaseModel):
    workspace_ref: UUID | None = None
    source: EntityInput
    relation_type: str
    target: EntityInput
    sensitivity: Literal["public", "internal", "confidential", "restricted"]
    source_memory_ref: UUID | None = None
    confidence: float
    valid_from: datetime
    valid_until: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    _bounded_metadata = field_validator("metadata")(_bounded_json)


ExportFormatVersion = Literal["albert-export-v1"]
# Single source for the portability bundle format id (used by /v1/export and
# published in the runtime compatibility manifest).
EXPORT_FORMAT_VERSION: str = get_args(ExportFormatVersion)[0]


class ExportBundle(BaseModel):
    format_version: ExportFormatVersion = EXPORT_FORMAT_VERSION
    exported_at: datetime
    organization_name: str
    workspaces: list[WorkspaceExport]
    memories: list[MemoryExport]
    relationships: list[RelationshipExport]


class ImportRequest(BaseModel):
    bundle: ExportBundle
    workspace_id: UUID | None = None


class ImportResult(BaseModel):
    memories_created: int
    memories_skipped: int = 0
    relationships_created: int
    workspaces_created: int


class HealthResponse(BaseModel):
    status: str
    database: str
    version: str
    embedding_provider: str
    embedding_model: str
    embedding_dimensions: int
    classifier_provider: str
    graph_store: str = "postgres"
    graph_builder: str = "regex"
    graph_builder_version: str = "built-in"
    graph_builder_error: str | None = None
