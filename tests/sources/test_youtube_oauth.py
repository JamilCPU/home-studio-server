"""Tests for the YouTube OAuth device flow (no network: Google is a fake http_post)."""

from __future__ import annotations

import json
import logging
import os
import stat
import sys
import threading
from contextlib import closing

import pytest

from src.db import connect, migrate
from src.sources.data_api import QuotaExceeded, QuotaMeter
from src.sources.youtube_oauth import (
    DEVICE_URL,
    REVOKE_URL,
    SCOPE,
    TOKEN_URL,
    OAuthError,
    OAuthNotConfigured,
    OAuthNotConnected,
    OAuthStateError,
    YouTubeOAuth,
)
from src.store.settings import latest_event, quota_used

CLIENT_ID = "fake-client.example"
CLIENT_SECRET = "fake-secret-value"
USER_CODE = "FAKE-CODE"
NOW = 1_800_000_000.0


class FakeGoogle:
    """Scripted responses per URL; records every call (url, form data)."""

    def __init__(self) -> None:
        self.responses: dict[str, list] = {DEVICE_URL: [], TOKEN_URL: [], REVOKE_URL: []}
        self.calls: list[tuple[str, dict]] = []

    def queue(self, url: str, *responses) -> None:
        self.responses[url].extend(responses)

    def __call__(self, url: str, data: dict) -> tuple[int, dict]:
        self.calls.append((url, dict(data)))
        if not self.responses[url]:
            return 200, {}
        item = self.responses[url].pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def posted(self, url: str) -> list[dict]:
        return [d for u, d in self.calls if u == url]


def device_code(**kw) -> tuple[int, dict]:
    body = {"device_code": "fake-device-code", "user_code": USER_CODE,
            "verification_url": "https://www.google.com/device", "expires_in": 1800,
            "interval": 5}
    body.update(kw)
    return 200, body


def token(access="fake-access-1", refresh="fake-refresh", expires_in=3600) -> tuple[int, dict]:
    body = {"access_token": access, "expires_in": expires_in, "scope": SCOPE,
            "token_type": "Bearer"}
    if refresh:
        body["refresh_token"] = refresh
    return 200, body


def pending() -> tuple[int, dict]:
    return 428, {"error": "authorization_pending"}


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> float:
        return self.now


class FakeService:
    """Data API stand-in: channels().list(...).execute()."""

    def __init__(self, title="My Channel") -> None:
        self.title = title
        self.calls: list[dict] = []

    def channels(self):
        return self

    def list(self, **kw):
        self.calls.append(kw)
        return self

    def execute(self):
        return {"items": [{"snippet": {"title": self.title}}]}


@pytest.fixture
def db_factory(tmp_path):
    path = tmp_path / "o.db"
    with closing(connect(path)) as conn:
        migrate(conn)
    return lambda: connect(path)


@pytest.fixture
def google():
    return FakeGoogle()


def make(tmp_path, google, db_factory=None, *, client_id=CLIENT_ID, spawn=None,
         clock=None, services=None, sleep=None, **kw) -> YouTubeOAuth:
    sleeps: list[float] = []
    created: list = []

    def service_factory(creds):
        svc = FakeService()
        created.append((creds, svc))
        return svc

    oauth = YouTubeOAuth(
        client_id, CLIENT_SECRET, str(tmp_path / "secrets" / "youtube_oauth.json"),
        db_factory=db_factory, http_post=google, clock=clock or Clock(),
        sleep=sleep or sleeps.append, spawn=spawn or (lambda target: target()),
        service_factory=services or service_factory, **kw,
    )
    oauth.test_sleeps = sleeps  # type: ignore[attr-defined]
    oauth.test_services = created  # type: ignore[attr-defined]
    return oauth


def token_file(tmp_path):
    return tmp_path / "secrets" / "youtube_oauth.json"


def write_token(tmp_path, **kw):
    record = {"access_token": "fake-access-0", "refresh_token": "fake-refresh",
              "expires_at": NOW + 3600, "scope": SCOPE, "token_type": "Bearer"}
    record.update(kw)
    path = token_file(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record), encoding="utf-8")
    return path


# -- state ------------------------------------------------------------------------------


def test_unconfigured_without_client(tmp_path, google):
    oauth = make(tmp_path, google, client_id="")
    assert oauth.state() == "unconfigured"
    assert oauth.status()["state"] == "unconfigured"
    with pytest.raises(OAuthNotConfigured):
        oauth.start()
    assert google.calls == []
    assert oauth.credentials() is None


