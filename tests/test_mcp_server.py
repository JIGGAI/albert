from __future__ import annotations

import httpx
import pytest
from starlette.responses import JSONResponse

from albert.mcp_server import ForwardBearerAuth, _request_api_key

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
