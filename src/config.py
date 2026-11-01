"""Configuration loader for Home Studio.

Loads config.yaml, optionally deep-merges config.local.yaml on top,
and applies environment variable overrides.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import yaml

from src.models import AppConfig

logger = logging.getLogger(__name__)


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge *override* into a copy of *base*.

    Nested dicts are merged; all other values are replaced.
    Neither input dict is mutated.
    """
    merged: dict = {}
    for key in base:
        if key in override:
            base_val = base[key]
            over_val = override[key]
            if isinstance(base_val, dict) and isinstance(over_val, dict):
                merged[key] = _deep_merge(base_val, over_val)
            else:
                merged[key] = over_val
        else:
            merged[key] = base[key]

    for key in override:
        if key not in base:
            merged[key] = override[key]

    return merged


def load_config(path: Path = Path("config.yaml")) -> AppConfig:
    """Load application configuration.

    1. Read *path* (default ``config.yaml``).
    2. If a sibling ``config.local.yaml`` exists, deep-merge it on top.
    3. Map the nested YAML structure to the flat ``AppConfig`` dataclass.
    4. Apply environment variable overrides for OAuth credentials.
    """
    logger.info("Loading config from %s", path)

    with open(path, encoding="utf-8") as fh:
        raw: dict = yaml.safe_load(fh) or {}

    local_path = path.parent / "config.local.yaml"
    if local_path.exists():
        logger.info("Merging local overrides from %s", local_path)
        with open(local_path, encoding="utf-8") as fh:
            local_raw: dict = yaml.safe_load(fh) or {}
        raw = _deep_merge(raw, local_raw)

    # -- Flatten nested YAML sections into AppConfig fields ----------------

    youtube = raw.get("youtube", {})
    curation = raw.get("curation", {})
    scoring = raw.get("scoring", {})
    playback = raw.get("playback", {})
    schedule = raw.get("schedule", {})
    api = raw.get("api", {})
    bluetooth = raw.get("bluetooth", {})
    db = raw.get("db", {})
    player = raw.get("player", {})
    agent = raw.get("agent", {})
    general = raw.get("general", {})

    # Convert search_topics list to tuple for the frozen dataclass
    search_topics_raw = curation.get("search_topics", ())
    search_topics = tuple(search_topics_raw) if search_topics_raw else ()

    # Bluetooth devices live under bluetooth.devices in the YAML
    bt_devices: dict[str, str] = bluetooth.get("devices", {}) or {}

    # -- Environment variable overrides ------------------------------------

    client_id = os.environ.get(
        "BRAINWASH_CLIENT_ID",
        youtube.get("client_id", ""),
    )
    client_secret = os.environ.get(
        "BRAINWASH_CLIENT_SECRET",
        youtube.get("client_secret", ""),
    )
    project_id = os.environ.get(
        "BRAINWASH_PROJECT_ID",
        youtube.get("project_id", ""),
    )

    if client_id and client_id != youtube.get("client_id", ""):
        logger.info("Using BRAINWASH_CLIENT_ID from environment")
    if client_secret and client_secret != youtube.get("client_secret", ""):
        logger.info("Using BRAINWASH_CLIENT_SECRET from environment")
    if project_id and project_id != youtube.get("project_id", ""):
        logger.info("Using BRAINWASH_PROJECT_ID from environment")

    youtube_api_key = os.environ.get(
        "HOME_STUDIO_YOUTUBE_API_KEY", youtube.get("api_key", "")
    )
    bt_speaker_mac = os.environ.get(
        "HOME_STUDIO_BT_SPEAKER_MAC", bluetooth.get("speaker_mac", "")
    )
    cookies_path = os.environ.get(
        "HOME_STUDIO_COOKIES_PATH",
        youtube.get("cookies_path", "secrets/cookies.txt"),
    )

    return AppConfig(
        # YouTube OAuth
        client_id=client_id,
        client_secret=client_secret,
        project_id=project_id,
        # Curation
        max_candidates=curation.get("max_candidates", 80),
        transcript_candidates=curation.get("transcript_candidates", 30),
        min_score=float(curation.get("min_score", 7.0)),
        max_feed_size=curation.get("max_feed_size", 15),
        search_topics=search_topics,
        min_duration_minutes=curation.get("min_duration_minutes", 5),
        max_duration_minutes=curation.get("max_duration_minutes", 60),
        recency_hours=curation.get("recency_hours", 48),
        language=curation.get("language", "en"),
        # Scoring
        claude_command=agent.get(
            "claude_command", scoring.get("claude_command", "claude")
        ),
        max_prompt_tokens=scoring.get("max_prompt_tokens", 50000),
        # Playback
        snapcast_port=playback.get("snapcast_port", 1704),
        snapcast_control_port=playback.get("snapcast_control_port", 1705),
        sample_rate=playback.get("sample_rate", 48000),
        channels=playback.get("channels", 2),
        snap_fifo=playback.get("snap_fifo", "/tmp/snapfifo"),
        # Schedule
        curation_time=schedule.get("curation_time", "04:00"),
        default_wakeup_time=schedule.get("default_wakeup_time", "07:30"),
        # API
        api_host=api.get("host", "0.0.0.0"),
        api_port=api.get("port", 8765),
        # Bluetooth
        bt_devices=bt_devices,
        # Home Studio
        db_path=db.get("path", "data/home_studio.db"),
        youtube_api_key=youtube_api_key,
        cookies_path=cookies_path,
        mpv_socket=player.get("mpv_socket", "/tmp/home-studio-mpv.sock"),
        bt_adapter=bluetooth.get("adapter", "hci1"),
        bt_speaker_mac=bt_speaker_mac,
        output_device_label=bluetooth.get("label", "Echo"),
        timezone=general.get("timezone", "America/Chicago"),
        claude_timeout_s=int(agent.get("timeout_s", 300)),
        quota_ceiling=int(youtube.get("quota_ceiling", 9500)),
    )
