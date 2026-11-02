"""The signed-in account's subscriptions, likes and playlists through the Data API (OAuth).

Every page is one ``list`` call (1 quota unit) through ``YouTubeOAuth.call``, so the shared
quota ceiling and execute lock apply. Hitting the ceiling keeps what was fetched so far.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from typing import Any

from src.sources.data_api import QuotaExceeded
from src.sources.ytdlp import FeedItem

logger = logging.getLogger(__name__)

PAGE_SIZE = 50
MAX_PAGES = 40  # per listing; a guard against runaway paging
SUBSCRIPTION_LIMIT = 1000
LIKED_LIMIT = 200
PLAYLIST_LIMIT = 50
PLAYLIST_ITEM_LIMIT = 500
_UNAVAILABLE_TITLES = frozenset({"Deleted video", "Private video"})


class AccountApi:
    def __init__(self, oauth: Any, max_pages: int = MAX_PAGES) -> None:
        self._oauth = oauth
        self._max_pages = max_pages

    def connected(self) -> bool:
        return self._oauth.state() == "connected"

    def _pages(self, what: str, make: Callable[[Any, str | None], Any]) -> Iterator[dict]:
        token: str | None = None
        for _ in range(self._max_pages):
            try:
                resp = self._oauth.call(lambda yt: make(yt, token))
            except QuotaExceeded:
                logger.warning("quota ceiling reached while importing %s; keeping what we have",
                               what)
                return
            yield resp
            token = resp.get("nextPageToken")
            if not token:
                return

    def _collect(self, what, make, to_item, limit: int) -> list[FeedItem]:
        out: list[FeedItem] = []
        for resp in self._pages(what, make):
            for raw in resp.get("items") or []:
                parsed = to_item(raw)
                if parsed is not None:
                    out.append(parsed)
                if len(out) >= limit:
                    return out
        return out

    def subscriptions(self, limit: int = SUBSCRIPTION_LIMIT) -> list[FeedItem]:
        """Subscribed channels (``video_id`` is empty), like ``YtDlp.subscribed_channels``."""

        def make(yt, token):
            return yt.subscriptions().list(part="snippet", mine=True, maxResults=PAGE_SIZE,
                                           pageToken=token)

        return self._collect("subscriptions", make, _subscription, limit)

    def liked(self, limit: int = LIKED_LIMIT) -> list[FeedItem]:
        def make(yt, token):
            return yt.videos().list(part="snippet", myRating="like", maxResults=PAGE_SIZE,
                                    pageToken=token)

        return self._collect("liked videos", make, _video, limit)

    def playlist_items(self, limit: int = PLAYLIST_ITEM_LIMIT) -> list[FeedItem]:
        """Videos in the account's own playlists (deleted and private entries skipped)."""

        def make_lists(yt, token):
            return yt.playlists().list(part="id", mine=True, maxResults=PAGE_SIZE,
                                       pageToken=token)

        playlist_ids = [
            p["id"] for resp in self._pages("playlists", make_lists)
            for p in resp.get("items") or [] if p.get("id")
        ][:PLAYLIST_LIMIT]
        out: list[FeedItem] = []
        for playlist_id in playlist_ids:
            def make_items(yt, token, pid=playlist_id):
                return yt.playlistItems().list(part="snippet", playlistId=pid,
                                               maxResults=PAGE_SIZE, pageToken=token)

            out.extend(self._collect("playlist items", make_items, _playlist_item,
                                     limit - len(out)))
            if len(out) >= limit:
                break
        return out


def _subscription(raw: dict) -> FeedItem | None:
    snippet = raw.get("snippet") or {}
    channel_id = (snippet.get("resourceId") or {}).get("channelId")
    if not channel_id:
        return None
    title = snippet.get("title") or ""
    return FeedItem("", title, channel_id, title or None)


def _video(raw: dict) -> FeedItem | None:
    snippet = raw.get("snippet") or {}
    if not raw.get("id"):
        return None
    return FeedItem(raw["id"], snippet.get("title") or "", snippet.get("channelId"),
                    snippet.get("channelTitle"))


def _playlist_item(raw: dict) -> FeedItem | None:
    snippet = raw.get("snippet") or {}
    video_id = (snippet.get("resourceId") or {}).get("videoId")
    title = snippet.get("title") or ""
    if not video_id or title in _UNAVAILABLE_TITLES:
        return None
    return FeedItem(video_id, title, snippet.get("videoOwnerChannelId"),
                    snippet.get("videoOwnerChannelTitle"))
