"""Video scoring via Claude Code CLI for YoutubeBrainwash.

Builds a scoring prompt from candidate videos, transcripts, and the
user's interest profile, then calls ``claude -p`` to produce relevance
scores.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from datetime import datetime, timezone

from src.models import AppConfig, ScoredVideo, TranscriptResult, VideoCandidate
from src.transcript import truncate_transcript

logger = logging.getLogger(__name__)


class ScoringError(Exception):
    """Raised when the Claude scoring pipeline fails."""


def score_videos(
    candidates: list[VideoCandidate],
    transcripts: dict[str, TranscriptResult],
    profile: dict,
    config: AppConfig,
) -> list[ScoredVideo]:
    """Score a list of candidate videos using Claude.

    Returns a list of ``ScoredVideo`` sorted by score descending.
    """
    if not candidates:
        logger.warning("No candidates to score")
        return []

    prompt = _build_scoring_prompt(candidates, transcripts, profile)
    raw_response = _call_claude(prompt, claude_command=config.claude_command)
    scored = _parse_scoring_response(raw_response, candidates)

    scored_sorted = sorted(scored, key=lambda sv: sv.score, reverse=True)
    logger.info(
        "Scored %d/%d candidates (top score: %.1f)",
        len(scored_sorted),
        len(candidates),
        scored_sorted[0].score if scored_sorted else 0.0,
    )
    return scored_sorted


def _build_scoring_prompt(
    candidates: list[VideoCandidate],
    transcripts: dict[str, TranscriptResult],
    profile: dict,
) -> str:
    """Assemble the full scoring prompt for Claude."""
    video_sections: list[str] = []

    for candidate in candidates:
        transcript_excerpt = ""
        tr = transcripts.get(candidate.video_id)
        if tr is not None:
            transcript_excerpt = truncate_transcript(tr.text, max_chars=2000)

        section = (
            f"### {candidate.title}\n"
            f"- **Video ID:** {candidate.video_id}\n"
            f"- **Channel:** {candidate.channel_name}\n"
            f"- **Duration:** {candidate.duration_seconds}s\n"
            f"- **Description:** {candidate.description[:500]}\n"
        )
        if transcript_excerpt:
            section += f"- **Transcript excerpt:** {transcript_excerpt}\n"
        else:
            section += "- **Transcript:** unavailable\n"

        video_sections.append(section)

    videos_block = "\n".join(video_sections)
    profile_block = json.dumps(profile, indent=2)

    return (
        "You are a YouTube content curator. Given the user's interest "
        "profile and a list of candidate videos with their transcripts, "
        "score each video from 1-10 for relevance and quality.\n\n"
        "## User Interest Profile\n"
        f"{profile_block}\n\n"
        "## Candidate Videos\n"
        f"{videos_block}\n\n"
        "## Instructions\n"
        "- Score each video 1-10 based on topic relevance, depth, and quality\n"
        "- Consider the user's topic weights and preferred formats\n"
        "- Penalize clickbait titles that don't match transcript content\n"
        "- Prefer deep technical content over surface-level overviews\n"
        "- Return ONLY valid JSON array: "
        '[{"video_id": "...", "score": 8.5, "reason": "..."}]\n'
    )


def _call_claude(prompt: str, claude_command: str = "claude") -> str:
    """Invoke the Claude CLI and return its stdout.

    Raises ``ScoringError`` on timeout, non-zero exit, or empty output.
    """
    try:
        result = subprocess.run(
            [claude_command, "-p", prompt],
            capture_output=True,
            text=True,
            timeout=300,
        )
    except subprocess.TimeoutExpired as exc:
        logger.error("Claude CLI timed out after 300s")
        raise ScoringError("Claude CLI timed out") from exc
    except FileNotFoundError as exc:
        logger.error("Claude CLI not found at '%s'", claude_command)
        raise ScoringError(
            f"Claude CLI not found: {claude_command}"
        ) from exc

    if result.returncode != 0:
        logger.error(
            "Claude CLI exited with code %d: %s",
            result.returncode,
            result.stderr.strip(),
        )
        raise ScoringError(
            f"Claude CLI failed (exit {result.returncode}): {result.stderr.strip()}"
        )

    output = result.stdout.strip()
    if not output:
        logger.error("Claude CLI returned empty output")
        raise ScoringError("Claude CLI returned empty output")

    logger.debug("Claude CLI response length: %d chars", len(output))
    return output


def _parse_scoring_response(
    response: str,
    candidates: list[VideoCandidate],
) -> list[ScoredVideo]:
    """Parse Claude's JSON response into a list of ``ScoredVideo``.

    Handles both raw JSON and markdown-fenced code blocks.
    """
    # Build a lookup for candidate metadata.
    candidate_map: dict[str, VideoCandidate] = {
        c.video_id: c for c in candidates
    }

    json_text = _extract_json(response)

    try:
        parsed = json.loads(json_text)
    except json.JSONDecodeError as exc:
        logger.error("Failed to parse scoring JSON: %s", exc)
        raise ScoringError(f"Invalid JSON in Claude response: {exc}") from exc

    if not isinstance(parsed, list):
        raise ScoringError(
            f"Expected JSON array, got {type(parsed).__name__}"
        )

    scored_at = datetime.now(timezone.utc)
    scored_videos: list[ScoredVideo] = []

    for entry in parsed:
        video_id = entry.get("video_id", "")
        candidate = candidate_map.get(video_id)

        if candidate is None:
            logger.warning(
                "Scoring response contains unknown video_id: %s",
                video_id,
            )
            continue

        scored_videos.append(
            ScoredVideo(
                video_id=video_id,
                title=candidate.title,
                channel_name=candidate.channel_name,
                score=float(entry.get("score", 0.0)),
                reason=str(entry.get("reason", "")),
                duration_seconds=candidate.duration_seconds,
                thumbnail_url=candidate.thumbnail_url,
                scored_at=scored_at,
            )
        )

    return scored_videos


def _extract_json(text: str) -> str:
    """Extract a JSON array from *text*.

    Looks for a fenced code block (```json ... ```) first, then falls
    back to finding the outermost ``[...]`` in the raw text.
    """
    # Try markdown code fence first.
    fence_match = re.search(
        r"```(?:json)?\s*\n?(.*?)```",
        text,
        re.DOTALL,
    )
    if fence_match:
        return fence_match.group(1).strip()

    # Fall back to outermost brackets.
    bracket_start = text.find("[")
    bracket_end = text.rfind("]")
    if bracket_start != -1 and bracket_end != -1 and bracket_end > bracket_start:
        return text[bracket_start : bracket_end + 1]

    # Last resort: return as-is and let the caller handle parse errors.
    return text.strip()
