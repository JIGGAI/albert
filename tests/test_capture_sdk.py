from __future__ import annotations

import sqlite3
from pathlib import Path

import httpx
import pytest

from albert.capture_sdk import (
    CaptureClient,
    EncryptedQueue,
    collect_vcs_context,
    generate_queue_key,
)


def make_queue(tmp_path: Path, **kwargs) -> EncryptedQueue:  # type: ignore[no-untyped-def]
    return EncryptedQueue(
        tmp_path / "capture.db", generate_queue_key(), "tenant:principal", **kwargs
    )


def test_queue_encrypts_scopes_bounds_and_survives_restart(tmp_path: Path):
    key = generate_queue_key()
    path = tmp_path / "capture.db"
    queue = EncryptedQueue(path, key, "org:user", max_items=1)
    assert queue.put("one", "event", "session", {"content": "private-marker"})
    assert b"private-marker" not in path.read_bytes()
    assert not queue.put("two", "event", "session", {"content": "other"})
    assert queue.dropped == 1
    restarted = EncryptedQueue(path, key, "org:user", max_items=1)
    row = restarted.ready(10)[0]
    assert restarted.decrypt(row["payload"])["content"] == "private-marker"
    with pytest.raises(ValueError, match="another capture scope"):
        EncryptedQueue(path, key, "other:user")


def test_queue_expires_retries_and_detects_corruption(tmp_path: Path):
    now = [100.0]
    queue = make_queue(tmp_path, max_age_seconds=10, max_retries=2, clock=lambda: now[0])
    queue.put("one", "event", "session", {"content": "safe"})
    row = queue.ready(1)[0]
    queue.retry(row["id"], 1)
    now[0] += 3
    row = queue.ready(1)[0]
    with sqlite3.connect(tmp_path / "capture.db") as db:
        db.execute("UPDATE queue SET payload=? WHERE id=?", (b"broken", row["id"]))
    with pytest.raises(ValueError, match="corrupt"):
        queue.decrypt(queue.ready(1)[0]["payload"])
    now[0] += 11
    assert queue.ready(1) == []


