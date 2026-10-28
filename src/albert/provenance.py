from __future__ import annotations

import hashlib
import re
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID

from fastapi import HTTPException

from albert.schemas import CaptureProvenance


def _fingerprint_remote(value: str | None) -> str | None:
    if value is None:
        return None
    remote = value.strip()
    if not remote:
        return None
    scp = re.fullmatch(r"(?:[^@/]+@)?([^:/]+):(.+)", remote)
    if scp and "://" not in remote:
        remote = f"ssh://{scp.group(1)}/{scp.group(2)}"
    parsed = urlsplit(remote)
    if parsed.scheme and parsed.hostname:
        host = parsed.hostname.casefold()
        port = f":{parsed.port}" if parsed.port else ""
        remote = urlunsplit((parsed.scheme.casefold(), f"{host}{port}", parsed.path, "", ""))
    normalized = remote.rstrip("/")
    if normalized.endswith(".git"):
        normalized = normalized[:-4]
    return hashlib.sha256(normalized.encode()).hexdigest()


def normalize_provenance(
    value: CaptureProvenance | None,
    *,
    organization_id: UUID,
    allow_absolute_path: bool,
) -> dict[str, object]:
    if value is None:
        return {}
    if value.vcs == "none" and any(
        item is not None
        for item in (value.remote_url, value.branch, value.ref, value.head, value.dirty)
    ):
        raise HTTPException(status_code=422, detail="No-VCS provenance cannot include VCS state")
    if value.vcs is None and any(
        item is not None
        for item in (value.remote_url, value.branch, value.ref, value.head, value.dirty)
    ):
        raise HTTPException(status_code=422, detail="VCS type is required with VCS state")

    remote_fingerprint = _fingerprint_remote(value.remote_url)
    identity_source = remote_fingerprint or value.project
    project_id = None
    if identity_source:
        digest = hashlib.sha256(
            f"{organization_id}:{identity_source.casefold()}".encode()
        ).hexdigest()
        project_id = f"prj_{digest[:32]}"

    worktree = value.worktree_path
    if worktree and not (allow_absolute_path and value.include_absolute_path):
        worktree = re.split(r"[/\\]", worktree.rstrip("/\\"))[-1]

    return {
        key: item
        for key, item in {
            "client": value.client,
            "agent": value.agent,
            "project": value.project,
            "project_id": project_id,
            "remote_fingerprint": remote_fingerprint,
            "worktree": worktree,
            "vcs": value.vcs,
            "branch": value.branch,
            "ref": value.ref,
            "source_commit": value.head,
            "dirty": value.dirty,
            "task": value.task,
            "run": value.run,
            "event": value.event,
            "source_session": value.source_session,
            "adapter_version": value.adapter_version,
        }.items()
        if item is not None
    }


def merge_event_provenance(
    session_provenance: dict[str, object], event_provenance: dict[str, object]
) -> dict[str, object]:
    if not event_provenance:
        return dict(session_provenance)
    session_project = session_provenance.get("project_id")
    event_project = event_provenance.get("project_id")
    if session_project and event_project and session_project != event_project:
        raise HTTPException(status_code=409, detail="Event project differs from capture session")
    merged = {**session_provenance, **event_provenance}
    source_commit = session_provenance.get("source_commit")
    observed_commit = event_provenance.get("source_commit")
    if source_commit is not None:
        merged["source_commit"] = source_commit
        if observed_commit is not None and observed_commit != source_commit:
            merged["observed_commit"] = observed_commit
    return merged
