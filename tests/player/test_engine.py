"""Tests for the playback engine, driven by FakeMpv."""

from __future__ import annotations

import threading

import pytest

from src.db import connect, migrate
from src.player.engine import PlaybackEngine
from src.player.mpv import MpvDied
from src.store import queue
from src.store.settings import recent_events
from src.store.videos import Video, upsert_video
from tests.player.fakes import FakeMpv


@pytest.fixture
def factory(tmp_path):
    path = tmp_path / "t.db"
    conn = connect(path)
    migrate(conn)
    conn.close()
    return lambda: connect(path)


@pytest.fixture
def mpv():
    return FakeMpv()


@pytest.fixture
def engine(factory, mpv):
    return PlaybackEngine(factory, mpv)


def add(factory, video_id: str, duration_s: int | None = 600) -> int:
    conn = factory()
    try:
        upsert_video(
            conn,
            Video(
                video_id=video_id, title=f"T {video_id}", channel_name=None, channel_id=None,
                description=None, duration_s=duration_s, published_at=None, tags=(),
                category=None, chapters=(), thumbnail_url=None, view_count=None,
                like_count=None, transcript=None,
            ),
        )
        return queue.enqueue(conn, video_id, "user")
    finally:
        conn.close()


def plays(factory):
    conn = factory()
    try:
        return conn.execute("SELECT * FROM plays ORDER BY id").fetchall()
    finally:
        conn.close()


def url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def test_play_next_loads_url_and_logs_play(engine, mpv, factory):
    item_id = add(factory, "a")
    item = engine.play_next()
    assert item.id == item_id
    assert mpv.calls_named("load") == [("load", url("a"))]
    rows = plays(factory)
    assert len(rows) == 1 and rows[0]["video_id"] == "a"
    assert rows[0]["queue_item_id"] == item_id and rows[0]["ended_at"] is None
    now = engine.now()
    assert now.state == "playing" and now.item.id == item_id


def test_eof_advances_and_marks_finished(engine, mpv, factory):
    add(factory, "a")
    add(factory, "b")
    engine.play_next()
    mpv.pos, mpv.dur = 599.0, 600.0
    engine.handle_event({"event": "end-file", "reason": "eof"})
    rows = plays(factory)
    assert rows[0]["end_reason"] == "finished"
    assert rows[0]["seconds_listened"] == 600.0  # full duration on eof
    assert rows[1]["video_id"] == "b" and rows[1]["ended_at"] is None
    assert mpv.calls_named("load")[-1] == ("load", url("b"))
    assert engine.now().item.video.video_id == "b"


def test_next_marks_skip_with_position(engine, mpv, factory):
    add(factory, "a")
    add(factory, "b")
    engine.play_next()
    mpv.pos = 192.0
    nxt = engine.next()
    assert nxt.video.video_id == "b"
    first = plays(factory)[0]
    assert first["end_reason"] == "skipped" and first["seconds_listened"] == 192.0


def test_next_with_unknown_position_logs_zero(engine, mpv, factory):
    add(factory, "a")
    engine.play_next()
    mpv.pos = None
    engine.next()
    assert plays(factory)[0]["seconds_listened"] == 0


def test_queue_empty_stops_and_idle(engine, mpv, factory):
    add(factory, "a")
    engine.play_next()
    mpv.pos, mpv.dur = 600.0, 600.0
    engine.handle_event({"event": "end-file", "reason": "eof"})
    assert mpv.calls_named("stop")
    assert engine.now().state == "idle" and engine.now().item is None
    assert plays(factory)[0]["end_reason"] == "finished"


def test_play_next_on_empty_queue_is_idle(engine, mpv):
    assert engine.play_next() is None
    assert mpv.calls_named("stop")
    assert engine.now().state == "idle"


def test_seek_is_clamped(engine, mpv, factory):
    add(factory, "a")
    engine.play_next()
    mpv.dur = 600.0
    engine.seek(-10)
    assert mpv.calls_named("seek")[-1] == ("seek", 0)
    engine.seek(9999)
    assert mpv.calls_named("seek")[-1] == ("seek", 599.0)


