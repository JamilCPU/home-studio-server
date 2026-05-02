"""Tests for src.models dataclasses."""

from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timezone

import pytest

from src.models import (
    AppConfig,
    FeedbackEntry,
    PlaybackState,
    ScoredVideo,
    VideoCandidate,
)


# ---------------------------------------------------------------------------
# AppConfig
# ---------------------------------------------------------------------------


class TestAppConfig:
    """Tests for the frozen AppConfig dataclass."""

    def test_defaults(self) -> None:
        config = AppConfig()
        assert config.client_id == ""
        assert config.client_secret == ""
        assert config.project_id == ""
        assert config.max_candidates == 80
        assert config.transcript_candidates == 30
        assert config.min_score == 7.0
        assert config.max_feed_size == 15
        assert config.search_topics == ()
        assert config.min_duration_minutes == 5
        assert config.max_duration_minutes == 60
        assert config.recency_hours == 48
        assert config.language == "en"
        assert config.claude_command == "claude"
        assert config.max_prompt_tokens == 50000
        assert config.snapcast_port == 1704
        assert config.snapcast_control_port == 1705
        assert config.sample_rate == 48000
        assert config.channels == 2
        assert config.snap_fifo == "/tmp/snapfifo"
        assert config.curation_time == "04:00"
        assert config.default_wakeup_time == "07:30"
        assert config.api_host == "0.0.0.0"
        assert config.api_port == 5000
        assert config.bt_devices == {}

    def test_frozen_direct_assignment_raises(self, sample_config: AppConfig) -> None:
        with pytest.raises(FrozenInstanceError):
            sample_config.max_candidates = 999  # type: ignore[misc]

    def test_frozen_replace_works(self, sample_config: AppConfig) -> None:
        updated = replace(sample_config, max_candidates=200)
        assert updated.max_candidates == 200
        # Original is unchanged
        assert sample_config.max_candidates == 80

    def test_custom_values(self) -> None:
        config = AppConfig(
            client_id="my-id",
            max_candidates=50,
            search_topics=("python", "rust"),
            bt_devices={"speaker": "AA:BB:CC:DD:EE:FF"},
        )
        assert config.client_id == "my-id"
        assert config.max_candidates == 50
        assert config.search_topics == ("python", "rust")
        assert config.bt_devices == {"speaker": "AA:BB:CC:DD:EE:FF"}


# ---------------------------------------------------------------------------
# VideoCandidate
# ---------------------------------------------------------------------------


class TestVideoCandidate:
    """Tests for the frozen VideoCandidate dataclass."""

    def test_duration_minutes(self, sample_video_candidate: VideoCandidate) -> None:
        # 1800 seconds == 30 minutes
        assert sample_video_candidate.duration_minutes == 30.0

    def test_duration_minutes_fractional(self) -> None:
        video = VideoCandidate(
            video_id="x",
            title="t",
            channel_name="c",
            channel_id="ci",
            description="d",
            published_at=datetime.now(timezone.utc),
            duration_seconds=90,
            view_count=0,
            thumbnail_url="",
            source="search",
        )
        assert video.duration_minutes == pytest.approx(1.5)

    def test_youtube_url(self, sample_video_candidate: VideoCandidate) -> None:
        assert sample_video_candidate.youtube_url == (
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        )

    def test_frozen(self, sample_video_candidate: VideoCandidate) -> None:
        with pytest.raises(FrozenInstanceError):
            sample_video_candidate.title = "hacked"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# ScoredVideo
# ---------------------------------------------------------------------------


class TestScoredVideo:
    """Tests for the frozen ScoredVideo dataclass."""

    def test_youtube_url(self, sample_scored_video: ScoredVideo) -> None:
        assert sample_scored_video.youtube_url == (
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        )

    def test_scored_at_default_is_utc(self) -> None:
        before = datetime.now(timezone.utc)
        video = ScoredVideo(
            video_id="v1",
            title="t",
            channel_name="c",
            score=8.0,
            reason="good",
            duration_seconds=600,
            thumbnail_url="",
        )
        after = datetime.now(timezone.utc)
        assert before <= video.scored_at <= after
        assert video.scored_at.tzinfo is not None

    def test_frozen(self, sample_scored_video: ScoredVideo) -> None:
        with pytest.raises(FrozenInstanceError):
            sample_scored_video.score = 0.0  # type: ignore[misc]


# ---------------------------------------------------------------------------
# FeedbackEntry
# ---------------------------------------------------------------------------


class TestFeedbackEntry:
    """Tests for the frozen FeedbackEntry dataclass."""

    def test_defaults(self) -> None:
        entry = FeedbackEntry(
            video_id="v1",
            title="Some Video",
            rating="thumbs_up",
        )
        assert entry.comment == ""
        assert entry.timestamp.tzinfo is not None

    def test_timestamp_default_is_utc(self) -> None:
        before = datetime.now(timezone.utc)
        entry = FeedbackEntry(video_id="v1", title="t", rating="neutral")
        after = datetime.now(timezone.utc)
        assert before <= entry.timestamp <= after

    def test_custom_comment(self) -> None:
        entry = FeedbackEntry(
            video_id="v1",
            title="t",
            rating="thumbs_down",
            comment="not my style",
        )
        assert entry.comment == "not my style"

    def test_frozen(self) -> None:
        entry = FeedbackEntry(video_id="v1", title="t", rating="neutral")
        with pytest.raises(FrozenInstanceError):
            entry.rating = "thumbs_up"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# PlaybackState
# ---------------------------------------------------------------------------


class TestPlaybackState:
    """Tests for the mutable PlaybackState dataclass."""

    def test_defaults(self) -> None:
        state = PlaybackState()
        assert state.is_playing is False
        assert state.current_video is None
        assert state.queue == []
        assert state.position_seconds == 0.0

    def test_is_mutable(self) -> None:
        state = PlaybackState()
        state.is_playing = True
        assert state.is_playing is True
        state.position_seconds = 42.5
        assert state.position_seconds == 42.5

    def test_queue_append(self, sample_scored_video: ScoredVideo) -> None:
        state = PlaybackState()
        state.queue.append(sample_scored_video)
        assert len(state.queue) == 1
        assert state.queue[0].video_id == "dQw4w9WgXcQ"

    def test_queue_pop(self, sample_scored_video: ScoredVideo) -> None:
        state = PlaybackState(queue=[sample_scored_video])
        popped = state.queue.pop(0)
        assert popped is sample_scored_video
        assert state.queue == []

    def test_set_current_video(self, sample_scored_video: ScoredVideo) -> None:
        state = PlaybackState()
        state.current_video = sample_scored_video
        assert state.current_video is sample_scored_video

    def test_queue_instances_are_independent(self) -> None:
        state_a = PlaybackState()
        state_b = PlaybackState()
        state_a.queue.append(
            ScoredVideo(
                video_id="a",
                title="a",
                channel_name="c",
                score=1.0,
                reason="r",
                duration_seconds=60,
                thumbnail_url="",
            )
        )
        assert len(state_b.queue) == 0
