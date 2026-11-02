"""YouTube Data API source with daily quota tracking.

Quota resets at midnight Pacific time, so the quota day is the Pacific date.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from contextlib import closing
from datetime import datetime
from zoneinfo import ZoneInfo

from src.fetcher import _parse_duration
from src.store import settings
from src.store.videos import Video

logger = logging.getLogger(__name__)

SEARCH_COST = 100
LIST_COST = 1
BATCH_SIZE = 50
_QUOTA_TZ = "America/Los_Angeles"


class QuotaExceeded(Exception):
    """Raised when a call would push today's usage past the safety ceiling."""


def _int_or_none(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _thumbnail(snippet: dict) -> str | None:
    thumbs = snippet.get("thumbnails") or {}
    for key in ("high", "medium", "default"):
        url = (thumbs.get(key) or {}).get("url")
        if url:
            return url
    return None


def _to_video(item: dict) -> Video:
    snippet = item.get("snippet") or {}
    stats = item.get("statistics") or {}
    details = item.get("contentDetails") or {}
    duration = details.get("duration")
    return Video(
        video_id=item["id"],
        title=snippet.get("title", ""),
        channel_name=snippet.get("channelTitle"),
        channel_id=snippet.get("channelId"),
        description=snippet.get("description"),
        duration_s=_parse_duration(duration) if duration else None,
        published_at=snippet.get("publishedAt"),
        tags=tuple(snippet.get("tags") or ()),
        category=snippet.get("categoryId"),
        chapters=(),
        thumbnail_url=_thumbnail(snippet),
        view_count=_int_or_none(stats.get("viewCount")),
        like_count=_int_or_none(stats.get("likeCount")),
        transcript=None,
    )


class QuotaMeter:
    """Daily quota spend plus serialized ``.execute()``, shared by every Data API client.

    googleapiclient's HTTP transport is not thread-safe, and the quota check and
    increment must not interleave: one lock covers both.
    """

    def __init__(
        self,
        db_factory: Callable,
        ceiling: int = 9500,
        clock: Callable[[], datetime] | None = None,
        lock: threading.Lock | None = None,
    ) -> None:
        self._db_factory = db_factory
        self._ceiling = ceiling
        self._clock = clock or (lambda: datetime.now(ZoneInfo(_QUOTA_TZ)))
        self.lock = lock or threading.Lock()

    def execute(self, request) -> dict:
        with self.lock:
            return request.execute()

    def today(self) -> str:
        return self._clock().astimezone(ZoneInfo(_QUOTA_TZ)).date().isoformat()

    def spend(self, units: int) -> None:
        """Record units, or raise QuotaExceeded (recording nothing) if over the ceiling."""
        day = self.today()
        with self.lock, closing(self._db_factory()) as conn:
            if settings.quota_used(conn, day) + units > self._ceiling:
                raise QuotaExceeded(f"{units} units would exceed ceiling {self._ceiling}")
            settings.add_quota(conn, day, units)


class DataApi:
    def __init__(
        self,
        api_key: str,
        db_factory: Callable,
        ceiling: int = 9500,
        service=None,
        clock: Callable[[], datetime] | None = None,
        meter: QuotaMeter | None = None,
    ) -> None:
        if service is None:
            from googleapiclient.discovery import build

            service = build("youtube", "v3", developerKey=api_key, cache_discovery=False)
        self._service = service
        self.meter = meter or QuotaMeter(db_factory, ceiling, clock)

    def _execute(self, request) -> dict:
        return self.meter.execute(request)

    def _spend(self, units: int) -> None:
        self.meter.spend(units)

    def search(self, query: str, max_results: int = 25) -> list[str]:
        self._spend(SEARCH_COST)
        resp = self._execute(
            self._service.search().list(part="id", q=query, type="video", maxResults=max_results)
        )
        return [i["id"]["videoId"] for i in resp.get("items", []) if i.get("id", {}).get("videoId")]

    def video_details(self, ids: list[str]) -> list[Video]:
        found: dict[str, Video] = {}
        for start in range(0, len(ids), BATCH_SIZE):
            batch = ids[start : start + BATCH_SIZE]
            self._spend(LIST_COST)
            resp = self._execute(
                self._service.videos().list(
                    part="snippet,contentDetails,statistics", id=",".join(batch)
                )
            )
            for item in resp.get("items", []):
                found[item["id"]] = _to_video(item)
        ordered = dict.fromkeys(ids)
        return [found[i] for i in ordered if i in found]

    def channel_uploads(self, channel_id: str, max_results: int = 25) -> list[str]:
        self._spend(LIST_COST)
        resp = self._execute(self._service.channels().list(part="contentDetails", id=channel_id))
        items = resp.get("items", [])
        if not items:
            return []
        uploads = items[0]["contentDetails"]["relatedPlaylists"]["uploads"]
        self._spend(LIST_COST)
        resp = self._execute(
            self._service.playlistItems().list(
                part="contentDetails", playlistId=uploads, maxResults=max_results
            )
        )
        return [i["contentDetails"]["videoId"] for i in resp.get("items", [])]
