"""Morning settings and system status endpoints."""

from __future__ import annotations

import math
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from yt_dlp.version import __version__ as YTDLP_VERSION

from src.api.app import Deps, ok
from src.api.models import morning_out
from src.api.routes_youtube import youtube_account_state
from src.scheduler import last_outcome, next_build_at
from src.player.engine import OK_MESSAGE
from src.player.mpv import MpvCommandError, MpvDied
from src.store.settings import (
    MorningSettings,
    get_morning,
    latest_event,
    quota_used,
    recent_events,
    save_morning,
)

QUOTA_TZ = ZoneInfo("America/Los_Angeles")  # same Pacific quota day as DataApi
EVENT_LIMIT = 20
_CLAUDE_ERRORS = {
    "ClaudeRateLimited": "rate_limited",
    "ClaudeNotLoggedIn": "not_logged_in",
    "ClaudeNotFound": "missing",
}

SIGNIN_COOKIES = frozenset({
    "SID", "__Secure-1PSID", "__Secure-3PSID", "LOGIN_INFO", "SAPISID", "__Secure-3PAPISID",
})
_SIGNIN_DOMAINS = ("youtube.com", "google.com")
_HTTPONLY_PREFIX = "#HttpOnly_"
# path -> (mtime_ns, size, latest sign-in expiry); avoids re-reading on every poll
_signin_cache: dict[str, tuple[int, int, float | None]] = {}

Day = Annotated[int, Field(ge=0, le=6)]
Topic = Annotated[str, Field(min_length=1, max_length=100)]


class MorningBody(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)
    enabled: bool
    wake_time: str = Field(pattern=r"^\d{2}:\d{2}$")
    days: list[Day] = Field(max_length=7)
    topics: list[Topic] = Field(max_length=20)
    count: int = Field(ge=1, le=6)
    build_lead_min: int | None = Field(default=None, ge=1, le=60)  # omitted: keep current


def quota_day(now: datetime | None = None) -> str:
    return (now or datetime.now(QUOTA_TZ)).astimezone(QUOTA_TZ).date().isoformat()


def _domain_matches(domain: str) -> bool:
    host = domain.lstrip(".").lower()
    return any(host == d or host.endswith("." + d) for d in _SIGNIN_DOMAINS)


def read_signin_expiry(path: Path) -> float | None:
    """Latest expiry (epoch seconds, inf for session cookies) among the YouTube/Google
    sign-in cookies in a Netscape cookies file, or None if there are none. Only cookie
    names are inspected; values are never kept or logged."""
    latest: float | None = None
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith(_HTTPONLY_PREFIX):
            line = line[len(_HTTPONLY_PREFIX):]
        elif line.startswith("#"):
            continue
        fields = line.split(chr(9))
        if len(fields) < 7 or fields[5] not in SIGNIN_COOKIES or not _domain_matches(fields[0]):
            continue
        try:
            expiry = float(fields[4])
        except ValueError:
            continue
        expiry = math.inf if expiry == 0 else expiry
        latest = expiry if latest is None else max(latest, expiry)
    return latest


def has_signin_cookies(path: Path) -> bool:
    """True if the file holds a non-expired sign-in cookie; cached on (mtime, size)."""
    st = path.stat()
    key = str(path)
    cached = _signin_cache.get(key)
    if cached is None or cached[:2] != (st.st_mtime_ns, st.st_size):
        cached = (st.st_mtime_ns, st.st_size, read_signin_expiry(path))
        _signin_cache[key] = cached
    expiry = cached[2]
    return expiry is not None and expiry > time.time()


def cookies_status(conn: sqlite3.Connection, cookies_path: str) -> str:
    """"missing" without a file or without YouTube sign-in cookies (yt-dlp/mpv create
    anonymous-only files); "expired" only while the last yt-dlp answer was a sign-in
    check made after the cookies file was last written; re-exporting clears it."""
    path = Path(cookies_path)
    try:
        if not has_signin_cookies(path):
            return "missing"
    except OSError:
        return "missing"
    event = latest_event(conn, "ytdlp")
    if event is None or event[0] != "error" or not event[1].startswith("SourceAuthError"):
        return "ok"
    written = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
    try:
        seen = datetime.fromisoformat(event[2])
    except ValueError:
        return "expired"
    return "expired" if seen > written else "ok"


def source_status(conn: sqlite3.Connection) -> str:
    """"broken" after SourceBroken until the next successful play ("ok: playing")."""
    row = conn.execute(
        "SELECT message FROM status_events WHERE component = 'player'"
        " AND (message LIKE 'SourceBroken%' OR message = ?) ORDER BY id DESC LIMIT 1",
        (OK_MESSAGE,),
    ).fetchone()
    return "broken" if row is not None and row[0].startswith("SourceBroken") else "ok"


def claude_status(conn: sqlite3.Connection) -> str:
    event = latest_event(conn, "agent")
    if event is None:
        return "unknown"
    level, message, _ = event
    if message.startswith("ok"):
        return "ok"
    error_class = message.split(":", 1)[0].strip()
    if level == "error" and error_class in _CLAUDE_ERRORS:
        return _CLAUDE_ERRORS[error_class]
    return "ok"


def _player_state(engine) -> str:
    try:
        return engine.now().state
    except (MpvDied, MpvCommandError, TimeoutError):
        return "unavailable"


def morning_response(conn: sqlite3.Connection, tz: str) -> dict:
    """MorningSettings plus when the next build starts and how the last morning went."""
    settings = get_morning(conn)
    nxt = next_build_at(conn, settings, datetime.now(ZoneInfo(tz)), tz)
    return {
        **morning_out(settings),
        "next_build_at": nxt.isoformat() if nxt is not None else None,
        "last_outcome": last_outcome(conn, tz),
    }


def build_router(deps: Deps) -> APIRouter:
    router = APIRouter()
    tz = deps.config.timezone

    @router.get("/morning")
    def get_settings():
        with closing(deps.db_factory()) as conn:
            return ok(morning_response(conn, tz))

    @router.put("/morning")
    def put_settings(body: MorningBody):
        with closing(deps.db_factory()) as conn:
            lead = body.build_lead_min or get_morning(conn).build_lead_min
            settings = MorningSettings(
                enabled=body.enabled, wake_time=body.wake_time, days=tuple(body.days),
                topics=tuple(body.topics), count=body.count, build_lead_min=lead,
            )
            try:
                save_morning(conn, settings)
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from None
            return ok(morning_response(conn, tz))

    @router.post("/morning/test")
    def morning_test():
        return ok({"request_id": deps.scheduler_run_now()}, 202)

    @router.get("/status")
    def status():
        with closing(deps.db_factory()) as conn:
            events = [
                {"component": r["component"], "level": r["level"], "message": r["message"],
                 "created_at": r["created_at"]}
                for r in recent_events(conn, EVENT_LIMIT)
            ]
            return ok({
                "player": _player_state(deps.engine),
                "bluetooth": deps.bt.last_state,
                "cookies": cookies_status(conn, deps.config.cookies_path),
                "youtube_account": youtube_account_state(deps.youtube_oauth),
                "claude": claude_status(conn),
                "source": source_status(conn),
                "ytdlp_version": YTDLP_VERSION,
                "quota_used_today": quota_used(conn, quota_day()),
                "quota_ceiling": deps.config.quota_ceiling,
                "events": events,
            })

    return router
