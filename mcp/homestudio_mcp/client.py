"""Thin async client for the Home Studio REST API."""

from collections.abc import Mapping
from typing import Any

import httpx
from mcp.server.fastmcp.exceptions import ToolError

DEFAULT_TIMEOUT_S = 5.0


class HomeStudioClient:
    """GETs from the Home Studio API; returns the envelope's `data` or raises ToolError."""

    def __init__(self, base_url: str, timeout_s: float = DEFAULT_TIMEOUT_S) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout_s = timeout_s

    async def get(self, path: str, params: Mapping[str, Any] | None = None) -> Any:
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url, timeout=self._timeout_s
            ) as client:
                response = await client.get(path, params=dict(params) if params else None)
        except httpx.TimeoutException as exc:
            raise ToolError("Home Studio timed out") from exc
        except httpx.HTTPError as exc:
            raise ToolError("Home Studio unreachable") from exc
        return _unwrap(response)


def _unwrap(response: httpx.Response) -> Any:
    try:
        envelope = response.json()
    except ValueError:
        envelope = None
    if not isinstance(envelope, dict) or "ok" not in envelope:
        raise ToolError(
            f"Home Studio returned an unexpected response (HTTP {response.status_code})"
        )
    if not envelope["ok"]:
        raise ToolError(str(envelope.get("error") or f"Home Studio error {response.status_code}"))
    return envelope.get("data")
