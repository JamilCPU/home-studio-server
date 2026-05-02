"""Fetch video candidates from YouTube Data API v3.

Receives an authenticated ``googleapiclient.discovery.Resource`` object and
returns lists of ``VideoCandidate`` instances ready for scoring.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from src.models import VideoCandidate

if TYPE_CHECKING:
    from googleapiclient.discovery import Resource

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_DURATION_RE = re.compile(
    r"^PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?$",
)


def _parse_duration(iso_duration: str) -> int:
    """Parse an ISO 8601 duration like ``PT1H2M30S`` into total seconds.

    Returns 0 when the string cannot be parsed.
    """
    match = _DURATION_RE.match(iso_duration)
    if not match:
        logger.warning("Unable to parse ISO 8601 duration: %s", iso_duration)
        return 0
    hours = int(match.group(1) or 0)
    minutes = int(match.group(2) or 0)
    seconds = int(match.group(3) or 0)
    return hours * 3600 + minutes * 60 + seconds


def _build_video_candidate(
    item: dict,
    details: dict,
    source: str,
) -> VideoCandidate:
    """Construct a ``VideoCandidate`` from YouTube API response dicts.

    Parameters
    ----------
    item:
        A resource from ``search().list`` or ``videos().list`` containing at
        least the ``snippet`` part.
    details:
        A resource from ``videos().list`` containing ``contentDetails`` and
        ``statistics`` parts.
    source:
        Label such as ``"subscription"``, ``"search"``, or ``"liked"``.
    """
    snippet = item.get("snippet", {})
    content_details = details.get("contentDetails", {})
    statistics = details.get("statistics", {})

    video_id = details.get("id") or item.get("id", {}).get("videoId", "")
    thumbnails = snippet.get("thumbnails", {})
    thumbnail_url = (
        thumbnails.get("high", {}).get("url")
        or thumbnails.get("medium", {}).get("url")
        or thumbnails.get("default", {}).get("url", "")
    )

    published_raw = snippet.get("publishedAt", "")
    try:
        published_at = datetime.fromisoformat(
            published_raw.replace("Z", "+00:00"),
        )
    except (ValueError, AttributeError):
        published_at = datetime.now(timezone.utc)

    view_count_raw = statistics.get("viewCount", "0")
    try:
        view_count = int(view_count_raw)
    except (ValueError, TypeError):
        view_count = 0

    return VideoCandidate(
        video_id=video_id,
        title=snippet.get("title", ""),
        channel_name=snippet.get("channelTitle", ""),
        channel_id=snippet.get("channelId", ""),
        description=snippet.get("description", ""),
        published_at=published_at,
        duration_seconds=_parse_duration(
            content_details.get("duration", "PT0S"),
        ),
        view_count=view_count,
        thumbnail_url=thumbnail_url,
        source=source,
    )


def _cutoff_iso(recency_hours: int) -> str:
    """Return an ISO 8601 timestamp ``recency_hours`` in the past."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=recency_hours)
    return cutoff.isoformat()


def _get_video_details(
    youtube: Resource,
    video_ids: list[str],
) -> dict[str, dict]:
    """Fetch ``contentDetails`` and ``statistics`` for a batch of video IDs.

    The YouTube API accepts up to 50 comma-separated IDs per call, so this
    function batches automatically.

    Returns a mapping of ``video_id -> full resource dict``.
    """
    results: dict[str, dict] = {}
    batch_size = 50
    for start in range(0, len(video_ids), batch_size):
        batch = video_ids[start : start + batch_size]
        try:
            response = (
                youtube.videos()
                .list(
                    part="snippet,contentDetails,statistics",
                    id=",".join(batch),
                )
                .execute()
            )
            for item in response.get("items", []):
                results[item["id"]] = item
        except Exception:
            logger.exception(
                "Failed to fetch video details for batch starting at index %d",
                start,
            )
    return results


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def fetch_subscription_videos(
    youtube: Resource,
    recency_hours: int = 48,
    max_results: int = 50,
) -> list[VideoCandidate]:
    """Fetch recent videos from the authenticated user's subscriptions.

    Pages through all subscriptions, then queries each channel for uploads
    published within the last *recency_hours*.
    """
    cutoff = _cutoff_iso(recency_hours)
    channel_ids: list[str] = []

    # --- collect all subscribed channel IDs ---
    page_token: str | None = None
    while True:
        try:
            kwargs: dict = {
                "part": "snippet",
                "mine": True,
                "maxResults": 50,
            }
            if page_token is not None:
                kwargs["pageToken"] = page_token
            response = youtube.subscriptions().list(**kwargs).execute()
        except Exception:
            logger.exception("Failed to list subscriptions (page=%s)", page_token)
            break

        for item in response.get("items", []):
            cid = (
                item.get("snippet", {})
                .get("resourceId", {})
                .get("channelId")
            )
            if cid:
                channel_ids.append(cid)

        page_token = response.get("nextPageToken")
        if not page_token:
            break

    logger.info("Found %d subscribed channels", len(channel_ids))

    # --- search each channel for recent uploads ---
    raw_items: list[dict] = []
    video_ids: list[str] = []

    for cid in channel_ids:
        try:
            response = (
                youtube.search()
                .list(
                    part="snippet",
                    channelId=cid,
                    order="date",
                    publishedAfter=cutoff,
                    type="video",
                    maxResults=10,
                )
                .execute()
            )
        except Exception:
            logger.exception(
                "Failed to search uploads for channel %s", cid,
            )
            continue

        for item in response.get("items", []):
            vid = item.get("id", {}).get("videoId")
            if vid and vid not in video_ids:
                raw_items.append(item)
                video_ids.append(vid)

        if len(video_ids) >= max_results:
            video_ids = video_ids[:max_results]
            raw_items = raw_items[:max_results]
            break

    if not video_ids:
        return []

    # --- enrich with duration / statistics ---
    details_map = _get_video_details(youtube, video_ids)
    candidates: list[VideoCandidate] = []

    for item in raw_items:
        vid = item.get("id", {}).get("videoId", "")
        detail = details_map.get(vid)
        if detail is None:
            continue
        candidates.append(_build_video_candidate(item, detail, "subscription"))

    logger.info(
        "Fetched %d subscription video candidates", len(candidates),
    )
    return candidates


