"""OAuth 2.0 authentication for the YouTube Data API v3."""

from __future__ import annotations

import logging
from pathlib import Path

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import Resource, build

from src.models import AppConfig

log = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/youtube",
]
TOKEN_PATH = Path("token.json")


def get_youtube_service(config: AppConfig) -> Resource:
    """Authenticate and return an authenticated YouTube API service resource."""
    credentials = _load_or_refresh_credentials(TOKEN_PATH)
    if credentials is None:
        credentials = _authenticate_interactive(config, TOKEN_PATH)
    return build("youtube", "v3", credentials=credentials)


def _load_or_refresh_credentials(token_path: Path) -> Credentials | None:
    """Load cached credentials from token.json, refreshing if expired.

    Forces re-authentication if the stored token is missing required scopes.
    """
    if not token_path.exists():
        return None

    try:
        credentials = Credentials.from_authorized_user_file(str(token_path), SCOPES)
    except Exception as exc:
        log.warning("Failed to load %s: %s — will re-authenticate", token_path, exc)
        return None

    # Check that all required scopes are present
    if not credentials.scopes or not set(SCOPES).issubset(credentials.scopes):
        missing = set(SCOPES) - set(credentials.scopes or set())
        log.warning(
            "Token missing scopes %s — will re-authenticate to acquire them",
            missing,
        )
        return None

    if credentials.valid:
        return credentials

    if credentials.expired and credentials.refresh_token:
        try:
            credentials.refresh(Request())
            _save_credentials(credentials, token_path)
            log.info("OAuth token refreshed successfully")
            return credentials
        except RefreshError as exc:
            log.warning("Token refresh failed (%s) — will re-authenticate", exc)
            return None

    return None


def _authenticate_interactive(config: AppConfig, token_path: Path) -> Credentials:
    """Run the local OAuth 2.0 flow to obtain credentials (opens browser)."""
    log.info("Starting interactive OAuth flow — a browser window will open")
    client_config = _build_client_config(config)
    flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
    credentials = flow.run_local_server(port=0)
    _save_credentials(credentials, token_path)
    log.info("Authentication successful — token saved to %s", token_path)
    return credentials


def _save_credentials(credentials: Credentials, token_path: Path) -> None:
    """Persist credentials to disk as JSON."""
    token_path.write_text(credentials.to_json())


def _build_client_config(config: AppConfig) -> dict:
    """Build the OAuth client config dict from AppConfig fields."""
    return {
        "installed": {
            "client_id": config.client_id,
            "client_secret": config.client_secret,
            "project_id": config.project_id,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
            "redirect_uris": ["urn:ietf:wg:oauth:2.0:oob", "http://localhost"],
        }
    }
