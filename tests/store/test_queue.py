"""Tests for the queue store."""

from __future__ import annotations

import pytest

from src.store import queue
from src.store.videos import Video, upsert_video


def add_video(conn, video_id: str, duration_s: int | None = 100) -> str:
    upsert_video(
        conn,
        Video(
            video_id=video_id, title=f"T {video_id}", channel_name=None, channel_id=None,
            description=None, duration_s=duration_s, published_at=None, tags=(), category=None,
            chapters=(), thumbnail_url=None, view_count=None, like_count=None, transcript=None,
        ),
    )
    return video_id


def ids(items) -> list[str]:
    return [i.video.video_id for i in items]


def test_enqueue_end_orders_by_position(db):
    for v in ("a", "b", "c"):
        queue.enqueue(db, add_video(db, v), "user")
    assert ids(queue.upcoming(db)) == ["a", "b", "c"]


def test_enqueue_next_goes_after_playing(db):
    for v in ("a", "b", "c"):
        queue.enqueue(db, add_video(db, v), "user")
    assert queue.pop_next(db).video.video_id == "a"  # a playing; b, c queued
    queue.enqueue(db, add_video(db, "n"), "agent", where="next")
    assert ids(queue.upcoming(db)) == ["n", "b", "c"]
    assert queue.current(db).video.video_id == "a"
    queue.enqueue(db, add_video(db, "m"), "agent", where="next")
    assert ids(queue.upcoming(db)) == ["m", "n", "b", "c"]


def test_enqueue_next_with_nothing_playing_goes_first(db):
    queue.enqueue(db, add_video(db, "a"), "user")
    queue.enqueue(db, add_video(db, "n"), "user", where="next")
    assert ids(queue.upcoming(db)) == ["n", "a"]


def test_enqueue_next_with_only_playing_item(db):
    queue.enqueue(db, add_video(db, "a"), "user")
    queue.pop_next(db)
    queue.enqueue(db, add_video(db, "n"), "user", where="next")
    assert ids(queue.upcoming(db)) == ["n"]


def test_pop_next_advances_and_marks_played(db):
    first = queue.enqueue(db, add_video(db, "a"), "user")
    second = queue.enqueue(db, add_video(db, "b"), "user")
    assert queue.pop_next(db).id == first
    assert queue.current(db).id == first
    assert queue.pop_next(db).id == second
    statuses = {r["id"]: r["status"] for r in db.execute("SELECT id, status FROM queue_items")}
    assert statuses == {first: "played", second: "playing"}


def test_pop_next_empty_returns_none(db):
    assert queue.pop_next(db) is None
    queue.enqueue(db, add_video(db, "a"), "user")
    queue.pop_next(db)
    assert queue.pop_next(db) is None
    assert queue.current(db) is None  # playback stops, no autoplay


def test_previous_restores_last_played(db):
    for v in ("a", "b", "c"):
        queue.enqueue(db, add_video(db, v), "user")
    queue.pop_next(db)
    queue.pop_next(db)  # b playing, a played, c queued
    restored = queue.previous(db)
    assert restored.video.video_id == "a"
    assert queue.current(db).video.video_id == "a"
    assert ids(queue.upcoming(db)) == ["b", "c"]


def test_previous_with_nothing_played_is_noop(db):
    queue.enqueue(db, add_video(db, "a"), "user")
    queue.pop_next(db)
    assert queue.previous(db) is None
    assert queue.current(db).video.video_id == "a"


def test_previous_after_queue_ran_dry(db):
    queue.enqueue(db, add_video(db, "a"), "user")
    queue.enqueue(db, add_video(db, "b"), "user")
    queue.pop_next(db)
    queue.pop_next(db)
    queue.pop_next(db)  # nothing playing, both played
    assert queue.previous(db).video.video_id == "b"
    assert queue.current(db).video.video_id == "b"


def test_reorder_sets_new_order(db):
    a = queue.enqueue(db, add_video(db, "a"), "user")
    b = queue.enqueue(db, add_video(db, "b"), "user")
    c = queue.enqueue(db, add_video(db, "c"), "user")
    queue.reorder(db, [c, a, b])
    assert ids(queue.upcoming(db)) == ["c", "a", "b"]


def test_reorder_rejects_mismatched_ids(db):
    a = queue.enqueue(db, add_video(db, "a"), "user")
    b = queue.enqueue(db, add_video(db, "b"), "user")
    for bad in ([a], [a, b, 999], [a, a], [a, 999]):
        with pytest.raises(ValueError):
            queue.reorder(db, bad)
    assert ids(queue.upcoming(db)) == ["a", "b"]


def test_remove_and_clear_mark_removed(db):
    a = queue.enqueue(db, add_video(db, "a"), "user")
    queue.enqueue(db, add_video(db, "b"), "user")
    queue.remove(db, a)
    assert ids(queue.upcoming(db)) == ["b"]
    assert db.execute("SELECT status FROM queue_items WHERE id = ?", (a,)).fetchone()[0] == "removed"
    queue.clear(db)
    assert queue.upcoming(db) == []
    assert db.execute("SELECT COUNT(*) FROM queue_items").fetchone()[0] == 2


def test_remaining_seconds_sums_queued(db):
    queue.enqueue(db, add_video(db, "a", 100), "user")
    queue.enqueue(db, add_video(db, "b", None), "user")
    queue.enqueue(db, add_video(db, "c", 50), "user")
    queue.pop_next(db)  # a playing: not counted
    assert queue.remaining_seconds(db) == 50


def test_item_carries_metadata(db):
    item_id = queue.enqueue(db, add_video(db, "a"), "agent", reason="because")
    item = queue.upcoming(db)[0]
    assert (item.id, item.source, item.reason, item.status) == (item_id, "agent", "because", "queued")
    assert item.agent_request_id is None and item.added_at
    assert item.video.title == "T a"


def test_previous_uses_latest_play_not_highest_id(db):
    from src.store.videos import start_play

    ids_ = {v: queue.enqueue(db, add_video(db, v), "user") for v in ("a", "b", "c")}
    queue.reorder(db, [ids_["c"], ids_["b"], ids_["a"]])
    for n, expected in enumerate(("c", "b", "a")):
        item = queue.pop_next(db)
        assert item.video.video_id == expected
        play = start_play(db, expected, item.id)
        db.execute("UPDATE plays SET started_at = ? WHERE id = ?", (f"2026-11-01T10:0{n}:00", play))
        db.commit()
    assert queue.previous(db).video.video_id == "b"  # c has the higher id but played first
    assert ids(queue.upcoming(db)) == ["a"]
