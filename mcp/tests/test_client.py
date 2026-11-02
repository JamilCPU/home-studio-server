"""HomeStudioClient: unwraps the envelope, maps failures to ToolError."""

import httpx
import pytest
from mcp.server.fastmcp.exceptions import ToolError

from homestudio_mcp.client import HomeStudioClient
from tests.conftest import HOME, ok


async def test_get_unwraps_data(api) -> None:
    route = api.get("/api/queue").respond(json=ok({"items": [], "remaining_s": 0}))
    client = HomeStudioClient(HOME + "/")
    assert await client.get("/api/queue", {"limit": 5}) == {"items": [], "remaining_s": 0}
    assert dict(route.calls.last.request.url.params) == {"limit": "5"}


async def test_error_envelope_raises_tool_error(api) -> None:
    api.get("/api/now").respond(404, json={"ok": False, "data": None, "error": "video not found"})
    with pytest.raises(ToolError, match="^video not found$"):
        await HomeStudioClient(HOME).get("/api/now")


async def test_timeout_and_unreachable(api) -> None:
    route = api.get("/api/now")
    route.mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(ToolError, match="^Home Studio timed out$"):
        await HomeStudioClient(HOME).get("/api/now")
    route.mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(ToolError, match="^Home Studio unreachable$"):
        await HomeStudioClient(HOME).get("/api/now")


async def test_non_json_is_tool_error(api) -> None:
    route = api.get("/api/now")
    route.respond(502, text="<html>bad gateway</html>")
    expected = r"^Home Studio returned an unexpected response \(HTTP 502\)$"
    with pytest.raises(ToolError, match=expected):
        await HomeStudioClient(HOME).get("/api/now")
    route.respond(200, json=[1, 2, 3])
    with pytest.raises(ToolError, match=r"\(HTTP 200\)"):
        await HomeStudioClient(HOME).get("/api/now")
