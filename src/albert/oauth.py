"""OAuth 2.1 front for Albert's MCP server, so claude.ai can add Albert as a connector.

Claude (web, Desktop, mobile, Claude Code) connects to a remote MCP server with OAuth:
it discovers the authorization server from the resource's 401 and metadata, registers
itself (Dynamic Client Registration) or identifies itself with a Client ID Metadata
Document, sends the user to ``/oauth/authorize`` with PKCE, and exchanges the code at
``/oauth/token``. Albert has no user accounts of its own, so the authorization page
asks the person for an Albert **connection link**: the single-use, capability-capped
invitation an operator mints in the console. Redeeming it mints one scoped API key, and
that key *is* the access token Claude receives. The MCP server keeps forwarding bearer
keys unchanged, every connector user gets their own principal, and revoking the key in
the console disconnects that user.

The feature is off until ``ALBERT_OAUTH_ISSUER`` names the public HTTPS origin under
which ``/oauth/*``, ``/.well-known/*`` and the MCP endpoint are reachable.
"""

from __future__ import annotations

import base64
import hashlib
import html
import secrets
import time
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit
from uuid import UUID

import httpx
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from albert.config import get_settings
from albert.credentials import redeem_link
from albert.db import get_session
from albert.models import OAuthAuthorizationCode, OAuthClient
from albert.security import _digest

router = APIRouter(tags=["oauth"])

CODE_TTL = timedelta(minutes=10)
CLAUDE_REDIRECT = "https://claude.ai/api/mcp/auth_callback"
_CIMD_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_CIMD_TTL_SECONDS = 600


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _issuer() -> str:
    issuer = (get_settings().oauth_issuer or "").rstrip("/")
    if not issuer:
        raise HTTPException(status_code=404, detail="OAuth is not enabled on this Brain")
    return issuer


def _resource() -> str:
    return get_settings().public_mcp_url


def _loopback(url: str) -> bool:
    parts = urlsplit(url)
    return parts.scheme == "http" and parts.hostname in {"localhost", "127.0.0.1", "::1"}


def _valid_redirect(url: str) -> bool:
    parts = urlsplit(url)
    if not parts.netloc or parts.fragment:
        return False
    return parts.scheme == "https" or _loopback(url)


def _redirects_match(registered: str, requested: str) -> bool:
    """Exact match, except that loopback redirects may use any port (RFC 8252 §7.3)."""
    if registered == requested:
        return True
    a, b = urlsplit(registered), urlsplit(requested)
    return (
        _loopback(registered)
        and _loopback(requested)
        and a.hostname == b.hostname
        and a.path == b.path
    )


def _code_key() -> bytes:
    pepper = get_settings().api_key_pepper.get_secret_value().encode()
    return hashlib.sha256(b"albert-oauth-code:" + pepper).digest()


def _seal(value: str) -> bytes:
    nonce = secrets.token_bytes(12)
    return nonce + AESGCM(_code_key()).encrypt(nonce, value.encode(), b"oauth-code")


def _unseal(blob: bytes) -> str:
    return AESGCM(_code_key()).decrypt(blob[:12], blob[12:], b"oauth-code").decode()


def _hash_code(code: str) -> bytes:
    return _digest(f"oauth-code:{code}")


def _pkce_ok(verifier: str, challenge: str) -> bool:
    if not (43 <= len(verifier) <= 128):
        return False
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    expected = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return secrets.compare_digest(expected, challenge)


def _oauth_error(code: str, description: str, status_code: int = 400) -> JSONResponse:
    return JSONResponse(
        {"error": code, "error_description": description},
        status_code=status_code,
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
    )


def _fetch_cimd(client_id: str) -> dict[str, Any]:
    """A Client ID Metadata Document: the client_id is the HTTPS URL of its own metadata."""
    cached = _CIMD_CACHE.get(client_id)
    if cached and cached[0] > time.monotonic():
        return cached[1]
    try:
        response = httpx.get(client_id, timeout=5.0, follow_redirects=False)
        response.raise_for_status()
        document = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="client metadata document unavailable") from exc
    if not isinstance(document, dict) or document.get("client_id") != client_id:
        raise HTTPException(status_code=400, detail="client metadata document is invalid")
    redirects = document.get("redirect_uris")
    if not isinstance(redirects, list) or not all(
        isinstance(item, str) and _valid_redirect(item) for item in redirects
    ):
        raise HTTPException(
            status_code=400, detail="client metadata document has no usable redirect_uris"
        )
    _CIMD_CACHE[client_id] = (time.monotonic() + _CIMD_TTL_SECONDS, document)
    return document