def test_skip_is_clamped(engine, mpv, factory):
    add(factory, "a")
    engine.play_next()
    mpv.dur, mpv.pos = 600.0, 100.0
    engine.skip(-500)
    assert mpv.calls_named("seek")[-1] == ("seek", 0)
    mpv.pos = 590.0
    engine.skip(30)
    assert mpv.calls_named("seek")[-1] == ("seek", 599.0)
    mpv.pos = 100.0
    engine.skip(15)
    assert mpv.calls_named("seek")[-1] == ("seek", 115.0)


def test_previous_restarts_when_past_5s(engine, mpv, factory):
    add(factory, "a")
    add(factory, "b")
    engine.play_next()
    engine.next()
    mpv.pos = 42.0
    item = engine.previous()
    assert item.video.video_id == "b"
    assert mpv.calls_named("seek")[-1] == ("seek", 0)
    assert len(mpv.calls_named("load")) == 2
    assert len(plays(factory)) == 2


def test_previous_goes_back_when_under_5s(engine, mpv, factory):
    add(factory, "a")
    add(factory, "b")
    engine.play_next()
    engine.next()
    mpv.pos = 2.0
    item = engine.previous()
    assert item.video.video_id == "a"
    assert mpv.calls_named("load")[-1] == ("load", url("a"))
    rows = plays(factory)
    assert rows[1]["end_reason"] == "skipped" and rows[1]["seconds_listened"] == 2.0
    assert rows[2]["video_id"] == "a"


def test_previous_with_no_history_keeps_current(engine, mpv, factory):
    add(factory, "a")
    engine.play_next()
    mpv.pos = 1.0
    item = engine.previous()
    assert item.video.video_id == "a"
    assert len(mpv.calls_named("load")) == 1


def test_error_event_logs_and_advances(engine, mpv, factory):
    add(factory, "a")
    add(factory, "b")
    engine.play_next()
    mpv.pos = 3.0
    engine.handle_event({"event": "end-file", "reason": "error"})
    rows = plays(factory)
    assert rows[0]["end_reason"] == "error"
    assert rows[1]["video_id"] == "b"
    conn = factory()
    try:
        events = recent_events(conn)
    finally:
        conn.close()
    assert any(e["component"] == "player" and e["level"] == "error" for e in events)


def test_replaced_load_end_file_is_ignored(engine, mpv, factory):
    add(factory, "a")
    add(factory, "b")
    engine.play_next()
    for reason in ("stop", "redirect", "quit"):
        engine.handle_event({"event": "end-file", "reason": reason})
    assert len(plays(factory)) == 1
    assert plays(factory)[0]["ended_at"] is None
    assert engine.now().item.video.video_id == "a"


def test_stop_ends_play_and_requeues_item(engine, mpv, factory):
    add(factory, "a")
    engine.play_next()
    mpv.pos = 50.0
    engine.stop()
    assert plays(factory)[0]["end_reason"] == "stopped"
    assert plays(factory)[0]["seconds_listened"] == 50.0
    assert mpv.calls_named("stop")
    assert engine.now().state == "idle"
    conn = factory()
    try:
        assert [i.video.video_id for i in queue.upcoming(conn)] == ["a"]
    finally:
        conn.close()


def test_toggle_pause_and_now_state(engine, mpv, factory):
    add(factory, "a", duration_s=300)
    engine.toggle_pause()  # idle: no-op
    assert not mpv.calls_named("set_pause")
    engine.play_next()
    mpv.pos = 12.5
    engine.toggle_pause()
    now = engine.now()
    assert mpv.calls_named("set_pause")[-1] == ("set_pause", True)
    assert now.state == "paused" and now.position_s == 12.5
    assert now.duration_s == 300
    engine.toggle_pause()
    assert engine.now().state == "playing"


