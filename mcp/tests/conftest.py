"""Shared fixtures: the Home Studio API is mocked with respx."""

import pytest
import respx

HOME = "http://home-studio.test:8765"


def ok(data: object) -> dict:
    return {"ok": True, "data": data, "error": None}


@pytest.fixture
def api():
    with respx.mock(base_url=HOME, assert_all_called=False) as mock:
        yield mock
