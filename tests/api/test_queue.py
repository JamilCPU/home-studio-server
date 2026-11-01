"""Tests for /api/queue and /api/search."""

from __future__ import annotations

import pytest

from src.sources.ytdlp import SourceAuthError
from src.store import queue
from src.store.settings import latest_event
from tests.api.conftest import make_video

VID = "dQw4w9WgXcQ"


def _queue_ids(api) -> list[str]:
    return [i["video_id"] for i in api.client.get("/api/queue").json()["data"]["items"]]


def test_get_queue(api):
    for vid in ("aaaaaaaaaaa", "bbbbbbbbbbb"):
        api.stored(make_video(vid, duration_s=100))
        with api.conn() as conn:
            queue.enqueue(conn, vid, "agent", reason="fits your taste")
    resp = api.client.get("/api/queue")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["remaining_s"] == 200
    assert [i["video_id"] for i in data["items"]] == ["aaaaaaaaaaa", "bbbbbbbbbbb"]
    assert data["items"][0]["reason"] == "fits your taste"
    assert data["items"][0]["source"] == "agent"


def test_post_queue_by_video_id_fetches_and_enqueues(api):
    api.catalog.add(make_video(VID))
    resp = api.client.post("/api/queue", json={"video_id": VID, "where": "end"})
    assert resp.status_code == 201
    body = resp.json()
    assert body["ok"] is True
    assert body["data"]["video_id"] == VID
    assert body["data"]["source"] == "user"
    assert isinstance(body["data"]["id"], int)
    assert _queue_ids(api) == [VID]


@pytest.mark.parametrize(
    "url",
    [
        f"https://www.youtube.com/watch?v={VID}&t=30s",
        f"https://youtu.be/{VID}?si=abc",
        f"https://www.youtube.com/shorts/{VID}",
        f"youtube.com/watch?v={VID}",
        f"https://m.youtube.com/watch?v={VID}",
    ],
)
def test_post_queue_by_url(api, url):
    api.catalog.add(make_video(VID))
    resp = api.client.post("/api/queue", json={"url": url, "where": "next"})
    assert resp.status_code == 201
    assert resp.json()["data"]["video_id"] == VID


def test_post_queue_next_goes_first(api):
    for vid in ("aaaaaaaaaaa", VID):
        api.catalog.add(make_video(vid))
    api.client.post("/api/queue", json={"video_id": "aaaaaaaaaaa", "where": "end"})
    api.client.post("/api/queue", json={"video_id": VID, "where": "next"})
    assert _queue_ids(api) == [VID, "aaaaaaaaaaa"]


@pytest.mark.parametrize(
    "url",
    ["https://vimeo.com/12345", "https://www.youtube.com/channel/UCabc",
     "https://www.youtube.com/watch?v=short", "not a url"],
)
def test_queue_post_rejects_non_youtube_url(api, url):
    resp = api.client.post("/api/queue", json={"url": url, "where": "end"})
    assert resp.status_code == 400
    assert resp.json() == {"ok": False, "data": None, "error": "not a YouTube video link"}


@pytest.mark.parametrize("body", [{"where": "end"},
                                  {"video_id": VID, "url": f"https://youtu.be/{VID}",
                                   "where": "end"}])
def test_post_queue_needs_exactly_one_of_id_or_url(api, body):
    resp = api.client.post("/api/queue", json=body)
    assert resp.status_code == 400
    assert resp.json()["ok"] is False


def test_post_queue_unknown_video_404(api):
    resp = api.client.post("/api/queue", json={"video_id": VID, "where": "end"})
    assert resp.status_code == 404
    assert resp.json()["error"] == "video not found"


def test_post_queue_bad_where_422(api):
    resp = api.client.post("/api/queue", json={"video_id": VID, "where": "middle"})
    assert resp.status_code == 422
    assert "where" in resp.json()["error"]


def test_post_queue_without_data_api_uses_ytdlp(api_no_data_api):
    api_no_data_api.catalog.add(make_video(VID))
    resp = api_no_data_api.client.post("/api/queue", json={"video_id": VID, "where": "end"})
    assert resp.status_code == 201


