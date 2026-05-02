"""Tests for src.auth — OAuth helpers (external deps mocked)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.auth import (
    SCOPES,
    _build_client_config,
    _load_or_refresh_credentials,
    get_youtube_service,
)
from src.models import AppConfig


# ---------------------------------------------------------------------------
# _build_client_config
# ---------------------------------------------------------------------------


class TestBuildClientConfig:
    """Tests for _build_client_config."""

    def test_returns_installed_structure(self, sample_config: AppConfig) -> None:
        result = _build_client_config(sample_config)
        assert "installed" in result
        installed = result["installed"]
        assert installed["client_id"] == "test-client-id"
        assert installed["client_secret"] == "test-client-secret"
        assert installed["project_id"] == "test-project"

    def test_contains_google_uris(self, sample_config: AppConfig) -> None:
        installed = _build_client_config(sample_config)["installed"]
        assert installed["auth_uri"] == "https://accounts.google.com/o/oauth2/auth"
        assert installed["token_uri"] == "https://oauth2.googleapis.com/token"
        assert (
            installed["auth_provider_x509_cert_url"]
            == "https://www.googleapis.com/oauth2/v1/certs"
        )

    def test_redirect_uris(self, sample_config: AppConfig) -> None:
        installed = _build_client_config(sample_config)["installed"]
        assert "urn:ietf:wg:oauth:2.0:oob" in installed["redirect_uris"]
        assert "http://localhost" in installed["redirect_uris"]

    def test_uses_config_values(self) -> None:
        config = AppConfig(
            client_id="custom-id",
            client_secret="custom-secret",
            project_id="custom-proj",
        )
        installed = _build_client_config(config)["installed"]
        assert installed["client_id"] == "custom-id"
        assert installed["client_secret"] == "custom-secret"
        assert installed["project_id"] == "custom-proj"


# ---------------------------------------------------------------------------
# _load_or_refresh_credentials
# ---------------------------------------------------------------------------


class TestLoadOrRefreshCredentials:
    """Tests for _load_or_refresh_credentials."""

    def test_returns_none_when_token_missing(self, tmp_path: Path) -> None:
        token_path = tmp_path / "token.json"
        result = _load_or_refresh_credentials(token_path)
        assert result is None

    @patch("src.auth.Credentials.from_authorized_user_file")
    def test_returns_none_when_file_is_corrupt(
        self, mock_load: MagicMock, tmp_path: Path
    ) -> None:
        token_path = tmp_path / "token.json"
        token_path.write_text("not-valid-json")
        mock_load.side_effect = ValueError("bad json")
        result = _load_or_refresh_credentials(token_path)
        assert result is None

    @patch("src.auth.Credentials.from_authorized_user_file")
    def test_returns_none_when_scopes_missing(
        self, mock_load: MagicMock, tmp_path: Path
    ) -> None:
        token_path = tmp_path / "token.json"
        token_path.write_text("{}")
        cred = MagicMock()
        # Only one of the two required scopes present
        cred.scopes = {"https://www.googleapis.com/auth/youtube.readonly"}
        mock_load.return_value = cred
        result = _load_or_refresh_credentials(token_path)
        assert result is None

    @patch("src.auth.Credentials.from_authorized_user_file")
    def test_returns_none_when_scopes_is_none(
        self, mock_load: MagicMock, tmp_path: Path
    ) -> None:
        token_path = tmp_path / "token.json"
        token_path.write_text("{}")
        cred = MagicMock()
        cred.scopes = None
        mock_load.return_value = cred
        result = _load_or_refresh_credentials(token_path)
        assert result is None

    @patch("src.auth.Credentials.from_authorized_user_file")
    def test_returns_valid_credentials(
        self, mock_load: MagicMock, tmp_path: Path
    ) -> None:
        token_path = tmp_path / "token.json"
        token_path.write_text("{}")
        cred = MagicMock()
        cred.scopes = set(SCOPES)
        cred.valid = True
        mock_load.return_value = cred
        result = _load_or_refresh_credentials(token_path)
        assert result is cred

    @patch("src.auth._save_credentials")
    @patch("src.auth.Request")
    @patch("src.auth.Credentials.from_authorized_user_file")
    def test_refreshes_expired_token(
        self,
        mock_load: MagicMock,
        mock_request_cls: MagicMock,
        mock_save: MagicMock,
        tmp_path: Path,
    ) -> None:
        token_path = tmp_path / "token.json"
        token_path.write_text("{}")
        cred = MagicMock()
        cred.scopes = set(SCOPES)
        cred.valid = False
        cred.expired = True
        cred.refresh_token = "refresh-tok"
        mock_load.return_value = cred
        result = _load_or_refresh_credentials(token_path)
        cred.refresh.assert_called_once_with(mock_request_cls.return_value)
        mock_save.assert_called_once_with(cred, token_path)
        assert result is cred

    @patch("src.auth.Request")
    @patch("src.auth.Credentials.from_authorized_user_file")
    def test_returns_none_on_refresh_failure(
        self,
        mock_load: MagicMock,
        mock_request_cls: MagicMock,
        tmp_path: Path,
    ) -> None:
        from google.auth.exceptions import RefreshError

        token_path = tmp_path / "token.json"
        token_path.write_text("{}")
        cred = MagicMock()
        cred.scopes = set(SCOPES)
        cred.valid = False
        cred.expired = True
        cred.refresh_token = "refresh-tok"
        cred.refresh.side_effect = RefreshError("expired")
        mock_load.return_value = cred
        result = _load_or_refresh_credentials(token_path)
        assert result is None


# ---------------------------------------------------------------------------
# get_youtube_service
# ---------------------------------------------------------------------------


class TestGetYoutubeService:
    """Tests for get_youtube_service."""

    @patch("src.auth.build")
    @patch("src.auth._load_or_refresh_credentials")
    def test_uses_cached_credentials(
        self,
        mock_load_creds: MagicMock,
        mock_build: MagicMock,
        sample_config: AppConfig,
    ) -> None:
        mock_cred = MagicMock()
        mock_load_creds.return_value = mock_cred
        mock_build.return_value = MagicMock(name="service")

        service = get_youtube_service(sample_config)

        mock_build.assert_called_once_with(
            "youtube", "v3", credentials=mock_cred
        )
        assert service is mock_build.return_value

    @patch("src.auth.build")
    @patch("src.auth._authenticate_interactive")
    @patch("src.auth._load_or_refresh_credentials")
    def test_falls_back_to_interactive_auth(
        self,
        mock_load_creds: MagicMock,
        mock_interactive: MagicMock,
        mock_build: MagicMock,
        sample_config: AppConfig,
    ) -> None:
        mock_load_creds.return_value = None
        mock_new_cred = MagicMock()
        mock_interactive.return_value = mock_new_cred

        get_youtube_service(sample_config)

        mock_interactive.assert_called_once_with(sample_config, Path("token.json"))
        mock_build.assert_called_once_with(
            "youtube", "v3", credentials=mock_new_cred
        )


# ---------------------------------------------------------------------------
# SCOPES constant
# ---------------------------------------------------------------------------


class TestScopes:
    """Verify SCOPES contains the expected YouTube API scopes."""

    def test_contains_readonly_scope(self) -> None:
        assert "https://www.googleapis.com/auth/youtube.readonly" in SCOPES

    def test_contains_full_scope(self) -> None:
        assert "https://www.googleapis.com/auth/youtube" in SCOPES

    def test_exactly_two_scopes(self) -> None:
        assert len(SCOPES) == 2
