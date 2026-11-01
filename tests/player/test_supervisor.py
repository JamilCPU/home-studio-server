"""Tests for the mpv supervisor's restart decisions (fakes only, no real mpv)."""

from __future__ import annotations

import threading

import pytest

from src.db import connect, migrate
from src.player.mpv import MpvIpc
from src.player.supervisor import MpvSupervisor
from src.store.settings import recent_events
from tests.player.fakes import FakeMpv


class FakeEngine:
    def __init__(self) -> None:
        self.replaced: list[FakeMpv] = []

    def replace_mpv(self, mpv) -> None:
        self.replaced.append(mpv)


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


class Factory:
    def __init__(self) -> None:
        self.made: list[FakeMpv] = []
        self.fail_next = 0

    def __call__(self) -> FakeMpv:
        if self.fail_next:
            self.fail_next -= 1
            raise OSError("mpv not found")
        mpv = FakeMpv()
        self.made.append(mpv)
        return mpv


@pytest.fixture
def factory_db(tmp_path):
    path = tmp_path / "s.db"
    with connect(path) as conn:
        migrate(conn)
    return lambda: connect(path)


@pytest.fixture
def parts(factory_db):
    engine, clock, make, first = FakeEngine(), Clock(), Factory(), FakeMpv()
    sup = MpvSupervisor(engine, first, make, factory_db, clock=clock)
    return sup, engine, clock, make, first


def player_events(db_factory):
    conn = db_factory()
    try:
        return [(r["level"], r["message"]) for r in recent_events(conn, 50)
                if r["component"] == "player"][::-1]
    finally:
        conn.close()


def test_alive_mpv_is_left_alone(parts):
    sup, engine, clock, make, _ = parts
    clock.t += 500
    assert sup.step() is False
    assert make.made == [] and engine.replaced == []


def test_dead_mpv_restarted_after_2s_backoff(parts, factory_db):
    sup, engine, clock, make, first = parts
    first.dead = True
    assert sup.step() is False  # death noticed, restart scheduled
    clock.t += 1.9
    assert sup.step() is False
    clock.t += 0.1
    assert sup.step() is True
    assert first.closed
    assert engine.replaced == make.made and len(make.made) == 1
    assert sup.mpv is make.made[0]
    assert player_events(factory_db)[-1] == ("info", "mpv restarted")


def test_backoff_doubles_up_to_60s(parts):
    sup, _, clock, make, first = parts
    waits = []
    current = first
    for _ in range(7):
        current.dead = True
        sup.step()
        start = clock.t
        while not sup.step():
            clock.t += 1
        waits.append(clock.t - start)
        current = sup.mpv
    assert waits == [2, 4, 8, 16, 32, 60, 60]


def test_backoff_resets_after_mpv_stays_up(parts):
    sup, _, clock, make, first = parts
    for _ in range(3):  # three quick deaths push the delay to 16 s
        sup.mpv.dead = True
        sup.step()
        clock.t += 60
        sup.step()
    clock.t += 61  # healthy for over a minute
    sup.step()
    sup.mpv.dead = True
    sup.step()
    clock.t += 2
    assert sup.step() is True


def test_failed_restart_logs_error_and_retries_later(parts, factory_db):
    sup, engine, clock, make, first = parts
    make.fail_next = 1
    first.dead = True
    sup.step()
    clock.t += 2
    assert sup.step() is False
    assert engine.replaced == []
    assert player_events(factory_db)[-1][0] == "error"
    sup.step()  # schedules the next attempt, now 4 s out
    clock.t += 3.9
    assert sup.step() is False
    clock.t += 0.1
    assert sup.step() is True
    assert engine.replaced == make.made


def test_failed_swap_closes_new_mpv_and_retries(factory_db):
    class FlakyEngine(FakeEngine):
        def __init__(self):
            super().__init__()
            self.fail = True

        def replace_mpv(self, mpv):
            if self.fail:
                self.fail = False
                raise RuntimeError("db locked")
            super().replace_mpv(mpv)

    engine, clock, make, first = FlakyEngine(), Clock(), Factory(), FakeMpv()
    sup = MpvSupervisor(engine, first, make, factory_db, clock=clock)
    first.dead = True
    sup.step()
    clock.t += 2
    assert sup.step() is False
    assert make.made[0].closed
    assert sup.mpv is first
    assert player_events(factory_db)[-1][0] == "error"
    sup.step()
    clock.t += 4
    assert sup.step() is True
    assert engine.replaced == [make.made[1]]
    assert sup.mpv is make.made[1] and not make.made[1].closed


def test_run_forever_stops_on_event(parts):
    sup, *_ = parts
    stop = threading.Event()
    thread = threading.Thread(target=sup.run_forever, args=(stop,), kwargs={"poll_s": 0.01})
    thread.start()
    stop.set()
    thread.join(timeout=2)
    assert not thread.is_alive()


def test_close_closes_current_mpv(parts):
    sup, *_, first = parts
    sup.close()
    assert first.closed


def test_mpv_ipc_exposes_dead():
    ipc = MpvIpc.__new__(MpvIpc)
    ipc._dead = False
    assert ipc.dead is False
    ipc._dead = True
    assert ipc.dead is True
