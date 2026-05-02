"""Tests for src.api — FastAPI endpoints for YoutubeBrainwash."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient

from src.api import AppState, _state, app


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_state():
    """Reset the API module-level state before each test."""
    _state.feed = []
    _state.playback.is_playing = False
    _state.playback.current_video = None
    _state.playback.queue = []
    _state.playback.position_seconds = 0.0
    _state.feedback_log = []
    yield


@pytest.fixture
def async_client():
    """Provide a reusable AsyncClient factory (as a context manager)."""
    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    )


# ---------------------------------------------------------------------------
# GET /api/health
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/health")

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert "version" in data


# ---------------------------------------------------------------------------
# GET /api/feed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_feed_returns_list_when_empty():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/feed")

    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


@pytest.mark.asyncio
async def test_feed_returns_videos_when_populated(sample_scored_video):
    _state.feed = [sample_scored_video]

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/feed")

    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["video_id"] == sample_scored_video.video_id
    assert data[0]["title"] == sample_scored_video.title


# ---------------------------------------------------------------------------
# POST /api/feed/play
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@patch("src.api.play_video")
async def test_play_with_empty_queue_returns_404(mock_play):
    """POST /api/feed/play with no video_id and empty queue returns 404."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/api/feed/play", json={})

    assert resp.status_code == 404
    mock_play.assert_not_called()


@pytest.mark.asyncio
@patch("src.api.play_video")
async def test_play_with_specific_video(mock_play, sample_scored_video):
    """POST /api/feed/play with a known video_id plays that video."""
    _state.feed = [sample_scored_video]

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/api/feed/play",
            json={"video_id": sample_scored_video.video_id},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["is_playing"] is True
    assert data["current_video"]["video_id"] == sample_scored_video.video_id
    mock_play.assert_called_once_with(sample_scored_video.video_id)


@pytest.mark.asyncio
@patch("src.api.play_video")
async def test_play_with_unknown_video_returns_404(mock_play):
    """POST /api/feed/play with an unknown video_id returns 404."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/api/feed/play",
            json={"video_id": "nonexistent"},
        )

    assert resp.status_code == 404
    mock_play.assert_not_called()


# ---------------------------------------------------------------------------
# POST /api/feed/feedback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@patch("src.api._save_feedback")
async def test_feedback_with_valid_data_returns_ok(mock_save):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/api/feed/feedback",
            json={
                "video_id": "vid1",
                "rating": "thumbs_up",
                "comment": "Great video",
            },
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    mock_save.assert_called_once()


@pytest.mark.asyncio
@patch("src.api._save_feedback")
async def test_feedback_with_neutral_rating(mock_save):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/api/feed/feedback",
            json={"video_id": "vid2", "rating": "neutral"},
        )

    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


@pytest.mark.asyncio
async def test_feedback_with_invalid_rating_returns_400():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/api/feed/feedback",
            json={"video_id": "vid1", "rating": "invalid_rating"},
        )

    assert resp.status_code == 400
    assert "invalid_rating" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_feedback_with_missing_rating_returns_422():
    """Missing required field returns 422 (Pydantic validation)."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/api/feed/feedback",
            json={"video_id": "vid1"},
        )

    assert resp.status_code == 422
