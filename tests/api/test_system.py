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
    data = resp.json()["data"]
    assert data.pop("last_outcome") is None
    assert data.pop("next_build_at").endswith(("-05:00", "-06:00"))  # America/Chicago
    assert data == {
        "enabled": True, "wake_time": "07:45", "days": [0, 1, 2, 3, 4, 5, 6],
        "topics": ["technology", "AI", "hardware", "engineering", "news/politics"], "count": 3,
        "build_lead_min": 5,
    }


def _settings(data: dict) -> dict:
    return {k: data[k] for k in MORNING}


def test_put_morning(api):
    resp = api.client.put("/api/morning", json=MORNING)
    assert resp.status_code == 200
    assert _settings(resp.json()["data"]) == MORNING
    assert _settings(api.client.get("/api/morning").json()["data"]) == MORNING


def test_morning_next_build_null_when_disabled(api):
    data = api.client.put("/api/morning", json={**MORNING, "enabled": False}).json()["data"]
    assert data["next_build_at"] is None


def test_morning_last_outcome_shape(api):
    from src.store.agent import create_request

    with api.conn() as conn:
        rid = create_request(conn, "m", "morning", "end")
    outcome = api.client.get("/api/morning").json()["data"]["last_outcome"]
    assert set(outcome) == {"date", "built", "fallback", "started", "request_id", "status"}
    assert (outcome["request_id"], outcome["status"]) == (rid, "pending")


def test_put_morning_build_lead(api):
    data = api.client.put("/api/morning", json={**MORNING, "build_lead_min": 20}).json()["data"]
    assert data["build_lead_min"] == 20
    again = api.client.put("/api/morning", json=MORNING).json()["data"]  # omitted: kept
    assert again["build_lead_min"] == 20


@pytest.mark.parametrize(
    "change, field",
    [({"wake_time": "7:45"}, "wake_time"), ({"count": 7}, "count"), ({"days": [7]}, "days"),
     ({"build_lead_min": 0}, "build_lead_min"), ({"build_lead_min": 61}, "build_lead_min")],
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


FUTURE = 4102444800  # 2100-01-01
TAB = chr(9)


def _line(name, expiry=FUTURE, domain=".youtube.com", prefix=""):
    return prefix + TAB.join([domain, "TRUE", "/", "TRUE", str(expiry), name, "x"])


def _write_cookies(tmp_path, *lines, marker="x"):
    rows = lines or (_line("__Secure-3PSID"),)
    text = chr(10).join(["# Netscape HTTP Cookie File", *rows, "# " + marker])
    (tmp_path / "cookies.txt").write_text(text + chr(10))


def _status(api) -> dict:
    resp = api.client.get("/api/status")
    assert resp.status_code == 200
    return resp.json()["data"]


def test_status_shape(api, tmp_path):
    _write_cookies(tmp_path)
    with api.conn() as conn:
        add_quota(conn, quota_day(), 300)
        log_event(conn, "player", "error", "mpv process died")
    data = _status(api)
    assert set(data) == {"player", "bluetooth", "cookies", "claude", "ytdlp_version",
                         "quota_used_today", "quota_ceiling", "events", "source"}
    assert data["source"] == "ok"
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
    _write_cookies(tmp_path)
    with api.conn() as conn:
        log_event(conn, "ytdlp", "error", "SourceAuthError: cookies or sign-in check")
    assert _status(api)["cookies"] == "expired"
    with api.conn() as conn:
        log_event(conn, "ytdlp", "info", "ok")
    assert _status(api)["cookies"] == "ok"


def test_status_cookies_ok_once_cookies_file_replaced(api, tmp_path):
    import os
    import time

    cookies = tmp_path / "cookies.txt"
    _write_cookies(tmp_path)
    old = time.time() - 3600
    os.utime(cookies, (old, old))
    with api.conn() as conn:
        log_event(conn, "ytdlp", "error", "SourceAuthError: cookies or sign-in check")
    assert _status(api)["cookies"] == "expired"
    _write_cookies(tmp_path, marker="fresh")  # re-exported after the error
    future = time.time() + 5
    os.utime(cookies, (future, future))
    assert _status(api)["cookies"] == "ok"


def test_status_source_broken_until_a_successful_play(api):
    with api.conn() as conn:
        log_event(conn, "player", "info", "ok: playing")
        log_event(conn, "player", "error", "SourceBroken: 3 consecutive playback errors")
        log_event(conn, "player", "error", "playback failed: X")
    assert _status(api)["source"] == "broken"
    with api.conn() as conn:
        log_event(conn, "player", "info", "ok: playing")
    assert _status(api)["source"] == "ok"


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


ANON = [_line(n) for n in ("PREF", "SOCS", "YSC", "VISITOR_INFO1_LIVE", "GPS", "__Secure-YNID",
                           "__Secure-ROLLOUT_TOKEN", "VISITOR_PRIVACY_METADATA")]


def test_status_cookies_anonymous_only_is_missing(api, tmp_path):
    _write_cookies(tmp_path, *ANON)
    assert _status(api)["cookies"] == "missing"


def test_status_cookies_signin_cookie_is_ok(api, tmp_path):
    _write_cookies(tmp_path, *ANON, _line("__Secure-3PSID"))
    assert _status(api)["cookies"] == "ok"


def test_status_cookies_all_signin_expired_is_missing(api, tmp_path):
    _write_cookies(tmp_path, *ANON, _line("SID", 1000), _line("LOGIN_INFO", 1000, ".google.com"))
    assert _status(api)["cookies"] == "missing"


def test_status_cookies_httponly_prefix_and_session_cookie(api, tmp_path):
    _write_cookies(tmp_path, _line("__Secure-1PSID", 0, prefix="#HttpOnly_"))
    assert _status(api)["cookies"] == "ok"


def test_status_cookies_other_domain_ignored(api, tmp_path):
    _write_cookies(tmp_path, _line("SID", domain=".example.com"))
    assert _status(api)["cookies"] == "missing"


def test_status_cookies_mtime_cache_avoids_reread(api, tmp_path, monkeypatch):
    import os
    import src.api.routes_system as rs

    _write_cookies(tmp_path)
    calls = []
    real = rs.read_signin_expiry
    monkeypatch.setattr(rs, "read_signin_expiry", lambda p: calls.append(p) or real(p))
    assert _status(api)["cookies"] == "ok"
    assert _status(api)["cookies"] == "ok"
    assert len(calls) == 1
    _write_cookies(tmp_path, *ANON, marker="changed")
    future = os.stat(tmp_path / "cookies.txt").st_mtime + 5
    os.utime(tmp_path / "cookies.txt", (future, future))
    assert _status(api)["cookies"] == "missing"
    assert len(calls) == 2
