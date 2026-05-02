"""FastAPI application for YoutubeBrainwash phone remote control and feed management."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from contextlib import asynccontextmanager

from src.config import load_config
from src.models import FeedbackEntry, PlaybackState, ScoredVideo
from src.player import play_video, stop_playback

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Pydantic request / response models
# ---------------------------------------------------------------------------


class PlayRequest(BaseModel):
    video_id: str | None = None


class FeedbackRequest(BaseModel):
    video_id: str
    rating: str
    comment: str = ""


class VideoResponse(BaseModel):
    video_id: str
    title: str
    channel_name: str
    score: float
    reason: str
    duration_seconds: int
    thumbnail_url: str
    youtube_url: str


class PlaybackResponse(BaseModel):
    is_playing: bool
    current_video: VideoResponse | None
    queue: list[VideoResponse]
    position_seconds: float


class FeedbackResponse(BaseModel):
    status: str


# ---------------------------------------------------------------------------
# Internal state
# ---------------------------------------------------------------------------

DATA_DIR = Path("data")
FEED_PATH = DATA_DIR / "current_feed.json"
FEEDBACK_PATH = DATA_DIR / "feedback.json"


class AppState:
    """Holds runtime state for the API server."""

    def __init__(self) -> None:
        self.feed: list[ScoredVideo] = []
        self.playback: PlaybackState = PlaybackState()
        self.feedback_log: list[FeedbackEntry] = []


_state = AppState()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _scored_video_from_dict(raw: dict[str, Any]) -> ScoredVideo:
    """Deserialise a dict into a ``ScoredVideo``."""
    scored_at = raw.get("scored_at")
    if isinstance(scored_at, str):
        scored_at = datetime.fromisoformat(scored_at)
    elif scored_at is None:
        scored_at = datetime.now(timezone.utc)

    return ScoredVideo(
        video_id=raw["video_id"],
        title=raw["title"],
        channel_name=raw["channel_name"],
        score=float(raw["score"]),
        reason=raw.get("reason", ""),
        duration_seconds=int(raw["duration_seconds"]),
        thumbnail_url=raw.get("thumbnail_url", ""),
        scored_at=scored_at,
    )


def _video_response(sv: ScoredVideo) -> VideoResponse:
    return VideoResponse(
        video_id=sv.video_id,
        title=sv.title,
        channel_name=sv.channel_name,
        score=sv.score,
        reason=sv.reason,
        duration_seconds=sv.duration_seconds,
        thumbnail_url=sv.thumbnail_url,
        youtube_url=sv.youtube_url,
    )


def _playback_response(pb: PlaybackState) -> PlaybackResponse:
    return PlaybackResponse(
        is_playing=pb.is_playing,
        current_video=_video_response(pb.current_video) if pb.current_video else None,
        queue=[_video_response(v) for v in pb.queue],
        position_seconds=pb.position_seconds,
    )


def _load_feed() -> list[ScoredVideo]:
    """Load the current feed from disk, returning an empty list on failure."""
    if not FEED_PATH.exists():
        logger.warning("Feed file not found at %s — starting with empty feed", FEED_PATH)
        return []

    try:
        with open(FEED_PATH, encoding="utf-8") as fh:
            raw_list: list[dict[str, Any]] = json.load(fh)
        return [_scored_video_from_dict(item) for item in raw_list]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        logger.error("Failed to parse feed file %s: %s", FEED_PATH, exc)
        return []


def _save_feedback(entry: FeedbackEntry) -> None:
    """Append a feedback entry to the feedback JSON file."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    existing: list[dict[str, Any]] = []
    if FEEDBACK_PATH.exists():
        try:
            with open(FEEDBACK_PATH, encoding="utf-8") as fh:
                existing = json.load(fh)
        except (json.JSONDecodeError, TypeError):
            logger.warning("Corrupted feedback file — starting fresh")

    entry_dict = asdict(entry)
    # datetime is not JSON-serialisable by default
    entry_dict["timestamp"] = entry.timestamp.isoformat()
    existing.append(entry_dict)

    with open(FEEDBACK_PATH, "w", encoding="utf-8") as fh:
        json.dump(existing, fh, indent=2, ensure_ascii=False)

    logger.info("Saved feedback for video %s (rating=%s)", entry.video_id, entry.rating)


