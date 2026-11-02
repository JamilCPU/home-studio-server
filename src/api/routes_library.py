"""Ratings, listening history and taste-profile endpoints."""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from src.agent.taste import remove_trait
from src.api.app import Deps, ok
from src.api.models import history_out, rating_entry_out, taste_out
from src.store.agent import add_correction, latest_taste
from src.store.videos import add_rating, get_video, history, list_ratings


class RatingBody(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)
    video_id: str = Field(min_length=1, max_length=64)
    liked: bool | None = None
    satisfaction: int | None = Field(default=None, ge=1, le=10)
    notes: str | None = Field(default=None, max_length=2000)


class CorrectionBody(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)
    text: str = Field(min_length=1, max_length=1000)


class TraitBody(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)
    kind: Literal["likes", "dislikes"]
    text: str = Field(min_length=1, max_length=1000)


def _normalize_since(value: str) -> str:
    """Parse an ISO 8601 timestamp (naive = UTC, trailing Z ok) into a UTC isoformat string."""
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        raise HTTPException(422, "since: must be an ISO 8601 timestamp") from None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def build_router(deps: Deps) -> APIRouter:
    router = APIRouter()

    @router.post("/ratings")
    def rate(body: RatingBody):
        with closing(deps.db_factory()) as conn:
            if get_video(conn, body.video_id) is None:
                raise HTTPException(404, "video not found")
            try:
                rating_id = add_rating(
                    conn, body.video_id, body.liked, body.satisfaction, body.notes or None
                )
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from None
            return ok({"id": rating_id}, 201)

    @router.get("/history")
    def get_history(
        unrated: bool = False,
        since: str | None = Query(default=None),
        limit: int = Query(default=50, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
    ):
        since_utc = _normalize_since(since) if since is not None else None
        with closing(deps.db_factory()) as conn:
            entries = history(
                conn, unrated_only=unrated, since=since_utc, limit=limit, offset=offset
            )
            return ok({"entries": [history_out(e) for e in entries]})

    @router.get("/ratings")
    def get_ratings(
        since: str | None = Query(default=None),
        limit: int = Query(default=50, ge=1, le=200),
    ):
        since_utc = _normalize_since(since) if since is not None else None
        with closing(deps.db_factory()) as conn:
            rows = list_ratings(conn, since=since_utc, limit=limit)
            return ok({"ratings": [rating_entry_out(r) for r in rows]})

    @router.get("/taste")
    def get_taste():
        with closing(deps.db_factory()) as conn:
            return ok(taste_out(latest_taste(conn)))

    @router.post("/taste/corrections")
    def correct_taste(body: CorrectionBody):
        with closing(deps.db_factory()) as conn:
            correction_id = add_correction(conn, body.text)
        deps.worker.submit_taste_refresh()
        return ok({"id": correction_id}, 202)

    @router.delete("/taste/traits")
    def delete_trait(body: TraitBody):
        with closing(deps.db_factory()) as conn:
            try:
                taste = remove_trait(conn, body.kind, body.text)
            except ValueError as exc:
                raise HTTPException(404, str(exc)) from None
            return ok(taste_out(taste))

    return router
