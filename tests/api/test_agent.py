"""Tests for /api/agent/requests."""

from __future__ import annotations

from src.store import queue
from src.store.agent import get_request, set_status
from tests.api.conftest import make_video

REQUEST_KEYS = {"id", "prompt", "status", "summary", "error", "items", "created_at",
                "finished_at"}


def test_agent_request_202_and_worker_submit_called(api):
    resp = api.client.post("/api/agent/requests", json={"prompt": "something on GPUs",
                                                        "play": "now"})
    assert resp.status_code == 202
    body = resp.json()
    assert body["ok"] is True
    request_id = body["data"]["id"]
    assert api.worker.submitted == [request_id]
    with api.conn() as conn:
        req = get_request(conn, request_id)
    assert req.prompt == "something on GPUs"
    assert req.kind == "user"
    assert req.play == "now"


def test_agent_request_validation(api):
    assert api.client.post("/api/agent/requests", json={"prompt": "", "play": "now"}
                           ).status_code == 422
    assert api.client.post("/api/agent/requests", json={"prompt": "x" * 1001, "play": "end"}
                           ).status_code == 422
    bad_play = api.client.post("/api/agent/requests", json={"prompt": "x", "play": "later"})
    assert bad_play.status_code == 422
    assert "play" in bad_play.json()["error"]
    assert api.worker.submitted == []


def test_get_agent_request_with_items(api):
    request_id = api.client.post("/api/agent/requests", json={"prompt": "p", "play": "end"}
                                 ).json()["data"]["id"]
    api.stored(make_video("aaaaaaaaaaa"))
    with api.conn() as conn:
        queue.enqueue(conn, "aaaaaaaaaaa", "agent", reason="r", agent_request_id=request_id)
        set_status(conn, request_id, "done", summary="Picked one")
    resp = api.client.get(f"/api/agent/requests/{request_id}")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert set(data) == REQUEST_KEYS
    assert data["status"] == "done"
    assert data["summary"] == "Picked one"
    assert data["finished_at"] is not None
    assert [i["video_id"] for i in data["items"]] == ["aaaaaaaaaaa"]


def test_get_agent_request_missing_404(api):
    resp = api.client.get("/api/agent/requests/999")
    assert resp.status_code == 404
    assert resp.json() == {"ok": False, "data": None, "error": "request not found"}


def test_list_agent_requests(api):
    for prompt in ("one", "two", "three"):
        api.client.post("/api/agent/requests", json={"prompt": prompt, "play": "end"})
    resp = api.client.get("/api/agent/requests", params={"limit": 2})
    assert resp.status_code == 200
    requests = resp.json()["data"]["requests"]
    assert [r["prompt"] for r in requests] == ["three", "two"]
    assert set(requests[0]) == REQUEST_KEYS
