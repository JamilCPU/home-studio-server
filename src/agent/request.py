"""Agent request pipeline: plan, shortlist, pick (three Claude CLI calls)."""

from __future__ import annotations

import logging
import sqlite3
from string import Template
from typing import Any, Literal

from src.agent.claude_cli import ClaudeBadOutput, ClaudeError, ClaudeRunner, extract_json
from src.agent.context import build_taste_context
from src.sources.catalog import ensure_transcript, ensure_videos
from src.sources.data_api import QuotaExceeded
from src.sources.ytdlp import SourceAuthError, SourceError
from src.store.agent import AgentRequest, get_request, set_status
from src.store.queue import enqueue
from src.store.settings import log_event
from src.store.videos import Video, disliked_video_ids, played_video_ids

logger = logging.getLogger(__name__)

COMPONENT = "agent"
MAX_QUERIES = 4
MAX_COUNT = 12
MAX_SHORTLIST = 8
MAX_CANDIDATES = 40
MAX_REASON_CHARS = 140
MAX_EVENT_CHARS = 300
TRANSCRIPT_CHARS = 3000
NO_PICKS_MESSAGE = "no suitable videos found"
AUTH_EVENT_MESSAGE = "SourceAuthError: cookies or sign-in check"

PLAN_PROMPT = Template(
    """You plan YouTube searches for a personal listening station.

Reply with JSON only, no prose and no code fences. Schema:
{"queries": ["<search query>", ...], "count": <integer 1-12>, "target_minutes": <integer or null>}

Rules:
- "queries": 1 to 4 distinct YouTube search queries that would find what was asked for.
- "count": how many videos to queue (1 to 12).
- "target_minutes": total listening time wanted, or null if not stated.

Request:
$request

$taste
"""
)

SHORTLIST_PROMPT = Template(
    """You shortlist candidate YouTube videos for a personal listening station.

Reply with JSON only, no prose and no code fences. Schema:
{"shortlist": ["<video_id>", ...]}

Rules:
- Choose at most 8 video ids, only from the candidates below.
- Prefer videos that match the request and the listener's taste.

Request:
$request

Candidates (video_id | title | channel | duration m:ss | published):
$candidates
"""
)

PICK_PROMPT = Template(
    """You make the final picks for a personal listening station queue.

Reply with JSON only, no prose and no code fences. Schema:
{"picks": [{"video_id": "<video_id>", "reason": "<why, at most 140 characters>"}, ...]}

Rules:
- Pick at most $count videos, only from the shortlist below, in the order they should play.
- Each reason says why this video suits the request and the listener.
- Target total listening time in minutes: $target.

Request:
$request

$taste

Shortlist (video_id | title | channel | duration m:ss | published), each followed by a transcript excerpt:
$shortlist
"""
)


class NoPicks(Exception):
    """The pipeline produced no usable videos."""


def _mmss(seconds: int | None) -> str:
    total = int(seconds or 0)
    return f"{total // 60}:{total % 60:02d}"


def _meta_line(video: Video) -> str:
    published = (video.published_at or "-")[:10]
    return (
        f"{video.video_id} | {video.title} | {video.channel_name or '-'} | "
        f"{_mmss(video.duration_s)} | {published}"
    )


def _ask_json(claude: ClaudeRunner, prompt: str, step: str) -> dict[str, Any]:
    parsed = extract_json(claude(prompt))
    if not isinstance(parsed, dict):
        raise ClaudeBadOutput(f"{step}: expected a JSON object")
    return parsed


def _str_list(value: Any, key: str, step: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
        raise ClaudeBadOutput(f"{step}: '{key}' must be a list of strings")
    return value


def _parse_plan(data: dict[str, Any]) -> tuple[list[str], int, int | None]:
    raw = _str_list(data.get("queries"), "queries", "plan")
    queries = [q.strip() for q in raw if q.strip()]
    if not queries:
        raise ClaudeBadOutput("plan: 'queries' must not be empty")
    count = data.get("count")
    if isinstance(count, bool) or not isinstance(count, int):
        raise ClaudeBadOutput("plan: 'count' must be an integer")
    target = data.get("target_minutes")
    if isinstance(target, bool) or not (target is None or isinstance(target, int)):
        raise ClaudeBadOutput("plan: 'target_minutes' must be an integer or null")
    return queries[:MAX_QUERIES], max(1, min(MAX_COUNT, count)), target


def _parse_picks(data: dict[str, Any]) -> list[tuple[str, str]]:
    raw = data.get("picks")
    if not isinstance(raw, list):
        raise ClaudeBadOutput("pick: 'picks' must be a list")
    picks: list[tuple[str, str]] = []
    for item in raw:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("video_id"), str)
            or not isinstance(item.get("reason"), str)
        ):
            raise ClaudeBadOutput("pick: each pick needs string 'video_id' and 'reason'")
        picks.append((item["video_id"], item["reason"][:MAX_REASON_CHARS]))
    return picks


def _search(query: str, data_api, ytdlp) -> list[str]:
    try:
        return data_api.search(query, 25)
    except QuotaExceeded:
        logger.warning("Data API quota exhausted; searching via yt-dlp")
        return ytdlp.search(query, 20)


