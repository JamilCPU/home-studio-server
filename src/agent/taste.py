"""Taste profile: refresh from feedback, user edits, and seeding from account imports."""

from __future__ import annotations

import logging
import sqlite3
from string import Template
from typing import Any, Literal

from src.agent.claude_cli import ClaudeBadOutput, ClaudeRunner, extract_json
from src.agent.context import build_taste_context
from src.store.agent import (
    TasteProfile,
    latest_taste,
    mark_corrections_applied,
    pending_corrections,
    ratings_since_last_taste,
    save_taste,
)
from src.store.settings import imported

logger = logging.getLogger(__name__)

MAX_SUMMARY_CHARS = 400
MAX_TRAITS = 10
REFRESH_RATING_THRESHOLD = 5
MAX_SEED_LINES = 200
IMPORT_KINDS = ("subscription", "liked", "playlist", "history", "recommended")

_SCHEMA = (
    'Reply with JSON only, no prose and no code fences. Schema:\n'
    '{"summary": "<plain-language summary, at most 400 characters>", '
    '"likes": ["<short trait>", ...], "dislikes": ["<short trait>", ...]}\n\n'
    "Rules:\n"
    "- At most 10 likes and 10 dislikes, each a short plain-language phrase.\n"
    "- Write for a person to read and edit, not for a machine.\n"
)

REFRESH_PROMPT = Template(
    "You maintain a short taste profile of what one listener likes and dislikes "
    "on a personal listening station.\n\n"
    + _SCHEMA
    + "- Update the current profile using the recent feedback; keep what still holds.\n"
    "$corrections_rule\n"
    "Current taste profile and recent feedback:\n$context\n\n"
    "$corrections"
)

SEED_PROMPT = Template(
    "You write a first taste profile of what one listener likes and dislikes on a "
    "personal listening station, from their YouTube account.\n\n"
    + _SCHEMA
    + "- Infer from the account data only. Dislikes may be empty if nothing suggests any.\n\n"
    "Account data (kind | title | channel id):\n$lines\n"
)

_CORRECTIONS_RULE = (
    "- The listener's own corrections below are their exact words and take priority "
    "over anything inferred from feedback.\n"
)


def _clean_traits(value: Any, key: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
        raise ClaudeBadOutput(f"taste: '{key}' must be a list of strings")
    return [t for t in (x.strip() for x in value) if t][:MAX_TRAITS]


def _parse_taste(raw: str) -> tuple[str, list[str], list[str]]:
    data = extract_json(raw)
    if not isinstance(data, dict):
        raise ClaudeBadOutput("taste: expected a JSON object")
    summary = data.get("summary")
    if not isinstance(summary, str):
        raise ClaudeBadOutput("taste: 'summary' must be a string")
    likes = _clean_traits(data.get("likes"), "likes")
    dislikes = _clean_traits(data.get("dislikes"), "dislikes")
    return summary.strip()[:MAX_SUMMARY_CHARS], likes, dislikes


def _saved(conn: sqlite3.Connection, summary: str, likes, dislikes, source: str) -> TasteProfile:
    save_taste(conn, summary, likes, dislikes, source)
    taste = latest_taste(conn)
    assert taste is not None
    return taste


def refresh_taste(conn: sqlite3.Connection, *, claude: ClaudeRunner) -> TasteProfile:
    """Ask Claude for an updated profile and save it as a new agent version."""
    corrections = pending_corrections(conn)
    if corrections:
        quoted = "\n".join(f'- "{text}"' for _, text in corrections)
        block = f"Listener corrections (their own words):\n{quoted}\n"
        rule = _CORRECTIONS_RULE
    else:
        block, rule = "", ""
    prompt = REFRESH_PROMPT.substitute(
        corrections_rule=rule, context=build_taste_context(conn), corrections=block
    )
    summary, likes, dislikes = _parse_taste(claude(prompt))
    taste = _saved(conn, summary, likes, dislikes, "agent")
    mark_corrections_applied(conn, [cid for cid, _ in corrections])
    return taste


def should_refresh(conn: sqlite3.Connection) -> bool:
    if ratings_since_last_taste(conn) >= REFRESH_RATING_THRESHOLD:
        return True
    if pending_corrections(conn):
        return True
    if latest_taste(conn) is None:
        return conn.execute("SELECT COUNT(*) FROM ratings").fetchone()[0] >= 1
    return False


def remove_trait(
    conn: sqlite3.Connection, kind: Literal["likes", "dislikes"], text: str
) -> TasteProfile:
    """Save a user version of the latest profile without the given like/dislike."""
    if kind not in ("likes", "dislikes"):
        raise ValueError(f"unknown trait kind {kind!r}")
    taste = latest_taste(conn)
    if taste is None:
        raise ValueError("no taste profile yet")
    target = text.strip()
    current = list(getattr(taste, kind))
    if target not in current:
        raise ValueError(f"{target!r} is not in {kind}")
    current.remove(target)
    likes = current if kind == "likes" else list(taste.likes)
    dislikes = current if kind == "dislikes" else list(taste.dislikes)
    return _saved(conn, taste.summary, likes, dislikes, "user")


def seed_taste_from_imports(conn: sqlite3.Connection, *, claude: ClaudeRunner) -> TasteProfile | None:
    """Build a first profile from account imports; None if one exists or nothing was imported."""
    if latest_taste(conn) is not None:
        return None
    rows = [row for kind in IMPORT_KINDS for row in imported(conn, kind)]
    if not rows:
        return None
    lines = "\n".join(
        f"{r['kind']} | {r['title'] or ''} | {r['channel_id'] or ''}" for r in rows[:MAX_SEED_LINES]
    )
    summary, likes, dislikes = _parse_taste(claude(SEED_PROMPT.substitute(lines=lines)))
    return _saved(conn, summary, likes, dislikes, "agent")
