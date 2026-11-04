from __future__ import annotations

import json
import sys
from io import StringIO
from pathlib import Path

import httpx

from albert.capture_sdk import CaptureClient, EncryptedQueue, generate_queue_key
from albert.codex_adapter import CODEX_EVENTS, CodexAdapter, CodexState, main


def make_adapter(
    tmp_path: Path, handler: httpx.MockTransport
) -> tuple[CodexAdapter, EncryptedQueue]:
    queue = EncryptedQueue(tmp_path / "capture.db", generate_queue_key(), "codex:test")
    http = httpx.Client(base_url="https://albert.test", transport=handler)
    client = CaptureClient(
        "https://albert.test",
        "secret",
        queue,
        local_event_classes=CODEX_EVENTS,
        client=http,
    )
    return CodexAdapter(client, CodexState(tmp_path / "state.db")), queue


def envelope(hook: str, tmp_path: Path, **values: object) -> dict[str, object]:
    return {
        "hook_event_name": hook,
        "session_id": "session-1",
        "turn_id": "turn-1",
        "cwd": str(tmp_path),
        "model": "gpt-5.3-codex",
        "permission_mode": "dontAsk",
        "transcript_path": str(tmp_path / "must-not-be-read.jsonl"),
        **values,
    }


def test_codex_golden_lifecycle_is_bounded_cited_and_idempotent(tmp_path: Path):
    requests: list[tuple[str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append((request.url.path, body))
        if request.url.path == "/v1/context/session-start":
            return httpx.Response(
                200,
                json={
                    "context": "Prefer the shared capture contract.",
                    "citations": [{"source_uri": "memory://approved/codex-1"}],
                },
            )
        if request.url.path == "/v1/capture/sessions":
            return httpx.Response(201, json={"id": "server-session"})
        return httpx.Response(201, json={"id": "stored"})

    adapter, _ = make_adapter(tmp_path, httpx.MockTransport(handler))
    started = adapter.handle(envelope("SessionStart", tmp_path, source="startup"))
    adapter.handle(envelope("SessionStart", tmp_path, source="resume"))
    recalled = adapter.handle(
        envelope("UserPromptSubmit", tmp_path, prompt="Implement the Codex adapter")
    )
    compact = envelope("PreCompact", tmp_path, trigger="auto")
    adapter.handle(compact)
    adapter.handle(compact)
    stopped = envelope(
        "Stop",
        tmp_path,
        last_assistant_message="Codex adapter shipped with tests.",
        stop_hook_active=False,
    )
    adapter.handle(stopped)
    adapter.handle(stopped)

    for result in (started, recalled):
        context = result["hookSpecificOutput"]["additionalContext"]
        assert "NON-AUTHORITATIVE" in context
        assert "memory://approved/codex-1" in context

    event_requests = [body for path, body in requests if path.endswith("/events")]
    unique_events = {body["idempotency_key"]: body for body in event_requests}
    assert [item["event_class"] for item in unique_events.values()] == [
        "session_started",
        "user_goal",
        "pre_compaction_checkpoint",
        "assistant_outcome",
    ]
    assert [item["sequence"] for item in unique_events.values()] == [1, 2, 3, 4]
    assert all(item["body_kind"] == "summary" for item in unique_events.values())
    assert all("prompt" not in item and "transcript" not in item for item in unique_events.values())
    assert all(
        str(tmp_path) not in json.dumps(item["provenance"]) for item in unique_events.values()
    )

    checkpoints = [body for path, body in requests if path.endswith("/checkpoints")]
    assert len(checkpoints) == 4
    assert checkpoints[0]["idempotency_key"] == checkpoints[1]["idempotency_key"]
    assert checkpoints[2]["idempotency_key"] == checkpoints[3]["idempotency_key"]
    assert checkpoints[0]["idempotency_key"] != checkpoints[2]["idempotency_key"]
    assert not any(path.endswith("/end") for path, _ in requests)


def test_codex_outage_is_fail_open_and_secret_is_never_queued(tmp_path: Path):
    def offline(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    adapter, queue = make_adapter(tmp_path, httpx.MockTransport(offline))
    started = adapter.handle(envelope("SessionStart", tmp_path, source="startup"))
    secret = "password=supersecretvalue"
    denied = adapter.handle(
        envelope(
            "Stop",
            tmp_path,
            last_assistant_message=secret,
            stop_hook_active=False,
        )
    )

    assert "UNAVAILABLE" in started["hookSpecificOutput"]["additionalContext"]
    assert denied["results"][1] == {
        "status": "dropped",
        "reason": "secret_detected",
        "identifier": None,
    }
    assert all(secret not in str(queue.decrypt(row["payload"])) for row in queue.ready(100))
    assert secret.encode() not in (tmp_path / "capture.db").read_bytes()


def test_codex_fixture_declares_only_supported_hooks_and_reduced_fidelity():
    root = Path(__file__).parents[1]
    fixture = json.loads((root / "src/albert/codex_hooks/hooks.json").read_text())
    docs = (root / "docs/integrations/codex.md").read_text()
    hooks = fixture["hooks"]

    assert set(hooks) == {"SessionStart", "UserPromptSubmit", "PreCompact", "Stop"}
    assert all(
        group["hooks"][0]["command"] == "albert-codex-adapter"
        for groups in hooks.values()
        for group in groups
    )
    assert "0.154.0" in docs
    assert "does not expose a session-end or failure hook" in docs
    assert "transcript_path" in docs


def test_codex_command_hook_fails_open_when_credentials_are_unavailable(
    monkeypatch, capsys, tmp_path: Path
):
    event = envelope("SessionStart", tmp_path, source="startup")
    monkeypatch.setattr(sys, "stdin", StringIO(json.dumps(event)))
    monkeypatch.delenv("ALBERT_CAPTURE_TOKEN", raising=False)
    monkeypatch.delenv("ALBERT_CAPTURE_QUEUE_KEY", raising=False)

    main()

    assert capsys.readouterr().out == "{}\n"
