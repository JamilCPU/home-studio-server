"""Store for morning settings, API quota, status events and account imports."""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass

from src.store.videos import _now

logger = logging.getLogger(__name__)

_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


@dataclass(frozen=True)
class MorningSettings:
    enabled: bool
    wake_time: str
    days: tuple[int, ...]
    topics: tuple[str, ...]
    count: int
    build_lead_min: int = 5  # the build starts this many minutes before wake time


def get_morning(conn: sqlite3.Connection) -> MorningSettings:
    row = conn.execute("SELECT * FROM morning_settings WHERE id = 1").fetchone()
    return MorningSettings(
        enabled=bool(row["enabled"]),
        wake_time=row["wake_time"],
        days=tuple(json.loads(row["days_json"])),
        topics=tuple(json.loads(row["topics_json"])),
        count=row["count"],
        build_lead_min=row["build_lead_min"],
    )


def _validate(s: MorningSettings) -> None:
    if not _TIME_RE.match(s.wake_time):
        raise ValueError("wake_time must be HH:MM (24-hour)")
    if any(not isinstance(d, int) or not 0 <= d <= 6 for d in s.days):
        raise ValueError("days must be integers 0-6")
    if not 1 <= s.count <= 6:
        raise ValueError("count must be between 1 and 6")
    if not s.topics:
        raise ValueError("topics must not be empty")
    if not 1 <= s.build_lead_min <= 60:
        raise ValueError("build_lead_min must be between 1 and 60")


def save_morning(conn: sqlite3.Connection, s: MorningSettings) -> None:
    _validate(s)
    conn.execute(
        "UPDATE morning_settings SET enabled = ?, wake_time = ?, days_json = ?,"
        " topics_json = ?, count = ?, build_lead_min = ? WHERE id = 1",
        (
            int(s.enabled),
            s.wake_time,
            json.dumps(list(s.days)),
            json.dumps(list(s.topics)),
            s.count,
            s.build_lead_min,
        ),
    )
    conn.commit()


def add_quota(conn: sqlite3.Connection, day: str, units: int) -> int:
    """Add units to a day's quota counter and return the new total."""
    conn.execute(
        "INSERT INTO api_quota (day, units) VALUES (?, ?)"
        " ON CONFLICT(day) DO UPDATE SET units = units + excluded.units",
        (day, units),
    )
    conn.commit()
    return quota_used(conn, day)


def quota_used(conn: sqlite3.Connection, day: str) -> int:
    row = conn.execute("SELECT units FROM api_quota WHERE day = ?", (day,)).fetchone()
    return int(row[0]) if row else 0


def log_event(conn: sqlite3.Connection, component: str, level: str, message: str) -> None:
    conn.execute(
        "INSERT INTO status_events (component, level, message, created_at) VALUES (?, ?, ?, ?)",
        (component, level, message, _now()),
    )
    conn.commit()


def recent_events(conn: sqlite3.Connection, limit: int = 20) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM status_events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()


def latest_event(conn: sqlite3.Connection, component: str) -> tuple[str, str, str] | None:
    row = conn.execute(
        "SELECT level, message, created_at FROM status_events WHERE component = ?"
        " ORDER BY id DESC LIMIT 1",
        (component,),
    ).fetchone()
    return (row[0], row[1], row[2]) if row else None


def record_import(
    conn: sqlite3.Connection,
    kind: str,
    *,
    video_id: str | None = None,
    channel_id: str | None = None,
    title: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO account_imports (kind, video_id, channel_id, title, imported_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (kind, video_id, channel_id, title, _now()),
    )
    conn.commit()


def imported(conn: sqlite3.Connection, kind: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM account_imports WHERE kind = ? ORDER BY id", (kind,)
    ).fetchall()
