"""Tests for /api/now and /api/player/*."""

from __future__ import annotations

import pytest

from src.player.mpv import MpvCommandError, MpvDied
from src.store import queue
from tests.api.conftest import make_video


def _start_playing(api, video):
    api.stored(video)
    with api.conn() as conn:
        queue.enqueue(conn, video.video_id, "user")
    api.engine.play_next()


def test_now_idle(api):
    resp = api.client.get("/api/now")
    assert resp.status_code == 200
    assert resp.json() == {
        "ok": True,
        "data": {"state": "idle", "item": None, "position_s": 0.0, "duration_s": None,
                 "output_device": "Echo"},
        "error": None,
    }


def test_now_playing_includes_queue_item(api, chaptered_video):
    _start_playing(api, chaptered_video)
    data = api.client.get("/api/now").json()["data"]
    assert data["state"] == "playing"
    assert data["duration_s"] == 300.0
    item = data["item"]
    assert set(item) == {"id", "video_id", "title", "channel", "thumbnail_url", "duration_s",
                         "source", "reason", "chapters"}
    assert item["video_id"] == "abcdefghijk"
    assert item["channel"] == "Some Channel"
    assert item["source"] == "user"
    assert item["chapters"] == [{"title": "Intro", "start_s": 0.0},
                                {"title": "Main", "start_s": 42.5}]


@pytest.mark.parametrize(
    "path, call",
    [("toggle", "toggle_pause"), ("next", "play_next"), ("previous", "previous"),
     ("stop", "stop")],
)
def test_transport_endpoints_call_engine_and_return_now(api, path, call):
    _start_playing(api, make_video("aaaaaaaaaaa"))
    api.engine.calls.clear()
    resp = api.client.post(f"/api/player/{path}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert {"state", "item", "position_s", "duration_s", "output_device"} <= set(body["data"])
    assert api.engine.calls[0][0] == call


def test_toggle_when_idle_starts_queue(api):
    api.stored(make_video("aaaaaaaaaaa"))
    with api.conn() as conn:
        queue.enqueue(conn, "aaaaaaaaaaa", "user")
    data = api.client.post("/api/player/toggle").json()["data"]
    assert data["state"] == "playing"
    assert data["item"]["video_id"] == "aaaaaaaaaaa"


def test_toggle_pauses(api):
    _start_playing(api, make_video("aaaaaaaaaaa"))
    data = api.client.post("/api/player/toggle").json()["data"]
    assert data["state"] == "paused"


def test_seek_endpoint(api):
    _start_playing(api, make_video("aaaaaaaaaaa"))
    data = api.client.post("/api/player/seek", json={"position_s": 120.5}).json()["data"]
    assert data["position_s"] == 120.5
    assert api.engine.calls[-1] == ("seek", 120.5)


def test_seek_endpoint_clamps(api):
    _start_playing(api, make_video("aaaaaaaaaaa", duration_s=300))
    past_end = api.client.post("/api/player/seek", json={"position_s": 9999})
    assert past_end.status_code == 200
    assert past_end.json()["data"]["position_s"] == 299.0
    negative = api.client.post("/api/player/seek", json={"position_s": -50})
    assert negative.status_code == 200
    assert negative.json()["data"]["position_s"] == 0.0


def test_skip_endpoint(api):
    _start_playing(api, make_video("aaaaaaaaaaa"))
    api.client.post("/api/player/seek", json={"position_s": 100})
    assert api.client.post("/api/player/skip", json={"delta_s": 30}).json()["data"][
        "position_s"] == 130.0
    assert api.client.post("/api/player/skip", json={"delta_s": -15}).json()["data"][
        "position_s"] == 115.0
    assert ("skip", -15.0) in api.engine.calls


def test_seek_requires_position(api):
    resp = api.client.post("/api/player/seek", json={})
    assert resp.status_code == 422
    body = resp.json()
    assert body["ok"] is False and body["data"] is None
    assert "position_s" in body["error"]


def test_mpv_died_is_503(api):
    api.engine.raise_exc = MpvDied("gone")
    resp = api.client.post("/api/player/next")
    assert resp.status_code == 503
    assert resp.json() == {"ok": False, "data": None, "error": "player unavailable"}


def test_mpv_command_error_is_409(api):
    api.engine.raise_exc = MpvCommandError("seek failed: error running command")
    resp = api.client.post("/api/player/seek", json={"position_s": 1})
    assert resp.status_code == 409
    assert resp.json() == {"ok": False, "data": None,
                           "error": "seek failed: error running command"}


def test_now_mpv_died_is_503(api):
    api.engine.raise_exc = MpvDied("gone")
    assert api.client.get("/api/now").status_code == 503


def test_play_queue_item_starts_it(api):
    for v in ("aaaaaaaaaaa", "bbbbbbbbbbb"):
        api.stored(make_video(v))
    with api.conn() as conn:
        queue.enqueue(conn, "aaaaaaaaaaa", "user")
        b = queue.enqueue(conn, "bbbbbbbbbbb", "user")
    resp = api.client.post("/api/player/play", json={"queue_item_id": b})
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["state"] == "playing" and data["item"]["id"] == b
    assert api.engine.calls[-1] == ("play_item", b)


def test_play_unknown_queue_item_is_404(api):
    resp = api.client.post("/api/player/play", json={"queue_item_id": 777})
    assert resp.status_code == 404
    assert resp.json()["error"] == "queue item not found"


@pytest.mark.parametrize("body", [{}, {"queue_item_id": "x"}])
def test_play_needs_an_integer_id(api, body):
    assert api.client.post("/api/player/play", json=body).status_code == 422
