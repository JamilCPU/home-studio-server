"""Now-playing and transport-control endpoints."""

from __future__ import annotations

from typing import Any, Callable

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict

from src.api.app import Deps, ok
from src.api.models import now_out
from src.player.mpv import MpvCommandError, MpvDied


class SeekBody(BaseModel):
    model_config = ConfigDict(extra="ignore")
    position_s: float


class SkipBody(BaseModel):
    model_config = ConfigDict(extra="ignore")
    delta_s: float


def engine_call(fn: Callable[..., Any], *args: Any) -> Any:
    """Run an engine call, mapping mpv failures to 503/409."""
    try:
        return fn(*args)
    except (MpvDied, TimeoutError):
        raise HTTPException(503, "player unavailable") from None
    except MpvCommandError as exc:
        raise HTTPException(409, str(exc)) from None


def build_router(deps: Deps) -> APIRouter:
    router = APIRouter()

    def now_response():
        now = engine_call(deps.engine.now)
        return ok(now_out(now, deps.config.output_device_label))

    def control(fn: Callable[..., Any], *args: Any):
        engine_call(fn, *args)
        return now_response()

    @router.get("/now")
    def get_now():
        return now_response()

    @router.post("/player/toggle")
    def toggle():
        return control(deps.engine.toggle_pause)

    @router.post("/player/next")
    def next_track():
        return control(deps.engine.next)

    @router.post("/player/previous")
    def previous_track():
        return control(deps.engine.previous)

    @router.post("/player/stop")
    def stop():
        return control(deps.engine.stop)

    @router.post("/player/seek")
    def seek(body: SeekBody):
        return control(deps.engine.seek, body.position_s)

    @router.post("/player/skip")
    def skip(body: SkipBody):
        return control(deps.engine.skip, body.delta_s)

    return router
