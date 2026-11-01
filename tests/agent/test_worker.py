"""Tests for the background agent worker."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.agent.worker import AgentWorker
from src.db import connect, migrate
from src.store.agent import create_request, get_request, set_status
from src.store.settings import recent_events


@pytest.fixture
def factory(tmp_path):
    path = tmp_path / "worker.db"
    setup = connect(path)
    migrate(setup)
    setup.close()
    return lambda: connect(path)


class FakeEngine:
    def __init__(self, state="idle"):
        self.state = state
        self.played = 0

    def now(self):
        return SimpleNamespace(state=self.state)

    def play_next(self):
        self.played += 1


def make_worker(factory, run, engine=None, refresh=None, should=lambda conn: False):
    return AgentWorker(
        factory,
        run,
        engine or FakeEngine(),
        refresh or (lambda conn: None),
        should_refresh_fn=should,
    )


def test_worker_runs_in_order_and_starts_idle_engine(factory):
    conn = factory()
    ids = [create_request(conn, f"p{i}", "user", "now") for i in range(3)]
    order = []

    def run(c, rid):
        order.append(rid)
        set_status(c, rid, "done", summary="ok")
        return [1] if rid != ids[1] else []

    engine = FakeEngine("idle")
    worker = make_worker(factory, run, engine)
    worker.start()
    try:
        for rid in ids:
            worker.submit(rid)
        assert worker.join_idle(5)
    finally:
        worker.stop()
    assert order == ids
    assert engine.played == 2  # the request with no items does not start playback
    assert get_request(conn, ids[0]).status == "done"


def test_worker_does_not_interrupt_playing_engine(factory):
    conn = factory()
    rid = create_request(conn, "p", "user", "now")
    engine = FakeEngine("playing")
    worker = make_worker(factory, lambda c, r: [1], engine)
    worker.start()
    try:
        worker.submit(rid)
        assert worker.join_idle(5)
    finally:
        worker.stop()
    assert engine.played == 0


def test_worker_end_requests_do_not_start_engine(factory):
    conn = factory()
    rid = create_request(conn, "p", "user", "end")
    engine = FakeEngine("idle")
    worker = make_worker(factory, lambda c, r: [1], engine)
    worker.start()
    try:
        worker.submit(rid)
        assert worker.join_idle(5)
    finally:
        worker.stop()
    assert engine.played == 0


def test_worker_resets_running_on_start(factory):
    conn = factory()
    stuck = create_request(conn, "p", "user", "end")
    set_status(conn, stuck, "running")
    pending = create_request(conn, "q", "user", "end")
    worker = make_worker(factory, lambda c, r: [])
    worker.start()
    worker.stop()
    req = get_request(conn, stuck)
    assert (req.status, req.error) == ("failed", "server restarted")
    assert get_request(conn, pending).status == "pending"


def test_worker_survives_exceptions_and_logs(factory):
    conn = factory()
    bad = create_request(conn, "p", "user", "end")
    good = create_request(conn, "q", "user", "end")
    seen = []

    def run(c, rid):
        if rid == bad:
            raise RuntimeError("boom")
        seen.append(rid)
        return []

    worker = make_worker(factory, run)
    worker.start()
    try:
        worker.submit(bad)
        worker.submit(good)
        assert worker.join_idle(5)
    finally:
        worker.stop()
    assert seen == [good]
    assert get_request(conn, bad).status == "failed"


def test_worker_refreshes_taste_only_when_due(factory):
    conn = factory()
    rid = create_request(conn, "p", "user", "end")
    calls = []
    due = {"v": False}
    worker = make_worker(
        factory, lambda c, r: [], refresh=lambda c: calls.append(1), should=lambda c: due["v"]
    )
    worker.start()
    try:
        worker.submit(rid)
        assert worker.join_idle(5)
        assert calls == []
        due["v"] = True
        worker.submit(rid)
        assert worker.join_idle(5)
    finally:
        worker.stop()
    assert calls == [1]


def test_refresh_failure_is_logged_not_raised(factory):
    conn = factory()
    rid = create_request(conn, "p", "user", "end")

    def refresh(c):
        raise ValueError("bad taste")

    worker = make_worker(factory, lambda c, r: [], refresh=refresh, should=lambda c: True)
    worker.start()
    try:
        worker.submit(rid)
        assert worker.join_idle(5)
        worker.submit(rid)
        assert worker.join_idle(5)  # thread still alive
    finally:
        worker.stop()
    events = [e for e in recent_events(conn) if e["component"] == "agent"]
    assert events and events[0]["level"] == "error"
    assert events[0]["message"] == "ValueError: bad taste"


def test_submit_taste_refresh_runs_refresh(factory):
    calls = []
    worker = make_worker(factory, lambda c, r: [], refresh=lambda c: calls.append(1))
    worker.start()
    try:
        worker.submit_taste_refresh()
        assert worker.join_idle(5)
    finally:
        worker.stop()
    assert calls == [1]
