"""ChatGPT sign-in as Albert's LLM. Every request goes to a stubbed transport."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import stat
import time
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from pydantic import BaseModel, ValidationError
from typer.testing import CliRunner

import albert.chatgpt_oauth as chatgpt
import albert.worker as worker
from albert import graph_builders
from albert.admin import app as admin_app
from albert.chatgpt_oauth import (
    ChatGPTAuthError,
    ChatGPTClient,
    ChatGPTRequestError,
    Credential,
    CredentialStore,
    account_id_from_token,
    poll_device_login,
    provider_wait_seconds,
    read_credential,
    start_device_login,
    write_credential,
)
from albert.config import Settings
from albert.db import SessionLocal
from albert.graph_builders import GraphitiGraphBuilder
from albert.models import Job


def _token(account: str = "acct-1", exp: float | None = None) -> str:
    def part(value: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")

    claims = {
        "exp": exp or time.time() + 3600,
        "https://api.openai.com/auth": {"chatgpt_account_id": account},
    }
    return f"{part({'alg': 'none'})}.{part(claims)}.signature"


def _sse(*events: dict) -> bytes:
    return b"".join(f"data: {json.dumps(event)}\n\n".encode() for event in events)


def _completed(text: str) -> dict:
    return {
        "type": "response.completed",
        "response": {
            "output": [
                {"type": "reasoning", "summary": []},
                {"type": "message", "content": [{"type": "output_text", "text": text}]},
            ]
        },
    }


def _credential(path: Path, *, expires_in: float = 3600, access: str | None = None) -> Credential:
    credential = Credential(
        access_token=access or _token(),
        refresh_token="refresh-1",
        expires_at=time.time() + expires_in,
        account_id="acct-1",
    )
    write_credential(path, credential)
    return credential


# --- device sign-in -----------------------------------------------------------


def test_device_login_start_poll_and_exchange() -> None:
    seen: list[httpx.Request] = []
    approved = {"value": False}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/api/accounts/deviceauth/usercode":
            assert json.loads(request.content) == {"client_id": chatgpt.CLIENT_ID}
            return httpx.Response(
                200, json={"device_auth_id": "dev-1", "user_code": "AB-12", "interval": "3"}
            )
        if request.url.path == "/api/accounts/deviceauth/token":
            assert json.loads(request.content) == {"device_auth_id": "dev-1", "user_code": "AB-12"}
            if not approved["value"]:
                return httpx.Response(403, json={"error": "authorization_pending"})
            return httpx.Response(
                200, json={"authorization_code": "code-1", "code_verifier": "verifier-1"}
            )
        if request.url.path == "/oauth/token":
            form = dict(httpx.QueryParams(request.content.decode()))
            assert form == {
                "grant_type": "authorization_code",
                "code": "code-1",
                "redirect_uri": chatgpt.DEVICE_CALLBACK_URL,
                "client_id": chatgpt.CLIENT_ID,
                "code_verifier": "verifier-1",
            }
            return httpx.Response(
                200,
                json={
                    "access_token": _token("acct-9"),
                    "refresh_token": "refresh-9",
                    "expires_in": 600,
                },
            )
        raise AssertionError(request.url)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        login = start_device_login(client)
        assert (login.device_auth_id, login.user_code, login.interval_seconds) == (
            "dev-1",
            "AB-12",
            3.0,
        )
        assert login.verification_url == "https://auth.openai.com/codex/device"
        assert poll_device_login(client, "dev-1", "AB-12") is None
        approved["value"] = True
        credential = poll_device_login(client, "dev-1", "AB-12")
    assert credential is not None
    assert credential.account_id == "acct-9"
    assert credential.refresh_token == "refresh-9"
    assert 500 < credential.expires_at - time.time() <= 600
    assert all(request.url.host == "auth.openai.com" for request in seen)


def test_device_login_failure_names_status_not_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"code": "invalid_request"}, "secret": "x"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ChatGPTAuthError, match=r"HTTP 400, invalid_request\)$"):
            start_device_login(client)


# --- credential file ------------------------------------------------------------


def test_credential_file_is_private_and_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "secrets" / "chatgpt.json"
    credential = _credential(path)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert read_credential(path) == credential
    assert account_id_from_token(_token("acct-7")) == "acct-7"
    assert account_id_from_token("not-a-jwt") is None


def test_missing_or_broken_credential_is_an_auth_error(tmp_path: Path) -> None:
    with pytest.raises(ChatGPTAuthError, match="No ChatGPT account"):
        read_credential(tmp_path / "absent.json")
    broken = tmp_path / "broken.json"
    broken.write_text('{"access_token": "a"}')
    with pytest.raises(ChatGPTAuthError, match="incomplete"):
        read_credential(broken)


def _refresh_transport(calls: list[dict]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        form = dict(httpx.QueryParams(request.content.decode()))
        calls.append(form)
        return httpx.Response(
            200,
            json={
                "access_token": _token(),
                "refresh_token": f"refresh-{len(calls) + 1}",
                "expires_in": 3600,
            },
        )

    return httpx.MockTransport(handler)


def test_store_refreshes_near_expiry_and_persists_the_rotated_token(tmp_path: Path) -> None:
    path = tmp_path / "chatgpt.json"
    _credential(path, expires_in=60)
    calls: list[dict] = []
    with httpx.Client(transport=_refresh_transport(calls)) as client:
        store = CredentialStore(path, client)
        refreshed = store.current()
        assert calls == [
            {
                "grant_type": "refresh_token",
                "refresh_token": "refresh-1",
                "client_id": chatgpt.CLIENT_ID,
            }
        ]
        assert refreshed.refresh_token == "refresh-2"
        assert read_credential(path).refresh_token == "refresh-2"
        # Fresh now: no second refresh.
        assert store.current() == refreshed
    assert len(calls) == 1


def test_store_does_not_replay_a_refresh_another_process_already_did(tmp_path: Path) -> None:
    path = tmp_path / "chatgpt.json"
    rejected = _token("acct-1", exp=time.time() + 3600)
    _credential(path, access=_token("acct-1", exp=time.time() + 7200))
    calls: list[dict] = []
    with httpx.Client(transport=_refresh_transport(calls)) as client:
        # The token this process saw rejected is no longer the stored one.
        CredentialStore(path, client).current(force_refresh_of=rejected)
    assert calls == []


def test_refresh_rejection_asks_for_a_new_sign_in(tmp_path: Path) -> None:
    path = tmp_path / "chatgpt.json"
    _credential(path, expires_in=0)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"code": "refresh_token_reused"}})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ChatGPTAuthError, match=r"refresh_token_reused.*sign in"):
            CredentialStore(path, client).current()


# --- structured completions ---------------------------------------------------------


def test_complete_json_sends_the_codex_request_shape(tmp_path: Path) -> None:
    path = tmp_path / "chatgpt.json"
    credential = _credential(path)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_sse(
                {"type": "response.created"},
                {"type": "response.output_text.delta", "delta": '{"a"'},
                _completed('```json\n{"entities": ["Alice"]}\n```'),
            ),
        )

    schema = {"type": "object", "properties": {"entities": {"type": "array"}}}
    client = ChatGPTClient(
        path,
        "gpt-test",
        reasoning_effort="low",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    reply = client.complete_json(
        "Extract.", ["Alice works at Acme."], schema, schema_name="Entities"
    )
    assert reply == {"entities": ["Alice"]}
    (request,) = seen
    assert str(request.url) == "https://chatgpt.com/backend-api/codex/responses"
    assert request.headers["authorization"] == f"Bearer {credential.access_token}"
    assert request.headers["chatgpt-account-id"] == "acct-1"
    assert request.headers["originator"] == "albert"
    assert request.headers["accept"] == "text/event-stream"
    body = json.loads(request.content)
    assert body == {
        "model": "gpt-test",
        "store": False,
        "stream": True,
        "instructions": "Extract.",
        "input": [
            {"role": "user", "content": [{"type": "input_text", "text": "Alice works at Acme."}]}
        ],
        "text": {
            "verbosity": "low",
            "format": {
                "type": "json_schema",
                "name": "Entities",
                "schema": schema,
                "strict": False,
            },
        },
        "reasoning": {"effort": "low"},
    }


def test_complete_json_falls_back_to_streamed_deltas(tmp_path: Path) -> None:
    path = tmp_path / "chatgpt.json"
    _credential(path)

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["text"]["format"] == {"type": "json_object"}
        # The backend refuses json_object output unless the input names JSON.
        assert [item["content"][0]["text"] for item in body["input"]] == [
            "x",
            "Respond with a JSON object.",
        ]
        return httpx.Response(
            200,
            content=_sse(
                {"type": "response.output_text.delta", "delta": '{"ok": '},
                {"type": "response.output_text.delta", "delta": "true}"},
            )
            + b"data: [DONE]\n\n",
        )

    client = ChatGPTClient(
        path, "gpt-test", client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    assert client.complete_json("", ["x"]) == {"ok": True}


def test_stream_error_reports_the_backend_message(tmp_path: Path) -> None:
    path = tmp_path / "chatgpt.json"
    _credential(path)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=_sse(
                {
                    "type": "error",
                    "error": {"message": "model is not supported", "code": None},
                }
            ),
        )

    client = ChatGPTClient(
        path, "gpt-test", client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    with pytest.raises(ChatGPTRequestError, match="model is not supported"):
        client.complete_json("Reply in JSON.", ["json please"])


def test_rejected_access_token_is_refreshed_once_and_retried(tmp_path: Path) -> None:
    path = tmp_path / "chatgpt.json"
    first = _credential(path)
    used: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "auth.openai.com":
            return httpx.Response(
                200,
                json={
                    "access_token": _token("acct-1"),
                    "refresh_token": "refresh-2",
                    "expires_in": 3600,
                },
            )
        used.append(request.headers["authorization"])
        if len(used) == 1:
            return httpx.Response(401, json={"error": {"code": "token_expired"}})
        return httpx.Response(200, content=_sse(_completed('{"ok": 1}')))

    client = ChatGPTClient(
        path, "gpt-test", client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    assert client.complete_json("i", ["x"]) == {"ok": 1}
    assert used[0] == f"Bearer {first.access_token}"
    assert used[1] != used[0]
    assert read_credential(path).refresh_token == "refresh-2"


@pytest.mark.parametrize(
    ("response", "status", "code"),
    [
        (
            httpx.Response(429, json={"error": {"code": "usage_limit_reached"}}),
            429,
            "usage_limit_reached",
        ),
        (
            httpx.Response(
                200,
                content=_sse(
                    {"type": "response.failed", "response": {"error": {"code": "server_error"}}}
                ),
            ),
            None,
            "server_error",
        ),
        (httpx.Response(200, content=_sse({"type": "response.created"})), None, None),
    ],
)
def test_backend_failures_raise_request_errors(tmp_path: Path, response, status, code) -> None:
    path = tmp_path / "chatgpt.json"
    _credential(path)
    client = ChatGPTClient(
        path, "gpt-test", client=httpx.Client(transport=httpx.MockTransport(lambda _r: response))
    )
    with pytest.raises(ChatGPTRequestError) as raised:
        client.complete_json("i", ["x"])
    assert (raised.value.status, raised.value.code) == (status, code)


def test_non_json_reply_is_a_request_error(tmp_path: Path) -> None:
    path = tmp_path / "chatgpt.json"
    _credential(path)
    client = ChatGPTClient(
        path,
        "gpt-test",
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda _r: httpx.Response(200, content=_sse(_completed("hello")))
            )
        ),
    )
    with pytest.raises(ChatGPTRequestError, match="not JSON"):
        client.complete_json("i", ["x"])


def test_provider_wait_covers_sign_in_and_usage_limits_only() -> None:
    assert provider_wait_seconds(ChatGPTAuthError("gone")) == 300.0
    assert provider_wait_seconds(ChatGPTRequestError("limit", status=429)) == 300.0
    try:
        try:
            raise ChatGPTRequestError("x", code="usage_limit_reached")
        except ChatGPTRequestError as inner:
            raise RuntimeError("graphiti wrapped it") from inner
    except RuntimeError as wrapped:
        assert provider_wait_seconds(wrapped) == 300.0
    assert provider_wait_seconds(ChatGPTRequestError("bad", status=500)) is None
    assert provider_wait_seconds(ValueError("other")) is None


# --- configuration, classifier, Graphiti, worker, admin --------------------------------


def _oauth_settings(tmp_path: Path, **overrides) -> Settings:
    values = {
        "llm_provider": "chatgpt-oauth",
        "chatgpt_credentials_file": str(tmp_path / "chatgpt.json"),
        "classification_model": "gpt-test",
        **overrides,
    }
    return Settings(**values)


def test_settings_accept_graphiti_on_chatgpt_with_local_embeddings(tmp_path: Path) -> None:
    settings = _oauth_settings(
        tmp_path,
        graph_builder="graphiti",
        graph_store="falkordb",
        embedding_provider="sentence-transformers",
    )
    assert settings.openai_api_key is None
    assert settings.chatgpt_credentials_file == tmp_path / "chatgpt.json"
    assert _oauth_settings(tmp_path).graph_builder == "llm-classifier"


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"chatgpt_credentials_file": ""}, "ALBERT_CHATGPT_CREDENTIALS_FILE is required"),
        ({"classification_model": ""}, "ALBERT_CLASSIFICATION_MODEL is required"),
    ],
)
def test_settings_require_credential_file_and_model(tmp_path: Path, overrides, message) -> None:
    with pytest.raises(ValidationError, match=message):
        _oauth_settings(tmp_path, **overrides)


def test_classifier_uses_the_chatgpt_client(monkeypatch, tmp_path: Path) -> None:
    from albert import classifier

    calls: list[tuple] = []

    class Stub:
        def complete_json(self, instructions, messages, schema=None, *, schema_name="response"):
            calls.append((instructions, messages, schema, schema_name))
            return {"memory_type": "fact", "sensitivity": "internal", "confidence": 0.9}

    monkeypatch.setattr(classifier, "get_settings", lambda: _oauth_settings(tmp_path))
    monkeypatch.setattr(chatgpt, "get_chatgpt_client", lambda: Stub())
    result = classifier.classify("Alice owns the deploy runbook.")
    assert result.memory_type == "fact"
    ((instructions, messages, schema, name),) = calls
    assert instructions == classifier.CLASSIFIER_INSTRUCTION
    assert messages == ["Alice owns the deploy runbook."]
    assert (schema, name) == (None, "classification")


def test_graphiti_on_chatgpt_uses_the_codex_client_and_no_reranker(
    monkeypatch, tmp_path: Path
) -> None:
    settings = _oauth_settings(
        tmp_path,
        graph_builder="graphiti",
        graph_store="neo4j",
        neo4j_password="test-only",
        embedding_provider="sentence-transformers",
    )
    stub = SimpleNamespace(name="stub", dimensions=384, embed=lambda _text: [0.0] * 384)
    monkeypatch.setattr(graph_builders, "get_embedder", lambda: stub)
    client = GraphitiGraphBuilder(settings)._client()
    try:
        llm = client.llm_client
        assert isinstance(llm.client, ChatGPTClient)
        assert llm.client.model == "gpt-test"
        calls: list[tuple] = []

        def complete_json(instructions, messages, schema=None, *, schema_name="response"):
            calls.append((instructions, messages, schema, schema_name))
            return {"extracted_entities": [{"name": "Alice", "entity_type_id": 0}]}

        monkeypatch.setattr(llm.client, "complete_json", complete_json)

        class ExtractedEntities(BaseModel):
            extracted_entities: list[dict]

        from graphiti_core.prompts.models import Message

        reply = asyncio.run(
            llm.generate_response(
                [
                    Message(role="system", content="Extract."),
                    Message(role="user", content="Alice."),
                ],
                response_model=ExtractedEntities,
            )
        )
        assert reply["extracted_entities"][0]["name"] == "Alice"
        ((instructions, messages, schema, name),) = calls
        assert instructions.startswith("Extract.")
        assert messages == ["Alice."]
        assert schema == ExtractedEntities.model_json_schema()
        assert name == "ExtractedEntities"
        with pytest.raises(NotImplementedError):
            asyncio.run(client.cross_encoder.rank("q", ["p"]))
    finally:
        asyncio.run(GraphitiGraphBuilder._close_client(client))


def test_worker_waits_for_the_chatgpt_account_without_spending_attempts(
    monkeypatch, identity
) -> None:
    _key, organization, workspace = identity

    class WaitingBuilder:
        def forget(self, *_args) -> None:
            raise ChatGPTAuthError("No ChatGPT account is connected")

    monkeypatch.setattr(worker, "get_graph_builder", lambda: WaitingBuilder())
    with SessionLocal() as session:
        job = Job(
            organization_id=organization.id,
            job_type="graph_forget",
            payload={
                "workspace_id": str(workspace.id),
                "source_ids": [str(uuid4())],
                "episode_ids": [],
            },
            status="running",
            attempts=1,
        )
        session.add(job)
        session.commit()
        worker.process_job(session, job)
        session.refresh(job)
        assert (job.status, job.attempts) == ("pending", 0)
        assert job.last_error == "ChatGPTAuthError: No ChatGPT account is connected"
        session.delete(job)
        session.commit()


def test_admin_chatgpt_commands(monkeypatch, tmp_path: Path) -> None:
    from albert import config

    settings = _oauth_settings(tmp_path)
    monkeypatch.setattr(config, "get_settings", lambda: settings)
    monkeypatch.setattr(
        chatgpt,
        "start_device_login",
        lambda _client: chatgpt.DeviceLogin("dev-1", "AB-12", chatgpt.VERIFICATION_URL, 5.0),
    )
    pending = {"value": True}

    def poll(_client, device_auth_id, user_code):
        assert (device_auth_id, user_code) == ("dev-1", "AB-12")
        if pending["value"]:
            return None
        return Credential(_token("acct-3"), "refresh-3", time.time() + 3600, "acct-3")

    monkeypatch.setattr(chatgpt, "poll_device_login", poll)

    def run(*args: str) -> dict:
        result = CliRunner().invoke(admin_app, [*args, "--json"])
        assert result.exit_code == 0, result.output
        return json.loads(result.stdout)

    started = run("chatgpt-login-start")
    assert started["login"] == {
        "device_auth_id": "dev-1",
        "user_code": "AB-12",
        "verification_url": "https://auth.openai.com/codex/device",
        "interval_seconds": 5.0,
    }
    poll_args = ("chatgpt-login-poll", "--device-auth-id", "dev-1", "--user-code", "AB-12")
    assert run(*poll_args)["chatgpt"] == {"state": "pending", "account_id": None}
    assert run("chatgpt-status")["chatgpt"] == {"state": "disconnected", "account_id": None}
    pending["value"] = False
    connected = CliRunner().invoke(admin_app, [*poll_args, "--json"])
    assert "refresh-3" not in connected.output
    assert json.loads(connected.stdout)["chatgpt"] == {"state": "connected", "account_id": "acct-3"}
    assert read_credential(tmp_path / "chatgpt.json").refresh_token == "refresh-3"
    assert run("chatgpt-status")["chatgpt"] == {"state": "connected", "account_id": "acct-3"}
    assert run("chatgpt-logout")["chatgpt"] == {"state": "disconnected", "account_id": None}
    assert not (tmp_path / "chatgpt.json").exists()


def test_admin_chatgpt_commands_refuse_other_providers(monkeypatch) -> None:
    from albert import config

    monkeypatch.setattr(config, "get_settings", lambda: Settings())
    result = CliRunner().invoke(admin_app, ["chatgpt-status", "--json"])
    assert result.exit_code == 2
    assert json.loads(result.stderr)["error"]["code"] == "chatgpt_not_configured"


@pytest.mark.parametrize(
    ("error", "exit_code", "message"),
    [
        (ChatGPTAuthError("No ChatGPT account is connected"), 0, "waiting for a ChatGPT sign-in"),
        (
            ChatGPTRequestError("boom", status=500),
            1,
            "graph builder probe failed (ChatGPTRequestError)",
        ),
    ],
)
def test_validate_runtime_accepts_a_brain_awaiting_sign_in(
    monkeypatch, error, exit_code, message
) -> None:
    from albert import config
    from albert.graph_builders import set_graph_builder

    real = config.get_settings()
    monkeypatch.setattr(
        config, "get_settings", lambda: real.model_copy(update={"graph_builder": "graphiti"})
    )

    class Builder:
        def readiness(self) -> str:
            raise error

    set_graph_builder(Builder())  # type: ignore[arg-type]
    try:
        result = CliRunner().invoke(admin_app, ["validate-runtime"])
    finally:
        set_graph_builder(None)
    assert result.exit_code == exit_code, result.output
    assert message in result.output
