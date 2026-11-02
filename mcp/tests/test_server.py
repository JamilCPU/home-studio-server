"""MCP tools: the API is mocked with respx, tools run through an in-memory session."""

import httpx
import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from homestudio_mcp.client import HomeStudioClient
from homestudio_mcp.server import build_server
from tests.conftest import HOME, ok

EXPECTED_TOOLS = {
    "get_taste_profile",
    "list_ratings",
    "get_listening_history",
    "get_now_playing",
    "list_queue",
}
VIDEO = {
    "video_id": "dQw4w9WgXcQ",
    "title": "How GPUs Work",
    "channel": "Some Channel",
    "thumbnail_url": "https://i.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg",
    "duration_s": 1834,
    "chapters": [{"title": "Intro", "start_s": 0.0}],
}
QUEUE_ITEM = {**VIDEO, "id": 42, "source": "agent", "reason": "matches your taste"}
RATING = {"liked": True, "satisfaction": 8, "notes": "great", "created_at": "2026-11-01T14:20:00Z"}


def _server():
    return build_server(HomeStudioClient(HOME))


async def _call(name: str, args: dict | None = None):
    async with create_connected_server_and_client_session(_server()) as session:
        return await session.call_tool(name, args or {})


def _value(result):
    """The tool's structured output (list and optional returns are wrapped in "result")."""
    assert not result.isError, result.content
    assert result.structuredContent is not None
    return result.structuredContent


def _wrapped(result):
    return _value(result)["result"]


async def test_tools_registered() -> None:
    async with create_connected_server_and_client_session(_server()) as session:
        tools = {t.name: t for t in (await session.list_tools()).tools}
    assert set(tools) == EXPECTED_TOOLS
    for tool in tools.values():
        assert tool.description
        assert len(tool.description) < 300
        assert tool.outputSchema is not None, tool.name


async def test_taste_passthrough_and_null(api) -> None:
    taste = {
        "summary": "deep dives",
        "likes": ["teardowns"],
        "dislikes": ["clickbait"],
        "updated_at": "2026-11-01T09:00:00+00:00",
    }
    route = api.get("/api/taste")
    route.respond(json=ok(taste))
    assert _wrapped(await _call("get_taste_profile")) == taste
    route.respond(json=ok(None))
    assert _wrapped(await _call("get_taste_profile")) is None


async def test_history_trimmed_and_params(api) -> None:
    entry = {
        "play_id": 17,
        "video": VIDEO,
        "started_at": "2026-11-01T14:03:22+00:00",
        "seconds_listened": 912.5,
        "end_reason": "skipped",
        "source": "agent",
        "rating": RATING,
    }
    route = api.get("/api/history").respond(json=ok({"entries": [entry]}))
    args = {"since": "2026-11-01T00:00:00Z", "limit": 20}
    result = _wrapped(await _call("get_listening_history", args))
    assert dict(route.calls.last.request.url.params) == {
        "since": "2026-11-01T00:00:00Z",
        "limit": "20",
    }
    assert result == [
        {
            "play_id": 17,
            "video_id": "dQw4w9WgXcQ",
            "title": "How GPUs Work",
            "channel": "Some Channel",
            "duration_s": 1834,
            "started_at": "2026-11-01T14:03:22+00:00",
            "seconds_listened": 912.5,
            "end_reason": "skipped",
            "source": "agent",
            "rating": RATING,
        }
    ]
    await _call("get_listening_history")
    assert dict(route.calls.last.request.url.params) == {"limit": "50"}


async def test_ratings_forwards_params(api) -> None:
    rating = {
        "id": 12,
        "video_id": "dQw4w9WgXcQ",
        "title": "How GPUs Work",
        "channel": None,
        "liked": True,
        "satisfaction": 8,
        "notes": None,
        "created_at": "2026-11-01T14:21:05+00:00",
    }
    route = api.get("/api/ratings").respond(json=ok({"ratings": [rating]}))
    assert _wrapped(await _call("list_ratings", {"limit": 5, "since": "2026-11-01"})) == [rating]
    assert dict(route.calls.last.request.url.params) == {"limit": "5", "since": "2026-11-01"}
    await _call("list_ratings")
    assert dict(route.calls.last.request.url.params) == {"limit": "50"}


async def test_now_playing_idle_and_playing(api) -> None:
    route = api.get("/api/now")
    idle = {
        "state": "idle",
        "item": None,
        "position_s": 0.0,
        "duration_s": None,
        "output_device": "Echo",
    }
    route.respond(json=ok(idle))
    assert _value(await _call("get_now_playing")) == {
        "state": "idle",
        "video_id": None,
        "title": None,
        "channel": None,
        "position_s": 0.0,
        "duration_s": None,
        "output_device": "Echo",
    }
    playing = {
        "state": "playing",
        "item": QUEUE_ITEM,
        "position_s": 125.4,
        "duration_s": 1834.0,
        "output_device": "Echo",
    }
    route.respond(json=ok(playing))
    assert _value(await _call("get_now_playing")) == {
        "state": "playing",
        "video_id": "dQw4w9WgXcQ",
        "title": "How GPUs Work",
        "channel": "Some Channel",
        "position_s": 125.4,
        "duration_s": 1834.0,
        "output_device": "Echo",
    }


async def test_queue_trimmed(api) -> None:
    api.get("/api/queue").respond(json=ok({"items": [QUEUE_ITEM], "remaining_s": 1834}))
    assert _value(await _call("list_queue")) == {
        "items": [
            {
                "id": 42,
                "video_id": "dQw4w9WgXcQ",
                "title": "How GPUs Work",
                "channel": "Some Channel",
                "duration_s": 1834,
                "source": "agent",
                "reason": "matches your taste",
            }
        ],
        "remaining_s": 1834,
    }


async def test_limit_out_of_range_is_tool_error(api) -> None:
    routes = [api.get(path).respond(json=ok({})) for path in ("/api/ratings", "/api/history")]
    for name in ("list_ratings", "get_listening_history"):
        for limit, error_type in ((0, "greater_than_equal"), (201, "less_than_equal")):
            result = await _call(name, {"limit": limit})
            assert result.isError
            assert error_type in result.content[0].text
    assert not any(route.called for route in routes)


@pytest.mark.parametrize(
    ("tool", "path", "data"),
    [
        ("get_taste_profile", "/api/taste", ["not", "an", "object"]),
        ("list_ratings", "/api/ratings", {}),
        ("get_listening_history", "/api/history", {"entries": None}),
        ("get_now_playing", "/api/now", None),
        ("list_queue", "/api/queue", {"items": [1], "remaining_s": 0}),
    ],
)
async def test_bad_shape_is_tool_error(api, tool, path, data) -> None:
    api.get(path).respond(json=ok(data))
    result = await _call(tool)
    assert result.isError
    expected = (
        f"Error executing tool {tool}: Home Studio returned an unexpected response (HTTP 200)"
    )
    assert result.content[0].text == expected


async def test_api_error_surfaces_as_tool_error(api) -> None:
    error = {"ok": False, "data": None, "error": "player unavailable"}
    api.get("/api/now").respond(503, json=error)
    result = await _call("get_now_playing")
    assert result.isError
    assert "player unavailable" in result.content[0].text


async def test_healthz_route() -> None:
    app = _server().streamable_http_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        resp = await client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
