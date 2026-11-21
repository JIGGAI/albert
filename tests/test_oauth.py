"""OAuth 2.1 connector front: discovery, DCR, PKCE code flow against a connection link."""

from __future__ import annotations

import base64
import hashlib
import secrets
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from albert.api import app
from albert.config import get_settings
from albert.db import SessionLocal
from albert.models import APIKey, OAuthAuthorizationCode

from .conftest import create_principal_key

pytestmark = pytest.mark.anyio

ISSUER = "https://brain.example.test:10000"
REDIRECT = "https://claude.ai/api/mcp/auth_callback"


def _client(key: str | None = None) -> httpx.AsyncClient:
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t", headers=headers
    )


@pytest.fixture()
def oauth_enabled(monkeypatch):  # type: ignore[no-untyped-def]
    monkeypatch.setenv("ALBERT_OAUTH_ISSUER", ISSUER)
    monkeypatch.setenv("ALBERT_PUBLIC_MCP_URL", f"{ISSUER}/mcp")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture()
def manager_key(identity):  # type: ignore[no-untyped-def]
    _key, organization, _workspace = identity
    return create_principal_key(organization, None, "Console", ["console.read", "keys.manage"])


async def _link(manager_key: str, capabilities: list[str]) -> str:
    async with _client(manager_key) as manager:
        response = await manager.post(
            "/v1/settings/connection-links",
            json={"name": "Claude connector", "capabilities": capabilities},
        )
        assert response.status_code == 201, response.text
        return response.json()["url"]


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode()).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


async def _register(client: httpx.AsyncClient, redirect: str = REDIRECT) -> str:
    response = await client.post(
        "/oauth/register",
        json={
            "redirect_uris": [redirect],
            "client_name": "Claude",
            "token_endpoint_auth_method": "none",
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["token_endpoint_auth_method"] == "none"
    assert body["grant_types"] == ["authorization_code"]
    return body["client_id"]


async def test_oauth_is_off_without_an_issuer() -> None:
    get_settings.cache_clear()
    async with _client() as anonymous:
        assert (await anonymous.get("/.well-known/oauth-authorization-server")).status_code == 404
        assert (
            await anonymous.post("/oauth/register", json={"redirect_uris": [REDIRECT]})
        ).status_code == 404
        assert (
            await anonymous.post("/oauth/token", data={"grant_type": "authorization_code"})
        ).status_code == 404


async def test_discovery_documents(oauth_enabled) -> None:  # type: ignore[no-untyped-def]
    async with _client() as anonymous:
        server = (await anonymous.get("/.well-known/oauth-authorization-server")).json()
        assert server["issuer"] == ISSUER
        assert server["authorization_endpoint"] == f"{ISSUER}/oauth/authorize"
        assert server["token_endpoint"] == f"{ISSUER}/oauth/token"
        assert server["registration_endpoint"] == f"{ISSUER}/oauth/register"
        assert server["code_challenge_methods_supported"] == ["S256"]
        assert server["token_endpoint_auth_methods_supported"] == ["none"]
        assert server["client_id_metadata_document_supported"] is True
        for path in (
            "/.well-known/oauth-protected-resource",
            "/.well-known/oauth-protected-resource/mcp",
        ):
            resource = (await anonymous.get(path)).json()
            assert resource["resource"] == f"{ISSUER}/mcp"
            assert resource["authorization_servers"] == [ISSUER]
            assert resource["bearer_methods_supported"] == ["header"]


async def test_registration_rejects_bad_redirects_and_confidential_clients(oauth_enabled) -> None:  # type: ignore[no-untyped-def]
    async with _client() as anonymous:
        bad = await anonymous.post(
            "/oauth/register", json={"redirect_uris": ["http://evil.example/cb"]}
        )
        assert bad.status_code == 400 and bad.json()["error"] == "invalid_redirect_uri"
        loopback = await anonymous.post(
            "/oauth/register", json={"redirect_uris": ["http://localhost:49152/callback"]}
        )
        assert loopback.status_code == 201
        confidential = await anonymous.post(
            "/oauth/register",
            json={"redirect_uris": [REDIRECT], "token_endpoint_auth_method": "client_secret_post"},
        )
        assert (
            confidential.status_code == 400
            and confidential.json()["error"] == "invalid_client_metadata"
        )


async def test_authorize_validates_client_redirect_and_pkce(oauth_enabled) -> None:  # type: ignore[no-untyped-def]
    async with _client() as anonymous:
        client_id = await _register(anonymous)
        _verifier, challenge = _pkce()
        base = {
            "client_id": client_id,
            "response_type": "code",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "s",
        }
        assert (
            await anonymous.get(
                "/oauth/authorize", params={**base, "redirect_uri": "https://attacker.example/cb"}
            )
        ).status_code == 400
        assert (
            await anonymous.get(
                "/oauth/authorize", params={**base, "client_id": "nope", "redirect_uri": REDIRECT}
            )
        ).status_code == 400
        assert (
            await anonymous.get(
                "/oauth/authorize",
                params={**base, "redirect_uri": REDIRECT, "code_challenge_method": "plain"},
            )
        ).status_code == 400
        page = await anonymous.get("/oauth/authorize", params={**base, "redirect_uri": REDIRECT})
        assert page.status_code == 200
        assert "Albert connection link" in page.text
        assert 'name="code_challenge"' in page.text and challenge in page.text


async def test_full_code_flow_issues_a_scoped_key_once(oauth_enabled, manager_key) -> None:  # type: ignore[no-untyped-def]
    link_url = await _link(manager_key, ["memory.read", "memory.write"])
    async with _client() as anonymous:
        client_id = await _register(anonymous)
        verifier, challenge = _pkce()
        form = {
            "client_id": client_id,
            "redirect_uri": REDIRECT,
            "response_type": "code",
            "state": "xyz",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "scope": "memory",
            "link": link_url,
            "agent_name": "RJ on claude.ai",
        }
        submitted = await anonymous.post("/oauth/authorize", data=form)
        assert submitted.status_code == 302, submitted.text
        location = urlparse(submitted.headers["location"])
        assert f"{location.scheme}://{location.netloc}{location.path}" == REDIRECT
        query = parse_qs(location.query)
        assert query["state"] == ["xyz"]
        code = query["code"][0]

        # the link is single use: a second person cannot replay it
        again = await anonymous.post("/oauth/authorize", data=form)
        assert again.status_code == 400 and "already used" in again.text

        # wrong verifier burns the code
        wrong = await anonymous.post(
            "/oauth/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": REDIRECT,
                "client_id": client_id,
                "code_verifier": "x" * 50,
            },
        )
        assert wrong.status_code == 400 and wrong.json()["error"] == "invalid_grant"
        burned = await anonymous.post(
            "/oauth/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": REDIRECT,
                "client_id": client_id,
                "code_verifier": verifier,
            },
        )
        assert burned.status_code == 400

    # a fresh flow with the right verifier returns the Albert key as the access token
    link_url = await _link(manager_key, ["memory.read", "memory.write"])
    async with _client() as anonymous:
        verifier, challenge = _pkce()
        form.update({"link": link_url, "code_challenge": challenge})
        code = parse_qs(
            urlparse(
                (await anonymous.post("/oauth/authorize", data=form)).headers["location"]
            ).query
        )["code"][0]
        token = await anonymous.post(
            "/oauth/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": REDIRECT,
                "client_id": client_id,
                "code_verifier": verifier,
            },
        )
        assert token.status_code == 200, token.text
        body = token.json()
        assert body["token_type"] == "Bearer" and body["access_token"].startswith("alb_")
        assert token.headers["cache-control"] == "no-store"
        assert "expires_in" not in body and "refresh_token" not in body
        # the code row is gone and the sealed credential with it
        with SessionLocal() as session:
            assert session.query(OAuthAuthorizationCode).count() == 0
            keys = session.query(APIKey).filter(APIKey.name == "RJ on claude.ai").all()
            assert len(keys) == 2  # one per redeemed link, each its own principal
            assert all(sorted(key.capabilities) == ["memory.read", "memory.write"] for key in keys)

    # and it works against the REST API with exactly the link's capabilities
    async with _client(body["access_token"]) as connector:
        stored = await connector.post(
            "/v1/memories", json={"content": "Connector memories land in the link's workspace."}
        )
        assert stored.status_code in (200, 201), stored.text
        search = await connector.post("/v1/search", json={"query": "connector memories"})
        assert search.status_code == 200
        assert (await connector.get("/v1/settings/credentials")).status_code == 403


