"""Tests for src.config — YAML loading, deep merge, and env overrides."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from src.config import _deep_merge, load_config
from src.models import AppConfig


# ---------------------------------------------------------------------------
# _deep_merge
# ---------------------------------------------------------------------------


class TestDeepMerge:
    """Tests for the _deep_merge helper."""

    def test_flat_override(self) -> None:
        base = {"a": 1, "b": 2}
        override = {"b": 99}
        result = _deep_merge(base, override)
        assert result == {"a": 1, "b": 99}

    def test_nested_dict_merge(self) -> None:
        base = {"outer": {"a": 1, "b": 2}}
        override = {"outer": {"b": 99, "c": 3}}
        result = _deep_merge(base, override)
        assert result == {"outer": {"a": 1, "b": 99, "c": 3}}

    def test_override_adds_new_keys(self) -> None:
        base = {"x": 1}
        override = {"y": 2}
        result = _deep_merge(base, override)
        assert result == {"x": 1, "y": 2}

    def test_does_not_mutate_inputs(self) -> None:
        base = {"a": {"nested": 1}}
        override = {"a": {"nested": 2}}
        base_copy = {"a": {"nested": 1}}
        override_copy = {"a": {"nested": 2}}
        _deep_merge(base, override)
        assert base == base_copy
        assert override == override_copy

    def test_non_dict_override_replaces_dict(self) -> None:
        base = {"a": {"nested": 1}}
        override = {"a": "flat_now"}
        result = _deep_merge(base, override)
        assert result == {"a": "flat_now"}

    def test_empty_base(self) -> None:
        result = _deep_merge({}, {"a": 1})
        assert result == {"a": 1}

    def test_empty_override(self) -> None:
        result = _deep_merge({"a": 1}, {})
        assert result == {"a": 1}

    def test_both_empty(self) -> None:
        result = _deep_merge({}, {})
        assert result == {}


# ---------------------------------------------------------------------------
# load_config — real YAML file
# ---------------------------------------------------------------------------


class TestLoadConfig:
    """Tests for load_config using temporary YAML files."""

    def test_load_minimal_config(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            yaml.dump(
                {
                    "youtube": {
                        "client_id": "yaml-id",
                        "client_secret": "yaml-secret",
                        "project_id": "yaml-project",
                    },
                }
            ),
            encoding="utf-8",
        )
        cfg = load_config(config_file)
        assert isinstance(cfg, AppConfig)
        assert cfg.client_id == "yaml-id"
        assert cfg.client_secret == "yaml-secret"
        assert cfg.project_id == "yaml-project"

    def test_defaults_when_keys_missing(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.yaml"
        config_file.write_text("{}", encoding="utf-8")
        cfg = load_config(config_file)
        assert cfg.client_id == ""
        assert cfg.max_candidates == 80
        assert cfg.min_score == 7.0
        assert cfg.language == "en"
        assert cfg.api_port == 8765
        assert cfg.search_topics == ()

    def test_curation_section(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            yaml.dump(
                {
                    "curation": {
                        "max_candidates": 200,
                        "min_score": 8.5,
                        "search_topics": ["AI", "ML"],
                        "language": "fr",
                    },
                }
            ),
            encoding="utf-8",
        )
        cfg = load_config(config_file)
        assert cfg.max_candidates == 200
        assert cfg.min_score == 8.5
        assert cfg.search_topics == ("AI", "ML")
        assert cfg.language == "fr"

    def test_bluetooth_devices(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            yaml.dump(
                {
                    "bluetooth": {
                        "devices": {"speaker": "AA:BB:CC:DD:EE:FF"},
                    },
                }
            ),
            encoding="utf-8",
        )
        cfg = load_config(config_file)
        assert cfg.bt_devices == {"speaker": "AA:BB:CC:DD:EE:FF"}

    def test_local_yaml_merging(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            yaml.dump(
                {
                    "youtube": {"client_id": "base-id", "project_id": "base-proj"},
                    "curation": {"max_candidates": 50},
                }
            ),
            encoding="utf-8",
        )
        local_file = tmp_path / "config.local.yaml"
        local_file.write_text(
            yaml.dump(
                {
                    "youtube": {"client_id": "local-id"},
                    "curation": {"min_score": 9.0},
                }
            ),
            encoding="utf-8",
        )
        cfg = load_config(config_file)
        # local overrides client_id
        assert cfg.client_id == "local-id"
        # local does NOT wipe project_id from base
        assert cfg.project_id == "base-proj"
        # local adds min_score while keeping max_candidates from base
        assert cfg.max_candidates == 50
        assert cfg.min_score == 9.0


# ---------------------------------------------------------------------------
# Environment variable overrides
# ---------------------------------------------------------------------------


class TestEnvOverrides:
    """Tests for BRAINWASH_* environment variable overrides."""

    def test_env_overrides_client_id(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            yaml.dump({"youtube": {"client_id": "yaml-id"}}),
            encoding="utf-8",
        )
        monkeypatch.setenv("BRAINWASH_CLIENT_ID", "env-id")
        cfg = load_config(config_file)
        assert cfg.client_id == "env-id"

    def test_env_overrides_client_secret(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_file = tmp_path / "config.yaml"
        config_file.write_text("{}", encoding="utf-8")
        monkeypatch.setenv("BRAINWASH_CLIENT_SECRET", "env-secret")
        cfg = load_config(config_file)
        assert cfg.client_secret == "env-secret"

    def test_env_overrides_project_id(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_file = tmp_path / "config.yaml"
        config_file.write_text("{}", encoding="utf-8")
        monkeypatch.setenv("BRAINWASH_PROJECT_ID", "env-proj")
        cfg = load_config(config_file)
        assert cfg.project_id == "env-proj"

    def test_env_vars_take_precedence_over_yaml(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            yaml.dump(
                {
                    "youtube": {
                        "client_id": "yaml-id",
                        "client_secret": "yaml-secret",
                        "project_id": "yaml-proj",
                    },
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setenv("BRAINWASH_CLIENT_ID", "env-id")
        monkeypatch.setenv("BRAINWASH_CLIENT_SECRET", "env-secret")
        monkeypatch.setenv("BRAINWASH_PROJECT_ID", "env-proj")
        cfg = load_config(config_file)
        assert cfg.client_id == "env-id"
        assert cfg.client_secret == "env-secret"
        assert cfg.project_id == "env-proj"

    def test_yaml_used_when_env_not_set(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            yaml.dump({"youtube": {"client_id": "yaml-id"}}),
            encoding="utf-8",
        )
        monkeypatch.delenv("BRAINWASH_CLIENT_ID", raising=False)
        cfg = load_config(config_file)
        assert cfg.client_id == "yaml-id"


# ---------------------------------------------------------------------------
# Home Studio fields
# ---------------------------------------------------------------------------


class TestHomeStudioConfig:
    """Tests for the Home Studio config fields and HOME_STUDIO_* overrides."""

    def test_defaults(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.yaml"
        config_file.write_text("{}", encoding="utf-8")
        cfg = load_config(config_file)
        assert cfg.bt_adapter == "hci1"
        assert cfg.timezone == "America/Chicago"
        assert cfg.api_port == 8765
        assert cfg.quota_ceiling == 9500
        assert cfg.db_path == "data/home_studio.db"
        assert cfg.cookies_path == "secrets/cookies.txt"
        assert cfg.mpv_socket == "/tmp/home-studio-mpv.sock"
        assert cfg.output_device_label == "Echo"
        assert cfg.claude_timeout_s == 300
        assert cfg.youtube_api_key == ""
        assert cfg.bt_speaker_mac == ""

    def test_env_overrides_api_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_file = tmp_path / "config.yaml"
        config_file.write_text("{}", encoding="utf-8")
        monkeypatch.setenv("HOME_STUDIO_YOUTUBE_API_KEY", "k")
        cfg = load_config(config_file)
        assert cfg.youtube_api_key == "k"

    def test_env_overrides_mac_and_cookies(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            yaml.dump({"bluetooth": {"speaker_mac": "yaml"}}), encoding="utf-8"
        )
        monkeypatch.setenv("HOME_STUDIO_BT_SPEAKER_MAC", "11:22")
        monkeypatch.setenv("HOME_STUDIO_COOKIES_PATH", "/x/c.txt")
        cfg = load_config(config_file)
        assert cfg.bt_speaker_mac == "11:22"
        assert cfg.cookies_path == "/x/c.txt"

    def test_local_yaml_merges(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            yaml.dump({"bluetooth": {"adapter": "hci1"}}), encoding="utf-8"
        )
        (tmp_path / "config.local.yaml").write_text(
            yaml.dump({"bluetooth": {"speaker_mac": "AA:BB"}}), encoding="utf-8"
        )
        cfg = load_config(config_file)
        assert cfg.bt_speaker_mac == "AA:BB"
        assert cfg.bt_adapter == "hci1"

    def test_yaml_sections_map(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            yaml.dump(
                {
                    "youtube": {"api_key": "yk", "cookies_path": "c.txt"},
                    "db": {"path": "d.db"},
                    "player": {"mpv_socket": "/tmp/s.sock"},
                    "bluetooth": {"label": "Kitchen"},
                    "agent": {"claude_command": "cl", "timeout_s": 60},
                    "api": {"host": "127.0.0.1", "port": 9000},
                    "general": {"timezone": "UTC"},
                }
            ),
            encoding="utf-8",
        )
        cfg = load_config(config_file)
        assert cfg.youtube_api_key == "yk"
        assert cfg.cookies_path == "c.txt"
        assert cfg.db_path == "d.db"
        assert cfg.mpv_socket == "/tmp/s.sock"
        assert cfg.output_device_label == "Kitchen"
        assert cfg.claude_command == "cl"
        assert cfg.claude_timeout_s == 60
        assert cfg.api_host == "127.0.0.1"
        assert cfg.api_port == 9000
        assert cfg.timezone == "UTC"