def _find_video_by_id(video_id: str) -> ScoredVideo | None:
    for video in _state.feed:
        if video.video_id == video_id:
            return video
    return None


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

@asynccontextmanager
async def _lifespan(application: FastAPI):
    _state.feed = _load_feed()
    _state.playback = PlaybackState(queue=list(_state.feed))
    logger.info("Loaded %d videos into feed", len(_state.feed))
    yield


app = FastAPI(title="YoutubeBrainwash", version="0.1.0", lifespan=_lifespan)


# -- Endpoints --------------------------------------------------------------


@app.get("/api/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "version": "0.1.0"}


@app.get("/api/feed", response_model=list[VideoResponse])
async def get_feed() -> list[VideoResponse]:
    return [_video_response(v) for v in _state.feed]


@app.post("/api/feed/play", response_model=PlaybackResponse)
async def play(body: PlayRequest) -> PlaybackResponse:
    pb = _state.playback

    if body.video_id:
        video = _find_video_by_id(body.video_id)
        if video is None:
            raise HTTPException(status_code=404, detail=f"Video {body.video_id} not found in feed")
        pb.current_video = video
        pb.is_playing = True
        pb.position_seconds = 0.0
        # Rebuild queue starting after the selected video
        try:
            idx = _state.feed.index(video)
            pb.queue = list(_state.feed[idx + 1:])
        except ValueError:
            pb.queue = []
    else:
        # Play the top unplayed video from the queue
        if not pb.queue:
            raise HTTPException(status_code=404, detail="No videos left in queue")
        video = pb.queue.pop(0)
        pb.current_video = video
        pb.is_playing = True
        pb.position_seconds = 0.0

    logger.info("Playing: %s (%s)", pb.current_video.title, pb.current_video.video_id)
    play_video(pb.current_video.video_id)
    return _playback_response(pb)


@app.post("/api/feed/next", response_model=PlaybackResponse)
async def next_video() -> PlaybackResponse:
    pb = _state.playback

    if not pb.queue:
        pb.is_playing = False
        pb.current_video = None
        pb.position_seconds = 0.0
        logger.info("No more videos in queue — playback stopped")
        return _playback_response(pb)

    video = pb.queue.pop(0)
    pb.current_video = video
    pb.is_playing = True
    pb.position_seconds = 0.0
    logger.info("Skipped to next: %s (%s)", video.title, video.video_id)
    play_video(video.video_id)
    return _playback_response(pb)


@app.post("/api/feed/stop", response_model=PlaybackResponse)
async def stop() -> PlaybackResponse:
    pb = _state.playback
    pb.is_playing = False
    pb.position_seconds = 0.0
    stop_playback()
    logger.info("Playback stopped")
    return _playback_response(pb)


@app.get("/api/feed/now", response_model=PlaybackResponse)
async def now_playing() -> PlaybackResponse:
    return _playback_response(_state.playback)


@app.post("/api/feed/feedback", response_model=FeedbackResponse)
async def feedback(body: FeedbackRequest) -> FeedbackResponse:
    valid_ratings = {"thumbs_up", "thumbs_down", "neutral"}
    if body.rating not in valid_ratings:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid rating '{body.rating}'. Must be one of: {', '.join(sorted(valid_ratings))}",
        )

    video = _find_video_by_id(body.video_id)
    title = video.title if video else body.video_id

    entry = FeedbackEntry(
        video_id=body.video_id,
        title=title,
        rating=body.rating,
        comment=body.comment,
    )
    _save_feedback(entry)
    _state.feedback_log.append(entry)

    return FeedbackResponse(status="ok")


# ---------------------------------------------------------------------------
# Server entry-point
# ---------------------------------------------------------------------------


def start_server(host: str | None = None, port: int | None = None) -> None:
    """Run the API server using uvicorn."""
    config = load_config()
    final_host = host or config.api_host
    final_port = port or config.api_port
    logger.info("Starting API server on %s:%d", final_host, final_port)
    uvicorn.run(app, host=final_host, port=final_port)
