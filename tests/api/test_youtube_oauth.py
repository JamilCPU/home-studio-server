"""Tests for /api/youtube/oauth and /api/status.youtube_account."""

from __future__ import annotations

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from src.api.app import create_app
from src.sources.youtube_oauth import OAuthError, OAuthNotConfigured, OAuthStateError

KEYS = {"state", "user_code", "verification_url", "expires_at", "error", "channel_title"}
PENDING = {"state": "pending", "user_code": "FAKE-CODE",
           "verification_url": "https://www.google.com/device",
           "expires_at": "2026-11-01T15:30:00+00:00", "interval_s": 5}


def blank(state: str, **kw) -> dict:
    out = dict.fromkeys(KEYS)
    out.update(state=state, **kw)
    return out


class FakeOAuth:
    def __init__(self, state="disconnected", start_exc: Exception | None = None) -> None:
        self._state = state
        self.start_exc = start_exc
        self.calls: list[str] = []

    def state(self) -> str:
        return self._state

    def status(self) -> dict:
        if self._state == "connected":
            return blank("connected", channel_title="My Channel")
        return blank(self._state)

    def start(self) -> dict:
        self.calls.append("start")
        if self.start_exc is not None:
            raise self.start_exc
        self._state = "pending"
        return dict(PENDING)

    def disconnect(self) -> dict:
        self.calls.append("disconnect")
        self._state = "disconnected"
        return self.status()


def client(api, oauth) -> TestClient:
    app = create_app(replace(api.deps, youtube_oauth=oauth))
    return TestClient(app, raise_server_exceptions=False)


def test_get_without_oauth_service_is_unconfigured(api):
    resp = api.client.get("/api/youtube/oauth")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "data": blank("unconfigured"), "error": None}


def test_get_connected(api):
    data = client(api, FakeOAuth("connected")).get("/api/youtube/oauth").json()["data"]
    assert data == blank("connected", channel_title="My Channel")


@pytest.mark.parametrize("path", ["/api/youtube/oauth/start", "/api/youtube/oauth"])
def test_start_returns_code(api, path):
    oauth = FakeOAuth()
    resp = client(api, oauth).post(path)
    assert resp.status_code == 200
    assert resp.json()["data"] == PENDING
    assert oauth.calls == ["start"]


@pytest.mark.parametrize("exc, status", [
    (OAuthNotConfigured("YouTube sign-in isn't set up on the Pi yet"), 409),
    (OAuthStateError("already connected; disconnect first"), 409),
    (OAuthError("could not reach Google"), 502),
])
def test_start_errors(api, exc, status):
    resp = client(api, FakeOAuth(start_exc=exc)).post("/api/youtube/oauth/start")
    assert resp.status_code == status
    assert resp.json() == {"ok": False, "data": None, "error": str(exc)}


def test_start_without_oauth_service_is_409(api):
    resp = api.client.post("/api/youtube/oauth/start")
    assert resp.status_code == 409
    assert resp.json()["error"] == "YouTube sign-in isn't set up on the Pi yet"


def test_delete_disconnects(api):
    oauth = FakeOAuth("connected")
    resp = client(api, oauth).delete("/api/youtube/oauth")
    assert resp.status_code == 200
    assert resp.json()["data"] == blank("disconnected")
    assert oauth.calls == ["disconnect"]


def test_delete_without_oauth_service(api):
    assert api.client.delete("/api/youtube/oauth").json()["data"] == blank("unconfigured")


@pytest.mark.parametrize("state", ["disconnected", "pending", "connected", "error"])
def test_status_reports_youtube_account(api, state):
    data = client(api, FakeOAuth(state)).get("/api/status").json()["data"]
    assert data["youtube_account"] == state


def test_status_youtube_account_unconfigured_by_default(api):
    assert api.client.get("/api/status").json()["data"]["youtube_account"] == "unconfigured"
