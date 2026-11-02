"""Agent request endpoints ("find me something about ...")."""

from __future__ import annotations

from contextlib import closing
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from src.api.app import Deps, ok
from src.api.models import request_out
from src.store.agent import create_request, get_request, list_requests, queued_for_request


class AgentRequestBody(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)
    prompt: str = Field(min_length=1, max_length=1000)
    play: Literal["now", "end"]


def build_router(deps: Deps) -> APIRouter:
    router = APIRouter()

    @router.post("/agent/requests")
    def submit(body: AgentRequestBody):
        with closing(deps.db_factory()) as conn:
            request_id = create_request(conn, body.prompt, "user", body.play)
        deps.worker.submit(request_id)
        return ok({"id": request_id}, 202)

    @router.get("/agent/requests/{id}")
    def get_one(id: int):  # noqa: A002 - path name is part of the contract
        with closing(deps.db_factory()) as conn:
            req = get_request(conn, id)
            if req is None:
                raise HTTPException(404, "request not found")
            return ok(request_out(req, queued_for_request(conn, id)))

    @router.get("/agent/requests")
    def get_many(limit: int = Query(default=20, ge=1, le=100)):
        with closing(deps.db_factory()) as conn:
            reqs = list_requests(conn, limit)
            return ok({"requests": [request_out(r, queued_for_request(conn, r.id)) for r in reqs]})

    return router
