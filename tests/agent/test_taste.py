"""Tests for the taste profile."""

from __future__ import annotations

import json

import pytest

from src.agent.claude_cli import ClaudeBadOutput
from src.agent.taste import (
    refresh_taste,
    refresh_taste_from_imports,
    remove_trait,
    seed_taste_from_imports,
    should_refresh,
)
from src.store.agent import (
    add_correction,
    latest_taste,
    pending_corrections,
    save_taste,
)
from src.store.settings import record_import
from src.store.videos import Video, add_rating, upsert_video
from tests.agent.fakes import FakeClaude


def _video(video_id):
    return Video(
        video_id=video_id, title=f"Title {video_id}", channel_name="Chan", channel_id=None,
        description=None, duration_s=600, published_at="2026-01-02T00:00:00",
        tags=(), category=None, chapters=(), thumbnail_url=None, view_count=None,
        like_count=None, transcript=None,
    )


def _rate(db, n):
    for i in range(n):
        vid = f"v{i}"
        upsert_video(db, _video(vid))
        add_rating(db, vid, True, 8, None)


def _reply(summary="Likes robotics.", likes=("robotics",), dislikes=("clickbait",)):
    return json.dumps({"summary": summary, "likes": list(likes), "dislikes": list(dislikes)})


def test_refresh_saves_new_version(db):
    save_taste(db, "old", ["a"], [], "user")
    claude = FakeClaude([_reply(summary="x" * 500, likes=[" robotics ", "", "ros"] + ["l"] * 20)])

    taste = refresh_taste(db, claude=claude)

    assert taste.source == "agent"
    assert len(taste.summary) == 400
    assert taste.likes[:2] == ("robotics", "ros")
    assert len(taste.likes) == 10
    assert taste.dislikes == ("clickbait",)
    assert latest_taste(db) == taste
    assert "old" in claude.prompts[0]


def test_corrections_in_prompt_and_marked_applied(db):
    add_correction(db, "I actually hate podcasts")
    claude = FakeClaude([_reply()])

    refresh_taste(db, claude=claude)

    assert "I actually hate podcasts" in claude.prompts[0]
    assert pending_corrections(db) == []


def test_bad_output_saves_nothing_and_keeps_corrections(db):
    add_correction(db, "fix me")
    claude = FakeClaude([json.dumps({"summary": "s", "likes": "nope", "dislikes": []})])

    with pytest.raises(ClaudeBadOutput):
        refresh_taste(db, claude=claude)

    assert latest_taste(db) is None
    assert len(pending_corrections(db)) == 1


def test_should_refresh_after_5_ratings(db):
    assert should_refresh(db) is False
    _rate(db, 1)
    assert should_refresh(db) is True  # no taste yet, one rating exists
    save_taste(db, "s", [], [], "agent")
    assert should_refresh(db) is False
    _rate_more = [f"w{i}" for i in range(5)]
    for vid in _rate_more:
        upsert_video(db, _video(vid))
        add_rating(db, vid, True, None, None)
    assert should_refresh(db) is True


def test_should_refresh_with_pending_correction(db):
    save_taste(db, "s", [], [], "agent")
    assert should_refresh(db) is False
    add_correction(db, "less news")
    assert should_refresh(db) is True


def test_remove_trait_creates_user_version(db):
    save_taste(db, "s", ["a", "b"], ["c"], "agent")

    taste = remove_trait(db, "likes", " a ")

    assert taste.source == "user"
    assert taste.likes == ("b",)
    assert taste.dislikes == ("c",)
    assert taste.summary == "s"
    assert latest_taste(db) == taste


def test_remove_missing_trait_raises(db):
    save_taste(db, "s", ["a"], [], "agent")
    with pytest.raises(ValueError):
        remove_trait(db, "likes", "A")  # case-sensitive
    with pytest.raises(ValueError):
        remove_trait(db, "dislikes", "a")


