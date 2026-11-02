from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
import pytest

from albert.capture_sdk import CaptureClient, EncryptedQueue, generate_queue_key
from albert.openclaw_adapter import (
    OPENCLAW_EVENTS,
    OpenClawAdapter,
    OpenClawState,
    _client_from_environment,
)


def make_adapter(
    tmp_path: Path, handler: httpx.MockTransport
) -> tuple[OpenClawAdapter, EncryptedQueue]:
    queue = EncryptedQueue(tmp_path / "capture.db", generate_queue_key(), "openclaw:test")
    http = httpx.Client(base_url="https://albert.test", transport=handler)
    client = CaptureClient(
        "https://albert.test",
        "secret",
        queue,
        local_event_classes=OPENCLAW_EVENTS,
        client=http,
    )
    return OpenClawAdapter(client, OpenClawState(tmp_path / "state.db")), queue


def envelope(hook: str, **values: object) -> dict[str, object]:
    return {
        "hook": hook,
        "session_id": "physical-1",
        "session_key": "agent:main",
        "agent_id": "main",
        "run_id": "run-1",
        "project_ref": "jiggai/albert",
        **values,
    }


def test_private_credential_file_avoids_secret_process_environment(tmp_path: Path, monkeypatch):
    credential_file = tmp_path / "credentials.json"
    credential_file.write_text(
        json.dumps(
            {
                "ALBERT_API_URL": "https://brain.example",
                "ALBERT_CAPTURE_TOKEN": "file-only-token",
                "ALBERT_CAPTURE_QUEUE_KEY": generate_queue_key(),
                "ALBERT_CAPTURE_QUEUE_PATH": str(tmp_path / "queue.db"),
                "ALBERT_WORKSPACE_ID": "workspace-1",
            }
        ),
        encoding="utf-8",
    )
    os.chmod(credential_file, 0o600)
    monkeypatch.delenv("ALBERT_CAPTURE_TOKEN", raising=False)
    monkeypatch.setenv("ALBERT_CAPTURE_CREDENTIAL_FILE", str(credential_file))

    client, _, workspace_id = _client_from_environment()
    try:
        assert client.client.headers["authorization"] == "Bearer file-only-token"
        assert workspace_id == "workspace-1"
        assert "ALBERT_CAPTURE_TOKEN" not in os.environ
    finally:
        client.close()

    os.chmod(credential_file, 0o644)
    with pytest.raises(RuntimeError, match="permissions are unsafe"):
        _client_from_environment()


def test_openclaw_golden_lifecycle_is_bounded_cited_and_idempotent(tmp_path: Path):
    requests: list[tuple[str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append((request.url.path, body))
        if request.url.path == "/v1/context/session-start":
            return httpx.Response(
                200,
                json={
                    "context": "Prefer the shared contract.",
                    "citations": [{"source_uri": "memory://approved/1"}],
                },
            )
        if request.url.path == "/v1/capture/sessions":
            return httpx.Response(201, json={"id": "server-session"})
        return httpx.Response(201, json={"id": "stored"})

    adapter, _ = make_adapter(tmp_path, httpx.MockTransport(handler))
    adapter.handle(envelope("session_start"))
    adapter.handle(envelope("session_start"))
    recalled = adapter.handle(
        envelope(
            "before_prompt_build",
            current_user_message="Implement the OpenClaw adapter",
            message_id="message-1",
        )
    )
    compaction = envelope(
        "before_compaction",
        compaction_id="compact-1",
        message_count=12,
        token_count=345,
    )
    adapter.handle(compaction)
    adapter.handle(compaction)
    adapter.handle(envelope("agent_end", success=True, outcome="Adapter shipped with tests."))
    adapter.handle(envelope("session_end", reason="idle"))

    assert "NON-AUTHORITATIVE" in recalled["context"]
    assert "memory://approved/1" in recalled["context"]
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
    assert all("messages" not in item and "prompt" not in item for item in unique_events.values())
    checkpoint_keys = [
        body["idempotency_key"] for path, body in requests if path.endswith("/checkpoints")
    ]
    assert len(checkpoint_keys) == 3
    assert checkpoint_keys[0] == checkpoint_keys[1]
    assert checkpoint_keys[2] != checkpoint_keys[0]
    end = next(body for path, body in requests if path.endswith("/end"))
    assert end["final_sequence"] == 5

    adapter.handle({**envelope("session_start"), "session_id": "physical-2"})
    assert [body for path, body in requests if path.endswith("/events")][-1]["sequence"] == 1


def test_openclaw_outage_is_fail_open_and_secret_is_never_queued(tmp_path: Path):
    def offline(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    adapter, queue = make_adapter(tmp_path, httpx.MockTransport(offline))
    started = adapter.handle(envelope("session_start"))
    secret = "password=supersecretvalue"
    failed = adapter.handle(envelope("agent_end", success=False, outcome=secret))

    assert {item["status"] for item in started["results"]} == {"queued"}
    assert failed["results"][-1] == {
        "status": "dropped",
        "reason": "secret_detected",
        "identifier": None,
    }
    assert all(secret not in str(queue.decrypt(row["payload"])) for row in queue.ready(100))
    assert secret.encode() not in (tmp_path / "capture.db").read_bytes()


def test_native_plugin_fixture_declares_real_openclaw_compatibility():
    root = Path(__file__).parents[1] / "src/albert/openclaw_plugin"
    package = json.loads((root / "package.json").read_text())
    manifest = json.loads((root / "openclaw.plugin.json").read_text())
    source = (root / "index.js").read_text()

    assert package["openclaw"]["compat"]["pluginApi"] == ">=2026.9.6 <2027"
    assert manifest["id"] == "albert-memory"
    for hook in (
        "session_start",
        "before_prompt_build",
        "before_compaction",
        "agent_end",
        "session_end",
    ):
        assert f'api.on("{hook}"' in source
    assert "messages:" not in source
