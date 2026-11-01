"""Catalog cache: fetch each video and transcript at most once, persisted in SQLite."""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import replace

from src.sources.data_api import DataApi, QuotaExceeded
from src.sources.ytdlp import SourceAuthError, SourceError, YtDlp
from src.store.settings import log_event
from src.store.videos import Video, get_video, set_transcript, upsert_video
from src.transcript import truncate_transcript

logger = logging.getLogger(__name__)

COMPONENT = "catalog"


def ensure_videos(
    conn: sqlite3.Connection, ids: list[str], data_api: DataApi, ytdlp: YtDlp
) -> list[Video]:
    """Return Videos for `ids` in input order, fetching only those not yet stored.

    Ids that cannot be fetched are skipped. SourceAuthError propagates.
    """
    unique = list(dict.fromkeys(ids))
    missing = [i for i in unique if get_video(conn, i) is None]
    if missing:
        try:
            fetched = data_api.video_details(missing)
        except QuotaExceeded:
            logger.warning("Data API quota exhausted; using yt-dlp for %d ids", len(missing))
            fetched = []
            for video_id in missing:
                try:
                    fetched.append(ytdlp.metadata(video_id))
                except SourceAuthError:
                    raise
                except SourceError as exc:
                    logger.warning("yt-dlp metadata failed for %s: %s", video_id, exc)
        for video in fetched:
            upsert_video(conn, video)
    found = (get_video(conn, i) for i in unique)
    return [v for v in found if v is not None]


def ensure_transcript(
    conn: sqlite3.Connection, video_id: str, ytdlp: YtDlp, max_chars: int = 3000
) -> str | None:
    """Return the (truncated) transcript, fetching and caching it on first use."""
    video = get_video(conn, video_id)
    if video is not None and video.transcript:
        return video.transcript
    text = ytdlp.transcript(video_id)
    if not text:
        return None
    text = truncate_transcript(text, max_chars)
    if video is not None:
        set_transcript(conn, video_id, text)
    return text


def ensure_chapters(conn: sqlite3.Connection, video_id: str, ytdlp: YtDlp) -> None:
    """Fill chapters for a stored video that has none; never raises on source errors."""
    video = get_video(conn, video_id)
    if video is None or video.chapters:
        return
    try:
        fetched = ytdlp.metadata(video_id)
    except SourceError as exc:
        logger.warning("could not fetch chapters for %s: %s", video_id, exc)
        log_event(conn, COMPONENT, "warn", f"chapters unavailable for {video_id}")
        return
    if fetched.chapters:
        upsert_video(conn, replace(video, chapters=fetched.chapters))
