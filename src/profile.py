"""Interest profile management with auto-tuning from feedback."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT_PROFILE_PATH = Path("data/interest_profile.json")
FEEDBACK_PATH = Path("data/feedback.json")
SCORE_LOG_PATH = Path("data/score_log.json")


def load_profile(path: Path = DEFAULT_PROFILE_PATH) -> dict:
    """Load the interest profile from JSON."""
    if not path.exists():
        log.warning("Interest profile not found at %s — using empty profile", path)
        return {"topics": {}, "preferred_channels": [], "blocked_channels": []}

    with open(path) as f:
        return json.load(f)


def save_profile(profile: dict, path: Path = DEFAULT_PROFILE_PATH) -> None:
    """Save the interest profile to JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(profile, f, indent=2)
    log.info("Saved interest profile to %s", path)


def load_feedback(path: Path = FEEDBACK_PATH) -> list[dict]:
    """Load all feedback entries."""
    if not path.exists():
        return []
    with open(path) as f:
        return json.load(f)


def save_feedback_entry(entry: dict, path: Path = FEEDBACK_PATH) -> None:
    """Append a feedback entry to the feedback log."""
    entries = load_feedback(path)
    entries.append(entry)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(entries, f, indent=2)


def load_score_log(path: Path = SCORE_LOG_PATH) -> list[dict]:
    """Load the scoring history log."""
    if not path.exists():
        return []
    with open(path) as f:
        return json.load(f)


def append_score_log(scored_videos: list[dict], path: Path = SCORE_LOG_PATH) -> None:
    """Append scored video results to the log."""
    log_entries = load_score_log(path)
    run_entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "videos": scored_videos,
    }
    log_entries.append(run_entry)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(log_entries, f, indent=2)
    log.info("Appended %d scored videos to log", len(scored_videos))


def auto_tune_profile(
    profile: dict,
    feedback: list[dict],
    lookback_days: int = 14,
) -> dict:
    """Adjust topic weights based on recent feedback.

    Algorithm:
    - For each feedback entry with a comment, extract topic signals
    - thumbs_up on a video → boost topics that match its content
    - thumbs_down → reduce weight of matching topics
    - Channels in thumbs_up videos get added to preferred_channels
    - Channels in thumbs_down videos get added to blocked_channels (if repeated)

    Returns a new profile dict (does not mutate the original).
    """
    from copy import deepcopy

    cutoff = datetime.now(timezone.utc).timestamp() - (lookback_days * 86400)

    recent = []
    for entry in feedback:
        try:
            ts = datetime.fromisoformat(entry["timestamp"]).timestamp()
            if ts >= cutoff:
                recent.append(entry)
        except (KeyError, ValueError):
            continue

    if not recent:
        log.info("No recent feedback to tune from")
        return profile

    new_profile = deepcopy(profile)
    topics = new_profile.get("topics", {})
    preferred = set(new_profile.get("preferred_channels", []))
    blocked = set(new_profile.get("blocked_channels", []))

    thumbs_down_channels: dict[str, int] = {}

    for entry in recent:
        rating = entry.get("rating", "neutral")
        comment = entry.get("comment", "").lower()
        channel = entry.get("channel_name", "")
        title = entry.get("title", "").lower()

        if rating == "thumbs_up":
            if channel:
                preferred.add(channel)
            # Boost topics that appear in the title or comment
            for topic in topics:
                if topic.lower() in title or topic.lower() in comment:
                    topics[topic] = min(10, topics[topic] + 0.5)

        elif rating == "thumbs_down":
            if channel:
                thumbs_down_channels[channel] = thumbs_down_channels.get(channel, 0) + 1
            for topic in topics:
                if topic.lower() in title or topic.lower() in comment:
                    topics[topic] = max(1, topics[topic] - 0.5)

        # Parse explicit topic requests from comments
        if comment:
            _extract_topic_hints(comment, topics)

    # Block channels with 3+ thumbs_down
    for channel, count in thumbs_down_channels.items():
        if count >= 3:
            blocked.add(channel)
            preferred.discard(channel)

    new_profile["topics"] = topics
    new_profile["preferred_channels"] = sorted(preferred)
    new_profile["blocked_channels"] = sorted(blocked)

    log.info(
        "Auto-tuned profile: %d topic adjustments from %d feedback entries",
        len(recent),
        len(recent),
    )
    return new_profile


def _extract_topic_hints(comment: str, topics: dict[str, float]) -> None:
    """Extract topic hints from user comments like 'more X' or 'less Y'."""
    words = comment.split()
    for i, word in enumerate(words):
        if word in ("more", "want", "love", "prefer") and i + 1 < len(words):
            topic_hint = " ".join(words[i + 1 : i + 4]).strip(".,!?")
            if topic_hint and topic_hint not in topics:
                topics[topic_hint] = 6.0
                log.info("New topic from feedback: '%s' (weight=6.0)", topic_hint)

        elif word in ("less", "fewer", "skip", "hate", "boring") and i + 1 < len(words):
            topic_hint = " ".join(words[i + 1 : i + 4]).strip(".,!?")
            for existing in topics:
                if topic_hint in existing.lower():
                    topics[existing] = max(1, topics[existing] - 1.0)
                    log.info("Reduced topic weight: '%s' → %.1f", existing, topics[existing])


def get_feedback_summary(feedback: list[dict], days: int = 7) -> dict:
    """Generate a summary of recent feedback for inclusion in scoring prompts."""
    cutoff = datetime.now(timezone.utc).timestamp() - (days * 86400)

    liked = []
    disliked = []
    comments = []

    for entry in feedback:
        try:
            ts = datetime.fromisoformat(entry["timestamp"]).timestamp()
            if ts < cutoff:
                continue
        except (KeyError, ValueError):
            continue

        title = entry.get("title", "Unknown")
        rating = entry.get("rating", "neutral")
        comment = entry.get("comment", "")

        if rating == "thumbs_up":
            liked.append(title)
        elif rating == "thumbs_down":
            disliked.append(title)

        if comment:
            comments.append(f"On '{title}': {comment}")

    return {
        "liked_titles": liked[-10:],
        "disliked_titles": disliked[-10:],
        "recent_comments": comments[-10:],
    }
