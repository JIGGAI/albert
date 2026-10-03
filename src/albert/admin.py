from __future__ import annotations

from uuid import UUID

import typer
from sqlalchemy import select, text

from albert.db import SessionLocal, initialize_database
from albert.embeddings import get_embedder
from albert.models import APIKey, Job, Memory, Organization, Principal, Workspace
from albert.security import issue_api_key

app = typer.Typer(help="Albert administrative commands", no_args_is_help=True)

ALL_CAPABILITIES = [
    "memory.read",
    "memory.write",
    "memory.delete",
    "memory.export",
    "memory.import",
    "graph.query",
    "graph.write",
    "working_memory.write",
    "locks.acquire",
    "admin",
]


@app.command("init-db")
def init_db() -> None:
    """Create database extensions and tables for development."""
    initialize_database()
    typer.echo("Database initialized")


@app.command()
def bootstrap(
    organization: str = typer.Option(..., help="Initial organization name"),
    workspace: str = typer.Option(..., help="Initial workspace name"),
    principal: str = typer.Option("Administrator", help="Administrator display name"),
) -> None:
    """Create the first tenant and print its administrator key exactly once."""
    initialize_database()
    with SessionLocal() as session:
        org = session.scalar(select(Organization).where(Organization.name == organization))
        if org is None:
            org = Organization(name=organization)
            session.add(org)
            session.flush()
        work = session.scalar(
            select(Workspace).where(
                Workspace.organization_id == org.id, Workspace.name == workspace
            )
        )
        if work is None:
            work = Workspace(organization_id=org.id, name=workspace)
            session.add(work)
            session.flush()
        actor = session.scalar(
            select(Principal).where(
                Principal.organization_id == org.id,
                Principal.workspace_id == work.id,
                Principal.name == principal,
            )
        )
        if actor is None:
            actor = Principal(
                organization_id=org.id,
                workspace_id=work.id,
                name=principal,
                principal_type="human",
            )
            session.add(actor)
            session.flush()
        raw, prefix, digest = issue_api_key()
        session.add(
            APIKey(
                principal_id=actor.id,
                prefix=prefix,
                key_hash=digest,
                name="bootstrap administrator",
                capabilities=ALL_CAPABILITIES,
            )
        )
        session.commit()
    typer.echo("Bootstrap complete. Store this key securely; it will not be shown again:")
    typer.echo(raw)


@app.command("create-key")
def create_key(
    principal_id: str = typer.Option(...),
    name: str = typer.Option("agent key"),
    capabilities: str = typer.Option("memory.read,memory.write,graph.query"),
) -> None:
    """Issue an additional API key for an existing principal."""
    requested_capabilities = [item.strip() for item in capabilities.split(",") if item.strip()]
    known_capabilities = {*ALL_CAPABILITIES, "memory.confidential", "memory.restricted"}
    unknown = sorted(set(requested_capabilities) - known_capabilities)
    if unknown:
        raise typer.BadParameter(f"Unknown capabilities: {', '.join(unknown)}")
    with SessionLocal() as session:
        actor = session.get(Principal, UUID(principal_id))
        if actor is None:
            raise typer.BadParameter("Principal not found")
        raw, prefix, digest = issue_api_key()
        session.add(
            APIKey(
                principal_id=actor.id,
                prefix=prefix,
                key_hash=digest,
                name=name,
                capabilities=requested_capabilities,
            )
        )
        session.commit()
    typer.echo(raw)


@app.command("create-workspace")
def create_workspace(
    organization: str = typer.Option(..., help="Organization name"),
    name: str = typer.Option(..., help="Workspace name"),
) -> None:
    """Create a workspace in an existing organization."""
    with SessionLocal() as session:
        org = session.scalar(select(Organization).where(Organization.name == organization))
        if org is None:
            raise typer.BadParameter("Organization not found")
        existing = session.scalar(
            select(Workspace).where(
                Workspace.organization_id == org.id,
                Workspace.name == name,
            )
        )
        if existing is not None:
            typer.echo(str(existing.id))
            return
        workspace = Workspace(organization_id=org.id, name=name)
        session.add(workspace)
        session.commit()
        session.refresh(workspace)
        typer.echo(str(workspace.id))


@app.command("create-principal")
def create_principal(
    organization: str = typer.Option(..., help="Organization name"),
    name: str = typer.Option(..., help="Principal display name"),
    workspace: str | None = typer.Option(None, help="Optional workspace name"),
    principal_type: str = typer.Option("agent", help="agent, human, or service"),
) -> None:
    """Create an organization- or workspace-scoped principal."""
    if principal_type not in {"agent", "human", "service"}:
        raise typer.BadParameter("principal-type must be agent, human, or service")
    with SessionLocal() as session:
        org = session.scalar(select(Organization).where(Organization.name == organization))
        if org is None:
            raise typer.BadParameter("Organization not found")
        workspace_id = None
        if workspace is not None:
            record = session.scalar(
                select(Workspace).where(
                    Workspace.organization_id == org.id,
                    Workspace.name == workspace,
                )
            )
            if record is None:
                raise typer.BadParameter("Workspace not found")
            workspace_id = record.id
        principal = Principal(
            organization_id=org.id,
            workspace_id=workspace_id,
            name=name,
            principal_type=principal_type,
        )
        session.add(principal)
        session.commit()
        session.refresh(principal)
        typer.echo(str(principal.id))


