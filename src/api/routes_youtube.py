"""YouTube account sign-in (OAuth device flow) endpoints."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from src.api.app import Deps, ok
from src.sources.youtube_oauth import OAuthError, OAuthNotConfigured, OAuthStateError

NOT_SET_UP = "YouTube sign-in isn't set up on the Pi yet"
UNCONFIGURED = {
    "state": "unconfigured", "user_code": None, "verification_url": None,
    "expires_at": None, "error": None, "channel_title": None,
}


def youtube_account_state(oauth) -> str:
    """The /api/status value; no network call."""
    return "unconfigured" if oauth is None else oauth.state()


def build_router(deps: Deps) -> APIRouter:
    router = APIRouter()
    oauth = deps.youtube_oauth

    @router.get("/youtube/oauth")
    def get_oauth():
        return ok(dict(UNCONFIGURED) if oauth is None else oauth.status())

    def start():
        if oauth is None:
            raise HTTPException(409, NOT_SET_UP)
        try:
            return ok(oauth.start())
        except (OAuthNotConfigured, OAuthStateError) as exc:
            raise HTTPException(409, str(exc)) from None
        except OAuthError as exc:
            raise HTTPException(502, str(exc)) from None

    router.add_api_route("/youtube/oauth/start", start, methods=["POST"])
    router.add_api_route("/youtube/oauth", start, methods=["POST"])

    @router.delete("/youtube/oauth")
    def delete_oauth():
        return ok(dict(UNCONFIGURED) if oauth is None else oauth.disconnect())

    return router