async def test_token_endpoint_errors(oauth_enabled) -> None:  # type: ignore[no-untyped-def]
    async with _client() as anonymous:
        refresh = await anonymous.post(
            "/oauth/token", data={"grant_type": "refresh_token", "refresh_token": "x"}
        )
        assert refresh.status_code == 400 and refresh.json()["error"] == "unsupported_grant_type"
        missing = await anonymous.post("/oauth/token", data={"grant_type": "authorization_code"})
        assert missing.status_code == 400 and missing.json()["error"] == "invalid_request"
        unknown = await anonymous.post(
            "/oauth/token",
            data={
                "grant_type": "authorization_code",
                "code": "nope",
                "client_id": "c",
                "code_verifier": "v" * 43,
            },
        )
        assert unknown.status_code == 400 and unknown.json()["error"] == "invalid_grant"


async def test_client_id_metadata_document_clients(oauth_enabled, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from albert import oauth

    cimd = "https://claude.ai/.well-known/client-metadata.json"

    class _Response:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"client_id": cimd, "client_name": "Claude", "redirect_uris": [REDIRECT]}

    monkeypatch.setattr(oauth.httpx, "get", lambda *args, **kwargs: _Response())
    oauth._CIMD_CACHE.clear()
    async with _client() as anonymous:
        _verifier, challenge = _pkce()
        page = await anonymous.get(
            "/oauth/authorize",
            params={
                "client_id": cimd,
                "redirect_uri": REDIRECT,
                "response_type": "code",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            },
        )
        assert page.status_code == 200 and "Connect Claude to Albert" in page.text
        rejected = await anonymous.get(
            "/oauth/authorize",
            params={
                "client_id": cimd,
                "redirect_uri": "https://elsewhere.example/cb",
                "response_type": "code",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            },
        )
        assert rejected.status_code == 400
