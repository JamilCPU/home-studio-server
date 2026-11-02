"""ASGI middleware: only the gateway (holding the shared secret) may reach the MCP endpoint."""

import hmac
import json

from starlette.types import ASGIApp, Receive, Scope, Send

HTTP_UNAUTHORIZED = 401
WS_POLICY_VIOLATION = 1008
UNAUTHORIZED_BODY = json.dumps({"error": "unauthorized"}).encode()


class SharedSecretMiddleware:
    """HTTP needs the shared-secret header (constant-time compare) unless the path is open."""

    def __init__(
        self,
        app: ASGIApp,
        secret: str,
        *,
        header: str = "x-gateway-secret",
        open_paths: tuple[str, ...] = ("/healthz",),
    ) -> None:
        self._app = app
        self._secret = secret.encode()
        self._header = header.lower().encode("latin-1")
        self._open_paths = open_paths

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        kind = scope["type"]
        if kind == "lifespan" or (kind == "http" and self._allowed(scope)):
            await self._app(scope, receive, send)
            return
        if kind == "websocket":
            await send({"type": "websocket.close", "code": WS_POLICY_VIOLATION})
            return
        if kind == "http":
            await _send_unauthorized(send)

    def _allowed(self, scope: Scope) -> bool:
        if scope.get("path") in self._open_paths:
            return True
        value = dict(scope.get("headers", [])).get(self._header)
        return value is not None and hmac.compare_digest(value, self._secret)


async def _send_unauthorized(send: Send) -> None:
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(UNAUTHORIZED_BODY)).encode()),
    ]
    await send({"type": "http.response.start", "status": HTTP_UNAUTHORIZED, "headers": headers})
    await send({"type": "http.response.body", "body": UNAUTHORIZED_BODY})
