"""SharedSecretMiddleware: gateway header required except on open paths."""

import httpx

from homestudio_mcp.client import HomeStudioClient
from homestudio_mcp.middleware import SharedSecretMiddleware
from homestudio_mcp.server import build_server

SECRET = "gateway-secret"


def _app(reached: list[str]) -> SharedSecretMiddleware:
    async def inner(scope, receive, send) -> None:
        reached.append(scope.get("path", scope["type"]))
        if scope["type"] == "http":
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

    return SharedSecretMiddleware(inner, SECRET)


async def _request(app, path: str, headers: dict | None = None) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        return await client.post(path, headers=headers or {})


async def test_rejects_missing_or_wrong_header() -> None:
    reached: list[str] = []
    app = _app(reached)
    for headers in (
        None,
        {"X-Gateway-Secret": "wrong"},
        {"X-Gateway-Secret": ""},
        {"Authorization": f"Bearer {SECRET}"},
    ):
        resp = await _request(app, "/mcp", headers)
        assert resp.status_code == 401
        assert resp.json() == {"error": "unauthorized"}
    assert reached == []


async def test_passes_matching_header_and_open_paths() -> None:
    reached: list[str] = []
    app = _app(reached)
    assert (await _request(app, "/mcp", {"X-Gateway-Secret": SECRET})).status_code == 200
    assert (await _request(app, "/healthz")).status_code == 200
    assert reached == ["/mcp", "/healthz"]


async def test_websocket_closed_1008() -> None:
    reached: list[str] = []
    app = _app(reached)
    sent: list[dict] = []

    async def send(message) -> None:
        sent.append(message)

    await app({"type": "websocket", "path": "/mcp", "headers": []}, None, send)
    await app({"type": "lifespan"}, None, send)
    assert sent == [{"type": "websocket.close", "code": 1008}]
    assert reached == ["lifespan"]


async def test_real_mcp_endpoint_behind_secret() -> None:
    inner = build_server(HomeStudioClient("http://home-studio.test:8765")).streamable_http_app()
    app = SharedSecretMiddleware(inner, SECRET)
    initialize = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "gateway", "version": "1"},
        },
    }
    headers = {"Accept": "application/json, text/event-stream"}
    transport = httpx.ASGITransport(app=app)
    async with inner.router.lifespan_context(inner):
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            denied = await client.post("/mcp", json=initialize, headers=headers)
            allowed = await client.post(
                "/mcp", json=initialize, headers={**headers, "X-Gateway-Secret": SECRET}
            )
    assert denied.status_code == 401
    assert allowed.status_code == 200
    assert allowed.json()["result"]["serverInfo"]["name"] == "homestudio"
