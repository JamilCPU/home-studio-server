"""Tests for the morning scheduler."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from src.db import connect, migrate
from src.player.engine import NowPlaying
from src.scheduler import MorningScheduler, event_logged
from src.store.agent import list_requests
from src.store.queue import enqueue, upcoming
from src.store.settings import MorningSettings, log_event, record_import, save_morning
from src.store.videos import Video, upsert_video

CT = ZoneInfo("America/Chicago")
ALL_DAYS = (0, 1, 2, 3, 4, 5, 6)


def vid(video_id, duration_s=600):
    return Video(
        video_id=video_id, title=f"Title {video_id}", channel_name=None, channel_id=None,
        description=None, duration_s=duration_s, published_at=None, tags=(), category=None,
        chapters=(), thumbnail_url=None, view_count=None, like_count=None, transcript=None,
    )


class FakeEngine:
    def __init__(self, state="idle", fail_plays=0):
        self.state = state
        self.played = 0
        self.stopped = 0
        self.fail_plays = fail_plays  # play_next returns None this many times first

    def now(self):
        return NowPlaying(self.state, None, 0.0, None)

    def stop(self):
        self.stopped += 1
        self.state = "idle"

    def play_next(self):
        self.played += 1
        if self.fail_plays:
            self.fail_plays -= 1
            return None
        self.state = "playing"
        return object()


class Runner:
    """run_request_fn double: queues ``produce`` morning videos or returns nothing."""

    def __init__(self, produce=()):
        self.produce = list(produce)
        self.calls = []

    def __call__(self, conn, request_id):
        self.calls.append(request_id)
        return [enqueue(conn, v, "morning", agent_request_id=request_id) for v in self.produce]


@pytest.fixture
def factory(tmp_path):
    path = tmp_path / "sched.db"
    conn = connect(path)
    migrate(conn)
    save_morning(conn, MorningSettings(True, "07:45", ALL_DAYS, ("technology",), 3))
    for name in ("m1", "m2", "o1"):
        upsert_video(conn, vid(name))
    conn.close()
    return lambda: connect(path)


def at(y, mo, d, h, mi, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=CT)


def build(sched, now):
    """Tick a build and wait for its background thread."""
    actions = sched.tick(now)
    if sched._build_thread is not None:
        sched._build_thread.join(timeout=5)
    return actions


def make(factory, runner, engine=None):
    engine = engine or FakeEngine()
    return MorningScheduler(factory, run_request_fn=runner, engine=engine), engine


def test_build_at_0740_central(factory):
    runner = Runner(["m1"])
    sched, _ = make(factory, runner)
    assert build(sched, at(2026, 5, 6, 7, 40)) == ["built"]
    conn = factory()
    req = list_requests(conn)[0]
    assert (req.kind, req.play) == ("morning", "end")
    assert "technology" in req.prompt


def test_no_build_outside_window(factory):
    runner = Runner(["m1"])
    sched, _ = make(factory, runner)
    assert sched.tick(at(2026, 5, 6, 7, 39, 59)) == []
    assert sched.tick(at(2026, 5, 6, 7, 43)) == []
    assert runner.calls == []


def test_no_build_on_disabled_day(factory):
    conn = factory()
    # 2026-05-06 is a Wednesday (2); allow every day except it.
    save_morning(conn, MorningSettings(True, "07:45", (0, 1, 3, 4, 5, 6), ("tech",), 3))
    runner = Runner(["m1"])
    sched, _ = make(factory, runner)
    assert build(sched, at(2026, 5, 6, 7, 40)) == []
    assert runner.calls == []


def test_no_build_when_disabled(factory):
    conn = factory()
    save_morning(conn, MorningSettings(False, "07:45", ALL_DAYS, ("tech",), 3))
    runner = Runner(["m1"])
    sched, _ = make(factory, runner)
    assert build(sched, at(2026, 5, 6, 7, 40)) == []


def test_build_once_per_day(factory):
    runner = Runner(["m1"])
    sched, _ = make(factory, runner)
    assert build(sched, at(2026, 5, 6, 7, 40, 10)) == ["built"]
    assert sched.tick(at(2026, 5, 6, 7, 40, 30)) == []
    assert len(runner.calls) == 1
    assert build(sched, at(2026, 5, 7, 7, 40, 10)) == ["built"]


def test_start_at_0745_when_idle(factory):
    runner = Runner(["m1", "m2"])
    sched, engine = make(factory, runner)
    conn = factory()
    enqueue(conn, "o1", "user")
    build(sched, at(2026, 5, 6, 7, 40))
    assert sched.tick(at(2026, 5, 6, 7, 45, 5)) == ["started"]
    assert engine.played == 1
    assert [i.video.video_id for i in upcoming(factory())] == ["m1", "m2", "o1"]
    assert sched.tick(at(2026, 5, 6, 7, 45, 25)) == []
    assert engine.played == 1


def test_no_start_when_already_playing(factory):
    sched, engine = make(factory, Runner(["m1"]), FakeEngine("playing"))
    build(sched, at(2026, 5, 6, 7, 40))
    assert sched.tick(at(2026, 5, 6, 7, 45)) == []
    assert engine.played == 0
    assert sched.tick(at(2026, 5, 6, 7, 46)) == []  # marked started, no retry


def test_missed_start_window_is_skipped(factory):
    sched, engine = make(factory, Runner(["m1"]))
    assert sched.tick(at(2026, 5, 6, 7, 56)) == []
    assert engine.played == 0


def test_stale_morning_items_cleared_before_build(factory):
    conn = factory()
    enqueue(conn, "o1", "morning")
    enqueue(conn, "m2", "fallback")
    enqueue(conn, "m1", "user")
    sched, _ = make(factory, Runner([]))
    build(sched, at(2026, 5, 6, 7, 40))
    assert [(i.video.video_id, i.source) for i in upcoming(factory())] == [("m1", "user")]


def test_fallback_used_when_agent_fails(factory):
    conn = factory()
    record_import(conn, "recommended", video_id="m1")
    sched, _ = make(factory, Runner([]))
    assert build(sched, at(2026, 5, 6, 7, 40)) == ["built"]
    queued = upcoming(factory())
    assert [(i.video.video_id, i.source) for i in queued] == [("m1", "fallback")]


def test_fallback_used_when_agent_raises(factory):
    conn = factory()
    record_import(conn, "recommended", video_id="m1")

    def boom(conn, request_id):
        raise RuntimeError("claude missing")

    sched, _ = make(factory, boom)
    assert build(sched, at(2026, 5, 6, 7, 40)) == ["built"]


@pytest.mark.parametrize("day", [(2026, 3, 8), (2026, 11, 1)])
def test_dst_safe(factory, day):
    runner = Runner(["m1"])
    sched, _ = make(factory, runner)
    assert build(sched, at(*day, 7, 40)) == ["built"]
    assert sched.tick(at(*day, 7, 45)) == ["started"]


def test_tick_converts_utc_to_local(factory):
    from datetime import timezone

    sched, _ = make(factory, Runner(["m1"]))
    # 12:40 UTC on 2026-05-06 is 07:40 CDT.
    assert sched.tick(datetime(2026, 5, 6, 12, 40, tzinfo=timezone.utc)) == ["built"]


def test_tick_converts_utc_to_local_cst(factory):
    from datetime import timezone

    sched, _ = make(factory, Runner(["m1"]))
    # 13:40 UTC on 2026-11-02 is 07:40 CST (after the fall-back).
    assert sched.tick(datetime(2026, 11, 2, 13, 40, tzinfo=timezone.utc)) == ["built"]
    sched._build_thread.join(timeout=5)


def test_run_now_builds_and_starts(factory):
    runner = Runner(["m1"])
    sched, engine = make(factory, runner)
    request_id = sched.run_now()
    sched._last_thread.join(timeout=5)
    assert runner.calls == [request_id]
    assert engine.played == 1
    assert list_requests(factory())[0].kind == "morning"


def test_run_now_falls_back(factory):
    conn = factory()
    record_import(conn, "recommended", video_id="m2")
    sched, engine = make(factory, Runner([]))
    sched.run_now()
    sched._last_thread.join(timeout=5)
    assert [i.source for i in upcoming(factory())] == ["fallback"]
    assert engine.played == 1


def test_start_recovers_when_build_left_queue_empty(factory):
    conn = factory()
    record_import(conn, "recommended", video_id="m1")
    log_event(conn, "scheduler", "info", "built:2026-05-06")  # build crashed afterwards
    sched, engine = make(factory, Runner([]))
    assert sched.tick(at(2026, 5, 6, 7, 45)) == ["started"]
    assert engine.played == 1
    assert [(i.video.video_id, i.source) for i in upcoming(factory())] == [("m1", "fallback")]


def test_fallback_error_does_not_escape_build(factory, monkeypatch):
    import src.scheduler as mod

    def boom(conn, count):
        raise RuntimeError("db locked")

    monkeypatch.setattr(mod, "fallback_morning", boom)
    sched, _ = make(factory, Runner([]))
    assert build(sched, at(2026, 5, 6, 7, 40)) == ["built"]


def test_slow_agent_does_not_block_start(factory):
    import threading

    release = threading.Event()
    conn = factory()
    record_import(conn, "recommended", video_id="m1")

    def slow(conn, request_id):
        release.wait(5)
        return [enqueue(conn, "m2", "morning", agent_request_id=request_id)]

    sched, engine = make(factory, slow)
    assert sched.tick(at(2026, 5, 6, 7, 40)) == ["built"]
    assert sched.tick(at(2026, 5, 6, 7, 40, 20)) == []  # build still running, no second one
    assert sched.tick(at(2026, 5, 6, 7, 45)) == ["started"]
    assert engine.played == 1
    release.set()
    sched._build_thread.join(timeout=5)
    ordered = [(i.video.video_id, i.source) for i in upcoming(factory())]
    assert ordered == [("m1", "fallback"), ("m2", "morning")]


class ImportFn:
    def __init__(self):
        self.calls = 0

    def __call__(self):
        self.calls += 1


def import_tick(sched, now):
    actions = sched.tick(now)
    if sched._import_thread is not None:
        sched._import_thread.join(timeout=5)
    return actions


def test_daily_import_runs_once_at_0400(factory):
    fn = ImportFn()
    sched = MorningScheduler(factory, run_request_fn=Runner(), engine=FakeEngine(), import_fn=fn)
    assert import_tick(sched, at(2026, 5, 6, 3, 59)) == []
    assert import_tick(sched, at(2026, 5, 6, 4, 0)) == ["imported"]
    assert fn.calls == 1
    assert import_tick(sched, at(2026, 5, 6, 4, 1)) == []
    assert fn.calls == 1
    conn = factory()
    assert event_logged(conn, "imported:2026-05-06")
    conn.close()
    assert import_tick(sched, at(2026, 5, 7, 4, 0)) == ["imported"]
    assert fn.calls == 2


def test_daily_import_runs_when_morning_disabled(factory):
    conn = factory()
    save_morning(conn, MorningSettings(False, "07:45", ALL_DAYS, ("technology",), 3))
    conn.close()
    fn = ImportFn()
    sched = MorningScheduler(factory, run_request_fn=Runner(), engine=FakeEngine(), import_fn=fn)
    assert import_tick(sched, at(2026, 5, 6, 4, 2)) == ["imported"]
    assert fn.calls == 1


def test_daily_import_disabled_without_import_fn(factory):
    sched, _ = make(factory, Runner())
    assert sched.tick(at(2026, 5, 6, 4, 0)) == []


def test_daily_import_failure_does_not_break_tick(factory):
    def boom():
        raise RuntimeError("feed down")

    sched = MorningScheduler(factory, run_request_fn=Runner(), engine=FakeEngine(), import_fn=boom)
    assert import_tick(sched, at(2026, 5, 6, 4, 0)) == ["imported"]


def test_paused_item_is_stopped_and_morning_plays_first(factory):
    from tests.player.fakes import FakeMpv
    from src.player.engine import PlaybackEngine

    mpv = FakeMpv()
    engine = PlaybackEngine(factory, mpv)
    conn = factory()
    enqueue(conn, "o1", "user")
    engine.play_next()
    mpv.is_paused = True
    assert engine.now().state == "paused"
    sched, _ = make(factory, Runner(["m1", "m2"]), engine)
    build(sched, at(2026, 5, 6, 7, 40))
    assert sched.tick(at(2026, 5, 6, 7, 45, 5)) == ["started"]
    now = engine.now()
    assert (now.state, now.item.video.video_id) == ("playing", "m1")
    assert [i.video.video_id for i in upcoming(factory())] == ["m2", "o1"]


def test_failed_start_is_retried_within_window(factory):
    engine = FakeEngine(fail_plays=1)
    sched, _ = make(factory, Runner(["m1"]), engine)
    build(sched, at(2026, 5, 6, 7, 40))
    assert sched.tick(at(2026, 5, 6, 7, 45)) == []
    assert not event_logged(factory(), "started:2026-05-06")
    assert sched.tick(at(2026, 5, 6, 7, 45, 20)) == ["started"]
    assert engine.played == 2
    assert event_logged(factory(), "started:2026-05-06")
    assert sched.tick(at(2026, 5, 6, 7, 45, 40)) == []
    assert engine.played == 2


def test_empty_morning_retries_without_repeating_fallback(factory, monkeypatch):
    import src.scheduler as mod

    calls = []
    monkeypatch.setattr(mod, "fallback_morning", lambda conn, count: calls.append(count) or [])
    log_event(factory(), "scheduler", "info", "built:2026-05-06")
    sched, engine = make(factory, Runner([]))
    assert sched.tick(at(2026, 5, 6, 7, 45)) == []
    assert sched.tick(at(2026, 5, 6, 7, 45, 20)) == []
    assert len(calls) == 1
    enqueue(factory(), "m1", "morning")  # a slow build finished
    assert sched.tick(at(2026, 5, 6, 7, 45, 40)) == ["started"]
    assert engine.played == 1
