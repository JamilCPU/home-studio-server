"""Tests for ratings, history and taste endpoints."""

from __future__ import annotations

import pytest

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


def _set_started_at(api, play_id, started_at):
    with api.conn() as conn:
        conn.execute("UPDATE plays SET started_at=? WHERE id=?", (started_at, play_id))
        conn.commit()


def test_history_since_query(api):
    old = _played(api, "aaaaaaaaaaa")
    new = _played(api, "bbbbbbbbbbb")
    _set_started_at(api, old, "2026-10-01T00:00:00+00:00")
    _set_started_at(api, new, "2026-11-01T12:00:00+00:00")
    resp = api.client.get("/api/history", params={"since": "2026-11-01T00:00:00Z"})
    assert resp.status_code == 200
    assert [e["video"]["video_id"] for e in resp.json()["data"]["entries"]] == ["bbbbbbbbbbb"]
    offset = api.client.get("/api/history", params={"since": "2026-11-01T02:00:00+02:00"})
    assert [e["play_id"] for e in offset.json()["data"]["entries"]] == [new]
    naive = api.client.get("/api/history", params={"since": "2026-11-01T13:00:00"})
    assert naive.json()["data"]["entries"] == []


def test_history_bad_since_422(api):
    resp = api.client.get("/api/history", params={"since": "yesterday"})
    assert resp.status_code == 422
    body = resp.json()
    assert body["ok"] is False
    assert "since" in body["error"]


def test_get_ratings_shape(api):
    api.stored(make_video(VID))
    api.client.post("/api/ratings", json={"video_id": VID, "liked": True,
                                          "satisfaction": 8, "notes": "great"})
    resp = api.client.get("/api/ratings")
    assert resp.status_code == 200
    ratings = resp.json()["data"]["ratings"]
    assert len(ratings) == 1
    assert set(ratings[0]) == {"id", "video_id", "title", "channel", "liked", "satisfaction",
                               "notes", "created_at"}
    assert ratings[0]["video_id"] == VID
    assert ratings[0]["title"] == f"Title {VID}"
    assert ratings[0]["channel"] == "Some Channel"
    assert ratings[0]["liked"] is True
    assert ratings[0]["satisfaction"] == 8
    assert ratings[0]["notes"] == "great"


def test_get_ratings_since_and_limit(api):
    api.stored(make_video(VID))
    ids = [api.client.post("/api/ratings", json={"video_id": VID, "satisfaction": n})
           .json()["data"]["id"] for n in (3, 4, 5)]
    with api.conn() as conn:
        for rating_id, stamp in zip(ids, ("2026-10-01T00:00:00+00:00",
                                          "2026-11-01T10:00:00+00:00",
                                          "2026-11-01T11:00:00+00:00")):
            conn.execute("UPDATE ratings SET created_at=? WHERE id=?", (stamp, rating_id))
        conn.commit()
    since = {"since": "2026-11-01T00:00:00Z"}
    got = api.client.get("/api/ratings", params=since).json()["data"]["ratings"]
    assert [r["id"] for r in got] == [ids[2], ids[1]]
    one = api.client.get("/api/ratings", params={**since, "limit": 1}).json()["data"]["ratings"]
    assert [r["id"] for r in one] == [ids[2]]
    assert api.client.get("/api/ratings", params={"since": "nope"}).status_code == 422


@pytest.mark.parametrize("limit", [0, 201])
def test_get_ratings_limit_out_of_range_422(api, limit):
    resp = api.client.get("/api/ratings", params={"limit": limit})
    assert resp.status_code == 422
    assert "limit" in resp.json()["error"]


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
