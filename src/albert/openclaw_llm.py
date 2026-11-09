"""Use an OpenClaw gateway agent as Albert's LLM (``ALBERT_LLM_PROVIDER=openclaw``).

OpenClaw already holds the operator's model sign-in (for example a ChatGPT
account through its Codex OAuth profile) and exposes it on the gateway's
OpenResponses endpoint, ``POST /v1/responses``, where ``model`` names an agent:
``openclaw/<agentId>``. Albert posts one memory per call to a dedicated,
tool-less agent (``albert-capture classifier-agent`` installs it) and lets
OpenClaw pick the model, credential and provider the way it does for every other
agent on that box. Nothing here stores a model credential; the only secret is the
gateway's own bearer token.

The gateway rejects chat-completions-only fields (``response_format``, ``text``),
so the instruction asks for JSON and the reply is parsed leniently.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import httpx

from albert.llm_text import first_json_object, output_text

RESPONSE_TIMEOUT_SECONDS = 180.0
# Gateway restarts and token rotations are operator events, not faults in a job.
UNAVAILABLE_WAIT_SECONDS = 300.0


class OpenClawRequestError(RuntimeError):
    """The gateway refused or failed a request."""

    def __init__(self, message: str, *, status: int | None = None):
        super().__init__(message)
        self.status = status


class OpenClawUnavailableError(OpenClawRequestError):
    """The gateway is unreachable, restarting, or refusing this token: wait and retry."""


def _error_message(payload: Any, fallback: str) -> str:
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])
        if isinstance(error, str) and error:
            return error
    return fallback


class OpenClawClient:
    """Structured JSON completions from one OpenClaw agent."""

    def __init__(
        self,
        gateway_url: str,
        token: str,
        agent_id: str,
        *,
        client: httpx.Client | None = None,
    ) -> None:
        self.url = gateway_url.rstrip("/") + "/v1/responses"
        self.token = token
        self.agent_id = agent_id
        self.client = client or httpx.Client()

    def close(self) -> None:
        self.client.close()

    def complete_json(self, instructions: str, text: str) -> dict[str, Any]:
        """Send ``instructions`` and one user ``text``; return the JSON object replied."""
        body = {
            "model": f"openclaw/{self.agent_id}",
            "stream": False,
            "instructions": instructions,
            "input": text,
        }
        headers = {
            "Authorization": f"Bearer {self.token}",
            "content-type": "application/json",
            # Every memory gets its own gateway session, so no earlier memory's
            # text is in the transcript when the next one is labelled.
            "x-openclaw-session-key": f"agent:{self.agent_id}:albert:{uuid.uuid4()}",
        }
        try:
            response = self.client.post(
                self.url, json=body, headers=headers, timeout=RESPONSE_TIMEOUT_SECONDS
            )
        except httpx.HTTPError as exc:
            raise OpenClawUnavailableError(
                f"OpenClaw gateway unreachable ({type(exc).__name__})"
            ) from exc
        if not response.is_success:
            payload: Any = None
            try:
                payload = response.json()
            except ValueError:
                pass
            message = _error_message(payload, response.text[:200] or "no detail")
            status = response.status_code
            if status in {401, 403, 408, 429} or status >= 500:
                raise OpenClawUnavailableError(
                    f"OpenClaw gateway refused the request (HTTP {status}): {message}",
                    status=status,
                )
            raise OpenClawRequestError(
                f"OpenClaw gateway rejected the request (HTTP {status}): {message}",
                status=status,
            )
        try:
            reply = response.json()
        except ValueError as exc:
            raise OpenClawRequestError("OpenClaw gateway returned a non-JSON body") from exc
        if not isinstance(reply, dict):
            raise OpenClawRequestError("OpenClaw gateway returned an unexpected body")
        if reply.get("status") not in (None, "completed"):
            raise OpenClawRequestError(
                f"OpenClaw agent run {reply.get('status')}: {_error_message(reply, 'no detail')}"
            )
        text_out = output_text(reply)
        if not text_out.strip():
            raise OpenClawRequestError("OpenClaw agent returned an empty response")
        try:
            value = json.loads(first_json_object(text_out))
        except ValueError as exc:
            raise OpenClawRequestError("OpenClaw agent returned a reply that is not JSON") from exc
        if not isinstance(value, dict):
            raise OpenClawRequestError("OpenClaw agent returned JSON that is not an object")
        return value


_shared_client: OpenClawClient | None = None


def get_openclaw_client() -> OpenClawClient:
    """The process-wide client for the configured gateway agent."""
    global _shared_client
    if _shared_client is None:
        from albert.config import get_settings

        settings = get_settings()
        if settings.openclaw_gateway_token is None:
            raise OpenClawUnavailableError("ALBERT_LLM_PROVIDER=openclaw is not configured")
        _shared_client = OpenClawClient(
            settings.openclaw_gateway_url,
            settings.openclaw_gateway_token.get_secret_value(),
            settings.openclaw_agent_id,
        )
    return _shared_client


def provider_wait_seconds(exc: BaseException) -> float | None:
    """How long a job should wait for the gateway, or ``None`` if the error is unrelated."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, OpenClawUnavailableError):
            return UNAVAILABLE_WAIT_SECONDS
        current = current.__cause__ or current.__context__
    return None
