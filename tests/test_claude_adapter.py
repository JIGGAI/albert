from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from uuid import uuid4

import httpx
from fastapi.testclient import TestClient

from albert.api import app
from albert.capture_sdk import CaptureClient, EncryptedQueue, generate_queue_key
from albert.claude_adapter import CLAUDE_EVENTS, ClaudeAdapter, ClaudeState, main

from .conftest import create_identity, drain_jobs


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
    for event, groups in hooks.items():
        # Recall hooks get time for a search; capture-only hooks stay short.
        timeout = 5 if event in {"SessionStart", "UserPromptSubmit"} else 2
        assert groups == [
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": "albert-claude-adapter",
                        "timeout": timeout,
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


def test_claude_capture_is_recalled_in_a_fresh_session_of_the_same_project_only(tmp_path: Path):
    """Recall must resolve the same project identity the captured memories were stored under.

    Regression: the hook recalled with the folder name as project_ref while capture stored a
    server-derived ``prj_...`` identity, so hook recall always came back empty.
    """
    token, _organization, _workspace = create_identity("Claude project identity")
    http = TestClient(app, headers={"Authorization": f"Bearer {token}"})
    profile_key = f"claude-project-identity-{uuid4().hex}"
    created = http.post(
        "/v1/capture/profiles",
        json={
            "profile_key": profile_key,
            "version": 1,
            "allowed_event_classes": sorted(CLAUDE_EVENTS),
            "metadata_allowlists": {},
            "event_bounds": {},
            "sensitivity_floor": "internal",
            "source_trust": "verified",
            "retention_days": 14,
            "raw_body_policy": "deny",
            "deny_reasons": {},
        },
    )
    assert created.status_code == 201, created.text
    alpha, beta = tmp_path / "alpha-project", tmp_path / "beta-project"
    alpha.mkdir()
    beta.mkdir()

    queue_key = generate_queue_key()

    def adapter(name: str) -> ClaudeAdapter:
        """A fresh client per hook call (each hook is a new process) over the install's
        one on-disk queue and state, as in a real Claude Code install."""
        queue = EncryptedQueue(tmp_path / "capture.db", queue_key, "claude-code")
        client = CaptureClient(
            "http://testserver",
            token,
            queue,
            profile_key=profile_key,
            local_event_classes=CLAUDE_EVENTS,
            client=http,  # type: ignore[arg-type]
        )
        return ClaudeAdapter(client, ClaudeState(tmp_path / "state.db"))

    marker = "Project identity regression marker: CEDAR LANTERN 0471"
    adapter("first-start").handle(envelope("SessionStart", alpha, session_id="first"))
    adapter("first-stop").handle(
        envelope("Stop", alpha, session_id="first", prompt_id="p1", last_assistant_message=marker)
    )
    drain_jobs()

    def recall(folder: Path, session: str) -> str:
        adapter(f"{session}-start").handle(envelope("SessionStart", folder, session_id=session))
        result = adapter(f"{session}-prompt").handle(
            envelope(
                "UserPromptSubmit",
                folder,
                session_id=session,
                prompt_id=f"{session}-p",
                prompt="Recall the CEDAR LANTERN project marker.",
            )
        )
        return result["hookSpecificOutput"]["additionalContext"]

    same_project = recall(alpha, "fresh")
    assert "CEDAR LANTERN 0471" in same_project
    assert "--- CITATIONS ---\n- [1]" in same_project
    assert "CEDAR LANTERN 0471" not in recall(beta, "other")
