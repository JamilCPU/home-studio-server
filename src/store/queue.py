"""Store for the play queue (fractional positions so inserts never renumber)."""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from typing import Literal

from src.store.videos import _VIDEO_COLUMNS, Video, _now, _row_to_video

logger = logging.getLogger(__name__)

ITEM_SELECT = f"""
SELECT {_VIDEO_COLUMNS},
  q.id AS q_id, q.position AS q_position, q.source AS q_source, q.reason AS q_reason,
  q.agent_request_id AS q_agent_request_id, q.status AS q_status, q.added_at AS q_added_at
FROM queue_items q
JOIN videos v ON v.video_id = q.video_id
"""


@dataclass(frozen=True)
class QueueItem:
    id: int
    video: Video
    position: float
    source: str
    reason: str | None
    agent_request_id: int | None
    status: str
    added_at: str


def row_to_item(row: sqlite3.Row) -> QueueItem:
    return QueueItem(
        id=row["q_id"],
        video=_row_to_video(row),
        position=row["q_position"],
        source=row["q_source"],
        reason=row["q_reason"],
        agent_request_id=row["q_agent_request_id"],
        status=row["q_status"],
        added_at=row["q_added_at"],
    )


def _get(conn: sqlite3.Connection, item_id: int) -> QueueItem | None:
    row = conn.execute(f"{ITEM_SELECT} WHERE q.id = ?", (item_id,)).fetchone()
    return row_to_item(row) if row else None


def _scalar(conn: sqlite3.Connection, sql: str) -> float | None:
    row = conn.execute(sql).fetchone()
    return None if row is None or row[0] is None else float(row[0])


def _first_queued_position(conn: sqlite3.Connection) -> float | None:
    return _scalar(conn, "SELECT MIN(position) FROM queue_items WHERE status = 'queued'")


def _playing_position(conn: sqlite3.Connection) -> float | None:
    return _scalar(conn, "SELECT MAX(position) FROM queue_items WHERE status = 'playing'")


def _next_position(conn: sqlite3.Connection) -> float:
    playing = _playing_position(conn)
    first = _first_queued_position(conn)
    if playing is None:
        return 1.0 if first is None else first - 1.0
    if first is None or first <= playing:
        return playing + 1.0
    return (playing + first) / 2.0


def _end_position(conn: sqlite3.Connection) -> float:
    top = _scalar(
        conn, "SELECT MAX(position) FROM queue_items WHERE status IN ('queued','playing')"
    )
    return 1.0 if top is None else top + 1.0


def enqueue(
    conn: sqlite3.Connection,
    video_id: str,
    source: str,
    reason: str | None = None,
    agent_request_id: int | None = None,
    where: Literal["next", "end"] = "end",
) -> int:
    """Queue a video; "next" slots it right after the playing item, "end" appends."""
    if where not in ("next", "end"):
        raise ValueError("where must be 'next' or 'end'")
    position = _next_position(conn) if where == "next" else _end_position(conn)
    cur = conn.execute(
        "INSERT INTO queue_items (video_id, position, source, reason, agent_request_id,"
        " status, added_at) VALUES (?, ?, ?, ?, ?, 'queued', ?)",
        (video_id, position, source, reason, agent_request_id, _now()),
    )
    conn.commit()
    return int(cur.lastrowid)


def upcoming(conn: sqlite3.Connection) -> list[QueueItem]:
    rows = conn.execute(
        f"{ITEM_SELECT} WHERE q.status = 'queued' ORDER BY q.position, q.id"
    ).fetchall()
    return [row_to_item(r) for r in rows]


def current(conn: sqlite3.Connection) -> QueueItem | None:
    row = conn.execute(
        f"{ITEM_SELECT} WHERE q.status = 'playing' ORDER BY q.id DESC LIMIT 1"
    ).fetchone()
    return row_to_item(row) if row else None


def mark(conn: sqlite3.Connection, item_id: int, status: str) -> None:
    conn.execute("UPDATE queue_items SET status = ? WHERE id = ?", (status, item_id))
    conn.commit()


def pop_next(conn: sqlite3.Connection) -> QueueItem | None:
    """Finish the playing item and start the lowest-position queued one (None if empty)."""
    row = conn.execute(
        "SELECT id FROM queue_items WHERE status = 'queued' ORDER BY position, id LIMIT 1"
    ).fetchone()
    conn.execute("UPDATE queue_items SET status = 'played' WHERE status = 'playing'")
    if row is None:
        conn.commit()
        return None
    conn.execute("UPDATE queue_items SET status = 'playing' WHERE id = ?", (row[0],))
    conn.commit()
    return _get(conn, row[0])


def previous(conn: sqlite3.Connection) -> QueueItem | None:
    """Replay the most recently played item (latest play start, so a reorder does not
    confuse it); the current one returns to the queue front."""
    row = conn.execute(
        "SELECT q.id, MAX(p.started_at) AS last_start FROM queue_items q"
        " LEFT JOIN plays p ON p.queue_item_id = q.id WHERE q.status = 'played'"
        " GROUP BY q.id ORDER BY last_start IS NULL, last_start DESC, q.id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    first = _first_queued_position(conn)
    playing = current(conn)
    if playing is not None:
        position = playing.position if first is None else first - 1.0
        conn.execute(
            "UPDATE queue_items SET status = 'queued', position = ? WHERE id = ?",
            (position, playing.id),
        )
    conn.execute("UPDATE queue_items SET status = 'playing' WHERE id = ?", (row[0],))
    conn.commit()
    return _get(conn, row[0])


def requeue_playing(conn: sqlite3.Connection) -> int:
    """Put every 'playing' item back at the front of the queue (restart recovery)."""
    rows = conn.execute(
        "SELECT id FROM queue_items WHERE status = 'playing' ORDER BY position, id"
    ).fetchall()
    if not rows:
        return 0
    first = _first_queued_position(conn)
    base = (len(rows) + 1.0 if first is None else first) - len(rows)  # just before `first`
    for offset, row in enumerate(rows):
        conn.execute(
            "UPDATE queue_items SET status = 'queued', position = ? WHERE id = ?",
            (base + offset, row[0]),
        )
    conn.commit()
    return len(rows)


def remove(conn: sqlite3.Connection, item_id: int) -> None:
    mark(conn, item_id, "removed")


def reorder(conn: sqlite3.Connection, ids: list[int]) -> None:
    """Set the queued order; ids must be exactly the currently queued ids."""
    queued = {r[0] for r in conn.execute("SELECT id FROM queue_items WHERE status = 'queued'")}
    if len(ids) != len(set(ids)) or set(ids) != queued:
        raise ValueError("ids must match the set of queued item ids")
    base = _playing_position(conn) or 0.0
    for offset, item_id in enumerate(ids, start=1):
        conn.execute("UPDATE queue_items SET position = ? WHERE id = ?", (base + offset, item_id))
    conn.commit()


def clear(conn: sqlite3.Connection) -> None:
    conn.execute("UPDATE queue_items SET status = 'removed' WHERE status = 'queued'")
    conn.commit()


def remaining_seconds(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT COALESCE(SUM(COALESCE(v.duration_s, 0)), 0) FROM queue_items q"
        " JOIN videos v ON v.video_id = q.video_id WHERE q.status = 'queued'"
    ).fetchone()
    return int(row[0])