def test_run_forever_dispatches_events_until_stopped(engine, mpv, factory):
    add(factory, "a")
    add(factory, "b")
    engine.play_next()
    stop = threading.Event()
    original = mpv.wait_event

    def wait_event(timeout):
        evt = original(timeout)
        if evt is None:
            stop.set()
        return evt

    mpv.wait_event = wait_event
    mpv.push({"event": "end-file", "reason": "eof"})
    engine.run_forever(stop)
    assert engine.now().item.video.video_id == "b"


def queue_statuses(factory):
    conn = factory()
    try:
        return [r[0] for r in conn.execute("SELECT status FROM queue_items ORDER BY id")]
    finally:
        conn.close()


def error_events(factory):
    conn = factory()
    try:
        return [e for e in recent_events(conn) if e["component"] == "player"
                and e["level"] == "error"]
    finally:
        conn.close()


def test_load_failure_logs_error_and_plays_next(engine, mpv, factory):
    add(factory, "a")
    add(factory, "b")
    mpv.load_errors[url("a")] = TimeoutError("mpv did not answer loadfile")
    item = engine.play_next()
    assert item.video.video_id == "b"
    rows = plays(factory)
    assert rows[0]["video_id"] == "a" and rows[0]["end_reason"] == "error"
    assert rows[1]["video_id"] == "b" and rows[1]["ended_at"] is None
    assert queue_statuses(factory) == ["played", "playing"]
    assert engine.now().item.video.video_id == "b"
    assert error_events(factory)


def test_load_failure_on_last_item_goes_idle(engine, mpv, factory):
    add(factory, "a")
    mpv.load_errors[url("a")] = BrokenPipeError("gone")
    assert engine.play_next() is None
    assert engine.now().state == "idle"
    assert "playing" not in queue_statuses(factory)
    assert plays(factory)[0]["end_reason"] == "error"


def test_previous_load_failure_is_not_half_applied(engine, mpv, factory):
    add(factory, "a")
    add(factory, "b")
    engine.play_next()
    engine.next()
    mpv.pos = 1.0
    mpv.load_errors[url("a")] = TimeoutError("slow")
    engine.previous()
    rows = plays(factory)
    assert [r["video_id"] for r in rows] == ["a", "b", "a", "b"]
    assert rows[2]["end_reason"] == "error"
    assert engine.now().item.video.video_id == "b"
    assert queue_statuses(factory).count("playing") == 1


def test_mpv_died_on_load_requeues_and_stops_advancing(engine, mpv, factory):
    add(factory, "a")
    add(factory, "b")
    mpv.load_errors[url("a")] = MpvDied("mpv exited")
    assert engine.play_next() is None
    assert engine.now().state == "idle"
    assert len(plays(factory)) == 1  # b was not tried against a dead mpv
    assert queue_statuses(factory) == ["queued", "queued"]


def test_mpv_died_event_errors_play_and_requeues(engine, mpv, factory):
    add(factory, "a")
    add(factory, "b")
    engine.play_next()
    engine.handle_event({"event": "mpv-died"})
    assert plays(factory)[0]["end_reason"] == "error"
    assert engine.now().state == "idle"
    assert queue_statuses(factory) == ["queued", "queued"]
    assert error_events(factory)


def test_mpv_died_when_idle_is_harmless(engine):
    engine.handle_event({"event": "mpv-died"})
    assert engine.now().state == "idle"


def test_pause_does_not_carry_over_to_next_track(engine, mpv, factory):
    add(factory, "a")
    add(factory, "b")
    engine.play_next()
    engine.toggle_pause()
    assert engine.now().state == "paused"
    engine.next()
    assert engine.now().state == "playing"


def test_on_before_play_called(factory, mpv):
    seen = []
    eng = PlaybackEngine(factory, mpv, on_before_play=seen.append)
    add(factory, "hook1")
    eng.play_next()
    assert seen == ["hook1"]
    assert mpv.calls_named("load")


def test_on_before_play_exception_ignored(factory, mpv):
    def boom(_vid):
        raise RuntimeError("nope")

    eng = PlaybackEngine(factory, mpv, on_before_play=boom)
    add(factory, "hook2")
    assert eng.play_next() is not None
    assert mpv.calls_named("load")
