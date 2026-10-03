from __future__ import annotations

import typer
from sqlalchemy import select

from albert.db import SessionLocal, initialize_database
from albert.models import APIKey, Organization, Principal, Workspace
from albert.security import issue_api_key

app = typer.Typer(help="Albert administrative commands", no_args_is_help=True)

ALL_CAPABILITIES = [
    "memory.read",
    "memory.write",
    "memory.delete",
    "memory.export",
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
    from uuid import UUID

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
                capabilities=[item.strip() for item in capabilities.split(",") if item.strip()],
            )
        )
        session.commit()
    typer.echo(raw)


if __name__ == "__main__":
    app()

