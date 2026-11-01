"""Tests for the morning prompt and fallback set."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.agent.morning import PROMPT_TEMPLATE, fallback_morning, morning_prompt
from src.store.queue import enqueue, upcoming
from src.store.settings import MorningSettings, recent_events, record_import
from src.store.videos import Video, add_rating, upsert_video


def vid(video_id, duration_s=600):
    return Video(
        video_id=video_id, title=f"Title {video_id}", channel_name=None, channel_id=None,
        description=None, duration_s=duration_s, published_at=None, tags=(), category=None,
        chapters=(), thumbnail_url=None, view_count=None, like_count=None, transcript=None,
    )


def settings(topics=("technology",), count=3):
    return MorningSettings(True, "07:45", (0, 1, 2, 3, 4, 5, 6), topics, count)


def test_prompt_has_topics_and_no_names():
    s = settings(topics=("technology", "AI"), count=2)
    prompt = morning_prompt(s)
    assert "technology" in prompt and "AI" in prompt and "2" in prompt
    assert "podcast-style or long-form" in prompt
    assert prompt == PROMPT_TEMPLATE.format(count=2, topics="technology, AI")
    assert prompt == (
        "Morning set: pick 2 podcast-style or long-form videos for a wake-up listen, "
        "about: technology, AI. Prefer recent, substantive episodes; avoid shorts, clips "
        "and music."
    )


def test_fallback_prefers_unplayed_imports_longest_first(db):
    for v in (vid("short", 100), vid("long", 5000), vid("none", None), vid("bad", 9000)):
        upsert_video(db, v)
    record_import(db, "recommended", video_id="short")
    record_import(db, "subscription", video_id="long")
    record_import(db, "recommended", video_id="none")
    record_import(db, "recommended", video_id="bad")
    record_import(db, "recommended", video_id="missing-from-videos")
    add_rating(db, "bad", False, None, None)
    ids = fallback_morning(db, 3)
    queued = upcoming(db)
    assert [i.id for i in queued] == ids
    assert [i.video.video_id for i in queued] == ["long", "short", "none"]
    assert {i.source for i in queued} == {"fallback"}
    event = recent_events(db, 1)[0]
    assert (event["level"], event["message"]) == ("warn", "fallback: queued 3")


def test_fallback_uses_top_rated_not_played_recently(db):
    for name in ("a", "b", "c"):
        upsert_video(db, vid(name))
    add_rating(db, "a", True, 5, None)
    add_rating(db, "b", True, 9, None)
    add_rating(db, "c", True, 7, None)
    old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    now = datetime.now(timezone.utc).isoformat()
    db.execute("INSERT INTO plays (video_id, started_at) VALUES ('b', ?)", (now,))
    db.execute("INSERT INTO plays (video_id, started_at) VALUES ('c', ?)", (old,))
    db.commit()
    fallback_morning(db, 5)
    assert [i.video.video_id for i in upcoming(db)] == ["c", "a"]


def test_fallback_with_nothing_logs_zero(db):
    assert fallback_morning(db, 3) == []
    assert recent_events(db, 1)[0]["message"] == "fallback: queued 0"


def test_fallback_skips_already_queued(db):
    upsert_video(db, vid("x", 900))
    upsert_video(db, vid("y", 800))
    record_import(db, "recommended", video_id="x")
    record_import(db, "recommended", video_id="y")
    enqueue(db, "x", "user")
    fallback_morning(db, 2)
    assert [i.video.video_id for i in upcoming(db) if i.source == "fallback"] == ["y"]
