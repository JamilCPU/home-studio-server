"""Store for videos, plays, ratings and listening history."""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

_VIDEO_COLUMNS = (
    "v.video_id, v.title, v.channel_name, v.channel_id, v.description, v.duration_s, "
    "v.published_at, v.tags_json, v.category, v.chapters_json, v.thumbnail_url, "
    "v.view_count, v.like_count, v.transcript"
)


@dataclass(frozen=True)
class Chapter:
    title: str
    start_s: float


@dataclass(frozen=True)
class Video:
    video_id: str
    title: str
    channel_name: str | None
    channel_id: str | None
    description: str | None
    duration_s: int | None
    published_at: str | None
    tags: tuple[str, ...]
    category: str | None
    chapters: tuple[Chapter, ...]
    thumbnail_url: str | None
    view_count: int | None
    like_count: int | None
    transcript: str | None


@dataclass(frozen=True)
class Rating:
    id: int
    liked: bool | None
    satisfaction: int | None
    notes: str | None
    created_at: str


@dataclass(frozen=True)
class HistoryEntry:
    play_id: int
    video: Video
    started_at: str
    seconds_listened: float | None
    end_reason: str | None
    source: str | None
    latest_rating: Rating | None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_video(row: sqlite3.Row) -> Video:
    tags = json.loads(row["tags_json"]) if row["tags_json"] else []
    chapters = json.loads(row["chapters_json"]) if row["chapters_json"] else []
    return Video(
        video_id=row["video_id"],
        title=row["title"],
        channel_name=row["channel_name"],
        channel_id=row["channel_id"],
        description=row["description"],
        duration_s=row["duration_s"],
        published_at=row["published_at"],
        tags=tuple(tags),
        category=row["category"],
        chapters=tuple(Chapter(title=c["title"], start_s=float(c["start_s"])) for c in chapters),
        thumbnail_url=row["thumbnail_url"],
        view_count=row["view_count"],
        like_count=row["like_count"],
        transcript=row["transcript"],
    )


def upsert_video(conn: sqlite3.Connection, v: Video) -> None:
    """Insert or update a video; a None transcript never clobbers a stored one."""
    conn.execute(
        """
        INSERT INTO videos (video_id, title, channel_name, channel_id, description, duration_s,
          published_at, tags_json, category, chapters_json, thumbnail_url, view_count,
          like_count, transcript, first_seen)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(video_id) DO UPDATE SET
          title=excluded.title, channel_name=excluded.channel_name,
          channel_id=excluded.channel_id, description=excluded.description,
          duration_s=excluded.duration_s, published_at=excluded.published_at,
          tags_json=excluded.tags_json, category=excluded.category,
          chapters_json=excluded.chapters_json, thumbnail_url=excluded.thumbnail_url,
          view_count=excluded.view_count, like_count=excluded.like_count,
          transcript=COALESCE(excluded.transcript, videos.transcript)
        """,
        (
            v.video_id,
            v.title,
            v.channel_name,
            v.channel_id,
            v.description,
            v.duration_s,
            v.published_at,
            json.dumps(list(v.tags)),
            v.category,
            json.dumps([{"title": c.title, "start_s": c.start_s} for c in v.chapters]),
            v.thumbnail_url,
            v.view_count,
            v.like_count,
            v.transcript,
            _now(),
        ),
    )
    conn.commit()


def get_video(conn: sqlite3.Connection, video_id: str) -> Video | None:
    row = conn.execute(
        f"SELECT {_VIDEO_COLUMNS} FROM videos v WHERE v.video_id = ?", (video_id,)
    ).fetchone()
    return _row_to_video(row) if row else None


def set_transcript(conn: sqlite3.Connection, video_id: str, text: str) -> None:
    conn.execute("UPDATE videos SET transcript = ? WHERE video_id = ?", (text, video_id))
    conn.commit()


