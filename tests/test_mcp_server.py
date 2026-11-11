from __future__ import annotations

import json
import os

import httpx
import pytest
from starlette.responses import JSONResponse

from albert.mcp_server import ForwardBearerAuth, _request_api_key, _settings

pytestmark = pytest.mark.anyio


async def test_http_mcp_requires_and_captures_caller_bearer() -> None:
    observed: list[str | None] = []

    async def endpoint(scope, receive, send):  # type: ignore[no-untyped-def]
        observed.append(_request_api_key.get())
        response = JSONResponse({"ok": True})
        await response(scope, receive, send)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=ForwardBearerAuth(endpoint)),
        base_url="http://test",
    ) as client:
        anonymous = await client.post("/mcp")
        authorized = await client.post(
            "/mcp", headers={"Authorization": "Bearer alb_example_secret"}
        )

    assert anonymous.status_code == 401
    assert authorized.status_code == 200
    assert observed == ["alb_example_secret"]
    assert _request_api_key.get() is None


async def test_streamable_http_tool_calls_forward_each_requests_bearer(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """End to end through the real MCP session manager, not only the middleware.

    The session's server task is started during the first request, so this
    proves a later request's key (not the initializing one) reaches the tool.
    """
    import socket
    import threading
    import time

    import anyio
    import uvicorn
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    import albert.mcp_server as mcp_server

    async def fake_request(method: str, path: str, payload: dict | None = None) -> dict:
        return {"seen_key": mcp_server._request_api_key.get()}

    monkeypatch.setattr(mcp_server, "_request", fake_request)
    monkeypatch.delenv("ALBERT_MCP_API_KEY", raising=False)

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    app = ForwardBearerAuth(mcp_server.mcp.streamable_http_app(host="127.0.0.1"))
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        await anyio.sleep(0.05)
    assert server.started

    try:
        http = httpx.AsyncClient(headers={"Authorization": "Bearer alb_first_key"})
        async with streamable_http_client(f"http://127.0.0.1:{port}/mcp", http_client=http) as (
            read,
            write,
            *_rest,
        ):
            async with ClientSession(read, write) as session:
                await session.initialize()
                first = await session.call_tool("memory_get", {"memory_id": "x"})
                http.headers["Authorization"] = "Bearer alb_second_key"
                second = await session.call_tool("memory_get", {"memory_id": "x"})
    finally:
        server.should_exit = True
        thread.join(timeout=5)
    assert first.structured_content == {"seen_key": "alb_first_key"}
    assert second.structured_content == {"seen_key": "alb_second_key"}


def _credential_file(tmp_path, mode: int = 0o600):  # type: ignore[no-untyped-def]
    path = tmp_path / "credentials.env"
    path.write_text(
        json.dumps({"ALBERT_API_URL": "https://brain.example/", "ALBERT_CAPTURE_TOKEN": "alb_file"})
    )
    os.chmod(path, mode)
    return path


@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_stdio_falls_back_to_the_owned_credential_file(
    tmp_path, monkeypatch, anyio_backend
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("ALBERT_API_URL", "http://127.0.0.1:8080")
    monkeypatch.setenv("ALBERT_MCP_API_KEY", "")
    monkeypatch.setenv("ALBERT_MCP_CREDENTIAL_FILE", str(_credential_file(tmp_path)))
    assert _settings() == ("https://brain.example", "alb_file")

    # An explicit key still wins.
    monkeypatch.setenv("ALBERT_MCP_API_KEY", "alb_explicit")
    assert _settings() == ("http://127.0.0.1:8080", "alb_explicit")


@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_stdio_ignores_a_credential_file_others_can_read(
    tmp_path, monkeypatch, anyio_backend
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("ALBERT_MCP_API_KEY", "")
    monkeypatch.setenv("ALBERT_MCP_CREDENTIAL_FILE", str(_credential_file(tmp_path, 0o644)))
    with pytest.raises(RuntimeError, match="required"):
        _settings()