def fetch_liked_videos(
    youtube: Resource,
    max_results: int = 50,
) -> list[VideoCandidate]:
    """Fetch videos the authenticated user has liked."""
    candidates: list[VideoCandidate] = []

    try:
        response = (
            youtube.videos()
            .list(
                part="snippet,contentDetails,statistics",
                myRating="like",
                maxResults=max_results,
            )
            .execute()
        )
    except Exception:
        logger.exception("Failed to fetch liked videos")
        return []

    for item in response.get("items", []):
        candidates.append(_build_video_candidate(item, item, "liked"))

    logger.info("Fetched %d liked video candidates", len(candidates))
    return candidates


def fetch_search_results(
    youtube: Resource,
    queries: list[str],
    recency_hours: int = 48,
    max_per_query: int = 10,
) -> list[VideoCandidate]:
    """Run topic-based searches and return recent video candidates."""
    cutoff = _cutoff_iso(recency_hours)
    raw_items: list[dict] = []
    video_ids: list[str] = []

    for query in queries:
        try:
            response = (
                youtube.search()
                .list(
                    part="snippet",
                    q=query,
                    type="video",
                    order="relevance",
                    publishedAfter=cutoff,
                    maxResults=max_per_query,
                )
                .execute()
            )
        except Exception:
            logger.exception("Search failed for query %r", query)
            continue

        for item in response.get("items", []):
            vid = item.get("id", {}).get("videoId")
            if vid and vid not in video_ids:
                raw_items.append(item)
                video_ids.append(vid)

    if not video_ids:
        return []

    details_map = _get_video_details(youtube, video_ids)
    candidates: list[VideoCandidate] = []

    for item in raw_items:
        vid = item.get("id", {}).get("videoId", "")
        detail = details_map.get(vid)
        if detail is None:
            continue
        candidates.append(_build_video_candidate(item, detail, "search"))

    logger.info("Fetched %d search video candidates", len(candidates))
    return candidates


# ---------------------------------------------------------------------------
# Post-processing
# ---------------------------------------------------------------------------


def deduplicate_candidates(
    candidates: list[VideoCandidate],
) -> list[VideoCandidate]:
    """Remove duplicate video IDs, keeping the first occurrence."""
    seen: set[str] = set()
    unique: list[VideoCandidate] = []
    for candidate in candidates:
        if candidate.video_id not in seen:
            seen.add(candidate.video_id)
            unique.append(candidate)
    return unique


def filter_candidates(
    candidates: list[VideoCandidate],
    min_duration_minutes: int = 5,
    max_duration_minutes: int = 60,
    language: str = "en",  # noqa: ARG001 — reserved for future language filtering
) -> list[VideoCandidate]:
    """Filter candidates by duration range.

    The *language* parameter is accepted for forward-compatibility but is not
    currently used for filtering (YouTube snippet data does not reliably
    expose the audio language).
    """
    min_seconds = min_duration_minutes * 60
    max_seconds = max_duration_minutes * 60
    filtered = [
        c
        for c in candidates
        if min_seconds <= c.duration_seconds <= max_seconds
    ]
    logger.info(
        "Filtered %d -> %d candidates (duration %d-%d min)",
        len(candidates),
        len(filtered),
        min_duration_minutes,
        max_duration_minutes,
    )
    return filtered
