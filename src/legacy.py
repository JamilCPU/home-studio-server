"""One-time import of the old YoutubeBrainwash JSON data."""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.store.agent import save_taste
from src.store.videos import Video, get_video, upsert_video

logger = logging.getLogger(__name__)

_RATING_LIKED = {"thumbs_up": 1, "thumbs_down": 0, "neutral": None}
_NO_TIMESTAMP = "1970-01-01T00:00:00+00:00"
_SUMMARY_TOPICS = 5
_MAX_LIST = 10


def _load(path: Path) -> Any:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("could not read legacy file %s", path.name)
        return None


def _minimal_video(video_id: str, title: str) -> Video:
    return Video(
        video_id=video_id, title=title, channel_name=None, channel_id=None, description=None,
        duration_s=None, published_at=None, tags=(), category=None, chapters=(),
        thumbnail_url=None, view_count=None, like_count=None, transcript=None,
    )


def _normalize_timestamp(value: Any) -> str:
    """Return a UTC ISO string like the store's _now(); naive input is taken as UTC."""
    if not value:
        return _NO_TIMESTAMP
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return _NO_TIMESTAMP
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def _text(value: Any) -> str | None:
    return None if value is None or value == "" else str(value)


def _import_ratings(conn: sqlite3.Connection, entries: Any) -> tuple[int, int]:
    """Insert legacy ratings. Re-runs skip rows already imported (same video/time/value)."""
    imported = skipped = 0
    if not isinstance(entries, list):
        return 0, 0
    for entry in entries:
        if not isinstance(entry, dict) or not entry.get("video_id"):
            skipped += 1
            continue
        rating = entry.get("rating")
        if not isinstance(rating, str) or rating not in _RATING_LIKED:
            skipped += 1
            continue
        liked = _RATING_LIKED[rating]
        notes = _text(entry.get("comment"))
        if liked is None and notes is None:
            skipped += 1  # would violate the ratings CHECK constraint
            continue
        video_id = str(entry["video_id"])
        created = _normalize_timestamp(entry.get("timestamp"))
        duplicate = conn.execute(
            "SELECT 1 FROM ratings WHERE video_id = ? AND created_at = ? AND liked IS ?"
            " AND notes IS ?",
            (video_id, created, liked, notes),
        ).fetchone()
        if duplicate:
            continue
        if get_video(conn, video_id) is None:
            upsert_video(conn, _minimal_video(video_id, _text(entry.get("title")) or ""))
        conn.execute(
            "INSERT INTO ratings (video_id, liked, satisfaction, notes, created_at)"
            " VALUES (?, ?, NULL, ?, ?)",
            (video_id, liked, notes, created),
        )
        imported += 1
    conn.commit()
    return imported, skipped


def _import_taste(conn: sqlite3.Connection, profile: Any) -> int:
    if conn.execute("SELECT 1 FROM taste_profiles WHERE source = 'legacy'").fetchone():
        return 0
    topics = profile.get("topics") if isinstance(profile, dict) else None
    if not isinstance(topics, dict):
        return 0
    weighted = [(str(k), v) for k, v in topics.items() if isinstance(v, (int, float))]
    ranked = sorted(weighted, key=lambda kv: kv[1], reverse=True)
    likes = [k for k, v in ranked if v > 0][:_MAX_LIST]
    dislikes = [k for k, v in reversed(ranked) if v < 0][:_MAX_LIST]
    if not likes and not dislikes:
        return 0
    if likes:
        summary = "Interested in: " + ", ".join(likes[:_SUMMARY_TOPICS])
    else:
        summary = "Avoids: " + ", ".join(dislikes[:_SUMMARY_TOPICS])
    save_taste(conn, summary, likes, dislikes, "legacy")
    return 1


def migrate_legacy(conn: sqlite3.Connection, data_dir: Path) -> dict[str, int]:
    data_dir = Path(data_dir)
    imported, skipped = _import_ratings(conn, _load(data_dir / "feedback.json"))
    taste = _import_taste(conn, _load(data_dir / "interest_profile.json"))
    return {"ratings": imported, "skipped": skipped, "taste": taste}
