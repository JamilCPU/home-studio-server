"""The homestudio MCP server: read-only tools over the Home Studio REST API."""

from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from pydantic import Field
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from homestudio_mcp.client import HomeStudioClient

INSTRUCTIONS = (
    "Read-only view of Jamil's Home Studio: the YouTube audio player on the home speaker. "
    "Taste profile, ratings, listening history, what is playing and what is queued."
)
TASTE_DESCRIPTION = (
    "Jamil's YouTube listening taste profile: a summary, likes and dislikes. "
    "Use it to know what he enjoys before recommending or judging media."
)
RATINGS_DESCRIPTION = (
    "Ratings Jamil gave to videos (liked, 1-10 satisfaction, notes), newest first. "
    "Use it when asked what he liked or disliked recently. "
    "since is an ISO 8601 timestamp."
)
HISTORY_DESCRIPTION = (
    "Videos played on the home speaker, newest first, with how long each was listened "
    "to and its rating. Use it for questions like what did I listen to this week. "
    "since is an ISO 8601 timestamp."
)
NOW_PLAYING_DESCRIPTION = (
    "What the home speaker is playing right now: state, title, channel, position and "
    "duration in seconds, and the output device."
)
QUEUE_DESCRIPTION = (
    "Videos waiting to play next on the home speaker, in order, and the total remaining seconds."
)

DEFAULT_LIMIT = 50
HISTORY_VIDEO_KEYS = ("video_id", "title", "channel", "duration_s")
HISTORY_PLAY_KEYS = ("started_at", "seconds_listened", "end_reason", "source", "rating")
NOW_ITEM_KEYS = ("video_id", "title", "channel")
NOW_PLAYER_KEYS = ("position_s", "duration_s", "output_device")
QUEUE_ITEM_KEYS = ("id", "video_id", "title", "channel", "duration_s", "source", "reason")

Limit = Annotated[int, Field(ge=1, le=200)]


def _pick(source: Any, keys: tuple[str, ...]) -> dict[str, Any]:
    source = source if isinstance(source, dict) else {}
    return {key: source.get(key) for key in keys}


def _trim_history(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "play_id": entry.get("play_id"),
        **_pick(entry.get("video"), HISTORY_VIDEO_KEYS),
        **_pick(entry, HISTORY_PLAY_KEYS),
    }


def _trim_now(data: dict[str, Any]) -> dict[str, Any]:
    return {
        "state": data.get("state"),
        **_pick(data.get("item"), NOW_ITEM_KEYS),
        **_pick(data, NOW_PLAYER_KEYS),
    }


def _query(limit: int, since: str | None) -> dict[str, Any]:
    params: dict[str, Any] = {"limit": limit}
    if since is not None:
        params["since"] = since
    return params


def _register_library_tools(mcp: FastMCP, client: HomeStudioClient) -> None:
    """Taste, ratings and listening history."""

    @mcp.tool(description=TASTE_DESCRIPTION)
    async def get_taste_profile() -> dict | None:
        return await client.get_dict("/api/taste", nullable=True)

    @mcp.tool(description=RATINGS_DESCRIPTION)
    async def list_ratings(limit: Limit = DEFAULT_LIMIT, since: str | None = None) -> list[dict]:
        data = await client.get_dict("/api/ratings", _query(limit, since), list_key="ratings")
        return data["ratings"]

    @mcp.tool(description=HISTORY_DESCRIPTION)
    async def get_listening_history(
        since: str | None = None, limit: Limit = DEFAULT_LIMIT
    ) -> list[dict]:
        data = await client.get_dict("/api/history", _query(limit, since), list_key="entries")
        return [_trim_history(entry) for entry in data["entries"]]


def _register_player_tools(mcp: FastMCP, client: HomeStudioClient) -> None:
    """Now playing and the queue."""

    @mcp.tool(description=NOW_PLAYING_DESCRIPTION)
    async def get_now_playing() -> dict[str, Any]:
        return _trim_now(await client.get_dict("/api/now"))

    @mcp.tool(description=QUEUE_DESCRIPTION)
    async def list_queue() -> dict[str, Any]:
        data = await client.get_dict("/api/queue", list_key="items")
        return {
            "items": [_pick(item, QUEUE_ITEM_KEYS) for item in data["items"]],
            "remaining_s": data.get("remaining_s"),
        }


def build_server(client: HomeStudioClient, *, host: str = "0.0.0.0", port: int = 8000) -> FastMCP:
    """Create the FastMCP server with every read tool bound to `client`."""
    mcp = FastMCP(
        "homestudio",
        instructions=INSTRUCTIONS,
        host=host,
        port=port,
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
    )

    @mcp.custom_route("/healthz", methods=["GET"])
    async def healthz(request: Request) -> Response:
        return JSONResponse({"ok": True})

    _register_library_tools(mcp, client)
    _register_player_tools(mcp, client)
    return mcp
