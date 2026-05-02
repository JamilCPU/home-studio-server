"""Tests for src.profile — interest profile management and auto-tuning."""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timezone

import pytest

from src.profile import (
    _extract_topic_hints,
    auto_tune_profile,
    get_feedback_summary,
    load_profile,
    save_profile,
)


# ---------------------------------------------------------------------------
# load_profile
# ---------------------------------------------------------------------------


class TestLoadProfile:
    def test_load_from_existing_file(self, tmp_path, sample_interest_profile):
        profile_path = tmp_path / "profile.json"
        profile_path.write_text(json.dumps(sample_interest_profile))

        result = load_profile(profile_path)

        assert result == sample_interest_profile
        assert "AI agents" in result["topics"]

    def test_load_from_missing_file_returns_empty(self, tmp_path):
        profile_path = tmp_path / "nonexistent.json"

        result = load_profile(profile_path)

        assert result == {
            "topics": {},
            "preferred_channels": [],
            "blocked_channels": [],
        }


# ---------------------------------------------------------------------------
# save_profile
# ---------------------------------------------------------------------------


class TestSaveProfile:
    def test_writes_valid_json(self, tmp_path, sample_interest_profile):
        profile_path = tmp_path / "data" / "profile.json"

        save_profile(sample_interest_profile, profile_path)

        assert profile_path.exists()
        loaded = json.loads(profile_path.read_text())
        assert loaded == sample_interest_profile

    def test_creates_parent_directories(self, tmp_path):
        profile_path = tmp_path / "deep" / "nested" / "dir" / "profile.json"

        save_profile({"topics": {}}, profile_path)

        assert profile_path.exists()

    def test_overwrites_existing_file(self, tmp_path):
        profile_path = tmp_path / "profile.json"
        profile_path.write_text('{"old": true}')

        new_profile = {"topics": {"new_topic": 5.0}}
        save_profile(new_profile, profile_path)

        loaded = json.loads(profile_path.read_text())
        assert loaded == new_profile
        assert "old" not in loaded


# ---------------------------------------------------------------------------
# auto_tune_profile
# ---------------------------------------------------------------------------


