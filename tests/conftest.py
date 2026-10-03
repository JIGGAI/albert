from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

DB_PATH = Path("/tmp") / f"albert-tests-{uuid4()}.sqlite3"
os.environ["ALBERT_DATABASE_URL"] = f"sqlite:///{DB_PATH}"
os.environ["ALBERT_API_KEY_PEPPER"] = "test-pepper-that-is-longer-than-thirty-two-characters"
os.environ["ALBERT_EMBEDDING_PROVIDER"] = "hashing"
os.environ["ALBERT_EMBEDDING_DIMENSIONS"] = "64"

from albert.api import app  # noqa: E402
from albert.db import Base, SessionLocal, engine  # noqa: E402
from albert.models import APIKey, Organization, Principal, Workspace  # noqa: E402
from albert.security import issue_api_key  # noqa: E402


def create_identity(
    name: str, workspace_scoped: bool = True
) -> tuple[str, Organization, Workspace]:
    with SessionLocal() as session:
        organization = Organization(name=f"{name}-{uuid4()}")
        session.add(organization)
        session.flush()
        workspace = Workspace(organization_id=organization.id, name="Default")
        session.add(workspace)
        session.flush()
        principal = Principal(
            organization_id=organization.id,
            workspace_id=workspace.id if workspace_scoped else None,
            name=name,
            principal_type="agent",
        )
        session.add(principal)
        session.flush()
        raw, prefix, digest = issue_api_key()
        session.add(
            APIKey(
                principal_id=principal.id,
                prefix=prefix,
                key_hash=digest,
                capabilities=[
                    "memory.read",
                    "memory.write",
                    "memory.delete",
                    "memory.export",
                    "memory.import",
                    "graph.query",
                    "graph.write",
                    "working_memory.write",
                    "locks.acquire",
                ],
            )
        )
        session.commit()
        session.refresh(organization)
        session.refresh(workspace)
        return raw, organization, workspace


@pytest.fixture(scope="session", autouse=True)
def database():  # type: ignore[no-untyped-def]
    Base.metadata.create_all(engine)
    yield
    # Flush queued traces before the tables disappear, as the API does on shutdown.
    from albert.trace_writer import get_trace_writer

    get_trace_writer().stop()
    Base.metadata.drop_all(engine)
    engine.dispose()
    DB_PATH.unlink(missing_ok=True)


@pytest.fixture()
def identity():  # type: ignore[no-untyped-def]
    return create_identity("Test Agent")


@pytest.fixture()
async def client(identity):  # type: ignore[no-untyped-def]
    key, _organization, _workspace = identity
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {key}"},
    ) as test_client:
        yield test_client


@pytest.fixture()
def anyio_backend() -> str:
    return "asyncio"


def create_principal_key(
    organization: Organization,
    workspace: Workspace | None,
    name: str,
    capabilities: list[str],
) -> str:
    """Issue a key for an additional principal inside an existing tenant."""
    with SessionLocal() as session:
        principal = Principal(
            organization_id=organization.id,
            workspace_id=workspace.id if workspace is not None else None,
            name=name,
            principal_type="agent",
        )
        session.add(principal)
        session.flush()
        raw, prefix, digest = issue_api_key()
        session.add(
            APIKey(
                principal_id=principal.id,
                prefix=prefix,
                key_hash=digest,
                capabilities=capabilities,
            )
        )
        session.commit()
        return raw


def drain_jobs() -> None:
    from albert.worker import claim_job, process_job

    while True:
        with SessionLocal() as session:
            job = claim_job(session, "test-worker")
            if job is None:
                return
            process_job(session, job)
