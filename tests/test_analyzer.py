"""Tests for src.analyzer — video scoring via Claude CLI."""

from __future__ import annotations

import json
import logging
import subprocess
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from src.analyzer import (
    ScoringError,
    _build_scoring_prompt,
    _call_claude,
    _extract_json,
    _parse_scoring_response,
    score_videos,
)
from src.models import AppConfig, ScoredVideo, TranscriptResult, VideoCandidate


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_candidate(
    video_id: str = "vid1",
    title: str = "Test Video",
    channel_name: str = "TestChan",
) -> VideoCandidate:
    return VideoCandidate(
        video_id=video_id,
        title=title,
        channel_name=channel_name,
        channel_id="UC_test",
        description="A test video description.",
        published_at=datetime(2026, 4, 30, tzinfo=timezone.utc),
        duration_seconds=600,
        view_count=1000,
        thumbnail_url="https://img.youtube.com/vi/vid1/hq.jpg",
        source="subscription",
    )


# ---------------------------------------------------------------------------
# _build_scoring_prompt
# ---------------------------------------------------------------------------


class TestBuildScoringPrompt:
    def test_contains_profile_data(self, sample_interest_profile):
        candidate = _make_candidate()
        prompt = _build_scoring_prompt([candidate], {}, sample_interest_profile)

        assert "AI agents" in prompt
        assert "LLM research" in prompt

    def test_contains_video_data(self, sample_interest_profile):
        candidate = _make_candidate(
            video_id="abc123",
            title="Deep Learning Explained",
            channel_name="SciChannel",
        )
        prompt = _build_scoring_prompt([candidate], {}, sample_interest_profile)

        assert "Deep Learning Explained" in prompt
        assert "abc123" in prompt
        assert "SciChannel" in prompt

    def test_contains_instructions(self, sample_interest_profile):
        candidate = _make_candidate()
        prompt = _build_scoring_prompt([candidate], {}, sample_interest_profile)

        assert "Instructions" in prompt
        assert "Score each video 1-10" in prompt
        assert "clickbait" in prompt

    def test_includes_transcript_excerpt(self, sample_interest_profile):
        candidate = _make_candidate(video_id="vid1")
        transcript = TranscriptResult(
            video_id="vid1",
            text="This is the transcript content about AI agents.",
            language="en",
            is_auto_generated=False,
        )
        prompt = _build_scoring_prompt(
            [candidate],
            {"vid1": transcript},
            sample_interest_profile,
        )

        assert "Transcript excerpt" in prompt
        assert "transcript content about AI agents" in prompt

    def test_missing_transcript_shows_unavailable(self, sample_interest_profile):
        candidate = _make_candidate(video_id="vid_no_transcript")
        prompt = _build_scoring_prompt([candidate], {}, sample_interest_profile)

        assert "Transcript:** unavailable" in prompt


# ---------------------------------------------------------------------------
# _extract_json
# ---------------------------------------------------------------------------


class TestExtractJson:
    def test_from_markdown_fence(self):
        text = 'Here is the result:\n```json\n[{"video_id": "a", "score": 8}]\n```'
        result = _extract_json(text)
        parsed = json.loads(result)
        assert isinstance(parsed, list)
        assert parsed[0]["video_id"] == "a"

    def test_from_markdown_fence_no_lang(self):
        text = 'Result:\n```\n[{"video_id": "b", "score": 7}]\n```'
        result = _extract_json(text)
        parsed = json.loads(result)
        assert parsed[0]["video_id"] == "b"

    def test_from_raw_brackets(self):
        text = 'Some preamble text [{"video_id": "c", "score": 6}] trailing text'
        result = _extract_json(text)
        parsed = json.loads(result)
        assert parsed[0]["video_id"] == "c"

    def test_from_plain_text_fallback(self):
        text = '[{"video_id": "d", "score": 5}]'
        result = _extract_json(text)
        parsed = json.loads(result)
        assert parsed[0]["video_id"] == "d"

    def test_no_json_returns_stripped_text(self):
        text = "  no json here  "
        result = _extract_json(text)
        assert result == "no json here"


# ---------------------------------------------------------------------------
# _parse_scoring_response
# ---------------------------------------------------------------------------