def test_disconnected_without_token(tmp_path, google):
    oauth = make(tmp_path, google)
    assert oauth.status() == {
        "state": "disconnected", "user_code": None, "verification_url": None,
        "expires_at": None, "error": None, "channel_title": None,
    }


def test_connected_from_saved_token(tmp_path, google):
    write_token(tmp_path)
    assert make(tmp_path, google).state() == "connected"


def test_corrupt_token_file_is_disconnected(tmp_path, google):
    path = token_file(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")
    assert make(tmp_path, google).state() == "disconnected"


# -- device flow ------------------------------------------------------------------------


def test_start_returns_code_and_polls_until_connected(tmp_path, google, db_factory):
    google.queue(DEVICE_URL, device_code())
    google.queue(TOKEN_URL, pending(), token())
    connected: list[int] = []
    oauth = make(tmp_path, google, db_factory, on_connected=lambda: connected.append(1))

    flow = oauth.start()

    assert flow == {"state": "pending", "user_code": USER_CODE,
                    "verification_url": "https://www.google.com/device",
                    "expires_at": "2027-01-15T08:30:00+00:00", "interval_s": 5}
    assert google.posted(DEVICE_URL) == [{"client_id": CLIENT_ID, "scope": SCOPE}]
    polls = google.posted(TOKEN_URL)
    assert len(polls) == 2
    assert polls[0] == {"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
                        "device_code": "fake-device-code",
                        "grant_type": "urn:ietf:params:oauth:grant-type:device_code"}
    assert oauth.test_sleeps == [5, 5]
    assert oauth.state() == "connected"
    assert connected == [1]
    saved = json.loads(token_file(tmp_path).read_text(encoding="utf-8"))
    assert saved["refresh_token"] == "fake-refresh"
    assert saved["access_token"] == "fake-access-1"
    assert saved["expires_at"] == NOW + 3600
    assert saved["scope"] == SCOPE
    with closing(db_factory()) as conn:
        assert latest_event(conn, "youtube")[:2] == ("info", "ok: YouTube account connected")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_token_file_is_mode_600(tmp_path, google):
    google.queue(DEVICE_URL, device_code())
    google.queue(TOKEN_URL, token())
    make(tmp_path, google).start()
    assert stat.S_IMODE(os.stat(token_file(tmp_path)).st_mode) == 0o600


def test_slow_down_adds_five_seconds(tmp_path, google):
    google.queue(DEVICE_URL, device_code())
    google.queue(TOKEN_URL, (400, {"error": "slow_down"}), pending(), token())
    oauth = make(tmp_path, google)
    oauth.start()
    assert oauth.test_sleeps == [5, 10, 10]


def test_network_error_while_polling_keeps_polling(tmp_path, google):
    google.queue(DEVICE_URL, device_code())
    google.queue(TOKEN_URL, OSError("network down"), token())
    oauth = make(tmp_path, google)
    oauth.start()
    assert oauth.state() == "connected"


@pytest.mark.parametrize("error, message", [
    ("access_denied", "access denied"),
    ("expired_token", "expired"),
    ("invalid_client", "invalid_client"),
])
def test_poll_stops_on_terminal_error(tmp_path, google, error, message):
    google.queue(DEVICE_URL, device_code())
    google.queue(TOKEN_URL, (400, {"error": error}), token())
    oauth = make(tmp_path, google)
    oauth.start()
    status = oauth.status()
    assert status["state"] == "error"
    assert message in status["error"]
    assert len(google.posted(TOKEN_URL)) == 1
    assert not token_file(tmp_path).exists()


def test_poll_gives_up_after_expiry(tmp_path, google):
    clock = Clock()
    google.queue(DEVICE_URL, device_code(expires_in=12, interval=5))
    google.queue(TOKEN_URL, *[pending()] * 5)

    def sleep(seconds):
        clock.now += seconds

    oauth = make(tmp_path, google, clock=clock, sleep=sleep)
    oauth.start()
    assert oauth.status()["state"] == "error"
    assert "expired" in oauth.status()["error"]
    assert len(google.posted(TOKEN_URL)) == 2  # at +5 s and +10 s; +15 s is past expiry


def test_pending_status_and_start_again_returns_same_code(tmp_path, google):
    google.queue(DEVICE_URL, device_code(), device_code(user_code="OTHER-CODE"))
    spawned = []
    oauth = make(tmp_path, google, spawn=spawned.append)
    first = oauth.start()
    status = oauth.status()
    assert status["state"] == "pending"
    assert status["user_code"] == USER_CODE
    assert status["expires_at"] == first["expires_at"]
    assert oauth.start() == first
    assert len(google.posted(DEVICE_URL)) == 1
    assert len(spawned) == 1


def test_pending_code_expires_in_status(tmp_path, google):
    clock = Clock()
    google.queue(DEVICE_URL, device_code(expires_in=60))
    oauth = make(tmp_path, google, clock=clock, spawn=lambda target: None)
    oauth.start()
    clock.now += 61
    assert oauth.status()["state"] == "error"


def test_start_after_error_gets_new_code(tmp_path, google):
    google.queue(DEVICE_URL, device_code(), device_code(user_code="NEW-CODE"))
    google.queue(TOKEN_URL, (400, {"error": "access_denied"}))
    oauth = make(tmp_path, google, spawn=lambda target: target())
    oauth.start()
    assert oauth.state() == "error"
    oauth._spawn = lambda target: None
    assert oauth.start()["user_code"] == "NEW-CODE"


def test_start_while_connected_is_refused(tmp_path, google):
    write_token(tmp_path)
    with pytest.raises(OAuthStateError):
        make(tmp_path, google).start()


@pytest.mark.parametrize("response", [(400, {"error": "invalid_client"}), OSError("down")])
def test_device_request_failure_raises(tmp_path, google, response):
    google.queue(DEVICE_URL, response)
    oauth = make(tmp_path, google)
    with pytest.raises(OAuthError):
        oauth.start()
    assert oauth.state() == "disconnected"


def test_token_without_refresh_token_is_an_error(tmp_path, google):
    google.queue(DEVICE_URL, device_code())
    google.queue(TOKEN_URL, token(refresh=None))
    oauth = make(tmp_path, google)
    oauth.start()
    assert oauth.state() == "error"
    assert not token_file(tmp_path).exists()


def test_disconnect_cancels_pending_poller(tmp_path, google):
    google.queue(DEVICE_URL, device_code())
    google.queue(TOKEN_URL, token())
    held = []
    oauth = make(tmp_path, google, spawn=held.append)
    oauth.start()
    oauth.disconnect()
    held[0]()  # the poller wakes after the cancel: it must not poll or connect
    assert google.posted(TOKEN_URL) == []
    assert oauth.state() == "disconnected"


def test_one_device_request_for_concurrent_starts(tmp_path, google):
    google.queue(DEVICE_URL, device_code(), device_code(user_code="SECOND"))
    oauth = make(tmp_path, google, spawn=lambda target: None)
    barrier = threading.Barrier(4)
    results = []

    def run():
        barrier.wait()
        results.append(oauth.start()["user_code"])

    threads = [threading.Thread(target=run) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results == [USER_CODE] * 4
    assert len(google.posted(DEVICE_URL)) == 1


# -- disconnect -------------------------------------------------------------------------


def test_disconnect_revokes_and_deletes(tmp_path, google):
    write_token(tmp_path)
    oauth = make(tmp_path, google)
    assert oauth.disconnect()["state"] == "disconnected"
    assert google.posted(REVOKE_URL) == [{"token": "fake-refresh"}]
    assert not token_file(tmp_path).exists()
    assert oauth.credentials() is None


def test_disconnect_still_deletes_when_revoke_fails(tmp_path, google):
    write_token(tmp_path)
    google.queue(REVOKE_URL, OSError("down"))
    oauth = make(tmp_path, google)
    oauth.disconnect()
    assert not token_file(tmp_path).exists()
    assert oauth.state() == "disconnected"


def test_disconnect_when_disconnected_is_a_no_op(tmp_path, google):
    oauth = make(tmp_path, google)
    assert oauth.disconnect()["state"] == "disconnected"
    assert google.calls == []


# -- credentials and refresh ------------------------------------------------------------


def test_credentials_use_saved_access_token(tmp_path, google):
    write_token(tmp_path)
    creds = make(tmp_path, google).credentials()
    assert creds.token == "fake-access-0"
    assert google.calls == []


def test_credentials_refresh_near_expiry(tmp_path, google):
    write_token(tmp_path, expires_at=NOW + 30)
    google.queue(TOKEN_URL, token(access="fake-access-2", refresh=None))
    oauth = make(tmp_path, google)
    assert oauth.credentials().token == "fake-access-2"
    assert google.posted(TOKEN_URL) == [{
        "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
        "refresh_token": "fake-refresh", "grant_type": "refresh_token",
    }]
    saved = json.loads(token_file(tmp_path).read_text(encoding="utf-8"))
    assert saved["access_token"] == "fake-access-2"
    assert saved["refresh_token"] == "fake-refresh"  # kept when Google sends none
    assert oauth.credentials().token == "fake-access-2"
    assert len(google.posted(TOKEN_URL)) == 1


def test_invalid_grant_disconnects_and_logs(tmp_path, google, db_factory):
    write_token(tmp_path, expires_at=NOW - 1)
    google.queue(TOKEN_URL, (400, {"error": "invalid_grant", "error_description": "revoked"}))
    oauth = make(tmp_path, google, db_factory)
    assert oauth.credentials() is None
    assert oauth.state() == "disconnected"
    assert not token_file(tmp_path).exists()
    with closing(db_factory()) as conn:
        level, message, _ = latest_event(conn, "youtube")
    assert level == "error" and message.startswith("OAuthRevoked:")


@pytest.mark.parametrize("response", [(500, {}), OSError("down")])
def test_other_refresh_failures_raise_and_keep_token(tmp_path, google, response):
    write_token(tmp_path, expires_at=NOW - 1)
    google.queue(TOKEN_URL, response)
    oauth = make(tmp_path, google)
    with pytest.raises(OAuthError):
        oauth.credentials()
    assert oauth.state() == "connected"
    assert token_file(tmp_path).exists()


def test_concurrent_credentials_refresh_once(tmp_path, google):
    write_token(tmp_path, expires_at=NOW - 1)
    google.queue(TOKEN_URL, token(access="fake-access-2", refresh=None))
    oauth = make(tmp_path, google)
    barrier = threading.Barrier(5)
    tokens = []

    def run():
        barrier.wait()
        tokens.append(oauth.credentials().token)

    threads = [threading.Thread(target=run) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert tokens == ["fake-access-2"] * 5
    assert len(google.posted(TOKEN_URL)) == 1


# -- Data API calls ---------------------------------------------------------------------


def test_channel_title_is_cached_and_costs_one_unit(tmp_path, google, db_factory):
    write_token(tmp_path)
    meter = QuotaMeter(db_factory, ceiling=100)
    oauth = make(tmp_path, google, db_factory, meter=meter)
    assert oauth.channel_title() == "My Channel"
    assert oauth.status()["channel_title"] == "My Channel"
    (creds, svc), = oauth.test_services
    assert svc.calls == [{"part": "snippet", "mine": True}]
    with closing(db_factory()) as conn:
        assert quota_used(conn, meter.today()) == 1


def test_call_without_connection_raises(tmp_path, google):
    with pytest.raises(OAuthNotConnected):
        make(tmp_path, google).call(lambda yt: yt.channels().list(part="id", mine=True))


def test_call_respects_quota_ceiling(tmp_path, google, db_factory):
    write_token(tmp_path)
    oauth = make(tmp_path, google, db_factory, meter=QuotaMeter(db_factory, ceiling=0))
    with pytest.raises(QuotaExceeded):
        oauth.call(lambda yt: yt.channels().list(part="id", mine=True))
    assert oauth.status()["channel_title"] is None  # the status still answers


def test_service_rebuilt_only_when_token_changes(tmp_path, google):
    write_token(tmp_path)
    oauth = make(tmp_path, google)
    oauth.call(lambda yt: yt.channels().list(part="id", mine=True))
    oauth.call(lambda yt: yt.channels().list(part="id", mine=True))
    assert len(oauth.test_services) == 1


def test_disconnect_clears_cached_title(tmp_path, google):
    write_token(tmp_path)
    oauth = make(tmp_path, google)
    oauth.channel_title()
    oauth.disconnect()
    assert oauth.status()["channel_title"] is None


# -- secrets never logged ---------------------------------------------------------------


def test_secrets_never_logged(tmp_path, google, db_factory, caplog):
    caplog.set_level(logging.DEBUG)
    google.queue(DEVICE_URL, device_code())
    google.queue(TOKEN_URL, OSError("boom"), (400, {"error": "slow_down"}), token())
    oauth = make(tmp_path, google, db_factory)
    oauth.start()
    write_token(tmp_path, expires_at=NOW - 1)
    oauth = make(tmp_path, google, db_factory)
    google.queue(TOKEN_URL, (400, {"error": "invalid_grant"}))
    oauth.credentials()
    with closing(db_factory()) as conn:
        events = " ".join(r[0] for r in conn.execute("SELECT message FROM status_events"))
    for secret in (USER_CODE, CLIENT_SECRET, "fake-refresh", "fake-access", "fake-device-code"):
        assert secret not in caplog.text
        assert secret not in events