def _candidates(conn: sqlite3.Connection, queries: list[str], data_api, ytdlp) -> list[Video]:
    ids: list[str] = []
    for query in queries:
        ids.extend(_search(query, data_api, ytdlp))
    excluded = played_video_ids(conn, 30) | disliked_video_ids(conn)
    fresh = [i for i in dict.fromkeys(ids) if i not in excluded]
    return ensure_videos(conn, fresh[:MAX_CANDIDATES], data_api, ytdlp)[:MAX_CANDIDATES]


def _shortlist_block(conn: sqlite3.Connection, videos: list[Video], ytdlp) -> tuple[str, bool]:
    """Metadata plus transcript per video; returns (text, saw_auth_error)."""
    blocks = []
    auth_failed = False
    for video in videos:
        try:
            text = ensure_transcript(conn, video.video_id, ytdlp, TRANSCRIPT_CHARS)
        except SourceAuthError:
            auth_failed = True
            text = None
        except SourceError as exc:
            logger.warning("transcript unavailable for %s: %s", video.video_id, exc)
            text = None
        blocks.append(f"{_meta_line(video)}\nTranscript: {text or '(no transcript)'}")
    return "\n\n".join(blocks), auth_failed


def _choose(
    conn: sqlite3.Connection, request: AgentRequest, claude, data_api, ytdlp
) -> list[tuple[Video, str]]:
    taste = build_taste_context(conn)
    plan = _ask_json(claude, PLAN_PROMPT.substitute(request=request.prompt, taste=taste), "plan")
    queries, count, target = _parse_plan(plan)

    candidates = _candidates(conn, queries, data_api, ytdlp)
    if not candidates:
        raise NoPicks(NO_PICKS_MESSAGE)
    lines = "\n".join(_meta_line(v) for v in candidates)
    prompt = SHORTLIST_PROMPT.substitute(request=request.prompt, candidates=lines)
    data = _ask_json(claude, prompt, "shortlist")
    wanted = set(_str_list(data.get("shortlist"), "shortlist", "shortlist")[:MAX_SHORTLIST])
    shortlisted = [v for v in candidates if v.video_id in wanted]
    if not shortlisted:
        raise NoPicks(NO_PICKS_MESSAGE)

    block, auth_failed = _shortlist_block(conn, shortlisted, ytdlp)
    if auth_failed:
        log_event(conn, "ytdlp", "error", AUTH_EVENT_MESSAGE)
    pick_prompt = PICK_PROMPT.substitute(
        count=count,
        target="none" if target is None else target,
        request=request.prompt,
        taste=taste,
        shortlist=block,
    )
    by_id = {v.video_id: v for v in shortlisted}
    chosen: dict[str, tuple[Video, str]] = {}
    for video_id, reason in _parse_picks(_ask_json(claude, pick_prompt, "pick")):
        if video_id in by_id and video_id not in chosen:
            chosen[video_id] = (by_id[video_id], reason)
    picks = list(chosen.values())[:count]
    if not picks:
        raise NoPicks(NO_PICKS_MESSAGE)
    return picks


def _enqueue_picks(
    conn: sqlite3.Connection, request: AgentRequest, picks: list[tuple[Video, str]], source: str
) -> list[int]:
    """Enqueue in play order. Each "next" lands before earlier ones, so go in reverse."""
    if request.play == "now":
        reverse_ids = [
            enqueue(conn, v.video_id, source, reason, request.id, "next")
            for v, reason in reversed(picks)
        ]
        return list(reversed(reverse_ids))
    return [enqueue(conn, v.video_id, source, reason, request.id, "end") for v, reason in picks]


def _fail(conn: sqlite3.Connection, request_id: int, exc: Exception) -> None:
    set_status(conn, request_id, "failed", error=str(exc) or type(exc).__name__)
    log_event(conn, COMPONENT, "error", f"{type(exc).__name__}: {exc}"[:MAX_EVENT_CHARS])
    if isinstance(exc, SourceAuthError):
        log_event(conn, "ytdlp", "error", AUTH_EVENT_MESSAGE)


def run_request(
    conn: sqlite3.Connection,
    request_id: int,
    *,
    claude: ClaudeRunner,
    data_api,
    ytdlp,
    source: Literal["agent", "morning"] = "agent",
) -> list[int]:
    """Run the three-step pipeline; failures mark the request failed and return []."""
    request = get_request(conn, request_id)
    if request is None:
        raise ValueError(f"unknown agent request {request_id}")
    set_status(conn, request_id, "running")
    try:
        picks = _choose(conn, request, claude, data_api, ytdlp)
        ids = _enqueue_picks(conn, request, picks, source)
        minutes = sum(v.duration_s or 0 for v, _ in picks) // 60
        set_status(conn, request_id, "done", summary=f"Queued {len(ids)} videos ({minutes} min)")
        log_event(conn, COMPONENT, "info", f"ok: queued {len(ids)}")
        return ids
    except (ClaudeError, SourceError, NoPicks) as exc:
        logger.warning("agent request %d failed: %s", request_id, exc)
        _fail(conn, request_id, exc)
    except Exception as exc:  # noqa: BLE001 - keep the worker thread alive
        logger.exception("agent request %d crashed", request_id)
        _fail(conn, request_id, exc)
    return []