class TestAutoTuneProfile:
    def test_thumbs_up_boosts_matching_topics(self, sample_interest_profile):
        feedback = [
            {
                "video_id": "v1",
                "title": "AI Agents in Practice",
                "channel_name": "TechChan",
                "rating": "thumbs_up",
                "comment": "",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        ]

        original_weight = sample_interest_profile["topics"]["AI agents"]
        result = auto_tune_profile(sample_interest_profile, feedback)

        assert result["topics"]["AI agents"] == min(10, original_weight + 0.5)

    def test_thumbs_down_reduces_matching_topics(self, sample_interest_profile):
        feedback = [
            {
                "video_id": "v2",
                "title": "Python tooling update",
                "channel_name": "PyChan",
                "rating": "thumbs_down",
                "comment": "",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        ]

        original_weight = sample_interest_profile["topics"]["Python tooling"]
        result = auto_tune_profile(sample_interest_profile, feedback)

        assert result["topics"]["Python tooling"] == max(1, original_weight - 0.5)

    def test_thumbs_up_adds_preferred_channel(self, sample_interest_profile):
        feedback = [
            {
                "video_id": "v1",
                "title": "Some Video",
                "channel_name": "NewFavorite",
                "rating": "thumbs_up",
                "comment": "",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        ]

        result = auto_tune_profile(sample_interest_profile, feedback)

        assert "NewFavorite" in result["preferred_channels"]

    def test_blocked_channels_after_three_thumbs_down(self, sample_interest_profile):
        feedback = [
            {
                "video_id": f"v{i}",
                "title": f"Bad Video {i}",
                "channel_name": "SpamChannel",
                "rating": "thumbs_down",
                "comment": "",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            for i in range(3)
        ]

        result = auto_tune_profile(sample_interest_profile, feedback)

        assert "SpamChannel" in result["blocked_channels"]

    def test_two_thumbs_down_does_not_block(self, sample_interest_profile):
        feedback = [
            {
                "video_id": f"v{i}",
                "title": f"Meh Video {i}",
                "channel_name": "BorderlineChannel",
                "rating": "thumbs_down",
                "comment": "",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            for i in range(2)
        ]

        result = auto_tune_profile(sample_interest_profile, feedback)

        assert "BorderlineChannel" not in result["blocked_channels"]

    def test_does_not_mutate_original(self, sample_interest_profile):
        original_copy = deepcopy(sample_interest_profile)
        feedback = [
            {
                "video_id": "v1",
                "title": "AI Agents tutorial",
                "channel_name": "TechChan",
                "rating": "thumbs_up",
                "comment": "more AI",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        ]

        auto_tune_profile(sample_interest_profile, feedback)

        assert sample_interest_profile == original_copy

    def test_no_recent_feedback_returns_original(self, sample_interest_profile):
        old_feedback = [
            {
                "video_id": "old",
                "title": "Old Video",
                "channel_name": "OldChan",
                "rating": "thumbs_up",
                "comment": "",
                "timestamp": "2020-01-01T00:00:00+00:00",
            },
        ]

        result = auto_tune_profile(sample_interest_profile, old_feedback)

        # Returns the original (not a copy) when no recent feedback
        assert result is sample_interest_profile

    def test_topic_weight_capped_at_10(self):
        profile = {
            "topics": {"AI agents": 9.8},
            "preferred_channels": [],
            "blocked_channels": [],
        }
        feedback = [
            {
                "video_id": "v1",
                "title": "AI Agents deep dive",
                "channel_name": "Chan",
                "rating": "thumbs_up",
                "comment": "",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        ]

        result = auto_tune_profile(profile, feedback)

        assert result["topics"]["AI agents"] == 10

    def test_topic_weight_floored_at_1(self):
        profile = {
            "topics": {"boring stuff": 1.2},
            "preferred_channels": [],
            "blocked_channels": [],
        }
        feedback = [
            {
                "video_id": "v1",
                "title": "boring stuff episode 99",
                "channel_name": "Chan",
                "rating": "thumbs_down",
                "comment": "",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        ]

        result = auto_tune_profile(profile, feedback)

        assert result["topics"]["boring stuff"] == 1


# ---------------------------------------------------------------------------
# _extract_topic_hints
# ---------------------------------------------------------------------------


class TestExtractTopicHints:
    def test_more_adds_new_topic(self):
        topics = {"existing": 5.0}
        _extract_topic_hints("more AI content", topics)

        assert "AI content" in topics
        assert topics["AI content"] == 6.0

    def test_want_adds_new_topic(self):
        topics = {}
        _extract_topic_hints("want machine learning", topics)

        assert "machine learning" in topics
        assert topics["machine learning"] == 6.0

    def test_less_reduces_matching_topics(self):
        # topic_hint "boring" must be a substring of existing topic lowercased
        topics = {"boring news": 7.0}
        _extract_topic_hints("less boring", topics)

        assert topics["boring news"] == 6.0

    def test_skip_reduces_matching_topics(self):
        # "daily" is a substring of "daily updates"
        topics = {"daily updates": 5.0}
        _extract_topic_hints("skip daily", topics)

        assert topics["daily updates"] == 4.0

    def test_reduction_floored_at_1(self):
        # "bad" is a substring of "bad topic"
        topics = {"bad topic": 1.5}
        _extract_topic_hints("hate bad", topics)

        assert topics["bad topic"] == 1

    def test_does_not_duplicate_existing_topic(self):
        topics = {"AI content": 8.0}
        _extract_topic_hints("more AI content", topics)

        # Should not overwrite existing topic with 6.0
        assert topics["AI content"] == 8.0

    def test_no_keywords_leaves_topics_unchanged(self):
        topics = {"AI": 5.0}
        original = dict(topics)
        _extract_topic_hints("this is a normal comment", topics)

        assert topics == original


# ---------------------------------------------------------------------------
# get_feedback_summary
# ---------------------------------------------------------------------------


class TestGetFeedbackSummary:
    def test_returns_correct_structure(self, sample_feedback_entries):
        result = get_feedback_summary(sample_feedback_entries)

        assert "liked_titles" in result
        assert "disliked_titles" in result
        assert "recent_comments" in result

    def test_liked_and_disliked_populated(self, sample_feedback_entries):
        result = get_feedback_summary(sample_feedback_entries)

        assert "AI Agent Architecture Patterns" in result["liked_titles"]
        assert "AI News Roundup #47" in result["disliked_titles"]

    def test_comments_included(self, sample_feedback_entries):
        result = get_feedback_summary(sample_feedback_entries)

        assert len(result["recent_comments"]) == 2
        assert any("practical" in c for c in result["recent_comments"])

    def test_old_feedback_excluded(self):
        old_entries = [
            {
                "video_id": "old1",
                "title": "Ancient Video",
                "rating": "thumbs_up",
                "comment": "old comment",
                "timestamp": "2020-01-01T00:00:00+00:00",
            },
        ]

        result = get_feedback_summary(old_entries, days=7)

        assert result["liked_titles"] == []
        assert result["disliked_titles"] == []
        assert result["recent_comments"] == []

    def test_empty_feedback_returns_empty_lists(self):
        result = get_feedback_summary([])

        assert result == {
            "liked_titles": [],
            "disliked_titles": [],
            "recent_comments": [],
        }

    def test_neutral_rating_not_in_liked_or_disliked(self):
        entries = [
            {
                "video_id": "n1",
                "title": "Neutral Video",
                "rating": "neutral",
                "comment": "",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        ]

        result = get_feedback_summary(entries)

        assert result["liked_titles"] == []
        assert result["disliked_titles"] == []

    def test_limits_to_10_entries(self):
        entries = [
            {
                "video_id": f"v{i}",
                "title": f"Video {i}",
                "rating": "thumbs_up",
                "comment": f"Comment {i}",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            for i in range(15)
        ]

        result = get_feedback_summary(entries)

        assert len(result["liked_titles"]) <= 10
        assert len(result["recent_comments"]) <= 10
