"""Tests for the OAuth account fetchers (subscriptions, likes, playlists)."""

from __future__ import annotations

import pytest

from src.sources.account_api import AccountApi
from src.sources.data_api import QuotaExceeded
from src.sources.ytdlp import FeedItem


class FakeOAuth:
    """Answers ``call(make_request)`` from pages keyed by (resource, pageToken/id)."""

    def __init__(self, pages: dict, quota_after: int | None = None) -> None:
        self.pages = pages
        self.requests: list[tuple[str, dict]] = []
        self.quota_after = quota_after

    def call(self, make_request, units: int = 1) -> dict:
        if self.quota_after is not None and len(self.requests) >= self.quota_after:
            raise QuotaExceeded("over")
        return make_request(self)

    def __getattr__(self, resource):
        if resource.startswith("_"):
            raise AttributeError(resource)
        oauth = self

        class _Res:
            def list(self, **kw):
                oauth.requests.append((resource, kw))
                key = (resource, kw.get("pageToken") or kw.get("playlistId"))
                return oauth.pages.get(key) or oauth.pages.get((resource, None), {})

        return lambda: _Res()


def sub(channel_id, title):
    return {"snippet": {"title": title, "resourceId": {"channelId": channel_id}}}


def video(video_id, title, channel="UCv"):
    return {"id": video_id, "snippet": {"title": title, "channelId": channel,
                                        "channelTitle": "Chan"}}


def item(video_id, title, owner="UCo"):
    return {"snippet": {"title": title, "resourceId": {"videoId": video_id},
                        "videoOwnerChannelId": owner, "videoOwnerChannelTitle": "Owner"}}


def test_subscriptions_pages_until_done():
    oauth = FakeOAuth({
        ("subscriptions", None): {"items": [sub("UCa", "A")], "nextPageToken": "p2"},
        ("subscriptions", "p2"): {"items": [sub("UCb", "B")]},
    })
    assert AccountApi(oauth).subscriptions() == [
        FeedItem("", "A", "UCa", "A"), FeedItem("", "B", "UCb", "B"),
    ]
    kw = oauth.requests[0][1]
    assert kw["mine"] is True and kw["part"] == "snippet" and kw["maxResults"] == 50
    assert oauth.requests[1][1]["pageToken"] == "p2"


def test_liked_uses_my_rating_and_limit():
    oauth = FakeOAuth({
        ("videos", None): {"items": [video("v1", "One"), video("v2", "Two")],
                           "nextPageToken": "more"},
    })
    assert AccountApi(oauth).liked(limit=2) == [
        FeedItem("v1", "One", "UCv", "Chan"), FeedItem("v2", "Two", "UCv", "Chan"),
    ]
    assert len(oauth.requests) == 1  # limit reached: no second page
    assert oauth.requests[0][1]["myRating"] == "like"


def test_playlist_items_across_my_playlists_skip_deleted():
    oauth = FakeOAuth({
        ("playlists", None): {"items": [{"id": "PL1"}, {"id": "PL2"}]},
        ("playlistItems", "PL1"): {"items": [item("v1", "One"), item("", "Gone"),
                                             item("v9", "Deleted video")]},
        ("playlistItems", "PL2"): {"items": [item("v2", "Two"), item("x", "Private video")]},
    })
    assert AccountApi(oauth).playlist_items() == [
        FeedItem("v1", "One", "UCo", "Owner"), FeedItem("v2", "Two", "UCo", "Owner"),
    ]
    assert oauth.requests[0][1]["mine"] is True


def test_quota_exceeded_keeps_what_was_fetched(caplog):
    oauth = FakeOAuth({
        ("subscriptions", None): {"items": [sub("UCa", "A")], "nextPageToken": "p2"},
    }, quota_after=1)
    assert AccountApi(oauth).subscriptions() == [FeedItem("", "A", "UCa", "A")]
    assert "quota" in caplog.text.lower()


def test_quota_exceeded_on_first_page_returns_empty():
    oauth = FakeOAuth({}, quota_after=0)
    assert AccountApi(oauth).liked() == []


@pytest.mark.parametrize("method", ["subscriptions", "liked", "playlist_items"])
def test_page_cap_stops_runaway_paging(method):
    page = {"items": [], "nextPageToken": "again"}
    oauth = FakeOAuth({(r, None): page for r in ("subscriptions", "videos", "playlists")}
                      | {(r, "again"): page for r in ("subscriptions", "videos", "playlists")})
    getattr(AccountApi(oauth, max_pages=3), method)()
    assert len(oauth.requests) <= 3
