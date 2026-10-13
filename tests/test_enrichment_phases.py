"""Enrichment keeps its external model calls outside the write transaction."""

from __future__ import annotations

from uuid import UUID

import httpx
import pytest
from sqlalchemy import select

from albert.db import SessionLocal
from albert.graph_builders import (
    BuildRequest,
    BuiltRelationship,
    get_graph_builder,
    set_graph_builder,
)
from albert.models import Memory, Relationship
from albert.services import _release_before_model_calls

from .conftest import drain_jobs

pytestmark = pytest.mark.anyio


class EditDuringBuild:
    """A builder that edits the memory from another session while it runs.

    The edit can only commit if enrichment released its transaction before
    calling the builder, which is the behaviour under test.
    """

    name = "edit-during-build"
    version = "test"

    def __init__(self) -> None:
        self.requests: list[BuildRequest] = []

    def build(self, request: BuildRequest, fallback: list) -> list[BuiltRelationship]:  # type: ignore[type-arg]
        del fallback
        self.requests.append(request)
        with SessionLocal() as other:
            memory = other.get(Memory, request.memory_id)
            assert memory is not None
            memory.content = f"{request.content} (edited mid-extraction)"
            other.commit()
        return []

    def readiness(self) -> str:
        return "test"


@pytest.fixture()
def restore_builder():  # type: ignore[no-untyped-def]
    yield
    set_graph_builder(None)


def test_releasing_the_read_transaction_refuses_to_discard_pending_work() -> None:
    with SessionLocal() as session:
        session.execute(select(Memory).limit(1))
        assert session.in_transaction()
        _release_before_model_calls(session)
        assert not session.in_transaction()

        session.add(Memory(organization_id=UUID(int=1), owner_principal_id=UUID(int=2)))
        with pytest.raises(RuntimeError, match="no pending changes"):
            _release_before_model_calls(session)


async def test_a_memory_edited_during_extraction_is_not_enriched_from_the_old_revision(
    client: httpx.AsyncClient, restore_builder
) -> None:  # type: ignore[no-untyped-def]
    builder = EditDuringBuild()
    set_graph_builder(builder)
    assert get_graph_builder() is builder

    response = await client.post(
        "/v1/memories", json={"content": "Albert runs on FalkorDB for graph storage."}
    )
    assert response.status_code == 201, response.text
    memory_id = UUID(response.json()["id"])

    drain_jobs()

    assert len(builder.requests) == 1
    with SessionLocal() as session:
        memory = session.get(Memory, memory_id)
        assert memory is not None
        # The concurrent edit committed, so no transaction was held across the
        # builder call.
        assert memory.content.endswith("(edited mid-extraction)")
        # And the stale result was discarded rather than written against the
        # revision it does not describe.
        assert "classification" not in (memory.metadata_ or {})
        assert (
            session.scalars(
                select(Relationship).where(Relationship.source_memory_id == memory_id)
            ).all()
            == []
        )
