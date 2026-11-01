"""FastAPI app factory, shared dependencies and the response envelope.

Every response is ``{"ok": bool, "data": <payload|null>, "error": <string|null>}``,
including validation errors (422) and unexpected failures (500).
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from typing import Any, Callable

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from src.models import AppConfig

logger = logging.getLogger(__name__)

_LOC_PREFIXES = ("body", "query", "path")


@dataclass(frozen=True)
class Deps:
    db_factory: Callable[[], sqlite3.Connection]
    engine: Any  # PlaybackEngine
    worker: Any  # AgentWorker
    data_api: Any  # DataApi, or None without an API key
    ytdlp: Any  # YtDlp
    bt: Any  # BtMonitor
    scheduler_run_now: Callable[[], int]
    config: AppConfig


def ok(data: Any, status: int = 200) -> JSONResponse:
    return JSONResponse({"ok": True, "data": data, "error": None}, status_code=status)


def fail(message: str, status: int) -> JSONResponse:
    return JSONResponse({"ok": False, "data": None, "error": message}, status_code=status)


def _describe_validation(exc: RequestValidationError) -> str:
    parts = []
    for err in exc.errors():
        loc = [str(p) for p in err.get("loc", ())]
        if len(loc) > 1 and loc[0] in _LOC_PREFIXES:
            loc = loc[1:]
        parts.append(f"{'.'.join(loc)}: {err.get('msg', 'invalid')}")
    return "; ".join(parts) or "invalid request"


async def _http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
    return fail(str(exc.detail), exc.status_code)


async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
    return fail(_describe_validation(exc), 422)


async def _unexpected_error(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("unhandled error on %s %s", request.method, request.url.path, exc_info=exc)
    return fail("internal error", 500)


def create_app(deps: Deps) -> FastAPI:
    from src.api import routes_agent, routes_library, routes_player, routes_queue, routes_system

    app = FastAPI(title="Home Studio", version="1.0")
    app.add_exception_handler(StarletteHTTPException, _http_error)
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(Exception, _unexpected_error)
    for module in (routes_player, routes_queue, routes_library, routes_agent, routes_system):
        app.include_router(module.build_router(deps), prefix="/api")
    return app
