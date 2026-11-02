"""Entry point: serve the homestudio MCP server over streamable HTTP behind the shared secret."""

import os
import sys

import uvicorn

from homestudio_mcp.client import HomeStudioClient
from homestudio_mcp.middleware import SharedSecretMiddleware
from homestudio_mcp.server import build_server

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8000


def _error(message: str) -> int:
    print(f"homestudio-mcp: {message}", file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    del argv  # configuration comes from the environment only
    api_url = os.environ.get("HOMESTUDIO_API_URL", "").strip()
    if not api_url:
        return _error("environment variable HOMESTUDIO_API_URL is required")
    secret = os.environ.get("HOMESTUDIO_MCP_SHARED_SECRET", "").strip()
    if not secret:
        return _error("environment variable HOMESTUDIO_MCP_SHARED_SECRET is required")
    host = os.environ.get("HOMESTUDIO_MCP_HOST", "").strip() or DEFAULT_HOST
    raw_port = os.environ.get("HOMESTUDIO_MCP_PORT", "").strip() or str(DEFAULT_PORT)
    try:
        port = int(raw_port)
    except ValueError:
        return _error(f"HOMESTUDIO_MCP_PORT must be an integer, got {raw_port!r}")
    server = build_server(HomeStudioClient(api_url), host=host, port=port)
    app = SharedSecretMiddleware(server.streamable_http_app(), secret)
    uvicorn.run(app, host=host, port=port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
