"""YouTube account sign-in with Google's OAuth 2.0 device flow ("TVs and Limited Input").

The Pi asks Google for a short user code, the listener enters it at google.com/device on
any phone or PC, and a background poller collects the token. The refresh token is kept in
a mode-600 JSON file; access tokens are refreshed from it on demand.

Never logged: the client secret, the tokens, the device code and the user code (the user
code only appears in API responses).
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.store.settings import log_event

logger = logging.getLogger(__name__)

DEVICE_URL = "https://oauth2.googleapis.com/device/code"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
SCOPE = "https://www.googleapis.com/auth/youtube.readonly"
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
DEFAULT_VERIFICATION_URL = "https://www.google.com/device"
COMPONENT = "youtube"
HTTP_TIMEOUT_S = 15.0
REFRESH_MARGIN_S = 60.0  # refresh this long before the access token expires
SLOW_DOWN_STEP_S = 5
MIN_INTERVAL_S = 1

HttpPost = Callable[[str, dict], "tuple[int, dict]"]

_POLL_ERRORS = {
    "access_denied": "access denied: sign-in was cancelled on the Google page",
    "expired_token": "the code expired before it was entered; try again",
}
_EXPIRED = "the code expired before it was entered; try again"


class OAuthNotConfigured(Exception):
    """No OAuth client ID/secret in the config."""


class OAuthStateError(Exception):
    """The request does not fit the current state (e.g. start while connected)."""


class OAuthError(Exception):
    """Google refused a request or could not be reached."""


class OAuthNotConnected(Exception):
    """A Data API call needs a connected account."""


def httpx_post(url: str, data: dict) -> tuple[int, dict]:
    """POST a form and return (status, JSON object or {})."""
    import httpx

    resp = httpx.post(url, data=data, timeout=HTTP_TIMEOUT_S)
    try:
        body = resp.json()
    except ValueError:
        body = {}
    return resp.status_code, body if isinstance(body, dict) else {}


def _default_spawn(target: Callable[[], None]) -> None:
    threading.Thread(target=target, name="youtube-oauth-poll", daemon=True).start()


def _default_service(credentials) -> Any:
    from googleapiclient.discovery import build

    return build("youtube", "v3", credentials=credentials, cache_discovery=False)


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class _Flow:
    device_code: str
    user_code: str
    verification_url: str
    expires_at: float
    interval_s: int


class _Unmetered:
    """Stand-in for a QuotaMeter when none is given (tests): serializes, counts nothing."""

    def __init__(self) -> None:
        self.lock = threading.Lock()

    def spend(self, units: int) -> None:
        return None

    def execute(self, request) -> dict:
        with self.lock:
            return request.execute()


class YouTubeOAuth:
    """Device-flow sign-in, token storage/refresh and OAuth Data API calls.

    States: "unconfigured", "disconnected", "pending", "connected", "error".
    ``_lock`` guards the state; ``_start_lock`` keeps a single device request (and so a
    single poller) at a time; ``_refresh_lock`` keeps a single token refresh at a time.
    """

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        token_path: str,
        *,
        db_factory: Callable[[], sqlite3.Connection] | None = None,
        http_post: HttpPost | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        spawn: Callable[[Callable[[], None]], None] = _default_spawn,
        meter: Any = None,
        service_factory: Callable[[Any], Any] = _default_service,
        on_connected: Callable[[], None] | None = None,
    ) -> None:
        self._client_id = client_id.strip()
        self._client_secret = client_secret.strip()
        self._path = Path(token_path)
        self._db_factory = db_factory
        self._post = http_post or httpx_post
        self._clock = clock
        self._sleep = sleep
        self._spawn = spawn
        self._meter = meter or _Unmetered()
        self._service_factory = service_factory
        self._on_connected = on_connected
        self._lock = threading.Lock()
        self._start_lock = threading.Lock()
        self._refresh_lock = threading.Lock()
        self._flow: _Flow | None = None
        self._error: str | None = None
        self._title: str | None = None
        self._service: tuple[str, Any] | None = None  # (access token, service)
        self._token = self._load() if self.configured else None

    # -- state ------------------------------------------------------------------------

    @property
    def configured(self) -> bool:
        return bool(self._client_id and self._client_secret)

    def state(self) -> str:
        """Current state, without any network call."""
        if not self.configured:
            return "unconfigured"
        with self._lock:
            return self._state_locked()

    def _state_locked(self) -> str:
        if self._flow is not None:
            if self._clock() < self._flow.expires_at:
                return "pending"
            self._flow, self._error = None, _EXPIRED
        if self._error is not None:
            return "error"
        return "connected" if self._token is not None else "disconnected"

    def status(self) -> dict:
        """The GET /api/youtube/oauth payload; every key is present, null when n/a."""
        out: dict[str, Any] = {
            "state": "unconfigured", "user_code": None, "verification_url": None,
            "expires_at": None, "error": None, "channel_title": None,
        }
        if not self.configured:
            return out
        with self._lock:
            state = self._state_locked()
            flow, error = self._flow, self._error
        out["state"] = state
        if state == "pending" and flow is not None:
            out.update(user_code=flow.user_code, verification_url=flow.verification_url,
                       expires_at=_iso(flow.expires_at))
        elif state == "error":
            out["error"] = error
        elif state == "connected":
            out["channel_title"] = self._safe_title()
        return out

    # -- device flow ------------------------------------------------------------------

    def start(self) -> dict:
        """Ask Google for a user code and poll for approval in the background.

        While a code is pending, returns that same code. Raises OAuthNotConfigured,
        OAuthStateError (already connected) or OAuthError (Google refused/unreachable).
        """
        if not self.configured:
            raise OAuthNotConfigured("YouTube sign-in isn't set up on the Pi yet")
        with self._start_lock:
            with self._lock:
                state = self._state_locked()
                if state == "pending" and self._flow is not None:
                    return self._flow_out(self._flow)
                if state == "connected":
                    raise OAuthStateError("already connected; disconnect first")
            flow = self._request_code()
            with self._lock:
                self._flow, self._error = flow, None
        self._spawn(lambda: self._poll(flow))
        return self._flow_out(flow)

    def _flow_out(self, flow: _Flow) -> dict:
        return {"state": "pending", "user_code": flow.user_code,
                "verification_url": flow.verification_url,
                "expires_at": _iso(flow.expires_at), "interval_s": flow.interval_s}

    def _request_code(self) -> _Flow:
        try:
            status, body = self._post(DEVICE_URL, {"client_id": self._client_id, "scope": SCOPE})
        except Exception as exc:  # noqa: BLE001 - any transport failure
            logger.warning("device code request failed: %s", type(exc).__name__)
            raise OAuthError("could not reach Google") from None
        if status != 200 or not body.get("device_code") or not body.get("user_code"):
            error = str(body.get("error") or status)
            logger.warning("device code request refused: %s", error)
            raise OAuthError(f"Google refused the sign-in request: {error}")
        return _Flow(
            device_code=str(body["device_code"]),
            user_code=str(body["user_code"]),
            verification_url=str(body.get("verification_url") or body.get("verification_uri")
                                 or DEFAULT_VERIFICATION_URL),
            expires_at=self._clock() + float(body.get("expires_in") or 1800),
            interval_s=max(int(body.get("interval") or 5), MIN_INTERVAL_S),
        )

    def _current(self, flow: _Flow) -> bool:
        with self._lock:
            return self._flow is flow

    def _poll(self, flow: _Flow) -> None:
        interval = flow.interval_s
        data = {"client_id": self._client_id, "client_secret": self._client_secret,
                "device_code": flow.device_code, "grant_type": DEVICE_GRANT}
        while True:
            self._sleep(interval)
            if not self._current(flow):
                return  # cancelled or replaced
            if self._clock() >= flow.expires_at:
                self._fail(flow, _EXPIRED)
                return
            try:
                status, body = self._post(TOKEN_URL, data)
            except Exception as exc:  # noqa: BLE001 - keep polling until expiry
                logger.warning("device token poll failed: %s", type(exc).__name__)
                continue
            if status == 200 and body.get("access_token"):
                self._finish(flow, body)
                return
            error = str(body.get("error") or status)
            if error == "authorization_pending":
                continue
            if error == "slow_down":
                interval += SLOW_DOWN_STEP_S
                continue
            logger.warning("device sign-in stopped: %s", error)
            self._fail(flow, _POLL_ERRORS.get(error, f"Google sign-in failed: {error}"))
            return

    def _fail(self, flow: _Flow, message: str) -> None:
        with self._lock:
            if self._flow is flow:
                self._flow, self._error = None, message

    def _finish(self, flow: _Flow, body: dict) -> None:
        if not body.get("refresh_token"):
            self._fail(flow, "Google returned no refresh token; disconnect the app at "
                             "myaccount.google.com and try again")
            return
        record = self._record(body, refresh_token=str(body["refresh_token"]))
        with self._lock:
            if self._flow is not flow:
                return
            try:
                self._save(record)
            except OSError as exc:
                logger.error("could not save the YouTube token file: %s", type(exc).__name__)
                self._flow, self._error = None, "could not save the sign-in on the Pi"
                return
            self._flow, self._error, self._token = None, None, record
            self._title, self._service = None, None
        logger.info("YouTube account connected")
        self._event("info", "ok: YouTube account connected")
        if self._on_connected is not None:
            try:
                self._on_connected()
            except Exception:  # noqa: BLE001 - the sign-in itself succeeded
                logger.exception("on_connected hook failed")

    def _record(self, body: dict, refresh_token: str) -> dict:
        return {
            "access_token": str(body["access_token"]),
            "refresh_token": refresh_token,
            "expires_at": self._clock() + float(body.get("expires_in") or 3600),
            "scope": str(body.get("scope") or SCOPE),
            "token_type": str(body.get("token_type") or "Bearer"),
        }

    # -- disconnect -------------------------------------------------------------------

    def disconnect(self) -> dict:
        """Cancel any pending code, revoke the token, delete the file: "disconnected"."""
        with self._refresh_lock, self._lock:
            token, self._token = self._token, None
            self._flow, self._error, self._title, self._service = None, None, None, None
            self._delete()
        if token is not None:
            self._revoke(token)
            logger.info("YouTube account disconnected")
            self._event("info", "ok: YouTube account disconnected")
        return self.status()

    def _revoke(self, token: dict) -> None:
        try:
            status, _ = self._post(REVOKE_URL, {"token": token.get("refresh_token")
                                                or token.get("access_token")})
        except Exception as exc:  # noqa: BLE001 - the local copy is gone either way
            logger.warning("token revoke failed: %s", type(exc).__name__)
            return
        if status != 200:
            logger.warning("token revoke answered %s", status)

    # -- credentials ------------------------------------------------------------------

    def credentials(self):
        """google.oauth2 Credentials with a fresh access token, or None if not connected.

        Refreshes when the access token is within a minute of expiry. ``invalid_grant``
        (revoked or expired) deletes the token, goes "disconnected", logs OAuthRevoked and
        returns None; any other refresh failure raises OAuthError and keeps the token.
        """
        token = self._fresh_token()
        if token is None:
            return None
        from google.oauth2.credentials import Credentials

        expiry = datetime.fromtimestamp(token["expires_at"], timezone.utc).replace(tzinfo=None)
        return Credentials(token=token["access_token"], expiry=expiry, scopes=[SCOPE])

    def _fresh_token(self) -> dict | None:
        if not self.configured:
            return None
        with self._refresh_lock:
            with self._lock:
                token = self._token
            if token is None:
                return None
            if float(token.get("expires_at") or 0) - REFRESH_MARGIN_S > self._clock():
                return token
            return self._refresh(token)

    def _refresh(self, token: dict) -> dict | None:
        data = {"client_id": self._client_id, "client_secret": self._client_secret,
                "refresh_token": token["refresh_token"], "grant_type": "refresh_token"}
        try:
            status, body = self._post(TOKEN_URL, data)
        except Exception as exc:  # noqa: BLE001
            logger.warning("token refresh failed: %s", type(exc).__name__)
            raise OAuthError("token refresh failed: could not reach Google") from None
        if status == 200 and body.get("access_token"):
            record = self._record(body, str(body.get("refresh_token") or token["refresh_token"]))
            with self._lock:
                if self._token is not token:
                    return self._token  # disconnected meanwhile
                self._save(record)
                self._token = record
            return record
        error = str(body.get("error") or status)
        if error == "invalid_grant":
            self._revoked()
            return None
        logger.warning("token refresh refused: %s", error)
        raise OAuthError(f"token refresh failed: {error}")

    def _revoked(self) -> None:
        with self._lock:
            self._token, self._title, self._service = None, None, None
            self._delete()
        logger.error("YouTube sign-in was revoked or expired; connect again")
        self._event("error", "OAuthRevoked: Google rejected the saved YouTube sign-in "
                             "(revoked or expired); connect again")

    # -- Data API ---------------------------------------------------------------------

    def call(self, make_request: Callable[[Any], Any], units: int = 1) -> dict:
        """Spend quota, then execute ``make_request(youtube_service)`` behind the meter lock.

        Raises OAuthNotConnected, QuotaExceeded or OAuthError.
        """
        service = self._service_for(self.credentials())
        self._meter.spend(units)
        return self._meter.execute(make_request(service))

    def _service_for(self, creds) -> Any:
        if creds is None:
            raise OAuthNotConnected("YouTube account not connected")
        with self._lock:
            if self._service is not None and self._service[0] == creds.token:
                return self._service[1]
        service = self._service_factory(creds)
        with self._lock:
            self._service = (creds.token, service)
        return service

    def channel_title(self) -> str | None:
        """The signed-in channel's title (channels.list mine=true, 1 unit), cached."""
        with self._lock:
            if self._title is not None:
                return self._title
        resp = self.call(lambda yt: yt.channels().list(part="snippet", mine=True))
        items = resp.get("items") or []
        title = ((items[0].get("snippet") or {}).get("title") if items else None) or None
        with self._lock:
            if self._token is not None:
                self._title = title
        return title

    def _safe_title(self) -> str | None:
        try:
            return self.channel_title()
        except Exception as exc:  # noqa: BLE001 - status must answer regardless
            logger.warning("could not read the channel title: %s", type(exc).__name__)
            return None

    # -- storage ----------------------------------------------------------------------

    def _load(self) -> dict | None:
        try:
            record = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            logger.warning("ignoring unreadable YouTube token file: %s", type(exc).__name__)
            return None
        if not isinstance(record, dict) or not isinstance(record.get("refresh_token"), str):
            logger.warning("ignoring YouTube token file without a refresh token")
            return None
        record.setdefault("access_token", "")
        record.setdefault("expires_at", 0)
        return record

    def _save(self, record: dict) -> None:
        """Write atomically with mode 600 (directory 700 when created)."""
        self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        tmp = self._path.with_name(self._path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(record, fh)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self._path)

    def _delete(self) -> None:
        try:
            self._path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.error("could not delete the YouTube token file: %s", type(exc).__name__)

    def _event(self, level: str, message: str) -> None:
        if self._db_factory is None:
            return
        try:
            with closing(self._db_factory()) as conn:
                log_event(conn, COMPONENT, level, message)
        except sqlite3.Error:
            logger.exception("could not log a youtube status event")