def test_remove_trait_without_profile_raises(db):
    with pytest.raises(ValueError):
        remove_trait(db, "likes", "a")


def test_seed_from_imports(db):
    record_import(db, "subscription", channel_id="UC1", title="Chan One")
    record_import(db, "liked", video_id="v1", title="A video")
    claude = FakeClaude([_reply()])

    taste = seed_taste_from_imports(db, claude=claude)

    assert taste is not None and taste.source == "agent"
    assert "subscription | Chan One | UC1" in claude.prompts[0]
    assert "liked | A video | " in claude.prompts[0]


def test_seed_skips_when_taste_exists_or_no_imports(db):
    claude = FakeClaude([])
    assert seed_taste_from_imports(db, claude=claude) is None
    record_import(db, "liked", video_id="v1", title="t")
    save_taste(db, "s", [], [], "user")
    assert seed_taste_from_imports(db, claude=claude) is None
    assert claude.prompts == []


def test_seed_caps_at_200_lines(db):
    for i in range(250):
        record_import(db, "history", video_id=f"v{i}", title=f"T{i}")
    claude = FakeClaude([_reply()])

    seed_taste_from_imports(db, claude=claude)

    assert claude.prompts[0].count("history | T") == 200


def test_seed_runs_over_a_legacy_only_profile_and_uses_it(db):
    save_taste(db, "Old summary from the legacy app", ["robotics"], ["drama"], "legacy")
    record_import(db, "liked", video_id="v1", title="A video")
    claude = FakeClaude([_reply()])
    taste = seed_taste_from_imports(db, claude=claude)
    assert taste is not None and taste.source == "agent"
    prompt = claude.prompts[0]
    assert "Old summary from the legacy app" in prompt
    assert "robotics" in prompt and "drama" in prompt


def test_seed_skipped_once_a_non_legacy_profile_exists(db):
    save_taste(db, "legacy", [], [], "legacy")
    save_taste(db, "agent made", [], [], "agent")
    record_import(db, "liked", video_id="v1", title="t")
    claude = FakeClaude([])
    assert seed_taste_from_imports(db, claude=claude) is None


def test_seed_lines_balanced_across_kinds(db):
    for i in range(250):
        record_import(db, "subscription", channel_id=f"UC{i}", title=f"S{i}")
    for i in range(30):
        record_import(db, "liked", video_id=f"l{i}", title=f"L{i}")
    for i in range(100):
        record_import(db, "history", video_id=f"h{i}", title=f"H{i}")
    claude = FakeClaude([_reply()])
    seed_taste_from_imports(db, claude=claude)
    prompt = claude.prompts[0]
    assert prompt.count("liked | L") == 30  # a small kind is kept whole
    assert prompt.count("subscription | S") == 85  # the big ones share the rest
    assert prompt.count("history | H") == 85


def test_refresh_from_imports_updates_an_existing_profile(db):
    save_taste(db, "Current agent summary", ["chips"], ["drama"], "agent")
    record_import(db, "liked", video_id="v1", title="A liked video")
    claude = FakeClaude([_reply(summary="Now with account data")])
    taste = refresh_taste_from_imports(db, claude=claude)
    assert taste is not None and taste.summary == "Now with account data"
    assert taste.source == "agent"
    prompt = claude.prompts[0]
    assert "liked | A liked video | " in prompt
    assert "Current agent summary" in prompt and "chips" in prompt


def test_refresh_from_imports_seeds_without_a_profile(db):
    record_import(db, "liked", video_id="v1", title="t")
    claude = FakeClaude([_reply()])
    assert refresh_taste_from_imports(db, claude=claude) is not None
    assert "current profile" not in claude.prompts[0].lower()


def test_refresh_from_imports_needs_imports(db):
    save_taste(db, "s", [], [], "agent")
    assert refresh_taste_from_imports(db, claude=FakeClaude([])) is None