def test_post_queue_auth_error_logs_event_and_503(api):
    api.data_api.quota_exceeded = True

    def boom(video_id):
        raise SourceAuthError("Sign in to confirm you're not a bot")

    api.ytdlp.metadata = boom
    resp = api.client.post("/api/queue", json={"video_id": VID, "where": "end"})
    assert resp.status_code == 503
    assert resp.json()["ok"] is False
    with api.conn() as conn:
        level, message, _ = latest_event(conn, "ytdlp")
    assert level == "error" and message.startswith("SourceAuthError")


def test_delete_queue_item(api):
    api.stored(make_video("aaaaaaaaaaa"))
    with api.conn() as conn:
        item_id = queue.enqueue(conn, "aaaaaaaaaaa", "user")
    resp = api.client.delete(f"/api/queue/{item_id}")
    assert resp.status_code == 200
    assert resp.json()["data"] == {"removed": 1}
    assert _queue_ids(api) == []


def test_delete_missing_queue_item_404(api):
    resp = api.client.delete("/api/queue/999")
    assert resp.status_code == 404
    assert resp.json()["ok"] is False


def test_clear_queue(api):
    for vid in ("aaaaaaaaaaa", "bbbbbbbbbbb"):
        api.stored(make_video(vid))
        with api.conn() as conn:
            queue.enqueue(conn, vid, "user")
    resp = api.client.delete("/api/queue")
    assert resp.status_code == 200
    assert resp.json()["data"] == {"removed": 2}
    assert _queue_ids(api) == []


def test_reorder_queue(api):
    ids = []
    for vid in ("aaaaaaaaaaa", "bbbbbbbbbbb", "ccccccccccc"):
        api.stored(make_video(vid))
        with api.conn() as conn:
            ids.append(queue.enqueue(conn, vid, "user"))
    resp = api.client.put("/api/queue/order", json={"ids": [ids[2], ids[0], ids[1]]})
    assert resp.status_code == 200
    assert [i["id"] for i in resp.json()["data"]["items"]] == [ids[2], ids[0], ids[1]]


def test_reorder_mismatch_400(api):
    resp = api.client.put("/api/queue/order", json={"ids": [1, 2]})
    assert resp.status_code == 400
    assert resp.json()["ok"] is False


def test_search_uses_data_api(api):
    api.catalog.add(make_video("aaaaaaaaaaa"))
    api.catalog.add(make_video("bbbbbbbbbbb", duration_s=None))
    api.data_api.search_results = ["aaaaaaaaaaa", "bbbbbbbbbbb"]
    resp = api.client.get("/api/search", params={"q": "rust async"})
    assert resp.status_code == 200
    results = resp.json()["data"]["results"]
    assert [r["video_id"] for r in results] == ["aaaaaaaaaaa", "bbbbbbbbbbb"]
    assert set(results[0]) == {"video_id", "title", "channel", "thumbnail_url", "duration_s",
                               "chapters"}
    assert results[1]["duration_s"] is None
    assert api.data_api.searches == ["rust async"]
    assert api.ytdlp.searches == []


def test_search_falls_back_to_ytdlp_on_quota(api):
    api.catalog.add(make_video("aaaaaaaaaaa"))
    api.data_api.quota_exceeded = True
    api.ytdlp.search_results = ["aaaaaaaaaaa"]
    results = api.client.get("/api/search", params={"q": "x"}).json()["data"]["results"]
    assert [r["video_id"] for r in results] == ["aaaaaaaaaaa"]
    assert api.ytdlp.searches == ["x"]


def test_search_without_data_api_uses_ytdlp(api_no_data_api):
    api_no_data_api.catalog.add(make_video("aaaaaaaaaaa"))
    api_no_data_api.ytdlp.search_results = ["aaaaaaaaaaa"]
    resp = api_no_data_api.client.get("/api/search", params={"q": "x"})
    assert [r["video_id"] for r in resp.json()["data"]["results"]] == ["aaaaaaaaaaa"]


@pytest.mark.parametrize("params", [{"q": ""}, {"q": "   "}, {}])
def test_search_empty_query_400(api, params):
    resp = api.client.get("/api/search", params=params)
    assert resp.status_code == 400
    assert resp.json()["ok"] is False
