"""Use a ChatGPT account (OpenAI OAuth) as Albert's LLM.

A ChatGPT sign-in is not an OpenAI API key: its tokens only work against the
ChatGPT Codex backend, which speaks the Responses API and always streams. This
module implements the three pieces Albert needs:

* device-code sign-in (``start_device_login`` / ``poll_device_login``), split in
  two steps so a control plane can show the code and poll without holding a
  long-running process;
* a credential file the Brain owns, refreshed under a file lock. OpenAI rotates
  refresh tokens on every refresh, so exactly one owner may hold a credential:
  every Albert process of one Brain shares the file, and nothing else keeps a
  copy;
* ``complete_json``: one structured-output call to the Codex backend.
"""

from __future__ import annotations

import base64
import contextlib
import fcntl
import json
import os
import tempfile
import time
import uuid
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from albert.llm_text import output_text as _output_text
from albert.llm_text import strip_code_fences as _strip_code_fences

AUTH_BASE_URL = "https://auth.openai.com"
# The public OAuth client of OpenAI's Codex apps; device sign-in and the Codex
# backend are only available to it.
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
DEVICE_CALLBACK_URL = f"{AUTH_BASE_URL}/deviceauth/callback"
VERIFICATION_URL = f"{AUTH_BASE_URL}/codex/device"
DEFAULT_BASE_URL = "https://chatgpt.com/backend-api/codex"
ORIGINATOR = "albert"
# Refresh this long before the access token expires.
REFRESH_MARGIN_SECONDS = 300
REQUEST_TIMEOUT_SECONDS = 30.0
RESPONSE_TIMEOUT_SECONDS = 180.0
_AUTH_CLAIM = "https://api.openai.com/auth"


class ChatGPTAuthError(RuntimeError):
    """The credential is missing, revoked or cannot be refreshed; sign in again."""


class ChatGPTRequestError(RuntimeError):
    """The Codex backend refused or failed a request."""

    def __init__(self, message: str, *, status: int | None = None, code: str | None = None):
        super().__init__(message)
        self.status = status
        self.code = code