def start_play(conn: sqlite3.Connection, video_id: str, queue_item_id: int | None) -> int:
    cur = conn.execute(
        "INSERT INTO plays (video_id, queue_item_id, started_at) VALUES (?, ?, ?)",
        (video_id, queue_item_id, _now()),
    )
    conn.commit()
    return int(cur.lastrowid)


def end_play(conn: sqlite3.Connection, play_id: int, seconds: float, reason: str) -> None:
    conn.execute(
        "UPDATE plays SET ended_at = ?, seconds_listened = ?, end_reason = ? WHERE id = ?",
        (_now(), seconds, reason, play_id),
    )
    conn.commit()


def add_rating(
    conn: sqlite3.Connection,
    video_id: str,
    liked: bool | None,
    satisfaction: int | None,
    notes: str | None,
) -> int:
    """Record a rating (every row is kept). Raises ValueError on empty/invalid input."""
    if liked is None and satisfaction is None and notes is None:
        raise ValueError("a rating needs liked, satisfaction or notes")
    if satisfaction is not None and not 1 <= satisfaction <= 10:
        raise ValueError("satisfaction must be between 1 and 10")
    cur = conn.execute(
        "INSERT INTO ratings (video_id, liked, satisfaction, notes, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (video_id, None if liked is None else int(liked), satisfaction, notes, _now()),
    )
    conn.commit()
    return int(cur.lastrowid)


_HISTORY_SELECT = f"""
SELECT {_VIDEO_COLUMNS},
  p.id AS play_id, p.started_at AS play_started_at, p.seconds_listened AS play_seconds,
  p.end_reason AS play_end_reason, q.source AS queue_source,
  r.id AS r_id, r.liked AS r_liked, r.satisfaction AS r_satisfaction,
  r.notes AS r_notes, r.created_at AS r_created_at
FROM plays p
JOIN videos v ON v.video_id = p.video_id
LEFT JOIN queue_items q ON q.id = p.queue_item_id
LEFT JOIN ratings r ON r.id = (SELECT MAX(id) FROM ratings WHERE video_id = p.video_id)
"""


def _row_to_entry(row: sqlite3.Row) -> HistoryEntry:
    rating = None
    if row["r_id"] is not None:
        liked = row["r_liked"]
        rating = Rating(
            id=row["r_id"],
            liked=None if liked is None else bool(liked),
            satisfaction=row["r_satisfaction"],
            notes=row["r_notes"],
            created_at=row["r_created_at"],
        )
    return HistoryEntry(
        play_id=row["play_id"],
        video=_row_to_video(row),
        started_at=row["play_started_at"],
        seconds_listened=row["play_seconds"],
        end_reason=row["play_end_reason"],
        source=row["queue_source"],
        latest_rating=rating,
    )


def history(
    conn: sqlite3.Connection, *, unrated_only: bool = False, limit: int = 50, offset: int = 0
) -> list[HistoryEntry]:
    """Plays newest first; unrated_only keeps plays whose video has no rating yet."""
    where = "WHERE r.id IS NULL" if unrated_only else ""
    rows = conn.execute(
        f"{_HISTORY_SELECT} {where} ORDER BY p.id DESC LIMIT ? OFFSET ?", (limit, offset)
    ).fetchall()
    return [_row_to_entry(r) for r in rows]


def recent_feedback(conn: sqlite3.Connection, limit: int = 50) -> list[HistoryEntry]:
    """Plays that were rated or skipped, newest first (context for the agent)."""
    rows = conn.execute(
        f"{_HISTORY_SELECT} WHERE r.id IS NOT NULL OR p.end_reason = 'skipped'"
        " ORDER BY p.id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [_row_to_entry(r) for r in rows]


def played_video_ids(conn: sqlite3.Connection, since_days: int) -> set[str]:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=since_days)).isoformat()
    rows = conn.execute(
        "SELECT DISTINCT video_id FROM plays WHERE started_at >= ?", (cutoff,)
    ).fetchall()
    return {r[0] for r in rows}


def disliked_video_ids(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT DISTINCT video_id FROM ratings WHERE liked = 0").fetchall()
    return {r[0] for r in rows}
