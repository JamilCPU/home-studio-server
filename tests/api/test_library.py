"""Tests for ratings, history and taste endpoints."""

from __future__ import annotations

from src.store.agent import latest_taste, pending_corrections, save_taste
from src.store.videos import end_play, start_play
from tests.api.conftest import make_video

VID = "aaaaaaaaaaa"


def _played(api, vid=VID, seconds=120.0):
    api.stored(make_video(vid))
    with api.conn() as conn:
        play_id = start_play(conn, vid, None)
        end_play(conn, play_id, seconds, "finished")
    return play_id


def test_post_rating(api):
    api.stored(make_video(VID))
    resp = api.client.post("/api/ratings", json={"video_id": VID, "liked": True,
                                                 "satisfaction": 8, "notes": "great"})
    assert resp.status_code == 201
    assert resp.json()["ok"] is True
    assert isinstance(resp.json()["data"]["id"], int)


def test_post_rating_notes_only(api):
    api.stored(make_video(VID))
    resp = api.client.post("/api/ratings", json={"video_id": VID, "notes": "too long"})
    assert resp.status_code == 201


def test_post_rating_empty_is_422(api):
    api.stored(make_video(VID))
    resp = api.client.post("/api/ratings", json={"video_id": VID})
    assert resp.status_code == 422
    assert resp.json()["ok"] is False


def test_post_rating_unknown_video_404(api):
    resp = api.client.post("/api/ratings", json={"video_id": VID, "liked": False})
    assert resp.status_code == 404
    assert resp.json()["error"] == "video not found"


def test_envelope_on_validation_error(api):
    api.stored(make_video(VID))
    resp = api.client.post("/api/ratings", json={"video_id": VID, "satisfaction": 11})
    assert resp.status_code == 422
    body = resp.json()
    assert body["ok"] is False
    assert body["data"] is None
    assert "satisfaction" in body["error"]


def test_notes_too_long_422(api):
    resp = api.client.post("/api/ratings", json={"video_id": VID, "notes": "x" * 2001})
    assert resp.status_code == 422
    assert "notes" in resp.json()["error"]


def test_history_shows_latest_rating(api):
    play_id = _played(api)
    api.client.post("/api/ratings", json={"video_id": VID, "liked": False})
    api.client.post("/api/ratings", json={"video_id": VID, "satisfaction": 9, "notes": "ok"})
    resp = api.client.get("/api/history")
    assert resp.status_code == 200
    entries = resp.json()["data"]["entries"]
    assert len(entries) == 1
    entry = entries[0]
    assert set(entry) == {"play_id", "video", "started_at", "seconds_listened", "end_reason",
                          "source", "rating"}
    assert entry["play_id"] == play_id
    assert entry["video"]["video_id"] == VID
    assert entry["seconds_listened"] == 120.0
    assert entry["end_reason"] == "finished"
    assert entry["source"] is None
    assert set(entry["rating"]) == {"liked", "satisfaction", "notes", "created_at"}
    assert entry["rating"]["satisfaction"] == 9
    assert entry["rating"]["liked"] is None


def test_history_unrated_and_paging(api):
    _played(api, "aaaaaaaaaaa")
    _played(api, "bbbbbbbbbbb")
    _played(api, "ccccccccccc")
    api.client.post("/api/ratings", json={"video_id": "bbbbbbbbbbb", "liked": True})
    unrated = api.client.get("/api/history", params={"unrated": "true"}).json()["data"]["entries"]
    assert [e["video"]["video_id"] for e in unrated] == ["ccccccccccc", "aaaaaaaaaaa"]
    assert unrated[0]["rating"] is None
    page = api.client.get("/api/history", params={"limit": 1, "offset": 1}).json()["data"]
    assert [e["video"]["video_id"] for e in page["entries"]] == ["bbbbbbbbbbb"]


def test_history_limit_validated(api):
    assert api.client.get("/api/history", params={"limit": 0}).status_code == 422


def test_get_taste_null_when_none(api):
    resp = api.client.get("/api/taste")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "data": None, "error": None}


def test_get_taste(api):
    with api.conn() as conn:
        save_taste(conn, "Likes deep dives", ["long-form"], ["clickbait"], "agent")
    data = api.client.get("/api/taste").json()["data"]
    assert set(data) == {"summary", "likes", "dislikes", "updated_at"}
    assert data["summary"] == "Likes deep dives"
    assert data["likes"] == ["long-form"]
    assert data["dislikes"] == ["clickbait"]


def test_post_correction_triggers_refresh(api):
    resp = api.client.post("/api/taste/corrections", json={"text": "less news please"})
    assert resp.status_code == 202
    assert isinstance(resp.json()["data"]["id"], int)
    assert api.worker.refreshes == 1
    with api.conn() as conn:
        assert [t for _, t in pending_corrections(conn)] == ["less news please"]


def test_post_correction_too_long_422(api):
    resp = api.client.post("/api/taste/corrections", json={"text": "x" * 1001})
    assert resp.status_code == 422
    assert api.worker.refreshes == 0


def test_delete_trait(api):
    with api.conn() as conn:
        save_taste(conn, "s", ["long-form", "hardware"], ["clickbait"], "agent")
    resp = api.client.request("DELETE", "/api/taste/traits",
                              json={"kind": "likes", "text": "hardware"})
    assert resp.status_code == 200
    assert resp.json()["data"]["likes"] == ["long-form"]
    with api.conn() as conn:
        assert latest_taste(conn).source == "user"


def test_delete_missing_trait_404(api):
    with api.conn() as conn:
        save_taste(conn, "s", ["long-form"], [], "agent")
    resp = api.client.request("DELETE", "/api/taste/traits",
                              json={"kind": "dislikes", "text": "nope"})
    assert resp.status_code == 404
    assert resp.json()["ok"] is False
