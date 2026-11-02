"""main(): env validation and the uvicorn hand-off."""

import pytest

from homestudio_mcp import __main__ as entry
from homestudio_mcp.middleware import SharedSecretMiddleware

API_URL = "http://home-studio.test:8765"
ENV = (
    "HOMESTUDIO_API_URL",
    "HOMESTUDIO_MCP_SHARED_SECRET",
    "HOMESTUDIO_MCP_HOST",
    "HOMESTUDIO_MCP_PORT",
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ENV:
        monkeypatch.delenv(name, raising=False)


def test_main_requires_env(monkeypatch, capsys) -> None:
    assert entry.main([]) == 1
    assert "HOMESTUDIO_API_URL" in capsys.readouterr().err
    monkeypatch.setenv("HOMESTUDIO_API_URL", API_URL)
    assert entry.main([]) == 1
    assert "HOMESTUDIO_MCP_SHARED_SECRET" in capsys.readouterr().err


def test_main_runs_uvicorn(monkeypatch) -> None:
    calls: list[tuple] = []
    monkeypatch.setattr(entry.uvicorn, "run", lambda app, **kw: calls.append((app, kw)))
    monkeypatch.setenv("HOMESTUDIO_API_URL", API_URL)
    monkeypatch.setenv("HOMESTUDIO_MCP_SHARED_SECRET", "s3cret")
    assert entry.main([]) == 0
    app, kwargs = calls[-1]
    assert isinstance(app, SharedSecretMiddleware)
    assert kwargs == {"host": "0.0.0.0", "port": 8000}
    monkeypatch.setenv("HOMESTUDIO_MCP_HOST", "127.0.0.1")
    monkeypatch.setenv("HOMESTUDIO_MCP_PORT", "9100")
    assert entry.main([]) == 0
    assert calls[-1][1] == {"host": "127.0.0.1", "port": 9100}


def test_main_rejects_bad_port(monkeypatch, capsys) -> None:
    monkeypatch.setenv("HOMESTUDIO_API_URL", API_URL)
    monkeypatch.setenv("HOMESTUDIO_MCP_SHARED_SECRET", "s3cret")
    monkeypatch.setenv("HOMESTUDIO_MCP_PORT", "eighty")
    assert entry.main([]) == 1
    assert "HOMESTUDIO_MCP_PORT" in capsys.readouterr().err
