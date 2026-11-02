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
        _, data = await self._fetch(path, params)
        return data

    async def get_dict(
        self,
        path: str,
        params: Mapping[str, Any] | None = None,
        *,
        list_key: str | None = None,
        nullable: bool = False,
    ) -> dict[str, Any] | None:
        """Like `get`, but `data` must be an object (or null when `nullable`).

        With `list_key`, `data[list_key]` must be a list of objects. Anything else is a
        ToolError("Home Studio returned an unexpected response (HTTP <status>)").
        """
        status, data = await self._fetch(path, params)
        if data is None and nullable:
            return None
        if not isinstance(data, dict) or (list_key and not _is_object_list(data.get(list_key))):
            raise _unexpected(status)
        return data

    async def _fetch(self, path: str, params: Mapping[str, Any] | None) -> tuple[int, Any]:
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url, timeout=self._timeout_s
            ) as client:
                response = await client.get(path, params=dict(params) if params else None)
        except httpx.TimeoutException as exc:
            raise ToolError("Home Studio timed out") from exc
        except httpx.HTTPError as exc:
            raise ToolError("Home Studio unreachable") from exc
        return response.status_code, _unwrap(response)


def _is_object_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, dict) for item in value)


def _unexpected(status: int) -> ToolError:
    return ToolError(f"Home Studio returned an unexpected response (HTTP {status})")


def _unwrap(response: httpx.Response) -> Any:
    try:
        envelope = response.json()
    except ValueError:
        envelope = None
    if not isinstance(envelope, dict) or "ok" not in envelope:
        raise _unexpected(response.status_code)
    if not envelope["ok"]:
        raise ToolError(str(envelope.get("error") or f"Home Studio error {response.status_code}"))
    return envelope.get("data")
