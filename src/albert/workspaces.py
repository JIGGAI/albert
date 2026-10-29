"""Deleting a workspace, and what happens to the memories it holds.

The caller picks one of three outcomes for the workspace's records:

- ``organization``: they become organization-wide, shared by every workspace.
- ``workspace``: they move into another workspace of the same organization.
- ``delete``: they are forgotten the way a single memory is (scrubbed, with the
  rows kept for audit identity), and the workspace's graph is removed.

Either way the workspace's credentials stop working: its API keys and connection
links are deleted and its principals deactivated. Principals are kept (detached
from the workspace) so audit events still name who did what. Traces and audit
events keep the old workspace id as history.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session, aliased

from albert.graph_builders import get_graph_builder
from albert.graph_store import get_graph_store
from albert.models import (
    APIKey,
    ConnectionLink,
    Episode,
    Memory,
    MemoryChunk,
    MemoryLink,
    Principal,
    ResourceLock,
    WorkingMemory,
    Workspace,
    utcnow,
)
from albert.security import AuthContext
from albert.services import _scrub_memory, audit, enqueue

MemoryOutcome = Literal["organization", "workspace", "delete"]


def _same_or_both_null(left, right):  # type: ignore[no-untyped-def]
    return (left == right) | (left.is_(None) & right.is_(None))


def _merge_duplicate_episodes(
    session: Session, organization_id: UUID, workspace_id: UUID, to: UUID | None, now: datetime
) -> None:
    """An identical episode from the same owner may already be in the target.

    Episodes are unique per scope, owner, content and source, so the moving copy
    hands its memories to the one already there and is retired.
    """
    there = aliased(Episode)
    duplicates = session.execute(
        select(Episode, there.id)
        .join(
            there,
            and_(
                there.organization_id == organization_id,
                there.workspace_id.is_(None) if to is None else there.workspace_id == to,
                there.owner_principal_id == Episode.owner_principal_id,
                there.content_hash == Episode.content_hash,
                _same_or_both_null(there.source_uri, Episode.source_uri),
            ),
        )
        .where(
            Episode.organization_id == organization_id,
            Episode.workspace_id == workspace_id,
            Episode.deleted_at.is_(None),
        )
    ).all()
    for episode, kept in duplicates:
        session.query(Memory).filter(Memory.episode_id == episode.id).update(
            {Memory.episode_id: kept}, synchronize_session=False
        )
        episode.content = ""
        episode.extra = {}
        episode.deleted_at = now
    session.flush()


def _move_records(
    session: Session, organization_id: UUID, workspace_id: UUID, to: UUID | None
) -> dict[str, int]:
    now = utcnow()
    _merge_duplicate_episodes(session, organization_id, workspace_id, to, now)
    moved: dict[str, int] = {}
    for name, model, live in (
        ("memories", Memory, Memory.status != "deleted"),
        ("episodes", Episode, Episode.deleted_at.is_(None)),
        ("chunks", MemoryChunk, None),
        ("links", MemoryLink, None),
        ("working_memory", WorkingMemory, None),
        ("locks", ResourceLock, None),
    ):
        query = session.query(model).filter(
            model.organization_id == organization_id, model.workspace_id == workspace_id
        )
        if live is not None:
            query = query.filter(live)
        moved[name] = query.update({model.workspace_id: to}, synchronize_session=False)
    moved["relationships"] = get_graph_store().move_workspace(
        session, organization_id=organization_id, workspace_id=workspace_id, to_workspace_id=to
    )
    return moved


def _delete_records(session: Session, organization_id: UUID, workspace_id: UUID) -> dict[str, int]:
    from albert.capture_retention import delete_capture_for_episode

    now = utcnow()
    memories = list(
        session.scalars(
            select(Memory).where(
                Memory.organization_id == organization_id,
                Memory.workspace_id == workspace_id,
                Memory.status != "deleted",
            )
        )
    )
    for memory in memories:
        _scrub_memory(session, memory, now)
    episodes = list(
        session.scalars(
            select(Episode).where(
                Episode.organization_id == organization_id,
                Episode.workspace_id == workspace_id,
                Episode.deleted_at.is_(None),
            )
        )
    )
    for episode in episodes:
        episode.content = ""
        episode.extra = {}
        episode.enrichment_error = None
        episode.deleted_at = now
        delete_capture_for_episode(session, episode.id, now)
    removed = {"memories": len(memories), "episodes": len(episodes)}
    for name, model in (("working_memory", WorkingMemory), ("locks", ResourceLock)):
        removed[name] = (
            session.query(model)
            .filter(model.organization_id == organization_id, model.workspace_id == workspace_id)
            .delete(synchronize_session=False)
        )
    removed["relationships"] = get_graph_store().delete_workspace(
        session, organization_id=organization_id, workspace_id=workspace_id
    )
    return removed


def _retire_credentials(session: Session, workspace_id: UUID) -> dict[str, int]:
    principals = list(
        session.scalars(select(Principal).where(Principal.workspace_id == workspace_id))
    )
    ids = [principal.id for principal in principals]
    keys = (
        session.query(APIKey).filter(APIKey.principal_id.in_(ids)).delete(synchronize_session=False)
        if ids
        else 0
    )
    links = (
        session.query(ConnectionLink)
        .filter(ConnectionLink.workspace_id == workspace_id)
        .delete(synchronize_session=False)
    )
    for principal in principals:
        principal.active = False
        principal.workspace_id = None
    return {"api_keys": keys, "connection_links": links, "principals": len(principals)}


def credential_counts(session: Session, organization_id: UUID) -> dict[UUID, int]:
    """Active API keys and unused connection links per workspace: what deleting revokes."""
    counts: dict[UUID, int] = {}
    keys = session.execute(
        select(Principal.workspace_id, func.count(APIKey.id))
        .join(APIKey, APIKey.principal_id == Principal.id)
        .where(
            Principal.organization_id == organization_id,
            Principal.workspace_id.is_not(None),
            APIKey.active.is_(True),
        )
        .group_by(Principal.workspace_id)
    ).all()
    links = session.execute(
        select(ConnectionLink.workspace_id, func.count(ConnectionLink.id))
        .where(
            ConnectionLink.organization_id == organization_id,
            ConnectionLink.workspace_id.is_not(None),
            ConnectionLink.used_at.is_(None),
            ConnectionLink.revoked_at.is_(None),
        )
        .group_by(ConnectionLink.workspace_id)
    ).all()
    for workspace_id, count in [*keys, *links]:
        counts[workspace_id] = counts.get(workspace_id, 0) + count
    return counts


def delete_workspace(
    session: Session,
    auth: AuthContext,
    workspace: Workspace,
    memories: MemoryOutcome,
    to: Workspace | None,
) -> dict:
    """Delete `workspace`, first sharing, moving or forgetting what it holds."""
    if memories == "delete":
        auth.require("memory.delete")
    if memories == "workspace":
        if to is None:
            raise HTTPException(status_code=422, detail="Choose the workspace to move memories to")
        if to.id == workspace.id:
            raise HTTPException(
                status_code=422, detail="Memories cannot move into the workspace being deleted"
            )
    elif to is not None:
        raise HTTPException(
            status_code=422, detail="A target workspace is only used with 'workspace'"
        )
    organization_id = workspace.organization_id
    if memories == "delete":
        records = _delete_records(session, organization_id, workspace.id)
    else:
        records = _move_records(session, organization_id, workspace.id, to.id if to else None)
    credentials = _retire_credentials(session, workspace.id)
    if getattr(get_graph_builder(), "keeps_own_store", False):
        # Graphiti keeps its own graph per workspace. Albert's copy of the facts
        # was moved or removed above; Graphiti's, under a scope that no longer
        # exists, is dropped (moved memories lose only its temporal history).
        enqueue(session, organization_id, "graph_forget_scope", {"workspace_id": str(workspace.id)})
    result = {
        "id": str(workspace.id),
        "name": workspace.name,
        "memories": memories,
        "to_workspace_id": str(to.id) if to else None,
        "records": records,
        "credentials": credentials,
    }
    audit(
        session,
        auth,
        "workspace.delete",
        resource_type="workspace",
        resource_id=str(workspace.id),
        detail={k: v for k, v in result.items() if k != "id"},
    )
    session.delete(workspace)
    session.commit()
    return result
