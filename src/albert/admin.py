from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from typing import Any, NoReturn
from uuid import UUID

import typer
from sqlalchemy import Engine, inspect, or_, select, text

from albert import __version__
from albert.db import SessionLocal, engine, initialize_database
from albert.embeddings import get_embedder
from albert.models import APIKey, Job, Memory, Organization, Principal, Workspace
from albert.security import ALL_CAPABILITIES, KNOWN_CAPABILITIES, issue_api_key

app = typer.Typer(help="Albert administrative commands", no_args_is_help=True)

# Version of the machine-readable `--json` output. Changes within a version are
# additive only; see contracts/admin-v1.schema.json.
ADMIN_CONTRACT_VERSION = "1"
JSON_COMMANDS = frozenset(
    {
        "chatgpt-login-poll",
        "chatgpt-login-start",
        "chatgpt-logout",
        "chatgpt-status",
        "contract",
        "create-key",
        "create-principal",
        "create-workspace",
        "list-keys",
        "list-principals",
        "revoke-key",
    }
)
JSON_OPTION = typer.Option(
    False, "--json", help="Print one machine-readable JSON object (admin contract)"
)


def _emit(payload: dict[str, Any]) -> None:
    typer.echo(
        json.dumps({"admin_contract": ADMIN_CONTRACT_VERSION, **payload}, separators=(",", ":"))
    )


def _fail(json_mode: bool, code: str, message: str) -> NoReturn:
    """Exit with code 2: a JSON error on stderr, or typer's usual BadParameter."""
    if not json_mode:
        raise typer.BadParameter(message)
    error = {"admin_contract": ADMIN_CONTRACT_VERSION, "error": {"code": code, "message": message}}
    typer.echo(json.dumps(error, separators=(",", ":")), err=True)
    raise typer.Exit(code=2)


def _parse_id(value: str, json_mode: bool) -> UUID:
    try:
        return UUID(value)
    except ValueError:
        if not json_mode:
            raise  # Text mode keeps its historical behavior: an uncaught ValueError.
        _fail(json_mode, "invalid_id", f"Invalid id: {value!r}")


def _timestamp(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:  # SQLite drops the offset; stored values are UTC.
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def _workspace_json(workspace: Workspace) -> dict[str, Any]:
    return {
        "id": str(workspace.id),
        "organization_id": str(workspace.organization_id),
        "name": workspace.name,
    }


def _principal_json(principal: Principal) -> dict[str, Any]:
    return {
        "id": str(principal.id),
        "organization_id": str(principal.organization_id),
        "name": principal.name,
        "principal_type": principal.principal_type,
        "workspace_id": str(principal.workspace_id) if principal.workspace_id else None,
        "active": principal.active,
    }


def _key_json(key: APIKey) -> dict[str, Any]:
    # Never includes key_hash or any credential material.
    return {
        "id": str(key.id),
        "principal_id": str(key.principal_id),
        "name": key.name,
        "prefix": key.prefix,
        "capabilities": list(key.capabilities),
        "active": key.active,
        "expires_at": _timestamp(key.expires_at),
        "created_at": _timestamp(key.created_at),
    }


@app.command("compat")
def compat(
    json_output: bool = typer.Option(
        True, "--json", help="Print JSON (the only format; accepted for symmetry)"
    ),
) -> None:
    """Print this image's runtime compatibility manifest as compact JSON.

    Needs no environment and no database. Consumers decide rollback with: a
    Brain migrated by image T can run image O iff every revision in
    T.migrations.heads is in O.migrations.known_revisions.
    """
    from albert.compatibility import (
        MigrationsUnavailable,
        compatibility_error,
        compatibility_manifest,
    )

    try:
        manifest = compatibility_manifest()
    except MigrationsUnavailable as exc:
        error = compatibility_error("migrations_unavailable", str(exc))
        sys.stderr.write(json.dumps(error, separators=(",", ":")) + "\n")
        raise typer.Exit(code=2) from exc
    sys.stdout.write(json.dumps(manifest, separators=(",", ":")) + "\n")


class SchemaNotReady(RuntimeError):
    pass


def ensure_schema_ready(engine: Engine) -> None:
    """Refuse to run against a database Alembic has not built.

    Creating tables here would leave no alembic_version row and make the next
    `alembic upgrade head` fail on tables that already exist.
    """
    if not inspect(engine).has_table("memories"):
        raise SchemaNotReady(
            "Database schema is missing; run `alembic upgrade head` before this command"
        )


@app.command("init-db")
def init_db() -> None:
    """Create tables directly for throwaway development databases only.

    Deployed databases must use `alembic upgrade head`; this command records no
    migration version.
    """
    initialize_database()
    typer.echo("Database initialized (development only; no Alembic version recorded)")


@app.command()
def bootstrap(
    organization: str = typer.Option(..., help="Initial organization name"),
    workspace: str = typer.Option(..., help="Initial workspace name"),
    principal: str = typer.Option("Administrator", help="Administrator display name"),
) -> None:
    """Create the first tenant and print its administrator key exactly once."""
    try:
        ensure_schema_ready(engine)
    except SchemaNotReady as exc:
        raise typer.BadParameter(str(exc)) from exc
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
    json_mode: bool = JSON_OPTION,
) -> None:
    """Issue an additional API key for an existing principal."""
    requested_capabilities = [item.strip() for item in capabilities.split(",") if item.strip()]
    unknown = sorted(set(requested_capabilities) - KNOWN_CAPABILITIES)
    if unknown:
        _fail(json_mode, "unknown_capabilities", f"Unknown capabilities: {', '.join(unknown)}")
    with SessionLocal() as session:
        actor = session.get(Principal, _parse_id(principal_id, json_mode))
        if actor is None:
            _fail(json_mode, "principal_not_found", "Principal not found")
        raw, prefix, digest = issue_api_key()
        key = APIKey(
            principal_id=actor.id,
            prefix=prefix,
            key_hash=digest,
            name=name,
            capabilities=requested_capabilities,
        )
        session.add(key)
        session.commit()
        if json_mode:
            session.refresh(key)
            _emit({"key": _key_json(key), "credential": raw})
            return
    typer.echo(raw)