def _client(session: Session, client_id: str) -> tuple[str, list[str]]:
    """Return (client_name, redirect_uris) for a registered or metadata-document client."""
    if client_id.startswith("https://"):
        document = _fetch_cimd(client_id)
        return str(document.get("client_name") or urlsplit(client_id).hostname or "Claude"), list(
            document["redirect_uris"]
        )
    record = session.get(OAuthClient, client_id)
    if record is None:
        raise HTTPException(status_code=400, detail="unknown client_id")
    return record.client_name, list(record.redirect_uris or [])


def _link_token(value: str) -> str:
    """Accept a full connection link URL or its bare token."""
    value = value.strip()
    if "://" in value:
        parts = [part for part in urlsplit(value).path.split("/") if part]
        if "onboard" in parts and parts.index("onboard") + 1 < len(parts):
            return parts[parts.index("onboard") + 1]
        raise HTTPException(status_code=400, detail="that is not an Albert connection link")
    return value


# ---------------------------------------------------------------------------
# discovery
# ---------------------------------------------------------------------------
@router.get("/.well-known/oauth-authorization-server", include_in_schema=False)
def authorization_server_metadata() -> dict[str, Any]:
    issuer = _issuer()
    return {
        "issuer": issuer,
        "authorization_endpoint": f"{issuer}/oauth/authorize",
        "token_endpoint": f"{issuer}/oauth/token",
        "registration_endpoint": f"{issuer}/oauth/register",
        "response_types_supported": ["code"],
        "response_modes_supported": ["query"],
        "grant_types_supported": ["authorization_code"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "client_id_metadata_document_supported": True,
        "scopes_supported": ["memory"],
        "service_documentation": "https://github.com/JIGGAI/albert/blob/main/docs/integrations/claude-ai-connector.md",
    }


def protected_resource_metadata() -> dict[str, Any]:
    issuer = _issuer()
    return {
        "resource": _resource(),
        "authorization_servers": [issuer],
        "bearer_methods_supported": ["header"],
        "scopes_supported": ["memory"],
        "resource_name": "Albert memory",
    }


@router.get("/.well-known/oauth-protected-resource", include_in_schema=False)
@router.get("/.well-known/oauth-protected-resource/{suffix:path}", include_in_schema=False)
def protected_resource(suffix: str = "") -> dict[str, Any]:
    return protected_resource_metadata()


# ---------------------------------------------------------------------------
# dynamic client registration (RFC 7591)
# ---------------------------------------------------------------------------
class ClientRegistration(BaseModel):
    redirect_uris: list[str] = Field(min_length=1, max_length=10)
    client_name: str | None = Field(default=None, max_length=200)
    client_uri: str | None = Field(default=None, max_length=500)
    token_endpoint_auth_method: str | None = None
    grant_types: list[str] | None = None
    response_types: list[str] | None = None
    scope: str | None = Field(default=None, max_length=200)


@router.post("/oauth/register", status_code=201, include_in_schema=False)
def register_client(
    data: ClientRegistration, session: Session = Depends(get_session)
) -> JSONResponse:
    _issuer()
    if not all(_valid_redirect(item) for item in data.redirect_uris):
        return _oauth_error(
            "invalid_redirect_uri", "redirect_uris must be https URLs or http loopback URLs"
        )
    if data.token_endpoint_auth_method not in (None, "none"):
        return _oauth_error(
            "invalid_client_metadata",
            "only public clients (token_endpoint_auth_method none) are supported",
        )
    if data.grant_types and not set(data.grant_types) <= {"authorization_code", "refresh_token"}:
        return _oauth_error(
            "invalid_client_metadata", "only the authorization_code grant is supported"
        )
    client = OAuthClient(
        client_id=f"albert-{secrets.token_urlsafe(24)}",
        client_name=(data.client_name or "Claude")[:200],
        client_uri=data.client_uri,
        redirect_uris=data.redirect_uris,
    )
    session.add(client)
    session.commit()
    return JSONResponse(
        {
            "client_id": client.client_id,
            "client_id_issued_at": int(datetime.now(UTC).timestamp()),
            "client_name": client.client_name,
            "redirect_uris": client.redirect_uris,
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code"],
            "response_types": ["code"],
            "scope": "memory",
        },
        status_code=201,
        headers={"Cache-Control": "no-store"},
    )


# ---------------------------------------------------------------------------
# authorization endpoint
# ---------------------------------------------------------------------------
def _validate_request(
    session: Session,
    *,
    client_id: str,
    redirect_uri: str,
    response_type: str,
    code_challenge: str,
    code_challenge_method: str,
) -> str:
    client_name, redirect_uris = _client(session, client_id)
    if not any(_redirects_match(item, redirect_uri) for item in redirect_uris):
        raise HTTPException(
            status_code=400, detail="redirect_uri is not registered for this client"
        )
    if response_type != "code":
        raise HTTPException(status_code=400, detail="response_type must be code")
    if code_challenge_method != "S256" or not (43 <= len(code_challenge) <= 128):
        raise HTTPException(status_code=400, detail="PKCE with S256 is required")
    return client_name


def _page(
    *,
    client_name: str,
    hidden: dict[str, str],
    agent_name: str,
    error: str | None = None,
) -> str:
    fields = "\n".join(
        f'<input type="hidden" name="{html.escape(key)}" value="{html.escape(value)}">'
        for key, value in hidden.items()
        if value
    )
    notice = f'<p class="error">{html.escape(error)}</p>' if error else ""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Connect {html.escape(client_name)} to Albert</title>
<style>
body {{ font: 16px/1.5 system-ui, sans-serif; max-width: 34rem; margin: 8vh auto;
  padding: 0 1.25rem; color: #1c1c1e; background: #fafafa; }}
h1 {{ font-size: 1.4rem; }}
label {{ display: block; margin: 1rem 0 .25rem; font-weight: 600; }}
input[type=text], input[type=url] {{ width: 100%; padding: .6rem; font: inherit;
  border: 1px solid #c9c9ce; border-radius: 8px; }}
button {{ margin-top: 1.25rem; padding: .7rem 1.2rem; border: 0; border-radius: 8px;
  background: #1b5e8a; color: #fff; font: inherit; font-weight: 600; }}
.error {{ color: #b3261e; background: #fdecea; padding: .6rem .8rem; border-radius: 8px; }}
small {{ color: #5c5c61; }}
</style></head><body>
<h1>Connect {html.escape(client_name)} to Albert</h1>
<p><strong>{html.escape(client_name)}</strong> wants to use your Albert memory.
Paste the connection link an Albert operator issued to you. It is used once and
creates a credential just for this connection, which the operator can revoke later.</p>
{notice}
<form method="post" action="">
{fields}
<label for="link">Albert connection link</label>
<input id="link" name="link" type="text" required autocomplete="off"
  placeholder="https://…/onboard/… or the bare token">
<label for="agent_name">Name for this connection</label>
<input id="agent_name" name="agent_name" type="text" maxlength="200"
  value="{html.escape(agent_name)}">
<button type="submit">Connect</button>
</form>
<p><small>Albert never sees your Claude account; Claude never sees your Albert
operator key.</small></p>
</body></html>"""


def _authorize_params(request: Request, form: dict[str, str] | None = None) -> dict[str, str]:
    source = form if form is not None else {k: v for k, v in request.query_params.items()}
    return {
        key: str(source.get(key, "") or "")
        for key in (
            "client_id",
            "redirect_uri",
            "response_type",
            "state",
            "code_challenge",
            "code_challenge_method",
            "scope",
            "resource",
        )
    }


@router.get("/oauth/authorize", response_class=HTMLResponse, include_in_schema=False)
def authorize_page(request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
    _issuer()
    params = _authorize_params(request)
    client_name = _validate_request(
        session,
        client_id=params["client_id"],
        redirect_uri=params["redirect_uri"],
        response_type=params["response_type"],
        code_challenge=params["code_challenge"],
        code_challenge_method=params["code_challenge_method"],
    )
    return HTMLResponse(
        _page(client_name=client_name, hidden=params, agent_name=f"{client_name} (connector)"),
        headers={"Cache-Control": "no-store"},
    )


@router.post("/oauth/authorize", include_in_schema=False)
def authorize_submit(
    request: Request,
    link: str = Form(...),
    agent_name: str = Form("Claude (connector)"),
    client_id: str = Form(""),
    redirect_uri: str = Form(""),
    response_type: str = Form("code"),
    state: str = Form(""),
    code_challenge: str = Form(""),
    code_challenge_method: str = Form("S256"),
    scope: str = Form(""),
    resource: str = Form(""),
    session: Session = Depends(get_session),
) -> Response:
    _issuer()
    params = _authorize_params(
        request,
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": response_type,
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": code_challenge_method,
            "scope": scope,
            "resource": resource,
        },
    )
    client_name = _validate_request(
        session,
        client_id=client_id,
        redirect_uri=redirect_uri,
        response_type=response_type,
        code_challenge=code_challenge,
        code_challenge_method=code_challenge_method,
    )
    name = (agent_name or f"{client_name} (connector)").strip()[:200] or client_name
    try:
        key, raw, _link = redeem_link(
            session, _link_token(link), agent_name=name, capabilities=None
        )
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, str) else "connection link rejected"
        return HTMLResponse(
            _page(client_name=client_name, hidden=params, agent_name=name, error=detail),
            status_code=400,
            headers={"Cache-Control": "no-store"},
        )
    code = secrets.token_urlsafe(32)
    session.add(
        OAuthAuthorizationCode(
            code_hash=_hash_code(code),
            client_id=client_id,
            redirect_uri=redirect_uri,
            code_challenge=code_challenge,
            scope=scope or None,
            resource=resource or None,
            api_key_id=key.id,
            credential=_seal(raw),
            expires_at=datetime.now(UTC) + CODE_TTL,
        )
    )
    session.commit()
    parts = urlsplit(redirect_uri)
    query = parse_qs(parts.query, keep_blank_values=True)
    query["code"] = [code]
    if state:
        query["state"] = [state]
    location = urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(query, doseq=True), "")
    )
    return RedirectResponse(location, status_code=302, headers={"Cache-Control": "no-store"})


# ---------------------------------------------------------------------------
# token endpoint
# ---------------------------------------------------------------------------
@router.post("/oauth/token", include_in_schema=False)
def token(
    grant_type: str = Form(""),
    code: str = Form(""),
    redirect_uri: str = Form(""),
    client_id: str = Form(""),
    code_verifier: str = Form(""),
    session: Session = Depends(get_session),
) -> JSONResponse:
    _issuer()
    if grant_type != "authorization_code":
        return _oauth_error(
            "unsupported_grant_type",
            "only authorization_code is supported; the issued access token does not expire, "
            "so no refresh is needed",
        )
    if not code or not code_verifier:
        return _oauth_error("invalid_request", "code and code_verifier are required")
    now = datetime.now(UTC)
    session.execute(delete(OAuthAuthorizationCode).where(OAuthAuthorizationCode.expires_at < now))
    record = session.scalar(
        select(OAuthAuthorizationCode).where(OAuthAuthorizationCode.code_hash == _hash_code(code))
    )
    if record is None:
        session.commit()
        return _oauth_error("invalid_grant", "authorization code is unknown, used, or expired")
    # single use, whatever happens next
    session.delete(record)
    session.commit()
    if (
        record.client_id != client_id
        or (redirect_uri and record.redirect_uri != redirect_uri)
        or not _pkce_ok(code_verifier, record.code_challenge)
    ):
        return _oauth_error("invalid_grant", "client, redirect_uri or PKCE verifier does not match")
    try:
        access_token = _unseal(record.credential)
    except Exception:
        return _oauth_error("server_error", "stored credential could not be opened", 500)
    return JSONResponse(
        {
            "access_token": access_token,
            "token_type": "Bearer",
            "scope": record.scope or "memory",
        },
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
    )


def code_table_name() -> str:  # pragma: no cover - documentation helper
    return OAuthAuthorizationCode.__tablename__


def client_id_for(key_id: UUID, session: Session) -> str | None:  # pragma: no cover
    row = session.scalar(
        select(OAuthAuthorizationCode).where(OAuthAuthorizationCode.api_key_id == key_id)
    )
    return row.client_id if row else None


__all__ = [
    "CLAUDE_REDIRECT",
    "client_id_for",
    "code_table_name",
    "protected_resource_metadata",
    "router",
]
