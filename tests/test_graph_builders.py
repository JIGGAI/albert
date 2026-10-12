from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

from albert.graph_builders import BuildRequest, GraphitiGraphBuilder


class TestGraphitiBuilder(GraphitiGraphBuilder):
    __test__ = False

    def _episode_type(self) -> str:
        return "text"


class FakeGraphiti:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.closed = False

    async def add_episode(self, **kwargs):  # type: ignore[no-untyped-def]
        self.calls.append(kwargs)
        source = SimpleNamespace(uuid="source", name="Albert", labels=["Entity", "Product"])
        target = SimpleNamespace(uuid="target", name="FalkorDB", labels=["Entity", "Database"])
        edge = SimpleNamespace(
            uuid="edge-1",
            source_node_uuid="source",
            target_node_uuid="target",
            name="RUNS ON",
            episodes=[kwargs["uuid"]],
            attributes={"confidence": 0.91},
            valid_at=datetime(2026, 1, 1, tzinfo=UTC),
            invalid_at=datetime(2026, 2, 1, tzinfo=UTC),
            reference_time=None,
        )
        return SimpleNamespace(nodes=[source, target], edges=[edge])

    async def close(self) -> None:
        self.closed = True


def test_graphiti_maps_temporal_facts_and_scopes_episode() -> None:
    fake = FakeGraphiti()
    builder = TestGraphitiBuilder(SimpleNamespace())  # type: ignore[arg-type]
    builder._client = lambda: fake  # type: ignore[method-assign]
    organization_id = uuid4()
    workspace_id = uuid4()
    memory_id = uuid4()
    built = builder.build(
        BuildRequest(
            memory_id=memory_id,
            organization_id=organization_id,
            workspace_id=workspace_id,
            subject="Runtime",
            content="Albert runs on FalkorDB.",
            occurred_at=datetime(2025, 12, 1, tzinfo=UTC),
            content_fingerprint="revision-1",
        ),
        [],
    )

    assert fake.closed is True
    assert fake.calls[0]["group_id"] == f"{organization_id}:{workspace_id}"
    assert fake.calls[0]["uuid"]
    assert len(built) == 1
    fact = built[0]
    assert fact.relationship.source.name == "Albert"
    assert fact.relationship.source.entity_type == "product"
    assert fact.relationship.relation_type == "runs_on"
    assert fact.relationship.target.name == "FalkorDB"
    assert fact.valid_from == datetime(2026, 1, 1, tzinfo=UTC)
    assert fact.valid_until == datetime(2026, 2, 1, tzinfo=UTC)
    assert fact.metadata["builder"] == "graphiti"


def test_graphiti_episode_identity_is_stable_for_retries_and_changes_for_edits() -> None:
    fake = FakeGraphiti()
    builder = TestGraphitiBuilder(SimpleNamespace())  # type: ignore[arg-type]
    builder._client = lambda: fake  # type: ignore[method-assign]
    base = {
        "memory_id": uuid4(),
        "organization_id": uuid4(),
        "workspace_id": None,
        "subject": "Runtime",
        "content": "Albert runs on FalkorDB.",
        "occurred_at": datetime(2026, 1, 1, tzinfo=UTC),
    }
    builder.build(BuildRequest(**base, content_fingerprint="one"), [])
    builder.build(BuildRequest(**base, content_fingerprint="one"), [])
    builder.build(BuildRequest(**base, content_fingerprint="two"), [])

    ids = [call["uuid"] for call in fake.calls]
    assert ids[0] == ids[1]
    assert ids[1] != ids[2]
    assert fake.calls[0]["group_id"].endswith(":*")
