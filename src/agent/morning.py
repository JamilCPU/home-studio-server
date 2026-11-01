"""Morning set: the request prompt and the fallback when the agent fails."""

from __future__ import annotations

import logging
import sqlite3

from src.store.queue import enqueue
from src.store.settings import MorningSettings, imported, log_event
from src.store.videos import disliked_video_ids, played_video_ids

logger = logging.getLogger(__name__)

COMPONENT = "scheduler"
FALLBACK_SOURCE = "fallback"
FALLBACK_IMPORT_KINDS = ("recommended", "subscription")
FALLBACK_REPEAT_DAYS = 14
NEVER_PLAYED_DAYS = 36500

PROMPT_TEMPLATE = (
    "Morning set: pick {count} podcast-style or long-form videos for a wake-up listen, "
    "about: {topics}. Prefer recent, substantive episodes; avoid shorts, clips and music."
)


def morning_prompt(settings: MorningSettings) -> str:
    """Request text built only from the fixed template plus the topics and count."""
    return PROMPT_TEMPLATE.format(count=settings.count, topics=", ".join(settings.topics))


def _unplayed_imports(conn: sqlite3.Connection, skip: set[str]) -> list[str]:
    ids: list[str] = []
    for kind in FALLBACK_IMPORT_KINDS:
        ids.extend(r["video_id"] for r in imported(conn, kind) if r["video_id"])
    if not ids:
        return []
    marks = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"SELECT video_id FROM videos WHERE video_id IN ({marks})"
        " ORDER BY duration_s IS NULL, duration_s DESC, video_id",
        ids,
    ).fetchall()
    return [r[0] for r in rows if r[0] not in skip]


def _top_rated(conn: sqlite3.Connection, skip: set[str]) -> list[str]:
    rows = conn.execute(
        "SELECT r.video_id FROM ratings r"
        " WHERE r.id = (SELECT MAX(id) FROM ratings WHERE video_id = r.video_id)"
        " AND r.satisfaction IS NOT NULL ORDER BY r.satisfaction DESC, r.id DESC"
    ).fetchall()
    return [r[0] for r in rows if r[0] not in skip]


def _already_queued(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT video_id FROM queue_items WHERE status IN ('queued','playing')"
    ).fetchall()
    return {r[0] for r in rows}


def fallback_morning(conn: sqlite3.Connection, count: int) -> list[int]:
    """Queue up to ``count`` unplayed imports, then favourites, as the fallback morning set."""
    never_played = played_video_ids(conn, NEVER_PLAYED_DAYS)
    recent = played_video_ids(conn, FALLBACK_REPEAT_DAYS)
    skip = disliked_video_ids(conn) | _already_queued(conn)
    chosen: list[str] = []
    for video_id in _unplayed_imports(conn, skip | never_played) + _top_rated(
        conn, skip | recent
    ):
        if video_id not in chosen:
            chosen.append(video_id)
        if len(chosen) >= count:
            break
    queue_ids = [
        enqueue(conn, video_id, FALLBACK_SOURCE, reason="fallback morning pick", where="end")
        for video_id in chosen
    ]
    log_event(conn, COMPONENT, "warn", f"fallback: queued {len(queue_ids)}")
    return queue_ids
