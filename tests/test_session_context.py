from __future__ import annotations

import httpx
import pytest

from albert.schemas import SearchHit, SearchResponse, SessionContextRequest
from albert.session_context import assemble_session_context, count_tokens

from .conftest import create_identity

pytestmark = pytest.mark.anyio


def _hit(index: int, content: str, **metadata: object) -> SearchHit:
    from uuid import UUID

    return SearchHit(
        memory_id=UUID(int=index),
        kind="memory",
        subject=f"subject {index}",
        content=content,
        score=1.0,
        backends=["vector", "lexical"],
        metadata={"memory_type": "fact", "confidence": 1.0, **metadata},
    )


def test_session_package_enforces_both_budgets_and_citations() -> None:
    result = SearchResponse(
        hits=[_hit(1, "one two three extra"), _hit(2, "four five six extra")], degraded=[]
    )
    request = SessionContextRequest(max_characters=100, max_tokens=16, max_items=10)

    package = assemble_session_context(result, request)

    assert package.characters == len(package.context) <= request.max_characters
    assert package.tokens == count_tokens(package.context) <= request.max_tokens
    assert len(package.citations) == package.context.count("\n[") + 1
    assert [citation["index"] for citation in package.citations] == list(
        range(1, len(package.citations) + 1)
    )
    assert package.truncated is True
    assert package.complete is False


async def _memory(client: httpx.AsyncClient, subject: str, **values: object) -> str:
    payload = {
        "subject": subject,
        "content": f"Session recall sentinel {subject}",
        "memory_type": "fact",
        **values,
    }
    response = await client.post("/v1/memories", json=payload)
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def test_session_start_ranks_scope_diversifies_and_filters_trust(
    client: httpx.AsyncClient,
) -> None:
    await _memory(
        client,
        "current branch procedure",
        project_ref="project-a",
        task_ref="task-a",
        memory_type="procedure",
        metadata={
            "capture_source_trust": "trusted",
            "provenance": {"branch": "feat/current"},
        },
    )
    durable = await _memory(
        client,
        "durable project decision",
        project_ref="project-a",
        memory_type="decision",
        metadata={"capture_source_trust": "verified", "provenance": {"branch": "main"}},
    )
    await _memory(
        client,
        "untrusted claim",
        project_ref="project-a",
        metadata={"capture_source_trust": "untrusted"},
    )
    await _memory(
        client,
        "quarantined claim",
        project_ref="project-a",
        metadata={"capture_source_trust": "trusted", "quarantined": True},
    )

    payload = {
        "query": "Session recall sentinel",
        "project_ref": "project-a",
        "task_ref": "task-a",
        "branch_ref": "feat/current",
        "max_characters": 4000,
        "max_tokens": 1000,
    }
    first = await client.post("/v1/context/session-start", json=payload)
    second = await client.post("/v1/context/session-start", json=payload)

    assert first.status_code == 200, first.text
    assert first.json() == second.json()
    body = first.json()
    assert body["citations"][0]["branch_ref"] == "feat/current"
    assert durable in {citation["memory_id"] for citation in body["citations"]}
    assert "untrusted claim" not in body["context"]
    assert "quarantined claim" not in body["context"]
    assert {citation["memory_type"] for citation in body["citations"]} >= {
        "procedure",
        "decision",
    }


async def test_session_start_is_tenant_isolated_and_reports_safe_degradation(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    foreign_key, _organization, _workspace = create_identity("Foreign session recall")
    async with httpx.AsyncClient(
        transport=client._transport,  # type: ignore[attr-defined]
        base_url="http://test",
        headers={"Authorization": f"Bearer {foreign_key}"},
    ) as foreign:
        await _memory(foreign, "foreign secret body")
    await _memory(client, "local safe body")

    import albert.search

    monkeypatch.setattr(
        albert.search,
        "get_embedder",
        lambda: (_ for _ in ()).throw(RuntimeError("backend body must not leak")),
    )
    response = await client.post(
        "/v1/context/session-start",
        json={"query": "Session recall sentinel", "trust_policy": "include"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert "local safe body" in body["context"]
    assert "foreign secret body" not in body["context"]
    assert body["degraded"] == ["vector retrieval unavailable: RuntimeError"]
    assert "backend body must not leak" not in response.text
    assert body["complete"] is False
