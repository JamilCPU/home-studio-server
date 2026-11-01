"""yt-dlp source: account feeds, metadata (with chapters), transcripts and search.

Uses the account cookies file when present. Calls are paced, never mark videos
watched, and cookie/bot-check failures surface as ``SourceAuthError``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

import yt_dlp
from yt_dlp.utils import DownloadError, ExtractorError

from src.store.videos import Chapter, Video

logger = logging.getLogger(__name__)

FeedKind = Literal["liked", "history", "recommended", "subscriptions", "watch_later"]

FEED_URLS: dict[str, str] = {
    "liked": ":ytfav",
    "history": ":ythistory",
    "recommended": ":ytrec",
    "subscriptions": ":ytsubs",
    "watch_later": ":ytwatchlater",
}
CHANNELS_URL = "https://www.youtube.com/feed/channels"
WATCH_URL = "https://www.youtube.com/watch?v={}"
_AUTH_MARKERS = ("sign in to confirm", "cookies", "login", "sign in")


class SourceError(Exception):
    """A yt-dlp call failed."""


class SourceAuthError(SourceError):
    """Cookies are missing/expired or YouTube asked to confirm the user is not a bot."""


@dataclass(frozen=True)
class FeedItem:
    video_id: str
    title: str
    channel_id: str | None
    channel_name: str | None


def _iso_date(upload_date: str | None) -> str | None:
    if upload_date and re.fullmatch(r"\d{8}", upload_date):
        return f"{upload_date[:4]}-{upload_date[4:6]}-{upload_date[6:]}"
    return upload_date or None


def _pick_track(tracks: dict[str, list[dict]] | None, lang: str) -> str | None:
    """URL of the best json3 track for `lang` (exact match first, then a lang prefix)."""
    if not tracks:
        return None
    keys = [k for k in tracks if k == lang] + [
        k for k in tracks if k != lang and k.startswith(lang)
    ]
    for key in keys:
        for fmt in tracks[key]:
            if fmt.get("ext") == "json3" and fmt.get("url"):
                return fmt["url"]
    return None


def _parse_json3(raw: bytes | str) -> str:
    data = json.loads(raw)
    parts = [
        seg.get("utf8", "")
        for event in data.get("events", [])
        for seg in event.get("segs") or []
    ]
    return re.sub(r"\s+", " ", "".join(parts)).strip()


class YtDlp:
    def __init__(
        self,
        cookies_path: str,
        min_interval_s: float = 2.0,
        ydl_factory: Callable[[dict], Any] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._cookies_path = cookies_path
        self._min_interval_s = min_interval_s
        self._factory = ydl_factory or yt_dlp.YoutubeDL
        self._sleep = sleep
        self._clock = clock
        self._last_call: float | None = None

    # -- public API ---------------------------------------------------------------

    def account_feed(self, kind: FeedKind, limit: int = 200) -> list[FeedItem]:
        info = self._extract(FEED_URLS[kind], self._flat_opts(limit))
        return self._entries(info)

    def subscribed_channels(self) -> list[FeedItem]:
        info = self._extract(CHANNELS_URL, self._flat_opts(None))
        return [
            FeedItem("", e.get("title") or "", e.get("channel_id") or e.get("id"), e.get("title"))
            for e in (info or {}).get("entries") or []
            if e
        ]

    def metadata(self, video_id: str) -> Video:
        info = self._extract(WATCH_URL.format(video_id), self._opts())
        categories = info.get("categories") or []
        return Video(
            video_id=info.get("id") or video_id,
            title=info.get("title") or "",
            channel_name=info.get("channel") or info.get("uploader"),
            channel_id=info.get("channel_id"),
            description=info.get("description"),
            duration_s=int(info["duration"]) if info.get("duration") is not None else None,
            published_at=_iso_date(info.get("upload_date")),
            tags=tuple(info.get("tags") or ()),
            category=categories[0] if categories else None,
            chapters=tuple(
                Chapter(title=c.get("title") or "", start_s=float(c.get("start_time") or 0))
                for c in info.get("chapters") or []
            ),
            thumbnail_url=info.get("thumbnail"),
            view_count=info.get("view_count"),
            like_count=info.get("like_count"),
            transcript=None,
        )

    def transcript(self, video_id: str, lang: str = "en") -> str | None:
        def work(ydl: Any) -> str | None:
            info = ydl.extract_info(WATCH_URL.format(video_id), download=False) or {}
            url = _pick_track(info.get("subtitles"), lang) or _pick_track(
                info.get("automatic_captions"), lang
            )
            if url is None:
                return None
            text = _parse_json3(ydl.urlopen(url).read())
            return text or None

        return self._call(work, self._opts())

    def search(self, query: str, n: int = 20) -> list[str]:
        info = self._extract(f"ytsearch{n}:{query}", self._flat_opts(None))
        return [i.video_id for i in self._entries(info)]

    # -- internals ----------------------------------------------------------------

    def _opts(self) -> dict:
        opts: dict[str, Any] = {"quiet": True, "no_warnings": True, "skip_download": True}
        if self._cookies_path and os.path.exists(self._cookies_path):
            opts["cookiefile"] = self._cookies_path
        return opts

    def _flat_opts(self, limit: int | None) -> dict:
        opts = {**self._opts(), "extract_flat": "in_playlist"}
        if limit is not None:
            opts["playlistend"] = limit
        return opts

    @staticmethod
    def _entries(info: dict | None) -> list[FeedItem]:
        return [
            FeedItem(
                video_id=e["id"],
                title=e.get("title") or "",
                channel_id=e.get("channel_id"),
                channel_name=e.get("channel") or e.get("uploader"),
            )
            for e in (info or {}).get("entries") or []
            if e and e.get("id")
        ]

    def _extract(self, url: str, opts: dict) -> dict:
        info = self._call(lambda ydl: ydl.extract_info(url, download=False), opts)
        return info or {}

    def _pace(self) -> None:
        if self._last_call is not None:
            wait = self._min_interval_s - (self._clock() - self._last_call)
            if wait > 0:
                self._sleep(wait)
        self._last_call = self._clock()

    def _call(self, work: Callable[[Any], Any], opts: dict) -> Any:
        self._pace()
        try:
            with self._factory(opts) as ydl:
                return work(ydl)
        except (DownloadError, ExtractorError) as exc:
            message = str(exc)
            lowered = message.lower()
            if any(marker in lowered for marker in _AUTH_MARKERS):
                logger.warning("yt-dlp needs authentication")
                raise SourceAuthError(message) from exc
            raise SourceError(message) from exc
        except (OSError, ValueError) as exc:
            raise SourceError(str(exc)) from exc
