"""Store for agent requests, taste profiles and taste corrections."""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass

from src.store.queue import ITEM_SELECT, QueueItem, row_to_item
from src.store.videos import _now

logger = logging.getLogger(__name__)

_FINISHED = ("done", "failed")


@dataclass(frozen=True)
class AgentRequest:
    id: int
    prompt: str
    kind: str
    play: str
    status: str
    summary: str | None
    error: str | None
    created_at: str
    finished_at: str | None


@dataclass(frozen=True)
class TasteProfile:
    id: int
    summary: str
    likes: tuple[str, ...]
    dislikes: tuple[str, ...]
    source: str
    created_at: str


def _row_to_request(row: sqlite3.Row) -> AgentRequest:
    return AgentRequest(
        id=row["id"],
        prompt=row["prompt"],
        kind=row["kind"],
        play=row["play"],
        status=row["status"],
        summary=row["summary"],
        error=row["error"],
        created_at=row["created_at"],
        finished_at=row["finished_at"],
    )


def create_request(conn: sqlite3.Connection, prompt: str, kind: str, play: str) -> int:
    cur = conn.execute(
        "INSERT INTO agent_requests (prompt, kind, play, status, created_at)"
        " VALUES (?, ?, ?, 'pending', ?)",
        (prompt, kind, play, _now()),
    )
    conn.commit()
    return int(cur.lastrowid)


def set_status(
    conn: sqlite3.Connection,
    request_id: int,
    status: str,
    *,
    summary: str | None = None,
    error: str | None = None,
) -> None:
    """Update status; done/failed also stamp finished_at. None summary/error keep old values."""
    finished = _now() if status in _FINISHED else None
    conn.execute(
        "UPDATE agent_requests SET status = ?, summary = COALESCE(?, summary),"
        " error = COALESCE(?, error), finished_at = COALESCE(?, finished_at) WHERE id = ?",
        (status, summary, error, finished, request_id),
    )
    conn.commit()


def get_request(conn: sqlite3.Connection, request_id: int) -> AgentRequest | None:
    row = conn.execute("SELECT * FROM agent_requests WHERE id = ?", (request_id,)).fetchone()
    return _row_to_request(row) if row else None


def list_requests(conn: sqlite3.Connection, limit: int = 20) -> list[AgentRequest]:
    rows = conn.execute(
        "SELECT * FROM agent_requests ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [_row_to_request(r) for r in rows]


def queued_for_request(conn: sqlite3.Connection, request_id: int) -> list[QueueItem]:
    """Every queue item the request added (any status), in queue order."""
    rows = conn.execute(
        f"{ITEM_SELECT} WHERE q.agent_request_id = ? ORDER BY q.position, q.id", (request_id,)
    ).fetchall()
    return [row_to_item(r) for r in rows]


def save_taste(
    conn: sqlite3.Connection,
    summary: str,
    likes: list[str] | tuple[str, ...],
    dislikes: list[str] | tuple[str, ...],
    source: str,
) -> int:
    cur = conn.execute(
        "INSERT INTO taste_profiles (summary, likes_json, dislikes_json, source, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (summary, json.dumps(list(likes)), json.dumps(list(dislikes)), source, _now()),
    )
    conn.commit()
    return int(cur.lastrowid)


def latest_taste(conn: sqlite3.Connection) -> TasteProfile | None:
    row = conn.execute("SELECT * FROM taste_profiles ORDER BY id DESC LIMIT 1").fetchone()
    if row is None:
        return None
    return TasteProfile(
        id=row["id"],
        summary=row["summary"],
        likes=tuple(json.loads(row["likes_json"])),
        dislikes=tuple(json.loads(row["dislikes_json"])),
        source=row["source"],
        created_at=row["created_at"],
    )


def add_correction(conn: sqlite3.Connection, text: str) -> int:
    cur = conn.execute(
        "INSERT INTO taste_corrections (text, created_at, applied) VALUES (?, ?, 0)",
        (text, _now()),
    )
    conn.commit()
    return int(cur.lastrowid)


def pending_corrections(conn: sqlite3.Connection) -> list[tuple[int, str]]:
    rows = conn.execute(
        "SELECT id, text FROM taste_corrections WHERE applied = 0 ORDER BY id"
    ).fetchall()
    return [(r[0], r[1]) for r in rows]


def mark_corrections_applied(conn: sqlite3.Connection, ids: list[int]) -> None:
    conn.executemany("UPDATE taste_corrections SET applied = 1 WHERE id = ?", [(i,) for i in ids])
    conn.commit()


def ratings_since_last_taste(conn: sqlite3.Connection) -> int:
    latest = conn.execute(
        "SELECT created_at FROM taste_profiles ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if latest is None:
        row = conn.execute("SELECT COUNT(*) FROM ratings").fetchone()
    else:
        row = conn.execute(
            "SELECT COUNT(*) FROM ratings WHERE created_at > ?", (latest[0],)
        ).fetchone()
    return int(row[0])
