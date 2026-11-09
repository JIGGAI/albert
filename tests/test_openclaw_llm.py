"""ALBERT_LLM_PROVIDER=openclaw: classify through an OpenClaw gateway agent."""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from albert import classifier
from albert.classifier import classify
from albert.config import Settings
from albert.openclaw_llm import (
    OpenClawClient,
    OpenClawRequestError,
    OpenClawUnavailableError,
    provider_wait_seconds,
)

COMMON = {
    "_env_file": None,
    "api_key_pepper": "test-pepper-that-is-longer-than-thirty-two-characters",
}


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        llm_provider="openclaw",
        openclaw_gateway_url="http://gateway.example:18789/",
        openclaw_gateway_token=SecretStr("gateway-token"),
        openclaw_agent_id="albert-classifier",
        openai_api_key=None,
        classification_model=None,
    )


def _reply(text: str, status: str = "completed") -> dict:
    return {
        "id": "resp_1",
        "status": status,
        "output": [
            {"type": "reasoning", "id": "rs_1", "summary": []},
            {
                "type": "message",
                "id": "msg_1",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text}],
            },
        ],
    }


def _transport(handler):  # type: ignore[no-untyped-def]
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_client_posts_one_isolated_session_per_call() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_reply('{"memory_type":"decision"}'))

    client = OpenClawClient(
        "http://gateway.example:18789/",
        "gateway-token",
        "albert-classifier",
        client=_transport(handler),
    )
    first = client.complete_json("Label it.", "We decided to ship.")
    second = client.complete_json("Label it.", "Another memory.")

    assert first == {"memory_type": "decision"} and second == first
    request = seen[0]
    assert str(request.url) == "http://gateway.example:18789/v1/responses"
    assert request.headers["authorization"] == "Bearer gateway-token"
    body = request.read().decode()
    assert '"model":"openclaw/albert-classifier"' in body.replace(" ", "")
    assert '"stream":false' in body.replace(" ", "")
    assert "response_format" not in body and '"text"' not in body  # the gateway rejects both
    keys = [r.headers["x-openclaw-session-key"] for r in seen]
    assert all(key.startswith("agent:albert-classifier:albert:") for key in keys)
    # No earlier memory's text may leak into a later label: every call is its own session.
    assert keys[0] != keys[1]


def test_client_tolerates_code_fences_and_prose() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_reply('Sure:\n```json\n{"memory_type":"lesson"}\n```'))

    client = OpenClawClient("http://g", "t", "a", client=_transport(handler))
    assert client.complete_json("i", "m") == {"memory_type": "lesson"}


def test_client_reports_failed_or_empty_responses() -> None:
    def failed(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"status": "failed", "output": [], "error": {"message": "model unavailable"}},
        )

    with pytest.raises(OpenClawRequestError, match="model unavailable"):
        OpenClawClient("http://g", "t", "a", client=_transport(failed)).complete_json("i", "m")

    def prose(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_reply("I cannot classify this."))

    with pytest.raises(OpenClawRequestError, match="not JSON"):
        OpenClawClient("http://g", "t", "a", client=_transport(prose)).complete_json("i", "m")


def test_gateway_availability_problems_wait_instead_of_failing_the_job() -> None:
    def refused(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    with pytest.raises(OpenClawUnavailableError) as down:
        OpenClawClient("http://g", "t", "a", client=_transport(refused)).complete_json("i", "m")
    assert provider_wait_seconds(down.value) == 300.0

    def unauthorized(_: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "Unauthorized"}})

    with pytest.raises(OpenClawUnavailableError) as denied:
        OpenClawClient("http://g", "t", "a", client=_transport(unauthorized)).complete_json(
            "i", "m"
        )
    assert denied.value.status == 401 and provider_wait_seconds(denied.value) == 300.0

    def bad_request(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400, json={"error": {"message": "Invalid `model`. Use `openclaw/<agentId>`."}}
        )

    with pytest.raises(OpenClawRequestError, match="Invalid `model`") as rejected:
        OpenClawClient("http://g", "t", "a", client=_transport(bad_request)).complete_json("i", "m")
    assert not isinstance(rejected.value, OpenClawUnavailableError)
    assert provider_wait_seconds(rejected.value) is None

    wrapped = RuntimeError("job failed")
    wrapped.__cause__ = OpenClawUnavailableError("gateway down")
    assert provider_wait_seconds(wrapped) == 300.0
    assert provider_wait_seconds(ValueError("other")) is None


def test_classifier_uses_the_gateway_provider(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(classifier, "get_settings", _settings)
    sent: dict = {}

    class FakeClient:
        def complete_json(self, instructions: str, text: str) -> dict:
            sent["instructions"] = instructions
            sent["text"] = text
            return {
                "memory_type": "lesson",
                "sensitivity": "confidential",
                "confidence": 0.8,
                "entities": [
                    {"name": "Albert", "entity_type": "system"},
                    {"name": "OpenClaw", "entity_type": "system"},
                ],
                "relationships": [
                    {"source": "Albert", "relation_type": "uses", "target": "OpenClaw"}
                ],
            }

    monkeypatch.setattr("albert.openclaw_llm.get_openclaw_client", lambda: FakeClient())
    result = classify("Albert uses OpenClaw.")
    assert sent == {
        "instructions": classifier.CLASSIFIER_INSTRUCTION,
        "text": "Albert uses OpenClaw.",
    }
    assert result.memory_type == "lesson"
    assert result.sensitivity == "confidential"
    assert result.relationships[0].relation_type == "uses"


def test_settings_for_the_openclaw_provider() -> None:
    with pytest.raises(ValidationError, match="ALBERT_OPENCLAW_GATEWAY_TOKEN"):
        Settings(**COMMON, llm_provider="openclaw", openclaw_gateway_token=None)
    # compose passes "" for unset variables; that must read as unset, not as a token.
    with pytest.raises(ValidationError, match="ALBERT_OPENCLAW_GATEWAY_TOKEN"):
        Settings(**COMMON, llm_provider="openclaw", openclaw_gateway_token="")

    settings = Settings(**COMMON, llm_provider="openclaw", openclaw_gateway_token="t")
    # The gateway picks the model per agent; no API key or model name is needed.
    assert settings.graph_builder == "llm-classifier"
    assert settings.openclaw_agent_id == "albert-classifier"
    assert settings.openclaw_gateway_url == "http://host.docker.internal:18789"

    # Graphiti drives its own OpenAI client and cannot go through the gateway.
    with pytest.raises(ValidationError, match="openai-compatible or chatgpt-oauth"):
        Settings(
            **COMMON,
            llm_provider="openclaw",
            openclaw_gateway_token="t",
            graph_builder="graphiti",
            graph_store="falkordb",
            embedding_provider="sentence-transformers",
        )
