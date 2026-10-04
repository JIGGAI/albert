"""Exercise vector, graph and memory-link behavior against a migrated PostgreSQL database."""

from __future__ import annotations

import sys
from uuid import uuid4

from sqlalchemy import func, select, text

from albert.db import SessionLocal, engine
from albert.graph import query_relationships
from albert.links import process_recall_traces, rebuild_links
from albert.models import (
    MemoryLink,
    MemoryStat,
    Organization,
    Principal,
    Trace,
    Workspace,
)
from albert.schemas import EpisodeCreate, MemoryCreate, SearchRequest
from albert.search import hybrid_search
from albert.security import AuthContext
from albert.services import create_episode, create_memory, enrich_memory


def main() -> None:
    if engine.dialect.name != "postgresql":
        raise RuntimeError("PostgreSQL smoke test requires a PostgreSQL database")

    with SessionLocal() as session:
        extension = session.scalar(
            text("SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector')")
        )
        hnsw = session.scalar(
            text(
                "SELECT EXISTS (SELECT 1 FROM pg_indexes "
                "WHERE indexname = 'ix_memory_chunks_embedding_hnsw')"
            )
        )
        if not extension or not hnsw:
            raise RuntimeError("pgvector extension or HNSW index is unavailable")
        fts_index = session.scalar(
            text(
                "SELECT EXISTS (SELECT 1 FROM pg_indexes "
                "WHERE indexname = 'ix_memories_search_vector')"
            )
        )
        if not fts_index:
            raise RuntimeError("stored full-text search index is missing")
        nulls_not_distinct = session.scalar(
            text(
                "SELECT index.indnullsnotdistinct FROM pg_index AS index "
                "JOIN pg_class AS relation ON relation.oid = index.indexrelid "
                "WHERE relation.relname = 'uq_entities_scope_name_type'"
            )
        )
        if nulls_not_distinct is not True:
            raise RuntimeError("entity uniqueness does not treat NULL workspaces consistently")

        suffix = uuid4().hex
        organization = Organization(name=f"PostgreSQL smoke {suffix}")
        session.add(organization)
        session.flush()
        workspace = Workspace(organization_id=organization.id, name="Default")
        session.add(workspace)
        session.flush()
        principal = Principal(
            organization_id=organization.id,
            workspace_id=workspace.id,
            name="Smoke Agent",
            principal_type="agent",
        )
        session.add(principal)
        session.flush()
        auth = AuthContext(
            principal_id=principal.id,
            organization_id=organization.id,
            workspace_id=workspace.id,
            capabilities=frozenset({"memory.read", "memory.write", "graph.query"}),
        )
        organization_auth = AuthContext(
            principal_id=principal.id,
            organization_id=organization.id,
            workspace_id=None,
            capabilities=auth.capabilities,
        )
        second_workspace = Workspace(organization_id=organization.id, name="Second")
        session.add(second_workspace)
        session.commit()
        first_episode, _ = create_episode(
            session,
            organization_auth,
            EpisodeCreate(
                workspace_id=workspace.id,
                content="Workspace-specific canonical episode",
                source_uri="smoke://shared",
            ),
        )
        second_episode, _ = create_episode(
            session,
            organization_auth,
            EpisodeCreate(
                workspace_id=second_workspace.id,
                content="Workspace-specific canonical episode",
                source_uri="smoke://shared",
            ),
        )
        if first_episode.id == second_episode.id:
            raise RuntimeError("episode deduplication crossed workspace boundaries")
        memory = create_memory(
            session,
            auth,
            MemoryCreate(
                subject="PostgreSQL vector smoke",
                content="Smoke Agent uses PostgreSQL.",
                project_ref="postgres-smoke",
            ),
        )
        enrich_memory(session, memory.id)
        session.commit()

        results = hybrid_search(
            session,
            organization_id=organization.id,
            workspace_id=workspace.id,
            request=SearchRequest(
                query="PostgreSQL",
                project_ref="postgres-smoke",
                limit=5,
            ),
            allowed_sensitivities=["public", "internal"],
        )
        matching = [hit for hit in results.hits if hit.memory_id == memory.id]
        if not matching or "vector" not in matching[0].backends:
            backends = [hit.backends for hit in matching]
            raise RuntimeError(
                "pgvector retrieval did not return the enriched memory; "
                f"matching_backends={backends}, degraded={results.degraded}"
            )
        relationships = query_relationships(
            session,
            organization_id=organization.id,
            workspace_id=workspace.id,
            query="PostgreSQL",
            temporal_as_of=None,
            limit=5,
            sensitivities=["public", "internal"],
        )
        if not relationships or relationships[0].target_name != "PostgreSQL":
            raise RuntimeError("graph extraction or query did not return the expected edge")

        # Memory links: nearest neighbours through pgvector, then recall processing.
        topic = "Brand voice posts sound sharp confident masculine friendly persuasive bold"
        linked = []
        for index in range(3):
            item = create_memory(
                session, auth, MemoryCreate(subject=f"Voice {index}", content=f"{topic} v{index}")
            )
            enrich_memory(session, item.id)
            linked.append(item.id)
        session.commit()

        def similar_links() -> int:
            return int(
                session.scalar(
                    select(func.count(MemoryLink.id)).where(
                        MemoryLink.organization_id == organization.id,
                        MemoryLink.kind == "similar",
                    )
                )
                or 0
            )

        if similar_links() < 2:
            raise RuntimeError(f"indexing wrote {similar_links()} similar links, expected >= 2")
        rebuilt = rebuild_links(session, organization.id)
        session.commit()
        if rebuilt["similar"] < 3 or similar_links() != rebuilt["similar"]:
            raise RuntimeError(f"rebuild-links produced {rebuilt}, stored {similar_links()}")
        session.add(
            Trace(
                kind="request",
                name="POST /v1/search",
                organization_id=organization.id,
                principal_id=principal.id,
                status="ok",
                http_status=200,
                duration_ms=1.0,
                summary={"query": "brand voice", "memory_ids": [str(i) for i in linked]},
                spans=[],
            )
        )
        session.commit()
        while process_recall_traces(session, settle_seconds=0):
            pass
        if process_recall_traces(session, settle_seconds=0) != 0:
            raise RuntimeError("recall processing is not idempotent")
        stat = session.get(MemoryStat, linked[0])
        recalled = session.scalar(
            select(func.count(MemoryLink.id)).where(
                MemoryLink.organization_id == organization.id, MemoryLink.kind == "recalled"
            )
        )
        if stat is None or stat.recall_count != 1 or recalled != 3:
            raise RuntimeError(
                f"recall processing wrote stat={stat and stat.recall_count}, links={recalled}"
            )

    print("PostgreSQL vector, graph and memory-link smoke test passed")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        message = str(exc).replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        print(
            f"::error title=PostgreSQL smoke test failed::{type(exc).__name__}: {message}",
            file=sys.stderr,
        )
        raise
