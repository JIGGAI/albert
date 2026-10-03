"""Exercise vector and graph behavior against a migrated PostgreSQL database."""

from __future__ import annotations

import sys
from uuid import uuid4

from sqlalchemy import text

from albert.db import SessionLocal, engine
from albert.graph import query_relationships
from albert.models import Organization, Principal, Workspace
from albert.schemas import MemoryCreate, SearchRequest
from albert.search import hybrid_search
from albert.security import AuthContext
from albert.services import create_memory, enrich_memory


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
                "WHERE indexname = 'ix_memories_embedding_hnsw')"
            )
        )
        if not extension or not hnsw:
            raise RuntimeError("pgvector extension or HNSW index is unavailable")

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

    print("PostgreSQL vector and graph smoke test passed")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        message = str(exc).replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        print(
            f"::error title=PostgreSQL vector and graph smoke test failed::"
            f"{type(exc).__name__}: {message}",
            file=sys.stderr,
        )
        raise
