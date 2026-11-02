"""Tests for the video / play / rating store."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from src.store.videos import (
    Chapter,
    Video,
    add_rating,
    disliked_video_ids,
    end_play,
    get_video,
    history,
    played_video_ids,
    recent_feedback,
    set_transcript,
    start_play,
    upsert_video,
)


def make_video(video_id: str = "vid1", **overrides) -> Video:
    base = Video(
        video_id=video_id,
        title=f"Title {video_id}",
        channel_name="Chan",
        channel_id="UC1",
        description="desc",
        duration_s=600,
        published_at="2026-01-01T00:00:00+00:00",
        tags=("a", "b"),
        category="Tech",
        chapters=(Chapter(title="Intro", start_s=0.0), Chapter(title="Main", start_s=60.5)),
        thumbnail_url="https://example.com/t.jpg",
        view_count=10,
        like_count=2,
        transcript=None,
    )
    return replace(base, **overrides)


def test_upsert_roundtrip(db):
    v = make_video()
    upsert_video(db, v)
    assert get_video(db, "vid1") == v
    assert get_video(db, "missing") is None


def test_upsert_preserves_transcript(db):
    upsert_video(db, make_video(transcript="hello"))
    first_seen = db.execute("SELECT first_seen FROM videos").fetchone()[0]
    upsert_video(db, make_video(title="New title", transcript=None))
    got = get_video(db, "vid1")
    assert got.title == "New title"
    assert got.transcript == "hello"
    assert db.execute("SELECT first_seen FROM videos").fetchone()[0] == first_seen


def test_upsert_overwrites_transcript_when_given(db):
    upsert_video(db, make_video(transcript="old"))
    upsert_video(db, make_video(transcript="new"))
    assert get_video(db, "vid1").transcript == "new"


def test_set_transcript(db):
    upsert_video(db, make_video())
    set_transcript(db, "vid1", "words")
    assert get_video(db, "vid1").transcript == "words"


def test_play_lifecycle(db):
    upsert_video(db, make_video())
    play_id = start_play(db, "vid1", None)
    end_play(db, play_id, 192.0, "skipped")
    entries = history(db)
    assert len(entries) == 1
    assert entries[0].play_id == play_id
    assert entries[0].end_reason == "skipped"
    assert entries[0].seconds_listened == 192.0
    assert entries[0].video.video_id == "vid1"
    assert entries[0].source is None
    assert entries[0].latest_rating is None


def test_history_source_from_queue_item(db):
    upsert_video(db, make_video())
    db.execute(
        "INSERT INTO queue_items (id, video_id, position, source, status, added_at)"
        " VALUES (7, 'vid1', 1.0, 'morning', 'playing', '2026-01-01T00:00:00+00:00')"
    )
    db.commit()
    start_play(db, "vid1", 7)
    assert history(db)[0].source == "morning"


def test_history_newest_first_with_paging(db):
    for vid in ("a", "b", "c"):
        upsert_video(db, make_video(vid))
        end_play(db, start_play(db, vid, None), 1.0, "finished")
    assert [e.video.video_id for e in history(db)] == ["c", "b", "a"]
    assert [e.video.video_id for e in history(db, limit=1, offset=1)] == ["b"]


def test_multiple_ratings_kept_latest_shown(db):
    upsert_video(db, make_video())
    end_play(db, start_play(db, "vid1", None), 10.0, "finished")
    add_rating(db, "vid1", True, 4, None)
    add_rating(db, "vid1", True, 9, "better on reflection")
    assert db.execute("SELECT COUNT(*) FROM ratings").fetchone()[0] == 2
    latest = history(db)[0].latest_rating
    assert latest.satisfaction == 9
    assert latest.notes == "better on reflection"
    assert latest.liked is True


def test_add_rating_notes_only_ok(db):
    upsert_video(db, make_video())
    assert isinstance(add_rating(db, "vid1", None, None, "just a note"), int)


@pytest.mark.parametrize("sat", [0, 11, -1])
def test_add_rating_bad_satisfaction_raises(db, sat):
    upsert_video(db, make_video())
    with pytest.raises(ValueError):
        add_rating(db, "vid1", None, sat, None)


def test_add_rating_all_none_raises(db):
    upsert_video(db, make_video())
    with pytest.raises(ValueError):
        add_rating(db, "vid1", None, None, None)


def test_unrated_filter(db):
    for vid in ("a", "b"):
        upsert_video(db, make_video(vid))
        end_play(db, start_play(db, vid, None), 1.0, "finished")
    add_rating(db, "a", True, None, None)
    assert [e.video.video_id for e in history(db, unrated_only=True)] == ["b"]
    assert len(history(db)) == 2


def test_recent_feedback_rated_or_skipped(db):
    for vid, reason in (("a", "finished"), ("b", "skipped"), ("c", "finished"), ("d", "stopped")):
        upsert_video(db, make_video(vid))
        end_play(db, start_play(db, vid, None), 1.0, reason)
    add_rating(db, "c", None, 7, None)
    assert [e.video.video_id for e in recent_feedback(db)] == ["c", "b"]


def test_played_video_ids_since_days(db):
    for vid in ("new", "old"):
        upsert_video(db, make_video(vid))
        start_play(db, vid, None)
    old = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
    db.execute("UPDATE plays SET started_at=? WHERE video_id='old'", (old,))
    db.commit()
    assert played_video_ids(db, 7) == {"new"}
    assert played_video_ids(db, 60) == {"new", "old"}


def test_disliked_ids(db):
    for vid in ("a", "b", "c"):
        upsert_video(db, make_video(vid))
    add_rating(db, "a", False, None, None)
    add_rating(db, "b", True, None, None)
    add_rating(db, "c", None, 3, None)
    assert disliked_video_ids(db) == {"a"}


def test_writes_visible_to_other_connection(db, tmp_path):
    from src.db import connect

    upsert_video(db, make_video())
    other = connect(tmp_path / "test.db")
    try:
        assert get_video(other, "vid1") is not None
    finally:
        other.close()
