from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import httpx

from albert.capture_sdk import CaptureClient, EncryptedQueue, generate_queue_key
from albert.claude_adapter import CLAUDE_EVENTS, ClaudeAdapter, ClaudeState, main


def make_adapter(
    tmp_path: Path, handler: httpx.MockTransport
) -> tuple[ClaudeAdapter, EncryptedQueue]:
    queue = EncryptedQueue(tmp_path / "capture.db", generate_queue_key(), "claude-code:test")
    http = httpx.Client(base_url="https://albert.test", transport=handler)
    client = CaptureClient(
        "https://albert.test",
        "secret",
        queue,
        local_event_classes=CLAUDE_EVENTS,
        client=http,
    )
    return ClaudeAdapter(client, ClaudeState(tmp_path / "state.db")), queue


def envelope(hook: str, tmp_path: Path, **values: object) -> dict[str, object]:
    return {
        "hook_event_name": hook,
        "session_id": "claude-session-1",
        "prompt_id": "prompt-1",
        "cwd": str(tmp_path),
        "transcript_path": str(tmp_path / "never-read.jsonl"),
        **values,
    }


def test_claude_golden_lifecycle_is_bounded_cited_and_idempotent(tmp_path: Path):
    requests: list[tuple[str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append((request.url.path, body))
        if request.url.path == "/v1/context/session-start":
            return httpx.Response(
                200,
                json={
                    "context": "Prefer the shared contract.",
                    "citations": [{"source_uri": "memory://approved/claude-1"}],
                },
            )
        if request.url.path == "/v1/capture/sessions":
            return httpx.Response(201, json={"id": "server-session"})
        return httpx.Response(201, json={"id": "stored"})

    adapter, _ = make_adapter(tmp_path, httpx.MockTransport(handler))
    started = envelope("SessionStart", tmp_path, source="startup")
    first = adapter.handle(started)
    adapter.handle(started)
    prompted = adapter.handle(
        envelope("UserPromptSubmit", tmp_path, prompt="Implement the adapter")
    )
    compact = envelope("PreCompact", tmp_path, trigger="auto")
    adapter.handle(compact)
    adapter.handle(compact)
    adapter.handle(envelope("Stop", tmp_path, last_assistant_message="Adapter shipped with tests."))
    adapter.handle(envelope("SessionEnd", tmp_path, reason="other"))

    context = first["hookSpecificOutput"]["additionalContext"]
    assert "NON-AUTHORITATIVE" in context
    assert "memory://approved/claude-1" in context
    assert prompted["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    assert "memory://approved/claude-1" in prompted["hookSpecificOutput"]["additionalContext"]
    recall_queries = [
        body["query"] for path, body in requests if path == "/v1/context/session-start"
    ]
    assert recall_queries[-1] == "Implement the adapter"
    assert {
        body["trust_policy"] for path, body in requests if path == "/v1/context/session-start"
    } == {"label_untrusted"}
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
    serialized = json.dumps(requests)
    assert str(tmp_path) not in serialized
    assert "transcript_path" not in serialized
    checkpoint_keys = [
        body["idempotency_key"] for path, body in requests if path.endswith("/checkpoints")
    ]
    assert checkpoint_keys[0] == checkpoint_keys[1]
    assert checkpoint_keys[2] != checkpoint_keys[0]
    end = next(body for path, body in requests if path.endswith("/end"))
    assert end["final_sequence"] == 5


def test_claude_failure_is_minimized_and_secret_is_never_queued(tmp_path: Path):
    def offline(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    adapter, queue = make_adapter(tmp_path, httpx.MockTransport(offline))
    started = adapter.handle(envelope("SessionStart", tmp_path, source="resume"))
    secret = "password=supersecretvalue"
    dropped = adapter.handle(
        envelope("Stop", tmp_path, last_assistant_message=secret, prompt_id="prompt-secret")
    )
    failed = adapter.handle(
        envelope(
            "StopFailure",
            tmp_path,
            error="rate_limit",
            error_details=secret,
            prompt_id="prompt-failure",
        )
    )

    assert {item["status"] for item in started["results"]} == {"queued"}
    assert dropped["results"][-1] == {
        "status": "dropped",
        "reason": "secret_detected",
        "identifier": None,
    }
    assert failed["results"][-1]["status"] == "queued"
    payloads = [queue.decrypt(row["payload"]) for row in queue.ready(100)]
    assert all(secret not in str(payload) for payload in payloads)
    assert secret.encode() not in (tmp_path / "capture.db").read_bytes()


def test_claude_secret_prompt_is_neither_recorded_nor_used_for_recall(tmp_path: Path):
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(201, json={"id": "stored"})

    adapter, _queue = make_adapter(tmp_path, httpx.MockTransport(handler))
    secret = "password=supersecretvalue"
    response = adapter.handle(envelope("UserPromptSubmit", tmp_path, prompt=secret))

    assert "hookSpecificOutput" not in response
    assert "/v1/context/session-start" not in paths
    assert not any(path.endswith("/events") for path in paths)
    assert secret.encode() not in (tmp_path / "capture.db").read_bytes()


def test_claude_plugin_bundles_albert_mcp_server_without_credentials():
    root = Path(__file__).parents[1] / "src/albert/claude_plugin"
    servers = json.loads((root / ".mcp.json").read_text())["mcpServers"]

    assert set(servers) == {"albert"}
    albert = servers["albert"]
    assert albert["command"] == "albert-mcp"
    assert albert["args"] == ["--transport", "stdio"]
    assert albert["env"]["ALBERT_MCP_API_KEY"] == "${ALBERT_MCP_API_KEY}"
    assert albert["env"]["ALBERT_API_URL"].startswith("${ALBERT_API_URL")


def test_claude_plugin_fixture_declares_only_supported_minimized_hooks():
    root = Path(__file__).parents[1] / "src/albert/claude_plugin"
    manifest = json.loads((root / ".claude-plugin/plugin.json").read_text())
    hooks = json.loads((root / "hooks/hooks.json").read_text())["hooks"]

    assert manifest["name"] == "albert-memory"
    assert set(hooks) == {
        "SessionStart",
        "UserPromptSubmit",
        "PreCompact",
        "Stop",
        "StopFailure",
        "SessionEnd",
    }
    for groups in hooks.values():
        assert groups == [
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": "albert-claude-adapter",
                        "timeout": 2,
                    }
                ]
            }
        ]


def test_claude_cli_missing_configuration_is_fail_open(monkeypatch, capsys):
    monkeypatch.delenv("ALBERT_CAPTURE_TOKEN", raising=False)
    monkeypatch.delenv("ALBERT_CAPTURE_QUEUE_KEY", raising=False)
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO('{"hook_event_name":"SessionStart","session_id":"s"}'),
    )

    main()

    output = capsys.readouterr()
    assert output.out == "{}\n"
    assert "RuntimeError" in output.err
    assert "ALBERT_CAPTURE" not in output.err