@dataclass(frozen=True)
class Credential:
    access_token: str
    refresh_token: str
    expires_at: float
    account_id: str

    def to_json(self) -> dict[str, Any]:
        return {
            "type": "chatgpt-oauth",
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at,
            "account_id": self.account_id,
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> Credential:
        try:
            access = str(data["access_token"])
            refresh = str(data["refresh_token"])
            expires = float(data["expires_at"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ChatGPTAuthError("ChatGPT credential file is incomplete") from exc
        account = str(data.get("account_id") or account_id_from_token(access) or "")
        if not access or not refresh or not account:
            raise ChatGPTAuthError("ChatGPT credential file is incomplete")
        return cls(access, refresh, expires, account)


def _jwt_claims(token: str) -> dict[str, Any]:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except (IndexError, ValueError):
        return {}
    return claims if isinstance(claims, dict) else {}


def account_id_from_token(token: str) -> str | None:
    """The ChatGPT account (workspace) an access token was issued for."""
    auth = _jwt_claims(token).get(_AUTH_CLAIM)
    value = auth.get("chatgpt_account_id") if isinstance(auth, dict) else None
    return value if isinstance(value, str) and value else None


def _token_expiry(token: str, expires_in: object) -> float:
    if isinstance(expires_in, int | float) and expires_in > 0:
        return time.time() + float(expires_in)
    exp = _jwt_claims(token).get("exp")
    if isinstance(exp, int | float):
        return float(exp)
    raise ChatGPTAuthError("OpenAI token response had no expiry")


def _error_code(response: httpx.Response) -> str | None:
    try:
        body = response.json()
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    error = body.get("error")
    if isinstance(error, dict):
        code = error.get("code") or error.get("type")
        return str(code) if code else None
    if isinstance(error, str):
        return error
    return None


def _auth_headers(content_type: str) -> dict[str, str]:
    return {"Content-Type": content_type, "originator": ORIGINATOR, "User-Agent": ORIGINATOR}


# --- device-code sign-in ---------------------------------------------------


@dataclass(frozen=True)
class DeviceLogin:
    device_auth_id: str
    user_code: str
    verification_url: str
    interval_seconds: float


def start_device_login(client: httpx.Client) -> DeviceLogin:
    """Ask OpenAI for a user code the account owner enters at ``verification_url``."""
    response = client.post(
        f"{AUTH_BASE_URL}/api/accounts/deviceauth/usercode",
        headers=_auth_headers("application/json"),
        json={"client_id": CLIENT_ID},
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    if response.status_code == 404:
        raise ChatGPTAuthError("OpenAI device sign-in is not available")
    if not response.is_success:
        raise ChatGPTAuthError(
            f"OpenAI device code request failed (HTTP {response.status_code}, "
            f"{_error_code(response) or 'no code'})"
        )
    body = response.json()
    device_auth_id = body.get("device_auth_id")
    user_code = body.get("user_code") or body.get("usercode")
    if not isinstance(device_auth_id, str) or not isinstance(user_code, str):
        raise ChatGPTAuthError("OpenAI device code response was incomplete")
    interval = body.get("interval")
    try:
        interval_seconds = max(1.0, float(interval)) if interval is not None else 5.0
    except (TypeError, ValueError):
        interval_seconds = 5.0
    return DeviceLogin(device_auth_id, user_code, VERIFICATION_URL, interval_seconds)


def poll_device_login(
    client: httpx.Client, device_auth_id: str, user_code: str
) -> Credential | None:
    """Check once whether the owner approved the code; ``None`` means not yet."""
    response = client.post(
        f"{AUTH_BASE_URL}/api/accounts/deviceauth/token",
        headers=_auth_headers("application/json"),
        json={"device_auth_id": device_auth_id, "user_code": user_code},
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    if response.status_code in {403, 404}:
        return None
    if not response.is_success:
        raise ChatGPTAuthError(
            f"OpenAI device authorization failed (HTTP {response.status_code}, "
            f"{_error_code(response) or 'no code'})"
        )
    body = response.json()
    code = body.get("authorization_code")
    verifier = body.get("code_verifier")
    if not isinstance(code, str) or not isinstance(verifier, str):
        raise ChatGPTAuthError("OpenAI device authorization response was incomplete")
    exchanged = client.post(
        f"{AUTH_BASE_URL}/oauth/token",
        headers=_auth_headers("application/x-www-form-urlencoded"),
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": DEVICE_CALLBACK_URL,
            "client_id": CLIENT_ID,
            "code_verifier": verifier,
        },
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    return _credential_from_token_response(exchanged, previous=None)


def _credential_from_token_response(
    response: httpx.Response, previous: Credential | None
) -> Credential:
    if not response.is_success:
        raise ChatGPTAuthError(
            f"OpenAI token request failed (HTTP {response.status_code}, "
            f"{_error_code(response) or 'no code'}); sign in to ChatGPT again"
        )
    body = response.json()
    access = body.get("access_token")
    refresh = body.get("refresh_token") or (previous.refresh_token if previous else None)
    if not isinstance(access, str) or not access or not isinstance(refresh, str) or not refresh:
        raise ChatGPTAuthError("OpenAI token response was incomplete")
    account = account_id_from_token(access) or (previous.account_id if previous else None)
    if not account:
        raise ChatGPTAuthError("OpenAI access token names no ChatGPT account")
    return Credential(access, refresh, _token_expiry(access, body.get("expires_in")), account)


# --- the Brain-owned credential file ---------------------------------------


def write_credential(path: Path, credential: Credential) -> None:
    """Atomically replace the credential file, readable only by its owner."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".chatgpt-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(credential.to_json(), handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)
        raise


def read_credential(path: Path) -> Credential:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ChatGPTAuthError("No ChatGPT account is connected") from exc
    except ValueError as exc:
        raise ChatGPTAuthError("ChatGPT credential file is not valid JSON") from exc
    if not isinstance(data, dict):
        raise ChatGPTAuthError("ChatGPT credential file is not a JSON object")
    return Credential.from_json(data)


@contextlib.contextmanager
def _locked(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def refresh_credential(client: httpx.Client, credential: Credential) -> Credential:
    response = client.post(
        f"{AUTH_BASE_URL}/oauth/token",
        headers=_auth_headers("application/x-www-form-urlencoded"),
        data={
            "grant_type": "refresh_token",
            "refresh_token": credential.refresh_token,
            "client_id": CLIENT_ID,
        },
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    return _credential_from_token_response(response, previous=credential)


class CredentialStore:
    """The credential file shared by every Albert process of one Brain."""

    def __init__(self, path: Path, client: httpx.Client) -> None:
        self.path = path
        self.client = client

    def current(self, *, force_refresh_of: str | None = None) -> Credential:
        """A usable credential, refreshed first when it is about to expire.

        ``force_refresh_of`` names an access token the backend just rejected;
        it is refreshed unless another process already replaced it.
        """
        credential = read_credential(self.path)
        if not self._needs_refresh(credential, force_refresh_of):
            return credential
        with _locked(self.path):
            # Another process may have refreshed while this one waited; using
            # its rotated token instead of refreshing again keeps the old
            # refresh token from being replayed (OpenAI revokes on reuse).
            credential = read_credential(self.path)
            if not self._needs_refresh(credential, force_refresh_of):
                return credential
            refreshed = refresh_credential(self.client, credential)
            write_credential(self.path, refreshed)
            return refreshed

    @staticmethod
    def _needs_refresh(credential: Credential, rejected: str | None) -> bool:
        if rejected is not None and credential.access_token == rejected:
            return True
        return credential.expires_at - time.time() < REFRESH_MARGIN_SECONDS


# --- one structured-output call ---------------------------------------------


def _sse_events(response: httpx.Response) -> Iterator[dict[str, Any]]:
    data: list[str] = []
    for line in response.iter_lines():
        if line.startswith("data:"):
            data.append(line[5:].lstrip())
            continue
        if line == "" and data:
            payload = "\n".join(data)
            data = []
            if payload == "[DONE]":
                return
            try:
                event = json.loads(payload)
            except ValueError:
                continue
            if isinstance(event, dict):
                yield event
    if data:
        with contextlib.suppress(ValueError):
            event = json.loads("\n".join(data))
            if isinstance(event, dict):
                yield event


class ChatGPTClient:
    """Structured JSON completions from the Codex backend with a ChatGPT sign-in."""

    def __init__(
        self,
        credentials_file: Path,
        model: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        reasoning_effort: str | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self.client = client or httpx.Client()
        self.store = CredentialStore(credentials_file, self.client)
        self.model = model
        self.url = base_url.rstrip("/") + "/responses"
        self.reasoning_effort = reasoning_effort
        self.session_id = str(uuid.uuid4())

    def close(self) -> None:
        self.client.close()

    def complete_json(
        self,
        instructions: str,
        messages: Sequence[str],
        schema: Mapping[str, Any] | None = None,
        *,
        schema_name: str = "response",
    ) -> dict[str, Any]:
        """Send system ``instructions`` and user ``messages``; return the JSON reply."""
        body: dict[str, Any] = {
            "model": self.model,
            "store": False,
            "stream": True,
            "instructions": instructions or "You are a helpful assistant.",
            "input": [
                {"role": "user", "content": [{"type": "input_text", "text": text}]}
                for text in messages
            ],
            "text": {
                "verbosity": "low",
                "format": (
                    {
                        "type": "json_schema",
                        "name": schema_name,
                        "schema": dict(schema),
                        "strict": False,
                    }
                    if schema is not None
                    else {"type": "json_object"}
                ),
            },
        }
        if self.reasoning_effort:
            body["reasoning"] = {"effort": self.reasoning_effort}
        credential = self.store.current()
        try:
            text = self._send(body, credential)
        except ChatGPTRequestError as exc:
            if exc.status != 401:
                raise
            credential = self.store.current(force_refresh_of=credential.access_token)
            text = self._send(body, credential)
        cleaned = _strip_code_fences(text)
        if not cleaned:
            raise ChatGPTRequestError("ChatGPT returned an empty response")
        try:
            value = json.loads(cleaned)
        except ValueError as exc:
            raise ChatGPTRequestError("ChatGPT returned a reply that is not JSON") from exc
        if not isinstance(value, dict):
            raise ChatGPTRequestError("ChatGPT returned JSON that is not an object")
        return value

    def _send(self, body: Mapping[str, Any], credential: Credential) -> str:
        headers = {
            "Authorization": f"Bearer {credential.access_token}",
            "chatgpt-account-id": credential.account_id,
            "originator": ORIGINATOR,
            "User-Agent": ORIGINATOR,
            "OpenAI-Beta": "responses=experimental",
            "accept": "text/event-stream",
            "content-type": "application/json",
            "session_id": self.session_id,
        }
        with self.client.stream(
            "POST", self.url, json=body, headers=headers, timeout=RESPONSE_TIMEOUT_SECONDS
        ) as response:
            if not response.is_success:
                response.read()
                code = _error_code(response)
                raise ChatGPTRequestError(
                    f"ChatGPT request failed (HTTP {response.status_code}, {code or 'no code'})",
                    status=response.status_code,
                    code=code,
                )
            deltas: list[str] = []
            for event in _sse_events(response):
                kind = event.get("type")
                if kind == "response.output_text.delta":
                    deltas.append(str(event.get("delta", "")))
                elif kind in {"response.completed", "response.done"}:
                    completed = event.get("response")
                    text = _output_text(completed) if isinstance(completed, dict) else ""
                    return text or "".join(deltas)
                elif kind in {"error", "response.failed", "response.incomplete"}:
                    detail = event.get("response") if kind != "error" else event
                    error = detail.get("error") if isinstance(detail, dict) else None
                    code = error.get("code") if isinstance(error, dict) else None
                    raise ChatGPTRequestError(
                        f"ChatGPT request failed ({kind}, {code or 'no code'})", code=code
                    )
            if deltas:
                return "".join(deltas)
            raise ChatGPTRequestError("ChatGPT stream ended without a response")


_shared_client: ChatGPTClient | None = None


def get_chatgpt_client() -> ChatGPTClient:
    """The process-wide client for the configured ChatGPT sign-in."""
    global _shared_client
    if _shared_client is None:
        from albert.config import get_settings

        settings = get_settings()
        if settings.chatgpt_credentials_file is None or settings.classification_model is None:
            raise ChatGPTAuthError("ALBERT_LLM_PROVIDER=chatgpt-oauth is not configured")
        _shared_client = ChatGPTClient(
            settings.chatgpt_credentials_file,
            settings.classification_model,
            base_url=settings.chatgpt_base_url,
            reasoning_effort=settings.chatgpt_reasoning_effort,
        )
    return _shared_client


def provider_wait_seconds(exc: BaseException) -> float | None:
    """How long a job should wait for the ChatGPT account, or ``None`` if unrelated.

    A missing or revoked sign-in and an exhausted ChatGPT usage limit are not
    faults in the job: it should wait for the owner (or the limit reset) rather
    than burn its retries and fail.
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, ChatGPTAuthError):
            return 300.0
        if isinstance(current, ChatGPTRequestError) and (
            current.status == 429
            or current.code in {"usage_limit_reached", "usage_not_included", "rate_limit_exceeded"}
        ):
            return 300.0
        current = current.__cause__ or current.__context__
    return None
