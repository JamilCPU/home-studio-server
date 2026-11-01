"""Queue and search endpoints."""

from __future__ import annotations

import logging
import re
import sqlite3
from contextlib import closing
from typing import Literal
from urllib.parse import parse_qs, urlparse

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from src.api.app import Deps, ok
from src.agent.request import AUTH_EVENT_MESSAGE
from src.api.models import queue_item_out, video_out
from src.sources.catalog import ensure_videos
from src.sources.data_api import QuotaExceeded
from src.sources.ytdlp import SourceAuthError, SourceError
from src.store import queue
from src.store.settings import log_event
from src.store.videos import Video

logger = logging.getLogger(__name__)

SEARCH_RESULTS = 20
_VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_WATCH_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}
_SHORT_HOSTS = {"youtu.be", "www.youtu.be"}
NOT_YOUTUBE = "not a YouTube video link"
COOKIES_EXPIRED = "YouTube sign-in required (cookies expired)"


class QueueBody(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)
    video_id: str | None = Field(default=None, max_length=64)
    url: str | None = Field(default=None, max_length=2000)
    where: Literal["next", "end"]


class OrderBody(BaseModel):
    model_config = ConfigDict(extra="ignore")
    ids: list[int]


class _NoDataApi:
    """Stand-in when there is no API key: makes ensure_videos fall back to yt-dlp."""

    def video_details(self, ids: list[str]) -> list[Video]:
        raise QuotaExceeded("no YouTube Data API key configured")


def parse_video_id(url: str) -> str | None:
    """The video id from a watch?v=, youtu.be/ or shorts/ link, else None."""
    parsed = urlparse(url if "://" in url else f"https://{url}")
    if parsed.scheme not in ("http", "https"):
        return None
    host = (parsed.hostname or "").lower()
    parts = [p for p in parsed.path.split("/") if p]
    candidate: str | None = None
    if host in _SHORT_HOSTS and len(parts) == 1:
        candidate = parts[0]
    elif host in _WATCH_HOSTS and parts == ["watch"]:
        candidate = (parse_qs(parsed.query).get("v") or [None])[0]
    elif host in _WATCH_HOSTS and len(parts) == 2 and parts[0] == "shorts":
        candidate = parts[1]
    return candidate if candidate and _VIDEO_ID.match(candidate) else None


def _resolve_id(body: QueueBody) -> str:
    if (body.video_id is None) == (body.url is None):
        raise HTTPException(400, "give exactly one of video_id or url")
    if body.url is not None:
        video_id = parse_video_id(body.url)
        if video_id is None:
            raise HTTPException(400, NOT_YOUTUBE)
        return video_id
    if not _VIDEO_ID.match(body.video_id or ""):
        raise HTTPException(404, "video not found")
    return body.video_id  # type: ignore[return-value]


def _auth_failed(conn: sqlite3.Connection, exc: SourceAuthError) -> HTTPException:
    """Record the cookie/bot-check failure (so /api/status says "expired") and build a 503."""
    logger.warning("YouTube auth failure: %s", exc)
    log_event(conn, "ytdlp", "error", AUTH_EVENT_MESSAGE)
    return HTTPException(503, COOKIES_EXPIRED)


def fetch_videos(conn: sqlite3.Connection, deps: Deps, ids: list[str]) -> list[Video]:
    """Catalog lookup; ids that cannot be fetched are dropped."""
    try:
        return ensure_videos(conn, ids, deps.data_api or _NoDataApi(), deps.ytdlp)
    except SourceAuthError as exc:
        raise _auth_failed(conn, exc) from None


def _search_ids(conn: sqlite3.Connection, deps: Deps, q: str) -> list[str]:
    if deps.data_api is not None:
        try:
            return deps.data_api.search(q, max_results=SEARCH_RESULTS)
        except QuotaExceeded:
            logger.warning("Data API quota exhausted; searching with yt-dlp")
    try:
        return deps.ytdlp.search(q, n=SEARCH_RESULTS)
    except SourceAuthError as exc:
        raise _auth_failed(conn, exc) from None
    except SourceError as exc:
        logger.warning("yt-dlp search failed: %s", exc)
        raise HTTPException(502, "search failed") from None


def build_router(deps: Deps) -> APIRouter:
    router = APIRouter()

    def items_out(conn: sqlite3.Connection) -> list[dict]:
        return [queue_item_out(i) for i in queue.upcoming(conn)]

    @router.get("/queue")
    def get_queue():
        with closing(deps.db_factory()) as conn:
            return ok({"items": items_out(conn), "remaining_s": queue.remaining_seconds(conn)})

    @router.post("/queue")
    def add_to_queue(body: QueueBody):
        video_id = _resolve_id(body)
        with closing(deps.db_factory()) as conn:
            if not fetch_videos(conn, deps, [video_id]):
                raise HTTPException(404, "video not found")
            item_id = queue.enqueue(conn, video_id, "user", where=body.where)
            item = next(i for i in queue.upcoming(conn) if i.id == item_id)
            return ok(queue_item_out(item), 201)

    @router.delete("/queue/{id}")
    def remove_item(id: int):  # noqa: A002 - path name is part of the contract
        with closing(deps.db_factory()) as conn:
            if id not in {i.id for i in queue.upcoming(conn)}:
                raise HTTPException(404, "queue item not found")
            queue.remove(conn, id)
            return ok({"removed": 1})

    @router.delete("/queue")
    def clear_queue():
        with closing(deps.db_factory()) as conn:
            count = len(queue.upcoming(conn))
            queue.clear(conn)
            return ok({"removed": count})

    @router.put("/queue/order")
    def reorder(body: OrderBody):
        with closing(deps.db_factory()) as conn:
            try:
                queue.reorder(conn, body.ids)
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from None
            return ok({"items": items_out(conn)})

    @router.get("/search")
    def search(q: str = ""):
        query = q.strip()
        if not query:
            raise HTTPException(400, "q must not be empty")
        if len(query) > 200:
            raise HTTPException(400, "q must be at most 200 characters")
        with closing(deps.db_factory()) as conn:
            ids = _search_ids(conn, deps, query)
            return ok({"results": [video_out(v) for v in fetch_videos(conn, deps, ids)]})

    return router