@app.command("list-principals")
def list_principals(
    organization: str = typer.Option(..., help="Organization name"),
) -> None:
    """List principals and their workspace scope."""
    with SessionLocal() as session:
        org = session.scalar(select(Organization).where(Organization.name == organization))
        if org is None:
            raise typer.BadParameter("Organization not found")
        principals = session.scalars(
            select(Principal).where(Principal.organization_id == org.id).order_by(Principal.name)
        )
        for principal in principals:
            typer.echo(
                f"{principal.id}\t{principal.name}\t{principal.principal_type}\t"
                f"workspace={principal.workspace_id or '*'}\tactive={principal.active}"
            )


@app.command("list-keys")
def list_keys(principal_id: str = typer.Option(...)) -> None:
    """List non-secret API-key metadata for one principal."""
    with SessionLocal() as session:
        actor = session.get(Principal, UUID(principal_id))
        if actor is None:
            raise typer.BadParameter("Principal not found")
        keys = session.scalars(
            select(APIKey).where(APIKey.principal_id == actor.id).order_by(APIKey.created_at)
        )
        for key in keys:
            typer.echo(
                f"{key.id}\t{key.name}\tprefix={key.prefix}\tactive={key.active}\t"
                f"expires={key.expires_at or '-'}"
            )


@app.command("revoke-key")
def revoke_key(key_id: str = typer.Option(...)) -> None:
    """Revoke an API key without deleting its audit identity."""
    with SessionLocal() as session:
        key = session.get(APIKey, UUID(key_id))
        if key is None:
            raise typer.BadParameter("API key not found")
        key.active = False
        session.commit()
    typer.echo("API key revoked")


@app.command("validate-runtime")
def validate_runtime(
    probe_embedding: bool = typer.Option(
        False, "--probe-embedding", help="Load and execute the configured embedding provider"
    ),
) -> None:
    """Validate provider configuration and optionally execute one embedding."""
    from albert.config import get_settings

    settings = get_settings()
    typer.echo(
        f"embedding={settings.embedding_provider}:{settings.embedding_model} "
        f"dimensions={settings.embedding_dimensions} classifier={settings.llm_provider}"
    )
    with SessionLocal() as session:
        if session.bind is not None and session.bind.dialect.name == "postgresql":
            vector_type = session.scalar(
                text(
                    "SELECT format_type(attribute.atttypid, attribute.atttypmod) "
                    "FROM pg_attribute AS attribute "
                    "JOIN pg_class AS relation ON relation.oid = attribute.attrelid "
                    "WHERE relation.relname = 'memories' "
                    "AND attribute.attname = 'embedding' "
                    "AND NOT attribute.attisdropped"
                )
            )
            expected = f"vector({settings.embedding_dimensions})"
            if vector_type != expected:
                typer.echo(
                    f"database embedding type is {vector_type!r}; expected {expected!r}",
                    err=True,
                )
                raise typer.Exit(code=1)
            hnsw_exists = session.scalar(
                text(
                    "SELECT EXISTS (SELECT 1 FROM pg_indexes "
                    "WHERE indexname = 'ix_memories_embedding_hnsw')"
                )
            )
            if not hnsw_exists:
                typer.echo("database HNSW embedding index is missing", err=True)
                raise typer.Exit(code=1)
            typer.echo("database vector schema and HNSW index are valid")
    if probe_embedding:
        embedder = get_embedder()
        vector = embedder.embed("Albert runtime readiness probe")
        if len(vector) != settings.embedding_dimensions:
            raise typer.Exit(code=1)
        typer.echo(f"embedding probe succeeded ({len(vector)} dimensions)")


@app.command("reindex-memories")
def reindex_memories(
    all_memories: bool = typer.Option(
        False, "--all", help="Re-enrich every active memory, including current embeddings"
    ),
) -> None:
    """Queue active memories whose embedding is absent or uses another model."""
    embedder = get_embedder()
    with SessionLocal() as session:
        statement = select(Memory).where(Memory.status == "active")
        memories = list(session.scalars(statement))
        selected = [
            memory
            for memory in memories
            if all_memories or memory.embedding is None or memory.embedding_model != embedder.name
        ]
        for memory in selected:
            session.add(
                Job(
                    organization_id=memory.organization_id,
                    job_type="enrich_memory",
                    payload={"memory_id": str(memory.id)},
                )
            )
        session.commit()
    typer.echo(f"queued {len(selected)} memories for enrichment with {embedder.name}")


if __name__ == "__main__":
    app()
