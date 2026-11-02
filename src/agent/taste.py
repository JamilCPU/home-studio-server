"""Taste profile: refresh from feedback, user edits, and seeding from account imports."""

from __future__ import annotations

import logging
import sqlite3
from itertools import chain, zip_longest
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
    "$legacy"
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


def _only_legacy_profiles(conn: sqlite3.Connection) -> bool:
    """True with no profile at all, or when every profile came from the legacy app."""
    row = conn.execute("SELECT COUNT(*) FROM taste_profiles WHERE source != 'legacy'").fetchone()
    return row[0] == 0


def _balanced(rows_by_kind: list[list[sqlite3.Row]], limit: int) -> list[sqlite3.Row]:
    """Round-robin across kinds, so one big kind (subscriptions) cannot crowd out the rest."""
    interleaved = chain.from_iterable(zip_longest(*rows_by_kind))
    return [r for r in interleaved if r is not None][:limit]


def _legacy_block(taste: TasteProfile | None) -> str:
    if taste is None or taste.source != "legacy":
        return ""
    return (
        "\nAn earlier profile from the listener's old app (use it as extra context):\n"
        f"{taste.summary}\n"
        f"Likes: {'; '.join(taste.likes) or '-'}\n"
        f"Dislikes: {'; '.join(taste.dislikes) or '-'}\n"
    )


def _current_block(taste: TasteProfile | None) -> str:
    if taste is None:
        return ""
    if taste.source == "legacy":
        return _legacy_block(taste)
    return (
        "\nThe listener's current profile (keep what the account data still supports):\n"
        f"{taste.summary}\n"
        f"Likes: {'; '.join(taste.likes) or '-'}\n"
        f"Dislikes: {'; '.join(taste.dislikes) or '-'}\n"
    )


def _import_lines(conn: sqlite3.Connection) -> str:
    rows = _balanced([imported(conn, kind) for kind in IMPORT_KINDS], MAX_SEED_LINES)
    return "\n".join(f"{r['kind']} | {r['title'] or ''} | {r['channel_id'] or ''}" for r in rows)


def refresh_taste_from_imports(
    conn: sqlite3.Connection, *, claude: ClaudeRunner
) -> TasteProfile | None:
    """Rebuild the profile from account imports even when one exists (it is passed along as
    context). Used after the first OAuth import; returns None when nothing was imported."""
    lines = _import_lines(conn)
    if not lines:
        return None
    prompt = SEED_PROMPT.substitute(lines=lines, legacy=_current_block(latest_taste(conn)))
    summary, likes, dislikes = _parse_taste(claude(prompt))
    return _saved(conn, summary, likes, dislikes, "agent")


def seed_taste_from_imports(conn: sqlite3.Connection, *, claude: ClaudeRunner) -> TasteProfile | None:
    """Build a first profile from account imports (at most 200 lines, balanced across kinds).

    Runs when there is no profile or only legacy ones (their traits are passed along as
    context); returns None otherwise or when nothing was imported.
    """
    if not _only_legacy_profiles(conn):
        return None
    lines = _import_lines(conn)
    if not lines:
        return None
    prompt = SEED_PROMPT.substitute(lines=lines, legacy=_legacy_block(latest_taste(conn)))
    summary, likes, dislikes = _parse_taste(claude(prompt))
    return _saved(conn, summary, likes, dislikes, "agent")