@app.command("create-workspace")
def create_workspace(
    organization: str = typer.Option(..., help="Organization name"),
    name: str = typer.Option(..., help="Workspace name"),
    json_mode: bool = JSON_OPTION,
) -> None:
    """Create a workspace in an existing organization."""
    with SessionLocal() as session:
        org = session.scalar(select(Organization).where(Organization.name == organization))
        if org is None:
            _fail(json_mode, "organization_not_found", "Organization not found")
        existing = session.scalar(
            select(Workspace).where(
                Workspace.organization_id == org.id,
                Workspace.name == name,
            )
        )
        if existing is not None:
            if json_mode:
                _emit({"workspace": _workspace_json(existing), "created": False})
            else:
                typer.echo(str(existing.id))
            return
        workspace = Workspace(organization_id=org.id, name=name)
        session.add(workspace)
        session.commit()
        session.refresh(workspace)
        if json_mode:
            _emit({"workspace": _workspace_json(workspace), "created": True})
        else:
            typer.echo(str(workspace.id))


@app.command("create-principal")
def create_principal(
    organization: str = typer.Option(..., help="Organization name"),
    name: str = typer.Option(..., help="Principal display name"),
    workspace: str | None = typer.Option(None, help="Optional workspace name"),
    principal_type: str = typer.Option("agent", help="agent, human, or service"),
    json_mode: bool = JSON_OPTION,
) -> None:
    """Create an organization- or workspace-scoped principal."""
    if principal_type not in {"agent", "human", "service"}:
        _fail(
            json_mode,
            "invalid_principal_type",
            "principal-type must be agent, human, or service",
        )
    with SessionLocal() as session:
        org = session.scalar(select(Organization).where(Organization.name == organization))
        if org is None:
            _fail(json_mode, "organization_not_found", "Organization not found")
        workspace_id = None
        if workspace is not None:
            record = session.scalar(
                select(Workspace).where(
                    Workspace.organization_id == org.id,
                    Workspace.name == workspace,
                )
            )
            if record is None:
                _fail(json_mode, "workspace_not_found", "Workspace not found")
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
        if json_mode:
            _emit({"principal": _principal_json(principal)})
        else:
            typer.echo(str(principal.id))


@app.command("list-principals")
def list_principals(
    organization: str = typer.Option(..., help="Organization name"),
    json_mode: bool = JSON_OPTION,
) -> None:
    """List principals and their workspace scope."""
    with SessionLocal() as session:
        org = session.scalar(select(Organization).where(Organization.name == organization))
        if org is None:
            _fail(json_mode, "organization_not_found", "Organization not found")
        principals = session.scalars(
            select(Principal).where(Principal.organization_id == org.id).order_by(Principal.name)
        )
        if json_mode:
            _emit(
                {
                    "organization": {"id": str(org.id), "name": org.name},
                    "principals": [_principal_json(principal) for principal in principals],
                }
            )
            return
        for principal in principals:
            typer.echo(
                f"{principal.id}\t{principal.name}\t{principal.principal_type}\t"
                f"workspace={principal.workspace_id or '*'}\tactive={principal.active}"
            )


