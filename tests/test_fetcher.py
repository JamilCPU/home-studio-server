"""Tests for src.fetcher module."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from src.fetcher import (
    _build_video_candidate,
    _parse_duration,
    deduplicate_candidates,
    fetch_liked_videos,
    fetch_search_results,
    fetch_subscription_videos,
    filter_candidates,
)
from src.models import VideoCandidate


# ---------------------------------------------------------------------------
# _parse_duration
# ---------------------------------------------------------------------------


class TestParseDuration:
    def test_hours_minutes_seconds(self) -> None:
        assert _parse_duration("PT1H2M30S") == 3750

    def test_minutes_only(self) -> None:
        assert _parse_duration("PT5M") == 300

    def test_seconds_only(self) -> None:
        assert _parse_duration("PT30S") == 30

    def test_hours_only(self) -> None:
        assert _parse_duration("PT1H") == 3600

    def test_invalid_returns_zero(self) -> None:
        assert _parse_duration("not-a-duration") == 0

    def test_empty_string_returns_zero(self) -> None:
        assert _parse_duration("") == 0


# ---------------------------------------------------------------------------
# _build_video_candidate
# ---------------------------------------------------------------------------


class TestBuildVideoCandidate:
    def test_constructs_from_api_dicts(self) -> None:
        item = {
            "snippet": {
                "title": "Test Video",
                "channelTitle": "TestChannel",
                "channelId": "UC_test",
                "description": "A test video description.",
                "publishedAt": "2026-04-30T12:00:00Z",
                "thumbnails": {
                    "high": {"url": "https://example.com/hq.jpg"},
                },
            },
            "id": {"videoId": "vid_001"},
        }
        details = {
            "id": "vid_001",
            "snippet": item["snippet"],
            "contentDetails": {"duration": "PT10M30S"},
            "statistics": {"viewCount": "12345"},
        }

        candidate = _build_video_candidate(item, details, "subscription")

        assert isinstance(candidate, VideoCandidate)
        assert candidate.video_id == "vid_001"
        assert candidate.title == "Test Video"
        assert candidate.channel_name == "TestChannel"
        assert candidate.channel_id == "UC_test"
        assert candidate.description == "A test video description."
        assert candidate.duration_seconds == 630
        assert candidate.view_count == 12345
        assert candidate.thumbnail_url == "https://example.com/hq.jpg"
        assert candidate.source == "subscription"
        assert candidate.published_at == datetime(
            2026, 4, 30, 12, 0, 0, tzinfo=timezone.utc,
        )

    def test_missing_fields_use_defaults(self) -> None:
        item: dict = {"snippet": {}, "id": {}}
        details: dict = {"id": "vid_empty", "contentDetails": {}, "statistics": {}}

        candidate = _build_video_candidate(item, details, "search")

        assert candidate.video_id == "vid_empty"
        assert candidate.title == ""
        assert candidate.channel_name == ""
        assert candidate.view_count == 0
        assert candidate.source == "search"

    def test_fallback_thumbnail_to_medium(self) -> None:
        item = {
            "snippet": {
                "thumbnails": {
                    "medium": {"url": "https://example.com/med.jpg"},
                },
            },
        }
        details: dict = {"id": "vid_t", "contentDetails": {}, "statistics": {}}

        candidate = _build_video_candidate(item, details, "liked")
        assert candidate.thumbnail_url == "https://example.com/med.jpg"


# ---------------------------------------------------------------------------
# deduplicate_candidates
# ---------------------------------------------------------------------------


class TestDeduplicateCandidates:
    def test_removes_duplicates_keeps_first(
        self, sample_video_candidate: VideoCandidate,
    ) -> None:
        from dataclasses import replace

        dup = replace(sample_video_candidate, title="Duplicate Title")
        result = deduplicate_candidates([sample_video_candidate, dup])

        assert len(result) == 1
        assert result[0].title == sample_video_candidate.title

    def test_no_duplicates_unchanged(
        self, sample_video_candidate: VideoCandidate,
    ) -> None:
        from dataclasses import replace

        other = replace(sample_video_candidate, video_id="other_id")
        result = deduplicate_candidates([sample_video_candidate, other])

        assert len(result) == 2

    def test_empty_list(self) -> None:
        assert deduplicate_candidates([]) == []


# ---------------------------------------------------------------------------
# filter_candidates
# ---------------------------------------------------------------------------


class TestFilterCandidates:
    def test_filters_by_duration_range(
        self, sample_video_candidate: VideoCandidate,
    ) -> None:
        from dataclasses import replace

        too_short = replace(sample_video_candidate, video_id="short", duration_seconds=60)
        too_long = replace(sample_video_candidate, video_id="long", duration_seconds=7200)
        just_right = replace(sample_video_candidate, video_id="ok", duration_seconds=600)

        result = filter_candidates(
            [too_short, too_long, just_right],
            min_duration_minutes=5,
            max_duration_minutes=60,
        )

        assert len(result) == 1
        assert result[0].video_id == "ok"

    def test_inclusive_boundaries(
        self, sample_video_candidate: VideoCandidate,
    ) -> None:
        from dataclasses import replace

        at_min = replace(sample_video_candidate, video_id="min", duration_seconds=300)
        at_max = replace(sample_video_candidate, video_id="max", duration_seconds=3600)

        result = filter_candidates(
            [at_min, at_max],
            min_duration_minutes=5,
            max_duration_minutes=60,
        )

        assert len(result) == 2

    def test_empty_list(self) -> None:
        assert filter_candidates([]) == []


# ---------------------------------------------------------------------------
# Helpers for mocking the YouTube API resource
# ---------------------------------------------------------------------------


def _make_youtube_mock() -> MagicMock:
    """Return a MagicMock that mimics ``googleapiclient.discovery.Resource``."""
    return MagicMock()


def _chain_execute(mock_resource: MagicMock, method_chain: list[str], response: dict) -> None:
    """Set up ``mock.method1().method2().execute()`` to return *response*.

    *method_chain* is e.g. ``["subscriptions", "list"]``.
    """
    obj = mock_resource
    for method in method_chain:
        obj = getattr(obj, method).return_value
    obj.execute.return_value = response


# ---------------------------------------------------------------------------
# fetch_subscription_videos
# ---------------------------------------------------------------------------


class TestFetchSubscriptionVideos:
    def test_returns_video_candidates(self) -> None:
        yt = _make_youtube_mock()

        # subscriptions().list().execute()
        yt.subscriptions.return_value.list.return_value.execute.return_value = {
            "items": [
                {"snippet": {"resourceId": {"channelId": "UC_ch1"}}},
            ],
            "nextPageToken": None,
        }

        # search().list().execute() — one video per channel
        yt.search.return_value.list.return_value.execute.return_value = {
            "items": [
                {
                    "id": {"videoId": "vid_sub1"},
                    "snippet": {
                        "title": "Sub Video",
                        "channelTitle": "Ch1",
                        "channelId": "UC_ch1",
                        "description": "desc",
                        "publishedAt": "2026-04-30T10:00:00Z",
                        "thumbnails": {"default": {"url": "https://img/thumb.jpg"}},
                    },
                },
            ],
        }

        # videos().list().execute() — details
        yt.videos.return_value.list.return_value.execute.return_value = {
            "items": [
                {
                    "id": "vid_sub1",
                    "snippet": {
                        "title": "Sub Video",
                        "channelTitle": "Ch1",
                        "channelId": "UC_ch1",
                        "description": "desc",
                        "publishedAt": "2026-04-30T10:00:00Z",
                        "thumbnails": {"default": {"url": "https://img/thumb.jpg"}},
                    },
                    "contentDetails": {"duration": "PT15M"},
                    "statistics": {"viewCount": "999"},
                },
            ],
        }

        result = fetch_subscription_videos(yt, recency_hours=48, max_results=50)

        assert len(result) == 1
        assert isinstance(result[0], VideoCandidate)
        assert result[0].video_id == "vid_sub1"
        assert result[0].source == "subscription"

    def test_returns_empty_when_no_subscriptions(self) -> None:
        yt = _make_youtube_mock()
        yt.subscriptions.return_value.list.return_value.execute.return_value = {
            "items": [],
        }

        result = fetch_subscription_videos(yt)
        assert result == []


# ---------------------------------------------------------------------------
# fetch_search_results
# ---------------------------------------------------------------------------


class TestFetchSearchResults:
    def test_returns_candidates_for_queries(self) -> None:
        yt = _make_youtube_mock()

        yt.search.return_value.list.return_value.execute.return_value = {
            "items": [
                {
                    "id": {"videoId": "vid_s1"},
                    "snippet": {
                        "title": "Search Result",
                        "channelTitle": "SearchCh",
                        "channelId": "UC_sch",
                        "description": "found it",
                        "publishedAt": "2026-04-29T08:00:00Z",
                        "thumbnails": {"default": {"url": "https://img/s.jpg"}},
                    },
                },
            ],
        }

        yt.videos.return_value.list.return_value.execute.return_value = {
            "items": [
                {
                    "id": "vid_s1",
                    "snippet": {
                        "title": "Search Result",
                        "channelTitle": "SearchCh",
                        "channelId": "UC_sch",
                        "description": "found it",
                        "publishedAt": "2026-04-29T08:00:00Z",
                        "thumbnails": {"default": {"url": "https://img/s.jpg"}},
                    },
                    "contentDetails": {"duration": "PT20M"},
                    "statistics": {"viewCount": "5000"},
                },
            ],
        }

        result = fetch_search_results(yt, queries=["AI agents"], recency_hours=48)

        assert len(result) == 1
        assert result[0].video_id == "vid_s1"
        assert result[0].source == "search"

    def test_returns_empty_for_no_results(self) -> None:
        yt = _make_youtube_mock()
        yt.search.return_value.list.return_value.execute.return_value = {
            "items": [],
        }

        result = fetch_search_results(yt, queries=["nonexistent topic"])
        assert result == []


# ---------------------------------------------------------------------------
# fetch_liked_videos
# ---------------------------------------------------------------------------


class TestFetchLikedVideos:
    def test_returns_liked_candidates(self) -> None:
        yt = _make_youtube_mock()

        yt.videos.return_value.list.return_value.execute.return_value = {
            "items": [
                {
                    "id": "vid_liked1",
                    "snippet": {
                        "title": "Liked Video",
                        "channelTitle": "LikedCh",
                        "channelId": "UC_liked",
                        "description": "I liked this",
                        "publishedAt": "2026-04-28T06:00:00Z",
                        "thumbnails": {"high": {"url": "https://img/l.jpg"}},
                    },
                    "contentDetails": {"duration": "PT8M"},
                    "statistics": {"viewCount": "100000"},
                },
            ],
        }

        result = fetch_liked_videos(yt, max_results=10)

        assert len(result) == 1
        assert result[0].video_id == "vid_liked1"
        assert result[0].source == "liked"
        assert result[0].view_count == 100000

    def test_returns_empty_on_api_error(self) -> None:
        yt = _make_youtube_mock()
        yt.videos.return_value.list.return_value.execute.side_effect = RuntimeError("API down")

        result = fetch_liked_videos(yt)
        assert result == []
