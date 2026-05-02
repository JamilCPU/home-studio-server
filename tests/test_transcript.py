"""Tests for src.transcript module."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from youtube_transcript_api import NoTranscriptFound, TranscriptsDisabled, YouTubeTranscriptApi

from src.models import TranscriptResult
from src.transcript import fetch_transcript, fetch_transcripts_batch, truncate_transcript


# ---------------------------------------------------------------------------
# fetch_transcript
# ---------------------------------------------------------------------------


class TestFetchTranscript:
    @patch("src.transcript._is_auto_generated", return_value=False)
    @patch.object(YouTubeTranscriptApi, "get_transcript")
    def test_success(
        self,
        mock_get: MagicMock,
        mock_auto: MagicMock,
    ) -> None:
        mock_get.return_value = [
            {"text": "Hello world.", "start": 0.0, "duration": 2.0},
            {"text": "How are you?", "start": 2.0, "duration": 2.0},
        ]

        result = fetch_transcript("vid_ok", language="en")

        assert result is not None
        assert isinstance(result, TranscriptResult)
        assert result.video_id == "vid_ok"
        assert result.text == "Hello world. How are you?"
        assert result.language == "en"
        assert result.is_auto_generated is False

        mock_get.assert_called_once_with("vid_ok", languages=["en", "en-auto"])

    @patch.object(YouTubeTranscriptApi, "get_transcript")
    def test_returns_none_on_transcripts_disabled(
        self,
        mock_get: MagicMock,
    ) -> None:
        mock_get.side_effect = TranscriptsDisabled("vid_dis")

        result = fetch_transcript("vid_dis")
        assert result is None

    @patch.object(YouTubeTranscriptApi, "get_transcript")
    def test_returns_none_on_no_transcript_found(
        self,
        mock_get: MagicMock,
    ) -> None:
        mock_get.side_effect = NoTranscriptFound(
            "vid_nf", ["en"], {"en": "English"},
        )

        result = fetch_transcript("vid_nf", language="en")
        assert result is None


# ---------------------------------------------------------------------------
# fetch_transcripts_batch
# ---------------------------------------------------------------------------


class TestFetchTranscriptsBatch:
    @patch("src.transcript._is_auto_generated", return_value=False)
    @patch.object(YouTubeTranscriptApi, "get_transcript")
    def test_returns_dict_of_successful_results(
        self,
        mock_get: MagicMock,
        mock_auto: MagicMock,
    ) -> None:
        def _side_effect(video_id: str, languages: list[str]) -> list[dict]:
            if video_id == "vid_a":
                return [{"text": "Transcript A.", "start": 0.0, "duration": 1.0}]
            if video_id == "vid_b":
                return [{"text": "Transcript B.", "start": 0.0, "duration": 1.0}]
            raise TranscriptsDisabled(video_id)

        mock_get.side_effect = _side_effect

        result = fetch_transcripts_batch(["vid_a", "vid_b", "vid_c"], language="en")

        assert len(result) == 2
        assert "vid_a" in result
        assert "vid_b" in result
        assert "vid_c" not in result
        assert result["vid_a"].text == "Transcript A."
        assert result["vid_b"].text == "Transcript B."

    @patch.object(YouTubeTranscriptApi, "get_transcript")
    def test_returns_empty_when_all_fail(
        self,
        mock_get: MagicMock,
    ) -> None:
        mock_get.side_effect = TranscriptsDisabled("any")

        result = fetch_transcripts_batch(["vid_x", "vid_y"])
        assert result == {}


# ---------------------------------------------------------------------------
# truncate_transcript
# ---------------------------------------------------------------------------


class TestTruncateTranscript:
    def test_short_text_unchanged(self) -> None:
        short = "This is a short transcript."
        assert truncate_transcript(short, max_chars=100) == short

    def test_long_text_cut_at_sentence_boundary(self) -> None:
        text = "First sentence. Second sentence. Third sentence is long enough to exceed the limit."
        result = truncate_transcript(text, max_chars=35)

        # Should cut after "Second sentence." since that is the last ". " before 35
        assert result == "First sentence. Second sentence."
        assert len(result) <= 35

    def test_long_text_no_boundary_cuts_hard(self) -> None:
        text = "A" * 100
        result = truncate_transcript(text, max_chars=50)

        assert len(result) == 50
        assert result == "A" * 50

    def test_exact_length_unchanged(self) -> None:
        text = "Exact length."
        assert truncate_transcript(text, max_chars=len(text)) == text

    def test_boundary_at_very_end(self) -> None:
        text = "One sentence. Overflow"
        result = truncate_transcript(text, max_chars=20)
        assert result == "One sentence."
