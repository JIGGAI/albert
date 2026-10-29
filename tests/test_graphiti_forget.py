"""Forgetting removes Graphiti's own copy of a source, on a real graph server.

Graphiti stores episodes, facts (RELATES_TO edges with a `fact` sentence) and
entity summaries of its own. The graph here is written the way Graphiti writes
it; the builder's forget must remove one source's episode, the facts it
created and the entities only it mentioned, and clear the summaries of entities
another episode still mentions, without touching anything else.
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest

from albert.config import Settings
from albert.graph_builders import BuildRequest, GraphitiGraphBuilder, _graphiti_group_id

FALKORDB_URL = os.environ.get("ALBERT_TEST_FALKORDB_URL")
NEO4J_URL = os.environ.get("ALBERT_TEST_NEO4J_URL")
NEO4J_PASSWORD = os.environ.get("ALBERT_TEST_NEO4J_PASSWORD", "")


@pytest.fixture(params=["falkordb", "neo4j"])
def builder(request):  # type: ignore[no-untyped-def]
    models = {
        "llm_provider": "openai-compatible",
        "embedding_provider": "openai-compatible",
        "openai_api_key": "test-only-placeholder",
        "classification_model": "test-model",
        "graph_builder": "graphiti",
    }
    if request.param == "falkordb":
        if not FALKORDB_URL:
            pytest.skip("ALBERT_TEST_FALKORDB_URL is not set")
        settings = Settings(
            graph_store="falkordb",
            falkordb_url=FALKORDB_URL,
            falkordb_graph=f"graphiti_forget_{uuid4().hex}",
            **models,
        )
    else:
        if not NEO4J_URL:
            pytest.skip("ALBERT_TEST_NEO4J_URL is not set")
        settings = Settings(
            graph_store="neo4j", neo4j_url=NEO4J_URL, neo4j_password=NEO4J_PASSWORD, **models
        )
    return GraphitiGraphBuilder(settings)


def _request(organization_id, workspace_id, content: str) -> BuildRequest:  # type: ignore[no-untyped-def]
    return BuildRequest(
        memory_id=uuid4(),
        organization_id=organization_id,
        workspace_id=workspace_id,
        subject=content,
        content=content,
        occurred_at=datetime.now(UTC),
        content_fingerprint="revision-1",
    )


async def _with_driver(builder: GraphitiGraphBuilder, group_id: str, work) -> Any:  # type: ignore[no-untyped-def]
    client = builder._client()
    try:
        _, driver, _ = client._resolve_request_scope(group_id)
        if getattr(driver, "_init_task", None) is not None:
            await driver._init_task
        return await work(client, driver)
    finally:
        await builder._settle_background_tasks(client)
        await builder._close_client(client)


async def _query(driver, query: str, **params) -> list[dict]:  # type: ignore[no-untyped-def]
    records, _, _ = await driver.execute_query(query, **params)
    return [dict(record) for record in records]


async def _seed(builder, group_id, first: BuildRequest, second: BuildRequest) -> dict[str, str]:  # type: ignore[no-untyped-def]
    """Two episodes as Graphiti leaves them, sharing one entity."""
    ids = {
        "first": str(uuid4()),
        "second": str(uuid4()),
        "only_first": str(uuid4()),
        "shared": str(uuid4()),
        "only_second": str(uuid4()),
        "first_fact": str(uuid4()),
        "second_fact": str(uuid4()),
    }

    async def work(client, driver):  # type: ignore[no-untyped-def]
        # Saved and tagged exactly as a build does it; the raw content stays when
        # extraction fails, which is the worst case for forgetting.
        await builder._prepare_episode(client, first, ids["first"], group_id)
        await builder._prepare_episode(client, second, ids["second"], group_id)
        await driver.execute_query(
            """
            MATCH (a:Episodic {uuid: $first}), (b:Episodic {uuid: $second})
            CREATE (x:Entity {uuid: $only_first, name: 'Osprey Vault', group_id: $group,
                              summary: 'Osprey Vault holds the payroll export'}),
                   (s:Entity {uuid: $shared, name: 'Osprey', group_id: $group,
                              summary: 'Osprey runs payroll and the cafeteria rota'}),
                   (y:Entity {uuid: $only_second, name: 'Osprey Rota', group_id: $group,
                              summary: 'The cafeteria rota'}),
                   (x)-[:RELATES_TO {uuid: $first_fact, group_id: $group, name: 'HOLDS',
                                     fact: 'Osprey Vault holds the payroll export',
                                     episodes: [$first]}]->(s),
                   (s)-[:RELATES_TO {uuid: $second_fact, group_id: $group, name: 'RUNS',
                                     fact: 'Osprey runs the cafeteria rota',
                                     episodes: [$second]}]->(y),
                   (a)-[:MENTIONS {uuid: randomUUID(), group_id: $group}]->(x),
                   (a)-[:MENTIONS {uuid: randomUUID(), group_id: $group}]->(s),
                   (b)-[:MENTIONS {uuid: randomUUID(), group_id: $group}]->(s),
                   (b)-[:MENTIONS {uuid: randomUUID(), group_id: $group}]->(y)
            SET a.entity_edges = [$first_fact], b.entity_edges = [$second_fact]
            """,
            group=group_id,
            **ids,
        )

    await _with_driver(builder, group_id, work)
    return ids


def test_forgetting_a_source_removes_only_its_graphiti_data(builder) -> None:  # type: ignore[no-untyped-def]
    organization_id, workspace_id = uuid4(), uuid4()
    group_id = _graphiti_group_id(organization_id, workspace_id)
    first = _request(organization_id, workspace_id, "Osprey Vault holds the payroll export")
    second = _request(organization_id, workspace_id, "Osprey runs the cafeteria rota")
    ids = asyncio.run(_seed(builder, group_id, first, second))

    forgotten = builder.forget(organization_id, workspace_id, [first.memory_id], [])
    assert forgotten == 1

    async def inspect(client, driver):  # type: ignore[no-untyped-def]
        episodes = await _query(
            driver,
            "MATCH (e:Episodic) WHERE e.group_id = $g RETURN e.uuid AS uuid, e.content AS content",
            g=group_id,
        )
        entities = await _query(
            driver,
            "MATCH (n:Entity) WHERE n.group_id = $g RETURN n.uuid AS uuid, n.summary AS summary",
            g=group_id,
        )
        facts = await _query(
            driver,
            "MATCH ()-[r:RELATES_TO]->() WHERE r.group_id = $g RETURN r.fact AS fact",
            g=group_id,
        )
        return episodes, entities, facts

    episodes, entities, facts = asyncio.run(_with_driver(builder, group_id, inspect))
    assert {e["uuid"] for e in episodes} == {ids["second"]}
    assert episodes[0]["content"] == second.content
    summaries = {e["uuid"]: e["summary"] for e in entities}
    assert set(summaries) == {ids["shared"], ids["only_second"]}
    assert summaries[ids["shared"]] == ""
    assert summaries[ids["only_second"]] == "The cafeteria rota"
    assert [f["fact"] for f in facts] == ["Osprey runs the cafeteria rota"]
    assert not any(
        "payroll" in str(value) for row in [*episodes, *entities, *facts] for value in row.values()
    )

    # An episode built before tagging is found by its id alone.
    assert builder.forget(organization_id, workspace_id, [], [ids["second"]]) == 1
    assert builder.forget(organization_id, workspace_id, [second.memory_id], []) == 0


def test_forgetting_a_workspace_drops_its_graphiti_group(builder) -> None:  # type: ignore[no-untyped-def]
    organization_id, doomed, kept = uuid4(), uuid4(), uuid4()
    doomed_group = _graphiti_group_id(organization_id, doomed)
    kept_group = _graphiti_group_id(organization_id, kept)
    asyncio.run(
        _seed(
            builder,
            doomed_group,
            _request(organization_id, doomed, "Doomed one"),
            _request(organization_id, doomed, "Doomed two"),
        )
    )
    asyncio.run(
        _seed(
            builder,
            kept_group,
            _request(organization_id, kept, "Kept one"),
            _request(organization_id, kept, "Kept two"),
        )
    )

    builder.forget_scope(organization_id, doomed)

    def nodes(group_id: str) -> int:
        async def count(client, driver):  # type: ignore[no-untyped-def]
            rows = await _query(
                driver, "MATCH (n) WHERE n.group_id = $g RETURN count(n) AS n", g=group_id
            )
            return rows[0]["n"]

        return asyncio.run(_with_driver(builder, group_id, count))

    assert nodes(doomed_group) == 0
    assert nodes(kept_group) == 5
