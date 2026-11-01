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


def _resolve(base_dir: Path, value: str) -> str:
    """A relative path from the YAML is relative to the config file's directory."""
    candidate = Path(value)
    return str(candidate if candidate.is_absolute() else base_dir / candidate)


def load_config(path: Path = Path("config.yaml")) -> AppConfig:
    """Load application configuration.

    1. Read *path* (default ``config.yaml``).
    2. If a sibling ``config.local.yaml`` exists, deep-merge it on top.
    3. Map the nested YAML structure to the flat ``AppConfig`` dataclass.
       Relative ``db.path`` and ``youtube.cookies_path`` values are resolved
       against the config file's directory (env var paths are used as given).
    4. Apply environment variable overrides.
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
    api = raw.get("api", {})
    bluetooth = raw.get("bluetooth", {})
    db = raw.get("db", {})
    player = raw.get("player", {})
    agent = raw.get("agent", {})
    general = raw.get("general", {})
    base_dir = Path(path).resolve().parent

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
    cookies_path = os.environ.get("HOME_STUDIO_COOKIES_PATH") or _resolve(
        base_dir, youtube.get("cookies_path", "secrets/cookies.txt")
    )

    return AppConfig(
        # YouTube OAuth
        client_id=client_id,
        client_secret=client_secret,
        project_id=project_id,
        # Curation
        max_candidates=curation.get("max_candidates", 80),
        transcript_candidates=curation.get("transcript_candidates", 30),
        language=curation.get("language", "en"),
        # Scoring
        claude_command=agent.get("claude_command", "claude"),
        max_prompt_tokens=scoring.get("max_prompt_tokens", 50000),
        # API
        api_host=api.get("host", "0.0.0.0"),
        api_port=api.get("port", 8765),
        # Home Studio
        db_path=_resolve(base_dir, db.get("path", "data/home_studio.db")),
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