@app.command("list-keys")
def list_keys(
    principal_id: str = typer.Option(...),
    json_mode: bool = JSON_OPTION,
) -> None:
    """List non-secret API-key metadata for one principal."""
    with SessionLocal() as session:
        actor = session.get(Principal, _parse_id(principal_id, json_mode))
        if actor is None:
            _fail(json_mode, "principal_not_found", "Principal not found")
        keys = session.scalars(
            select(APIKey).where(APIKey.principal_id == actor.id).order_by(APIKey.created_at)
        )
        if json_mode:
            _emit({"principal_id": str(actor.id), "keys": [_key_json(key) for key in keys]})
            return
        for key in keys:
            typer.echo(
                f"{key.id}\t{key.name}\tprefix={key.prefix}\tactive={key.active}\t"
                f"expires={key.expires_at or '-'}"
            )


@app.command("revoke-key")
def revoke_key(
    key_id: str = typer.Option(...),
    json_mode: bool = JSON_OPTION,
) -> None:
    """Revoke an API key without deleting its audit identity."""
    with SessionLocal() as session:
        key = session.get(APIKey, _parse_id(key_id, json_mode))
        if key is None:
            _fail(json_mode, "api_key_not_found", "API key not found")
        key.active = False
        session.commit()
        if json_mode:
            _emit({"key": {"id": str(key.id), "active": key.active}})
            return
    typer.echo("API key revoked")


@app.command("contract")
def contract(json_mode: bool = JSON_OPTION) -> None:
    """Print the machine-readable admin contract version (always JSON)."""
    _emit(
        {
            "package": "albert-memory",
            "package_version": __version__,
            "json_commands": sorted(JSON_COMMANDS),
        }
    )


@app.command("validate-runtime")
def validate_runtime(
    probe_embedding: bool = typer.Option(
        False, "--probe-embedding", help="Load and execute the configured embedding provider"
    ),
) -> None:
    """Validate configured providers and optionally execute one embedding."""
    from albert.config import get_settings
    from albert.graph_builders import get_graph_builder
    from albert.graph_store import get_graph_store

    settings = get_settings()
    typer.echo(
        f"embedding={settings.embedding_provider}:{settings.embedding_model} "
        f"dimensions={settings.embedding_dimensions} classifier={settings.llm_provider}"
    )
    with SessionLocal() as session:
        store_version = get_graph_store().ping(session)
        typer.echo(f"graph store probe succeeded ({store_version})")
        if session.bind is not None and session.bind.dialect.name == "postgresql":
            vector_type = session.scalar(
                text(
                    "SELECT format_type(attribute.atttypid, attribute.atttypmod) "
                    "FROM pg_attribute AS attribute "
                    "JOIN pg_class AS relation ON relation.oid = attribute.attrelid "
                    "WHERE relation.relname = 'memory_chunks' "
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
                    "WHERE indexname = 'ix_memory_chunks_embedding_hnsw')"
                )
            )
            if not hnsw_exists:
                typer.echo("database HNSW embedding index is missing", err=True)
                raise typer.Exit(code=1)
            typer.echo("database vector schema and HNSW index are valid")
    if probe_embedding:
        try:
            embedder = get_embedder()
            vector = embedder.embed("Albert runtime readiness probe")
        except Exception as exc:
            typer.echo(f"embedding probe failed ({type(exc).__name__})", err=True)
            raise typer.Exit(code=1) from exc
        if len(vector) != settings.embedding_dimensions:
            raise typer.Exit(code=1)
        typer.echo(f"embedding probe succeeded ({len(vector)} dimensions)")
    if settings.graph_builder == "graphiti":
        try:
            builder_version = get_graph_builder().readiness()
        except Exception as exc:
            typer.echo(f"graph builder probe failed ({type(exc).__name__})", err=True)
            raise typer.Exit(code=1) from exc
        typer.echo(f"graph builder probe succeeded (graphiti-core {builder_version})")


@app.command("reindex-memories")
def reindex_memories(
    all_memories: bool = typer.Option(
        False, "--all", help="Re-enrich every active memory, including current embeddings"
    ),
    workspace: str | None = typer.Option(
        None, help="Limit to workspaces with this name (in every organization)"
    ),
) -> None:
    """Queue active memories whose chunks are absent or embedded by another model."""
    embedder = get_embedder()
    with SessionLocal() as session:
        statement = select(Memory).where(Memory.status == "active")
        if not all_memories:
            statement = statement.where(
                or_(Memory.embedding_model.is_(None), Memory.embedding_model != embedder.name)
            )
        if workspace is not None:
            workspace_ids = list(
                session.scalars(select(Workspace.id).where(Workspace.name == workspace))
            )
            if not workspace_ids:
                raise typer.BadParameter("Workspace not found")
            statement = statement.where(Memory.workspace_id.in_(workspace_ids))
        selected = list(session.scalars(statement))
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


