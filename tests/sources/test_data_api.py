"""Tests for the YouTube Data API source."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.sources.data_api import DataApi, QuotaExceeded
from src.store import settings

NOW = datetime(2026, 11, 2, 3, 0, tzinfo=timezone.utc)  # 20:00 on Nov 1 Pacific
DAY = "2026-11-01"


class _Req:
    def __init__(self, result, log, name, kw):
        self._result, self._log = result, log
        log.append((name, kw))

    def execute(self):
        return self._result


class _Resource:
    def __init__(self, name, svc):
        self._name, self._svc = name, svc

    def list(self, **kw):
        return _Req(self._svc.responder(self._name, kw), self._svc.calls, self._name, kw)


class FakeService:
    def __init__(self, responder):
        self.responder = responder
        self.calls = []

    def search(self):
        return _Resource("search", self)

    def videos(self):
        return _Resource("videos", self)

    def channels(self):
        return _Resource("channels", self)

    def playlistItems(self):
        return _Resource("playlistItems", self)


def _api(db, service, ceiling=9500):
    """DataApi over the test database; each call gets its own connection, as in serve."""
    from src.db import connect

    path = db.execute("PRAGMA database_list").fetchone()[2]
    return DataApi(
        "key", lambda: connect(path), ceiling=ceiling, service=service, clock=lambda: NOW
    )


def test_search_records_100_units(db):
    svc = FakeService(
        lambda n, kw: {"items": [{"id": {"videoId": "a"}}, {"id": {"videoId": "b"}}]}
    )
    assert _api(db, svc).search("ai news", max_results=2) == ["a", "b"]
    assert settings.quota_used(db, DAY) == 100
    kw = svc.calls[0][1]
    assert kw["type"] == "video" and kw["part"] == "id" and kw["maxResults"] == 2


def test_search_raises_when_over_ceiling(db):
    settings.add_quota(db, DAY, 9450)
    svc = FakeService(lambda n, kw: {"items": []})
    with pytest.raises(QuotaExceeded):
        _api(db, svc).search("x")
    assert svc.calls == []
    assert settings.quota_used(db, DAY) == 9450


def test_video_details_batches_by_50(db):
    svc = FakeService(lambda n, kw: {"items": []})
    _api(db, svc).video_details([f"id{i}" for i in range(120)])
    assert len(svc.calls) == 3
    assert settings.quota_used(db, DAY) == 3
    assert svc.calls[0][1]["id"].count(",") == 49


def test_details_maps_duration_and_counts(db):
    items = [
        {
            "id": "b",
            "snippet": {
                "title": "T",
                "channelTitle": "C",
                "channelId": "cid",
                "description": "d",
                "publishedAt": "2026-01-01T00:00:00Z",
                "tags": ["x", "y"],
                "categoryId": "28",
                "thumbnails": {"default": {"url": "lo"}, "high": {"url": "hi"}},
            },
            "contentDetails": {"duration": "PT1H2M3S"},
            "statistics": {"viewCount": "1234", "likeCount": "56"},
        },
        {
            "id": "a",
            "snippet": {"title": "A", "thumbnails": {"medium": {"url": "mid"}}},
            "contentDetails": {"duration": "PT45S"},
            "statistics": {},
        },
    ]
    svc = FakeService(lambda n, kw: {"items": items})
    vids = _api(db, svc).video_details(["a", "missing", "b"])
    assert [v.video_id for v in vids] == ["a", "b"]
    a, b = vids
    assert b.duration_s == 3723 and b.view_count == 1234 and b.like_count == 56
    assert b.tags == ("x", "y") and b.category == "28" and b.thumbnail_url == "hi"
    assert b.channel_name == "C" and b.channel_id == "cid" and b.chapters == ()
    assert b.transcript is None
    assert a.thumbnail_url == "mid" and a.view_count is None and a.like_count is None
    assert a.duration_s == 45


def test_video_details_over_ceiling(db):
    settings.add_quota(db, DAY, 9500)
    with pytest.raises(QuotaExceeded):
        _api(db, FakeService(lambda n, kw: {"items": []})).video_details(["a"])


def test_channel_uploads_costs_two_units(db):
    def responder(name, kw):
        if name == "channels":
            return {"items": [{"contentDetails": {"relatedPlaylists": {"uploads": "UU1"}}}]}
        return {"items": [{"contentDetails": {"videoId": "v1"}}, {"contentDetails": {"videoId": "v2"}}]}

    svc = FakeService(responder)
    assert _api(db, svc).channel_uploads("c1", max_results=5) == ["v1", "v2"]
    assert settings.quota_used(db, DAY) == 2
    assert svc.calls[1][1]["playlistId"] == "UU1" and svc.calls[1][1]["maxResults"] == 5


def test_channel_uploads_unknown_channel(db):
    svc = FakeService(lambda n, kw: {"items": []})
    assert _api(db, svc).channel_uploads("nope") == []


def test_concurrent_calls_never_overlap_execute_or_overspend(db):
    import threading
    import time

    active, peak = [0], [0]
    guard = threading.Lock()

    def responder(name, kw):
        return {"items": [{"id": {"videoId": "a"}}]}

    class SlowReq:
        def execute(self):
            with guard:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
            time.sleep(0.01)
            with guard:
                active[0] -= 1
            return responder("search", {})

    class Svc(FakeService):
        def search(self):
            return type("R", (), {"list": lambda self, **kw: SlowReq()})()

    api = _api(db, Svc(responder), ceiling=500)
    results = []

    def worker():
        try:
            results.append(api.search("q"))
        except QuotaExceeded:
            results.append("quota")

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert peak[0] == 1
    assert results.count("quota") == 3
    assert settings.quota_used(db, DAY) == 500
