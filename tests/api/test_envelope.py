"""Envelope behavior for errors, and the docs/api.md contract check."""

from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path

from fastapi.testclient import TestClient

from src.api.app import create_app

DOC = Path(__file__).resolve().parents[2] / "docs" / "api.md"
HEADING = re.compile(r"^### (GET|POST|PUT|DELETE|PATCH) (/\S+)\s*$", re.MULTILINE)


def test_unknown_route_is_enveloped_404(api):
    resp = api.client.get("/api/nope")
    assert resp.status_code == 404
    assert resp.json() == {"ok": False, "data": None, "error": "Not Found"}


def test_wrong_method_is_enveloped_405(api):
    resp = api.client.put("/api/now")
    assert resp.status_code == 405
    assert resp.json()["ok"] is False


def test_unexpected_exception_is_enveloped_500(api, caplog):
    def broken() -> int:
        raise RuntimeError("secret detail")

    app = create_app(replace(api.deps, scheduler_run_now=broken))
    resp = TestClient(app, raise_server_exceptions=False).post("/api/morning/test")
    assert resp.status_code == 500
    assert resp.json() == {"ok": False, "data": None, "error": "internal error"}
    assert "secret detail" in caplog.text


def test_malformed_json_is_enveloped_422(api):
    resp = api.client.post("/api/player/seek", content=b"{nope",
                           headers={"content-type": "application/json"})
    assert resp.status_code == 422
    assert resp.json()["ok"] is False


def test_every_documented_endpoint_exists(api):
    documented = HEADING.findall(DOC.read_text(encoding="utf-8"))
    assert len(documented) >= 25
    paths = api.client.app.openapi()["paths"]
    missing = [f"{m} {p}" for m, p in documented if m.lower() not in paths.get(p, {})]
    assert missing == []


def test_every_route_is_documented(api):
    documented = set(HEADING.findall(DOC.read_text(encoding="utf-8")))
    paths = api.client.app.openapi()["paths"]
    actual = {(m.upper(), p) for p, ops in paths.items() for m in ops}
    assert actual - documented == set()