class TestParseScoringResponse:
    def test_valid_json_array_returns_scored_videos(self):
        candidates = [
            _make_candidate(video_id="v1", title="Video 1"),
            _make_candidate(video_id="v2", title="Video 2"),
        ]
        response = json.dumps([
            {"video_id": "v1", "score": 9.0, "reason": "Excellent"},
            {"video_id": "v2", "score": 7.5, "reason": "Good"},
        ])

        result = _parse_scoring_response(response, candidates)

        assert len(result) == 2
        assert all(isinstance(sv, ScoredVideo) for sv in result)
        assert result[0].video_id == "v1"
        assert result[0].score == 9.0
        assert result[0].reason == "Excellent"
        assert result[1].video_id == "v2"
        assert result[1].score == 7.5

    def test_unknown_video_id_logged_as_warning(self, caplog):
        candidates = [_make_candidate(video_id="v1")]
        response = json.dumps([
            {"video_id": "v1", "score": 8.0, "reason": "Good"},
            {"video_id": "unknown_id", "score": 5.0, "reason": "???"},
        ])

        with caplog.at_level(logging.WARNING, logger="src.analyzer"):
            result = _parse_scoring_response(response, candidates)

        assert len(result) == 1
        assert result[0].video_id == "v1"
        assert any("unknown_id" in record.message for record in caplog.records)

    def test_invalid_json_raises_scoring_error(self):
        candidates = [_make_candidate()]
        with pytest.raises(ScoringError, match="Invalid JSON"):
            _parse_scoring_response("not valid json at all", candidates)

    def test_non_array_json_raises_scoring_error(self):
        candidates = [_make_candidate()]
        with pytest.raises(ScoringError, match="Expected JSON array"):
            _parse_scoring_response('{"not": "an array"}', candidates)


# ---------------------------------------------------------------------------
# _call_claude
# ---------------------------------------------------------------------------


class TestCallClaude:
    @patch("src.analyzer.subprocess.run")
    def test_success(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout='[{"video_id": "v1", "score": 8}]',
            stderr="",
        )

        result = _call_claude("test prompt")

        assert result == '[{"video_id": "v1", "score": 8}]'
        mock_run.assert_called_once_with(
            ["claude", "-p", "test prompt"],
            capture_output=True,
            text=True,
            timeout=300,
        )

    @patch("src.analyzer.subprocess.run")
    def test_timeout_raises_scoring_error(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="claude", timeout=300)

        with pytest.raises(ScoringError, match="timed out"):
            _call_claude("test prompt")

    @patch("src.analyzer.subprocess.run")
    def test_nonzero_exit_raises_scoring_error(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="Some error occurred",
        )

        with pytest.raises(ScoringError, match="failed.*exit 1"):
            _call_claude("test prompt")

    @patch("src.analyzer.subprocess.run")
    def test_empty_output_raises_scoring_error(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="   ",
            stderr="",
        )

        with pytest.raises(ScoringError, match="empty output"):
            _call_claude("test prompt")

    @patch("src.analyzer.subprocess.run")
    def test_custom_claude_command(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="output",
            stderr="",
        )

        _call_claude("prompt", claude_command="/usr/local/bin/claude")

        assert mock_run.call_args[0][0][0] == "/usr/local/bin/claude"


# ---------------------------------------------------------------------------
# score_videos (end-to-end with mocked _call_claude)
# ---------------------------------------------------------------------------


class TestScoreVideos:
    @patch("src.analyzer._call_claude")
    def test_end_to_end(self, mock_claude, sample_config, sample_interest_profile):
        candidates = [
            _make_candidate(video_id="v1", title="Video One"),
            _make_candidate(video_id="v2", title="Video Two"),
        ]
        transcripts = {}

        mock_claude.return_value = json.dumps([
            {"video_id": "v1", "score": 9.0, "reason": "Great content"},
            {"video_id": "v2", "score": 6.0, "reason": "Decent"},
        ])

        result = score_videos(candidates, transcripts, sample_interest_profile, sample_config)

        assert len(result) == 2
        # Should be sorted descending by score
        assert result[0].video_id == "v1"
        assert result[0].score == 9.0
        assert result[1].video_id == "v2"
        assert result[1].score == 6.0

    @patch("src.analyzer._call_claude")
    def test_empty_candidates_returns_empty(self, mock_claude, sample_config, sample_interest_profile):
        result = score_videos([], {}, sample_interest_profile, sample_config)

        assert result == []
        mock_claude.assert_not_called()

    @patch("src.analyzer._call_claude")
    def test_sorted_descending(self, mock_claude, sample_config, sample_interest_profile):
        candidates = [
            _make_candidate(video_id="low"),
            _make_candidate(video_id="high"),
            _make_candidate(video_id="mid"),
        ]

        mock_claude.return_value = json.dumps([
            {"video_id": "low", "score": 3.0, "reason": "Low"},
            {"video_id": "high", "score": 10.0, "reason": "High"},
            {"video_id": "mid", "score": 6.5, "reason": "Mid"},
        ])

        result = score_videos(candidates, {}, sample_interest_profile, sample_config)

        scores = [sv.score for sv in result]
        assert scores == sorted(scores, reverse=True)
        assert result[0].video_id == "high"
        assert result[-1].video_id == "low"
