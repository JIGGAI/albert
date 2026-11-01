from __future__ import annotations

import json
import sys
from io import StringIO
from pathlib import Path

import httpx

from albert.capture_sdk import CaptureClient, EncryptedQueue, generate_queue_key
from albert.cursor_adapter import CURSOR_EVENTS, CursorAdapter, CursorState, main


def make_adapter(
    tmp_path: Path, handler: httpx.MockTransport
) -> tuple[CursorAdapter, EncryptedQueue]:
    queue = EncryptedQueue(tmp_path / "capture.db", generate_queue_key(), "cursor:test")
    http = httpx.Client(base_url="https://albert.test", transport=handler)
    client = CaptureClient(
        "https://albert.test",
        "secret",
        queue,
        local_event_classes=CURSOR_EVENTS,
        client=http,
    )
    return CursorAdapter(client, CursorState(tmp_path / "state.db")), queue


def envelope(hook: str, tmp_path: Path, **values: object) -> dict[str, object]:
    return {
        "hook_event_name": hook,
        "conversation_id": "conversation-1",
        "session_id": "conversation-1",
        "generation_id": "generation-1",
        "cursor_version": "3.27.22",
        "model": "test-model",
        "workspace_roots": [str(tmp_path)],
        "transcript_path": str(tmp_path / "must-not-be-read.jsonl"),
        **values,
    }


def test_cursor_golden_lifecycle_is_bounded_cited_and_idempotent(tmp_path: Path):
    requests: list[tuple[str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append((request.url.path, body))
        if request.url.path == "/v1/context/session-start":
            return httpx.Response(
                200,
                json={
                    "context": "Prefer the shared capture contract.",
                    "citations": [{"source_uri": "memory://approved/cursor-1"}],
                },
            )
        if request.url.path == "/v1/capture/sessions":
            return httpx.Response(201, json={"id": "server-session"})
        return httpx.Response(201, json={"id": "stored"})

    adapter, _ = make_adapter(tmp_path, httpx.MockTransport(handler))
    started = adapter.handle(envelope("sessionStart", tmp_path, composer_mode="agent"))
    adapter.handle(envelope("sessionStart", tmp_path, composer_mode="agent"))
    adapter.handle(envelope("beforeSubmitPrompt", tmp_path, prompt="Implement the Cursor adapter"))
    compact = envelope(
        "preCompact",
        tmp_path,
        trigger="auto",
        context_tokens=120_000,
        message_count=45,
        is_first_compaction=True,
    )
    adapter.handle(compact)
    adapter.handle(compact)
    response = envelope("afterAgentResponse", tmp_path, text="Cursor adapter shipped with tests.")
    adapter.handle(response)
    adapter.handle(response)
    stopped = envelope("stop", tmp_path, status="completed", loop_count=0)
    adapter.handle(stopped)
    adapter.handle(stopped)
    adapter.handle(envelope("sessionEnd", tmp_path, reason="completed"))

    assert "NON-AUTHORITATIVE" in started["additional_context"]
    assert "memory://approved/cursor-1" in started["additional_context"]
    event_requests = [body for path, body in requests if path.endswith("/events")]
    unique_events = {body["idempotency_key"]: body for body in event_requests}
    assert [item["event_class"] for item in unique_events.values()] == [
        "session_started",
        "user_goal",
        "pre_compaction_checkpoint",
        "assistant_outcome",
        "session_ended",
    ]
    assert [item["sequence"] for item in unique_events.values()] == [1, 2, 3, 4, 5]
    assert all(item["body_kind"] == "summary" for item in unique_events.values())
    assert all("transcript" not in item and "attachments" not in item for item in event_requests)
    assert all(str(tmp_path) not in json.dumps(item["provenance"]) for item in event_requests)

    checkpoints = [body for path, body in requests if path.endswith("/checkpoints")]
    assert len(checkpoints) == 5
    assert checkpoints[0]["idempotency_key"] == checkpoints[1]["idempotency_key"]
    assert checkpoints[2]["idempotency_key"] == checkpoints[3]["idempotency_key"]
    assert checkpoints[4]["idempotency_key"] not in {
        checkpoints[0]["idempotency_key"],
        checkpoints[2]["idempotency_key"],
    }
    end = next(body for path, body in requests if path.endswith("/end"))
    assert end["final_sequence"] == 5


def test_cursor_outage_is_fail_open_and_secret_is_never_queued(tmp_path: Path):
    def offline(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    adapter, queue = make_adapter(tmp_path, httpx.MockTransport(offline))
    started = adapter.handle(envelope("sessionStart", tmp_path, composer_mode="agent"))
    secret = "password=supersecretvalue"
    denied = adapter.handle(envelope("afterAgentResponse", tmp_path, text=secret))

    assert "UNAVAILABLE" in started["additional_context"]
    assert denied["results"][-1] == {
        "status": "dropped",
        "reason": "secret_detected",
        "identifier": None,
    }
    assert all(secret not in str(queue.decrypt(row["payload"])) for row in queue.ready(100))
    assert secret.encode() not in (tmp_path / "capture.db").read_bytes()


def test_cursor_error_status_is_observed_without_fabricated_error_details(tmp_path: Path):
    requests: list[tuple[str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append((request.url.path, body))
        if request.url.path == "/v1/context/session-start":
            return httpx.Response(200, json={"context": "", "citations": []})
        if request.url.path == "/v1/capture/sessions":
            return httpx.Response(201, json={"id": "server-session"})
        return httpx.Response(201, json={"id": "stored"})

    adapter, _ = make_adapter(tmp_path, httpx.MockTransport(handler))
    adapter.handle(envelope("stop", tmp_path, status="error", loop_count=0))

    failure = next(
        body
        for path, body in requests
        if path.endswith("/events") and body["event_class"] == "failure_summary"
    )
    assert failure["content"] == "Cursor reported that the agent loop stopped with error status."
    assert "error_message" not in failure


def test_cursor_fixture_declares_supported_hooks_rules_and_reduced_fidelity():
    root = Path(__file__).parents[1]
    fixture = json.loads((root / "src/albert/cursor_hooks/hooks.json").read_text())
    docs = (root / "docs/integrations/cursor.md").read_text()
    rule = (root / "src/albert/cursor_rules/albert-memory.mdc").read_text()

    assert fixture["version"] == 1
    assert set(fixture["hooks"]) == {
        "sessionStart",
        "beforeSubmitPrompt",
        "preCompact",
        "afterAgentResponse",
        "stop",
        "sessionEnd",
    }
    assert all(
        item["command"] == "albert-cursor-adapter"
        for items in fixture["hooks"].values()
        for item in items
    )
    assert "3.27.22" in docs
    assert "2026.10.22-27dcf5f" in docs
    assert "Cloud agents do not expose `sessionStart` or `sessionEnd`" in docs
    assert "transcript_path" in docs
    assert "non-authoritative" in rule


def test_cursor_command_hook_fails_open_when_credentials_are_unavailable(
    monkeypatch, capsys, tmp_path: Path
):
    event = envelope("sessionStart", tmp_path, composer_mode="agent")
    monkeypatch.setattr(sys, "stdin", StringIO(json.dumps(event)))
    monkeypatch.delenv("ALBERT_CAPTURE_TOKEN", raising=False)
    monkeypatch.delenv("ALBERT_CAPTURE_QUEUE_KEY", raising=False)

    main()

    assert capsys.readouterr().out == "{}\n"
