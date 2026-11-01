"""Tests for /api/morning and /api/status."""

from __future__ import annotations

import pytest

from src.store.settings import add_quota, log_event
from src.api.routes_system import quota_day

MORNING = {"enabled": True, "wake_time": "06:30", "days": [0, 1, 2, 3, 4],
           "topics": ["AI", "hardware"], "count": 2}


def test_get_morning_defaults(api):
    resp = api.client.get("/api/morning")
    assert resp.status_code == 200
    assert resp.json()["data"] == {
        "enabled": True, "wake_time": "07:45", "days": [0, 1, 2, 3, 4, 5, 6],
        "topics": ["technology", "AI", "hardware", "engineering", "news/politics"], "count": 3,
    }


def test_put_morning(api):
    resp = api.client.put("/api/morning", json=MORNING)
    assert resp.status_code == 200
    assert resp.json()["data"] == MORNING
    assert api.client.get("/api/morning").json()["data"] == MORNING


@pytest.mark.parametrize(
    "change, field",
    [({"wake_time": "7:45"}, "wake_time"), ({"count": 7}, "count"), ({"days": [7]}, "days")],
)
def test_put_morning_field_validation(api, change, field):
    resp = api.client.put("/api/morning", json={**MORNING, **change})
    assert resp.status_code == 422
    assert field in resp.json()["error"]


@pytest.mark.parametrize("change", [{"wake_time": "25:00"}, {"topics": []}])
def test_put_morning_store_validation_is_enveloped_422(api, change):
    resp = api.client.put("/api/morning", json={**MORNING, **change})
    assert resp.status_code == 422
    body = resp.json()
    assert body["ok"] is False and body["data"] is None and body["error"]


def test_morning_test_runs_now(api):
    resp = api.client.post("/api/morning/test")
    assert resp.status_code == 202
    assert resp.json()["data"] == {"request_id": 77}
    assert api.run_now_calls == [77]


def _status(api) -> dict:
    resp = api.client.get("/api/status")
    assert resp.status_code == 200
    return resp.json()["data"]


def test_status_shape(api, tmp_path):
    (tmp_path / "cookies.txt").write_text("# cookies")
    with api.conn() as conn:
        add_quota(conn, quota_day(), 300)
        log_event(conn, "player", "error", "mpv process died")
    data = _status(api)
    assert set(data) == {"player", "bluetooth", "cookies", "claude", "ytdlp_version",
                         "quota_used_today", "quota_ceiling", "events"}
    assert data["player"] == "idle"
    assert data["bluetooth"] == "connected"
    assert data["cookies"] == "ok"
    assert data["claude"] == "unknown"
    assert isinstance(data["ytdlp_version"], str) and data["ytdlp_version"]
    assert data["quota_used_today"] == 300
    assert data["quota_ceiling"] == 9500
    assert data["events"][0]["component"] == "player"
    assert set(data["events"][0]) == {"component", "level", "message", "created_at"}


def test_status_cookies_missing(api):
    assert _status(api)["cookies"] == "missing"


def test_status_cookies_expired_after_auth_error(api, tmp_path):
    (tmp_path / "cookies.txt").write_text("# cookies")
    with api.conn() as conn:
        log_event(conn, "ytdlp", "error", "SourceAuthError: cookies or sign-in check")
    assert _status(api)["cookies"] == "expired"
    with api.conn() as conn:
        log_event(conn, "ytdlp", "info", "ok")
    assert _status(api)["cookies"] == "ok"


@pytest.mark.parametrize(
    "level, message, expected",
    [
        ("info", "ok: request 3 done", "ok"),
        ("error", "ClaudeRateLimited: usage limit reached", "rate_limited"),
        ("error", "ClaudeNotLoggedIn: please run claude login", "not_logged_in"),
        ("error", "ClaudeNotFound: claude not on PATH", "missing"),
        ("error", "ValueError: bad json from model", "ok"),
    ],
)
def test_status_claude(api, level, message, expected):
    with api.conn() as conn:
        log_event(conn, "agent", level, message)
    assert _status(api)["claude"] == expected


@pytest.mark.parametrize("state", ["connected", "disconnected", "unconfigured"])
def test_status_bluetooth(api, state):
    api.bt.last_state = state
    assert _status(api)["bluetooth"] == state


def test_status_player_unavailable_when_mpv_died(api):
    from src.player.mpv import MpvDied

    api.engine.raise_exc = MpvDied("gone")
    assert _status(api)["player"] == "unavailable"
