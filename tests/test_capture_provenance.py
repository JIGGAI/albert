from __future__ import annotations

from uuid import UUID

import httpx
import pytest
from sqlalchemy import select

from albert.db import SessionLocal
from albert.models import CaptureEvent, CaptureSession, Episode

from .conftest import create_identity, drain_jobs

pytestmark = pytest.mark.anyio


def _provenance(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "client": "openclaw",
        "agent": "dev",
        "project": "Albert",
        "remote_url": "https://token@example.com/JIGGAI/albert.git?credential=hidden",
        "worktree_path": "/home/person/worktrees/albert-ticket",
        "vcs": "git",
        "branch": "feat/current",
        "ref": "refs/heads/feat/current",
        "head": "a" * 40,
        "dirty": False,
        "task": "0432",
        "run": "run-1",
        "source_session": "source-1",
        "adapter_version": "1.2.3",
    }
    value.update(overrides)
    return value


async def _start(
    client: httpx.AsyncClient, key: str, provenance: dict[str, object]
) -> httpx.Response:
    return await client.post(
        "/v1/capture/sessions",
        json={
            "client_session_id": key,
            "idempotency_key": key,
            "provenance": provenance,
        },
    )


async def test_provenance_is_normalized_minimized_and_commit_is_immutable(
    client: httpx.AsyncClient,
) -> None:
    started = await _start(client, "provenance-session", _provenance())
    assert started.status_code == 201, started.text
    provenance = started.json()["provenance"]
    assert provenance["project_id"].startswith("prj_")
    assert len(provenance["remote_fingerprint"]) == 64
    assert provenance["worktree"] == "albert-ticket"
    assert provenance["source_commit"] == "a" * 40
    assert "/home/person" not in started.text
    assert "token" not in started.text

    event = await client.post(
        f"/v1/capture/sessions/{started.json()['id']}/events",
        json={
            "sequence": 1,
            "idempotency_key": "provenance-event",
            "event_class": "decision",
            "content": "Preserve the source commit after a rebase.",
            "provenance": _provenance(
                head="b" * 40,
                branch="feat/rebased",
                ref="refs/heads/feat/rebased",
                event="event-1",
            ),
        },
    )
    assert event.status_code == 201, event.text
    event_provenance = event.json()["provenance"]
    assert event_provenance["source_commit"] == "a" * 40
    assert event_provenance["observed_commit"] == "b" * 40
    assert event_provenance["branch"] == "feat/rebased"

    with SessionLocal() as session:
        capture_session = session.get(CaptureSession, UUID(started.json()["id"]))
        capture_event = session.scalar(
            select(CaptureEvent).where(CaptureEvent.session_id == capture_session.id)
        )
        episode = session.get(Episode, capture_event.episode_id)
        assert capture_session.source_commit == "a" * 40
        assert capture_event.source_commit == "a" * 40
        assert episode.project_ref == provenance["project_id"]
        assert episode.task_ref == "0432"
        assert "/home/person" not in str(episode.extra)


async def test_vcs_edges_and_project_identity_are_honest_and_tenant_safe(
    client: httpx.AsyncClient,
) -> None:
    detached = await _start(
        client,
        "detached",
        _provenance(branch=None, ref=None, head="c" * 40, dirty=True),
    )
    assert detached.status_code == 201
    assert "branch" not in detached.json()["provenance"]
    assert detached.json()["provenance"]["dirty"] is True

    no_vcs = await _start(
        client,
        "no-vcs",
        _provenance(vcs="none", remote_url=None, branch=None, ref=None, head=None, dirty=None),
    )
    assert no_vcs.status_code == 201
    assert no_vcs.json()["provenance"]["vcs"] == "none"
    assert "source_commit" not in no_vcs.json()["provenance"]

    dishonest = await _start(
        client,
        "dishonest-no-vcs",
        _provenance(vcs="none", remote_url=None, branch=None, ref=None, head="d" * 40),
    )
    assert dishonest.status_code == 422

    second_worktree = await _start(
        client,
        "second-worktree",
        _provenance(worktree_path=r"C:\Users\person\worktrees\albert-copy"),
    )
    assert second_worktree.status_code == 201
    assert second_worktree.json()["provenance"]["worktree"] == "albert-copy"
    assert "Users" not in second_worktree.text
    assert (
        second_worktree.json()["provenance"]["project_id"]
        == detached.json()["provenance"]["project_id"]
    )

    foreign_key, _organization, _workspace = create_identity("Foreign provenance")
    async with httpx.AsyncClient(
        transport=client._transport,  # type: ignore[attr-defined]
        base_url="http://test",
        headers={"Authorization": f"Bearer {foreign_key}"},
    ) as foreign:
        foreign_session = await _start(foreign, "foreign", _provenance())
    assert foreign_session.status_code == 201
    assert (
        foreign_session.json()["provenance"]["project_id"]
        != detached.json()["provenance"]["project_id"]
    )


async def test_branch_recall_is_preferred_with_project_wide_fallback(
    client: httpx.AsyncClient,
) -> None:
    sessions: dict[str, dict[str, object]] = {}
    for branch, marker in (("feat/other", "other"), ("feat/preferred", "preferred")):
        started = await _start(
            client,
            f"branch-{marker}",
            _provenance(branch=branch, ref=f"refs/heads/{branch}", head=marker * 8),
        )
        assert started.status_code == 201
        sessions[branch] = started.json()
        written = await client.post(
            f"/v1/capture/sessions/{started.json()['id']}/events",
            json={
                "sequence": 1,
                "idempotency_key": f"event-{marker}",
                "event_class": "assistant_outcome",
                "content": "Branch preference sentinel knowledge",
            },
        )
        assert written.status_code == 201
    drain_jobs()

    project_id = sessions["feat/preferred"]["provenance"]["project_id"]  # type: ignore[index]
    search = await client.post(
        "/v1/search",
        json={
            "query": "Branch preference sentinel knowledge",
            "project_ref": project_id,
            "branch_ref": "feat/preferred",
            "limit": 10,
            "include_graph": False,
        },
    )
    assert search.status_code == 200, search.text
    hits = search.json()["hits"]
    assert len(hits) == 2
    assert hits[0]["metadata"]["provenance"]["branch"] == "feat/preferred"
    assert {hit["metadata"]["provenance"]["branch"] for hit in hits} == {
        "feat/preferred",
        "feat/other",
    }