@app.command("rebuild-links")
def rebuild_links_command(
    organization: str | None = typer.Option(None, help="Limit to one organization by name"),
) -> None:
    """Recompute the memory map's similarity and sequence links from stored chunks."""
    from albert.links import rebuild_links

    with SessionLocal() as session:
        organization_id = None
        if organization is not None:
            org = session.scalar(select(Organization).where(Organization.name == organization))
            if org is None:
                raise typer.BadParameter("Organization not found")
            organization_id = org.id
        result = rebuild_links(session, organization_id)
        session.commit()
    typer.echo(
        f"memories={result['memories']} similar={result['similar']} sequence={result['sequence']}"
    )


# --- ChatGPT sign-in (ALBERT_LLM_PROVIDER=chatgpt-oauth) ----------------------
#
# Device-code sign-in in two steps, so a control plane can show the code and poll
# without a long-running process. The credential lands in the Brain's own
# ALBERT_CHATGPT_CREDENTIALS_FILE and is never printed: refresh tokens rotate, so
# the Brain must be its only holder.


def _chatgpt_file(json_mode: bool) -> Any:
    from albert.config import get_settings

    settings = get_settings()
    if settings.llm_provider != "chatgpt-oauth" or settings.chatgpt_credentials_file is None:
        _fail(
            json_mode,
            "chatgpt_not_configured",
            "This Brain does not use ALBERT_LLM_PROVIDER=chatgpt-oauth",
        )
    return settings.chatgpt_credentials_file


def _chatgpt_state(state: str, account_id: str | None = None) -> None:
    _emit({"chatgpt": {"state": state, "account_id": account_id}})


@app.command("chatgpt-login-start")
def chatgpt_login_start(json_mode: bool = JSON_OPTION) -> None:
    """Start a ChatGPT device sign-in; show the user code at the verification URL."""
    import httpx

    from albert.chatgpt_oauth import ChatGPTAuthError, start_device_login

    _chatgpt_file(json_mode)
    try:
        with httpx.Client() as client:
            login = start_device_login(client)
    except (ChatGPTAuthError, httpx.HTTPError) as exc:
        _fail(
            json_mode,
            "chatgpt_auth_failed",
            str(exc)
            if isinstance(exc, ChatGPTAuthError)
            else f"OpenAI is unreachable ({type(exc).__name__})",
        )
    if not json_mode:
        typer.echo(f"Open {login.verification_url} and enter {login.user_code}")
        return
    _emit(
        {
            "login": {
                "device_auth_id": login.device_auth_id,
                "user_code": login.user_code,
                "verification_url": login.verification_url,
                "interval_seconds": login.interval_seconds,
            }
        }
    )


@app.command("chatgpt-login-poll")
def chatgpt_login_poll(
    device_auth_id: str = typer.Option(..., help="From chatgpt-login-start"),
    user_code: str = typer.Option(..., help="From chatgpt-login-start"),
    json_mode: bool = JSON_OPTION,
) -> None:
    """Check once whether the sign-in was approved; store the credential if so."""
    import httpx

    from albert.chatgpt_oauth import ChatGPTAuthError, poll_device_login, write_credential

    path = _chatgpt_file(json_mode)
    try:
        with httpx.Client() as client:
            credential = poll_device_login(client, device_auth_id, user_code)
    except (ChatGPTAuthError, httpx.HTTPError) as exc:
        _fail(
            json_mode,
            "chatgpt_auth_failed",
            str(exc)
            if isinstance(exc, ChatGPTAuthError)
            else f"OpenAI is unreachable ({type(exc).__name__})",
        )
    if credential is None:
        if json_mode:
            _chatgpt_state("pending")
        else:
            typer.echo("waiting for approval")
        return
    write_credential(path, credential)
    if json_mode:
        _chatgpt_state("connected", credential.account_id)
    else:
        typer.echo(f"connected ChatGPT account {credential.account_id}")


@app.command("chatgpt-status")
def chatgpt_status(json_mode: bool = JSON_OPTION) -> None:
    """Report whether a ChatGPT account is connected (never prints tokens)."""
    from albert.chatgpt_oauth import ChatGPTAuthError, read_credential

    path = _chatgpt_file(json_mode)
    try:
        account: str | None = read_credential(path).account_id
    except ChatGPTAuthError:
        account = None
    state = "connected" if account else "disconnected"
    if json_mode:
        _chatgpt_state(state, account)
    else:
        typer.echo(state if account is None else f"{state} ({account})")


@app.command("chatgpt-logout")
def chatgpt_logout(json_mode: bool = JSON_OPTION) -> None:
    """Remove this Brain's ChatGPT credential."""
    path = _chatgpt_file(json_mode)
    path.unlink(missing_ok=True)
    if json_mode:
        _chatgpt_state("disconnected")
    else:
        typer.echo("disconnected")


if __name__ == "__main__":
    app()