def test_outage_queues_then_flushes_lifecycle_once(tmp_path: Path):
    online = [False]
    requests: list[tuple[str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if not online[0]:
            raise httpx.ConnectError("offline", request=request)
        body = __import__("json").loads(request.content)
        requests.append((request.url.path, body))
        identifier = "session-1" if request.url.path.endswith("/sessions") else "event-1"
        return httpx.Response(201, json={"id": identifier})

    http = httpx.Client(base_url="https://albert.test", transport=httpx.MockTransport(handler))
    queue = make_queue(tmp_path)
    sdk = CaptureClient("https://albert.test", "secret", queue, client=http)
    assert sdk.start_session("client-1", "start-1", provenance={"vcs": "none"}).status == "queued"
    assert sdk.record_event("start-1", 1, "decision", "keep it", "event-1").status == "queued"
    assert sdk.checkpoint("start-1", 1, "checkpoint-1").status == "queued"
    assert sdk.end_session("start-1", 1, "end-1").status == "queued"
    assert sdk.health().queued == 4
    online[0] = True
    assert sdk.flush() == 4
    assert sdk.flush() == 0
    assert [path for path, _ in requests] == [
        "/v1/capture/sessions",
        "/v1/capture/sessions/session-1/events",
        "/v1/capture/sessions/session-1/checkpoints",
        "/v1/capture/sessions/session-1/end",
    ]
    assert sdk.health().queued == 0


def test_policy_denied_content_is_never_queued_and_metadata_is_minimized(tmp_path: Path):
    captured: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = __import__("json").loads(request.content)
        captured.append(body)
        return httpx.Response(201, json={"id": "event-1"})

    queue = make_queue(tmp_path)
    queue.map_session("session", "server-session")
    sdk = CaptureClient(
        "https://albert.test",
        "secret",
        queue,
        local_event_classes={"decision"},
        local_metadata_allowlists={"decision": {"safe"}},
        client=httpx.Client(base_url="https://albert.test", transport=httpx.MockTransport(handler)),
    )
    sdk._profile = sdk._profile.__class__(  # type: ignore[union-attr]
        "safe-default",
        1,
        frozenset({"decision"}),
        {"decision": frozenset({"safe", "server_only"})},
        {"decision": {"max_content_chars": 4}},
        float("inf"),
    )
    denied = sdk.record_event("session", 1, "user_goal", "never retain", "denied")
    assert denied.status == "dropped" and denied.reason == "policy_denied"
    assert queue.stats()[0] == 0
    delivered = sdk.record_event(
        "session",
        1,
        "decision",
        "abcdef",
        "allowed",
        metadata={"safe": "yes", "server_only": "no", "secret": "no"},
    )
    assert delivered.status == "delivered"
    assert captured[0]["content"] == "abcd"
    assert captured[0]["metadata"] == {"safe": "yes"}


def test_compatibility_profile_cache_and_bounded_recall_rendering(tmp_path: Path):
    calls = {"profile": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/openapi.json":
            return httpx.Response(
                200,
                json={
                    "paths": {
                        "/v1/capture/sessions": {},
                        "/v1/capture/sessions/{session_id}/events": {},
                        "/v1/context/session-start": {},
                    }
                },
            )
        if request.url.path.endswith("/profiles/narrow"):
            calls["profile"] += 1
            return httpx.Response(
                200,
                json={
                    "profile_key": "narrow",
                    "version": 2,
                    "allowed_event_classes": ["decision"],
                    "metadata_allowlists": {"decision": ["safe"]},
                    "event_bounds": {"decision": {"max_content_chars": 20}},
                },
            )
        return httpx.Response(
            200,
            json={
                "context": "remember this",
                "citations": [{"source_uri": "memory://1"}],
            },
        )

    sdk = CaptureClient(
        "https://albert.test",
        "secret",
        make_queue(tmp_path),
        profile_key="narrow",
        local_event_classes={"decision"},
        client=httpx.Client(base_url="https://albert.test", transport=httpx.MockTransport(handler)),
    )
    assert sdk.negotiate()
    assert sdk.refresh_profile() and sdk.refresh_profile()
    rendered = sdk.recall("task", max_characters=100)
    assert "NON-AUTHORITATIVE" in rendered and "memory://1" in rendered
    assert calls["profile"] == 2


def test_vcs_context_never_contains_absolute_path(tmp_path: Path):
    context = collect_vcs_context(tmp_path)
    assert context == {"vcs": "none", "project": tmp_path.name}
    assert str(tmp_path) not in str(context)


def test_a_delivered_session_start_is_reused_by_later_hook_processes(tmp_path: Path):
    """Each hook is a new process that calls start first. Re-sending the start with
    hook-specific provenance would be refused as a changed replay (409), so once the
    install has delivered it, later processes reuse the recorded server session."""
    starts: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/capture/sessions":
            starts.append(__import__("json").loads(request.content))
            return httpx.Response(201, json={"id": "server-session"})
        return httpx.Response(201, json={"id": "event"})

    queue = make_queue(tmp_path)

    def process() -> CaptureClient:
        http = httpx.Client(base_url="https://albert.test", transport=httpx.MockTransport(handler))
        return CaptureClient("https://albert.test", "secret", queue, client=http)

    first = process().start_session("client-1", "start-1", provenance={"event": "SessionStart"})
    later = process().start_session("client-1", "start-1", provenance={"event": "Stop"})

    assert first.status == later.status == "delivered"
    assert first.identifier == later.identifier == "server-session"
    assert len(starts) == 1


def test_recall_uses_its_own_timeout(tmp_path: Path):
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["timeout"] = request.extensions.get("timeout")
        return httpx.Response(200, json={"context": "ctx", "citations": []})

    http = httpx.Client(
        base_url="https://albert.test", transport=httpx.MockTransport(handler), timeout=1.0
    )
    sdk = CaptureClient(
        "https://albert.test", "secret", make_queue(tmp_path), client=http, recall_timeout=4.0
    )

    assert "ctx" in sdk.recall("task")
    assert seen["timeout"] == {"connect": 4.0, "read": 4.0, "write": 4.0, "pool": 4.0}
