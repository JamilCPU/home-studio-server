"""Builds the taste context (profile plus recent feedback) fed to agent prompts."""

from __future__ import annotations

import logging
import sqlite3

from src.store.agent import latest_taste
from src.store.videos import HistoryEntry, recent_feedback

logger = logging.getLogger(__name__)

MAX_FEEDBACK_LINES = 50
_NO_PROFILE = "(no taste profile yet)"
_NO_FEEDBACK = "(no feedback yet)"


def _mmss(seconds: float | None) -> str:
    total = int(seconds or 0)
    return f"{total // 60}:{total % 60:02d}"


def _liked_flag(entry: HistoryEntry) -> str:
    rating = entry.latest_rating
    if rating is None or rating.liked is None:
        return "-"
    return "y" if rating.liked else "n"


def _feedback_line(entry: HistoryEntry) -> str:
    rating = entry.latest_rating
    sat = "-" if rating is None or rating.satisfaction is None else str(rating.satisfaction)
    if rating is not None and rating.notes:
        detail = rating.notes
    elif entry.end_reason == "skipped":
        detail = f"skipped at {_mmss(entry.seconds_listened)}"
    else:
        detail = "-"
    return (
        f"{entry.video.title} | {entry.video.channel_name or '-'} | "
        f"liked={_liked_flag(entry)} sat={sat} | {detail}"
    )


def build_taste_context(conn: sqlite3.Connection) -> str:
    """Latest taste summary with likes/dislikes, then up to 50 feedback lines."""
    taste = latest_taste(conn)
    if taste is None:
        profile = _NO_PROFILE
    else:
        profile = "\n".join(
            [
                taste.summary,
                "Likes: " + ("; ".join(taste.likes) or "-"),
                "Dislikes: " + ("; ".join(taste.dislikes) or "-"),
            ]
        )
    entries = recent_feedback(conn, MAX_FEEDBACK_LINES)
    feedback = "\n".join(_feedback_line(e) for e in entries) or _NO_FEEDBACK
    return (
        f"Taste profile:\n{profile}\n\n"
        "Recent feedback (title | channel | liked=y/n/- sat=1-10/- | notes):\n"
        f"{feedback}"
    )
