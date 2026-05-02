"""Domain models for YoutubeBrainwash.

All dataclasses are frozen (immutable) except PlaybackState.
To update a frozen dataclass, use ``dataclasses.replace()``:

    updated = replace(config, max_candidates=100)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass(frozen=True)
class AppConfig:
    """Application configuration loaded from config.yaml."""

    # YouTube OAuth
    client_id: str = ""
    client_secret: str = ""
    project_id: str = ""

    # Curation
    max_candidates: int = 80
    transcript_candidates: int = 30
    min_score: float = 7.0
    max_feed_size: int = 15
    search_topics: tuple[str, ...] = ()
    min_duration_minutes: int = 5
    max_duration_minutes: int = 60
    recency_hours: int = 48
    language: str = "en"

    # Scoring
    claude_command: str = "claude"
    max_prompt_tokens: int = 50000

    # Playback
    snapcast_port: int = 1704
    snapcast_control_port: int = 1705
    sample_rate: int = 48000
    channels: int = 2
    snap_fifo: str = "/tmp/snapfifo"

    # Schedule
    curation_time: str = "04:00"
    default_wakeup_time: str = "07:30"

    # API
    api_host: str = "0.0.0.0"
    api_port: int = 5000

    # Bluetooth
    bt_devices: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class VideoCandidate:
    """A video discovered during search or subscription fetch."""

    video_id: str
    title: str
    channel_name: str
    channel_id: str
    description: str
    published_at: datetime
    duration_seconds: int
    view_count: int
    thumbnail_url: str
    source: str  # e.g. "subscription", "search", "liked"

    @property
    def duration_minutes(self) -> float:
        return self.duration_seconds / 60.0

    @property
    def youtube_url(self) -> str:
        return f"https://www.youtube.com/watch?v={self.video_id}"


@dataclass(frozen=True)
class TranscriptResult:
    """Result of a transcript fetch for a single video."""

    video_id: str
    text: str
    language: str
    is_auto_generated: bool


@dataclass(frozen=True)
class ScoredVideo:
    """A video after Claude scoring."""

    video_id: str
    title: str
    channel_name: str
    score: float
    reason: str
    duration_seconds: int
    thumbnail_url: str
    scored_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc),
    )

    @property
    def youtube_url(self) -> str:
        return f"https://www.youtube.com/watch?v={self.video_id}"


@dataclass(frozen=True)
class FeedbackEntry:
    """User feedback on a video (thumbs up/down/neutral)."""

    video_id: str
    title: str
    rating: str  # "thumbs_up", "thumbs_down", "neutral"
    comment: str = ""
    timestamp: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc),
    )


@dataclass
class PlaybackState:
    """Current playback state (mutable for real-time updates)."""

    is_playing: bool = False
    current_video: ScoredVideo | None = None
    queue: list[ScoredVideo] = field(default_factory=list)
    position_seconds: float = 0.0
