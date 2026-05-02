"""Shared fixtures for YoutubeBrainwash tests."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest


@pytest.fixture
def sample_config():
    """Return a minimal AppConfig-like dict for testing."""
    from src.models import AppConfig

    return AppConfig(
        client_id="test-client-id",
        client_secret="test-client-secret",
        project_id="test-project",
    )


@pytest.fixture
def sample_video_candidate():
    """Return a sample VideoCandidate."""
    from src.models import VideoCandidate

    return VideoCandidate(
        video_id="dQw4w9WgXcQ",
        title="Building AI Agents with Claude",
        channel_name="TechChannel",
        channel_id="UC123abc",
        description="A deep dive into building AI agents using Claude's tool use API.",
        published_at=datetime(2026, 4, 30, 12, 0, 0, tzinfo=timezone.utc),
        duration_seconds=1800,
        view_count=50000,
        thumbnail_url="https://i.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg",
        source="subscription",
    )


@pytest.fixture
def sample_scored_video():
    """Return a sample ScoredVideo."""
    from src.models import ScoredVideo

    return ScoredVideo(
        video_id="dQw4w9WgXcQ",
        title="Building AI Agents with Claude",
        channel_name="TechChannel",
        score=9.2,
        reason="Highly relevant deep-dive on AI agents with practical Claude examples",
        duration_seconds=1800,
        thumbnail_url="https://i.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg",
    )


@pytest.fixture
def sample_transcript():
    """Return a sample TranscriptResult."""
    from src.models import TranscriptResult

    return TranscriptResult(
        video_id="dQw4w9WgXcQ",
        text="Today we're going to explore how to build AI agents using Claude. "
        "We'll start with the basics of tool use and then move into more advanced patterns "
        "like multi-agent orchestration and autonomous loops.",
        language="en",
        is_auto_generated=False,
    )


@pytest.fixture
def sample_interest_profile():
    """Return a sample interest profile dict."""
    return {
        "topics": {
            "AI agents": 10,
            "LLM research": 9,
            "Claude / Anthropic": 9,
            "Python tooling": 6,
        },
        "preferred_channels": ["TechChannel"],
        "blocked_channels": [],
        "preferred_formats": {
            "deep_dive_tutorial": 9,
            "research_paper_walkthrough": 9,
            "news_roundup": 5,
        },
    }


@pytest.fixture
def sample_feedback_entries():
    """Return sample feedback entries."""
    return [
        {
            "video_id": "abc123",
            "title": "AI Agent Architecture Patterns",
            "channel_name": "TechChannel",
            "rating": "thumbs_up",
            "comment": "More content like this - practical AI agent tutorials",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
        {
            "video_id": "def456",
            "title": "AI News Roundup #47",
            "channel_name": "NewsChannel",
            "rating": "thumbs_down",
            "comment": "Too surface-level, prefer deep dives",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
    ]


@pytest.fixture
def tmp_data_dir(tmp_path):
    """Create a temporary data directory structure."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    return data_dir
