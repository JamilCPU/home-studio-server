"""Domain models for Home Studio.

All dataclasses are frozen (immutable) except PlaybackState.
To update a frozen dataclass, use ``dataclasses.replace()``:

    updated = replace(config, max_candidates=100)
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Default extra `claude` CLI arguments (agent.claude_args), checked against Claude Code
# 2.1.86: no tools, no MCP servers (only the empty --mcp-config), no saved session.
DEFAULT_CLAUDE_ARGS: tuple[str, ...] = (
    "--tools", "",
    "--strict-mcp-config",
    "--mcp-config", '{"mcpServers":{}}',
    "--no-session-persistence",
)
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
    language: str = "en"

    # Scoring
    claude_command: str = "claude"
    max_prompt_tokens: int = 50000

    # API
    api_host: str = "0.0.0.0"
    api_port: int = 8765

    # Home Studio
    db_path: str = "data/home_studio.db"
    youtube_api_key: str = ""
    cookies_path: str = "secrets/cookies.txt"
    mpv_socket: str = "/tmp/home-studio-mpv.sock"
    bt_adapter: str = "hci1"
    bt_speaker_mac: str = ""
    output_device_label: str = "Echo"
    timezone: str = "America/Chicago"
    claude_timeout_s: int = 300
    claude_args: tuple[str, ...] = DEFAULT_CLAUDE_ARGS
    quota_ceiling: int = 9500
    # YouTube account sign-in (OAuth device flow); values live in config.local.yaml
    youtube_oauth_client_id: str = ""
    youtube_oauth_client_secret: str = ""
    youtube_oauth_token_path: str = "secrets/youtube_oauth.json"


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
