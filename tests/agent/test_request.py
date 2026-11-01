"""Tests for the agent request pipeline."""

from __future__ import annotations

import json

import pytest

from src.agent.claude_cli import ClaudeRateLimited
from src.agent.context import build_taste_context
from src.agent.request import run_request
from src.sources.data_api import QuotaExceeded
from src.sources.ytdlp import SourceAuthError, SourceError
from src.store.agent import create_request, get_request, save_taste
from src.store.queue import enqueue, mark, upcoming
from src.store.settings import recent_events
from src.store.videos import (
    Video,
    add_rating,
    end_play,
    start_play,
    upsert_video,
)
from tests.agent.fakes import FakeClaude


def vid(video_id, duration_s=600):
    return Video(
        video_id=video_id, title=f"Title {video_id}", channel_name="Chan", channel_id=None,
        description=None, duration_s=duration_s, published_at="2026-01-02T00:00:00",
        tags=(), category=None, chapters=(), thumbnail_url=None, view_count=None,
        like_count=None, transcript=None,
    )


class FakeApi:
    def __init__(self, ids=(), quota=False):
        self.ids, self.quota, self.queries = list(ids), quota, []

    def search(self, query, max_results=25):
        self.queries.append(query)
        if self.quota:
            raise QuotaExceeded("quota")
        return list(self.ids)

    def video_details(self, ids):
        if self.quota:
            raise QuotaExceeded("quota")
        return [vid(i) for i in ids]


class FakeYt:
    def __init__(self, ids=(), tx_error=None):
        self.ids, self.tx_error, self.searched = list(ids), tx_error, []

    def search(self, query, n=20):
        self.searched.append(query)
        return list(self.ids)

    def metadata(self, video_id):
        return vid(video_id)

    def transcript(self, video_id, lang="en"):
        if self.tx_error and video_id == "a":
            raise self.tx_error
        return f"transcript of {video_id}"


def plan(count=3):
    return json.dumps({"queries": ["ros 2"], "count": count, "target_minutes": None})


def shortlist(*ids):
    return json.dumps({"shortlist": list(ids)})


def picks(*pairs):
    return json.dumps({"picks": [{"video_id": v, "reason": r} for v, r in pairs]})


def run(db, claude, api=None, yt=None, play="end", prompt="learn ROS 2"):
    rid = create_request(db, prompt, "user", play)
    ids = run_request(
        db, rid, claude=claude, data_api=api or FakeApi(["a", "b", "c"]), ytdlp=yt or FakeYt()
    )
    return rid, ids


def test_happy_path_enqueues_picks_with_reasons(db):
    claude = FakeClaude([plan(), shortlist("a", "b"), picks(("a", "because A"), ("b", "x" * 300))])
    rid, ids = run(db, claude)
    items = upcoming(db)
    assert [i.id for i in items] == ids
    assert [i.video.video_id for i in items] == ["a", "b"]
    assert items[0].reason == "because A"
    assert len(items[1].reason) == 140
    assert all(i.agent_request_id == rid and i.source == "agent" for i in items)
    req = get_request(db, rid)
    assert req.status == "done"
    assert req.summary == "Queued 2 videos (20 min)"
    assert recent_events(db)[0]["message"] == "ok: queued 2"
    assert recent_events(db)[0]["component"] == "agent"


def test_play_now_inserts_next_in_play_order(db):
    for v in ("p", "q"):
        upsert_video(db, vid(v))
    playing = enqueue(db, "p", "user")
    mark(db, playing, "playing")
    enqueue(db, "q", "user")
    claude = FakeClaude([plan(), shortlist("a", "b", "c"), picks(("a", "r"), ("b", "r"), ("c", "r"))])
    _, ids = run(db, claude, play="now")
    assert [i.video.video_id for i in upcoming(db)] == ["a", "b", "c", "q"]
    assert [i.id for i in upcoming(db)][:3] == ids


def test_falls_back_to_ytdlp_search_on_quota(db):
    yt = FakeYt(["a", "b"])
    claude = FakeClaude([plan(), shortlist("a"), picks(("a", "r"))])
    _, ids = run(db, claude, api=FakeApi(quota=True), yt=yt)
    assert yt.searched == ["ros 2"]
    assert len(ids) == 1


def test_without_data_api_searches_ytdlp(db):
    yt = FakeYt(["a", "b"])
    claude = FakeClaude([plan(), shortlist("a"), picks(("a", "r"))])
    rid = create_request(db, "learn ROS 2", "user", "end")
    ids = run_request(db, rid, claude=claude, data_api=None, ytdlp=yt)
    assert yt.searched == ["ros 2"]
    assert len(ids) == 1
    assert get_request(db, rid).status == "done"


def test_excludes_disliked_and_recently_played(db):
    for v in ("a", "b", "c"):
        upsert_video(db, vid(v))
    play = start_play(db, "a", None)
    end_play(db, play, 60, "finished")
    add_rating(db, "b", False, None, None)
    claude = FakeClaude([plan(), shortlist("a", "b", "c"), picks(("c", "r"))])
    run(db, claude)
    assert "a | Title a" not in claude.prompts[1]
    assert "b | Title b" not in claude.prompts[1]
    assert "c | Title c | Chan | 10:00 | 2026-01-02" in claude.prompts[1]


def test_unknown_pick_ids_ignored(db):
    claude = FakeClaude([plan(), shortlist("a", "zzz"), picks(("zzz", "r"), ("b", "r"), ("a", "ok"))])
    _, ids = run(db, claude)
    assert [i.video.video_id for i in upcoming(db)] == ["a"]


def test_picks_capped_at_count(db):
    claude = FakeClaude([plan(1), shortlist("a", "b"), picks(("a", "r"), ("b", "r"))])
    _, ids = run(db, claude)
    assert len(ids) == 1


def test_rate_limited_marks_failed(db):
    claude = FakeClaude([ClaudeRateLimited("Claude usage or rate limit reached")])
    rid, ids = run(db, claude)
    assert ids == []
    req = get_request(db, rid)
    assert req.status == "failed" and "rate" in req.error.lower()
    event = recent_events(db)[0]
    assert (event["component"], event["level"]) == ("agent", "error")
    assert event["message"].startswith("ClaudeRateLimited:")
    assert upcoming(db) == []


def test_empty_picks_fail_with_no_picks_event(db):
    claude = FakeClaude([plan(), shortlist("a"), picks(("zzz", "r"))])
    rid, ids = run(db, claude)
    assert ids == []
    req = get_request(db, rid)
    assert (req.status, req.error) == ("failed", "no suitable videos found")
    assert recent_events(db)[0]["message"] == "NoPicks: no suitable videos found"


@pytest.mark.parametrize(
    "bad_plan",
    [
        "not json at all",
        json.dumps({"queries": "ros", "count": 3}),
        json.dumps({"queries": [], "count": 3}),
        json.dumps({"queries": ["x"], "count": "3"}),
        json.dumps(["x"]),
    ],
)
def test_bad_plan_output_marks_failed(db, bad_plan):
    rid, ids = run(db, FakeClaude([bad_plan]))
    assert ids == []
    assert get_request(db, rid).status == "failed"


def test_bad_pick_shape_marks_failed(db):
    claude = FakeClaude([plan(), shortlist("a"), json.dumps({"picks": [{"video_id": "a"}]})])
    rid, _ = run(db, claude)
    assert get_request(db, rid).status == "failed"


def test_count_clamped_and_queries_limited(db):
    big = json.dumps({"queries": ["1", "2", "3", "4", "5", "6"], "count": 99, "target_minutes": 30})
    api = FakeApi(["a"])
    claude = FakeClaude([big, shortlist("a"), picks(("a", "r"))])
    run(db, claude, api=api)
    assert api.queries == ["1", "2", "3", "4"]
    assert "at most 12 videos" in claude.prompts[2]


def test_transcript_failure_tolerated(db):
    yt = FakeYt(tx_error=SourceError("no captions"))
    claude = FakeClaude([plan(), shortlist("a", "b"), picks(("a", "r"))])
    rid, ids = run(db, claude, yt=yt)
    assert get_request(db, rid).status == "done"
    assert "(no transcript)" in claude.prompts[2]
    assert "transcript of b" in claude.prompts[2]


def test_transcript_auth_error_tolerated_and_logged_once(db):
    yt = FakeYt(tx_error=SourceAuthError("sign in"))
    claude = FakeClaude([plan(), shortlist("a", "b"), picks(("a", "r"))])
    rid, _ = run(db, claude, yt=yt)
    assert get_request(db, rid).status == "done"
    ytdlp_events = [e for e in recent_events(db) if e["component"] == "ytdlp"]
    assert [e["message"] for e in ytdlp_events] == ["SourceAuthError: cookies or sign-in check"]


def test_search_auth_error_marks_failed_and_logs_ytdlp(db):
    api = FakeApi(quota=True)
    yt = FakeYt()
    yt.search = lambda q, n=20: (_ for _ in ()).throw(SourceAuthError("sign in"))
    rid, _ = run(db, FakeClaude([plan()]), api=api, yt=yt)
    assert get_request(db, rid).status == "failed"
    components = {(e["component"], e["message"]) for e in recent_events(db)}
    assert ("ytdlp", "SourceAuthError: cookies or sign-in check") in components


def test_unexpected_exception_marks_failed_without_raising(db):
    class Boom:
        def search(self, q, n=25):
            raise RuntimeError("boom")

    rid, ids = run(db, FakeClaude([plan()]), api=Boom())
    assert ids == []
    req = get_request(db, rid)
    assert req.status == "failed" and "boom" in req.error


def test_prompt_contains_request_and_taste(db):
    save_taste(db, "Loves hands-on robotics deep dives", ["robotics"], ["clickbait"], "user")
    claude = FakeClaude([ClaudeRateLimited("x")])
    run(db, claude, prompt="recommend videos for learning ROS 2")
    assert "recommend videos for learning ROS 2" in claude.prompts[0]
    assert "Loves hands-on robotics deep dives" in claude.prompts[0]


def test_morning_source_recorded(db):
    rid = create_request(db, "x", "morning", "end")
    claude = FakeClaude([plan(), shortlist("a"), picks(("a", "r"))])
    run_request(db, rid, claude=claude, data_api=FakeApi(["a"]), ytdlp=FakeYt(), source="morning")
    assert upcoming(db)[0].source == "morning"


def test_taste_context_formats_feedback_lines(db):
    upsert_video(db, vid("a"))
    upsert_video(db, vid("b"))
    play_a = start_play(db, "a", None)
    end_play(db, play_a, 125, "skipped")
    play_b = start_play(db, "b", None)
    end_play(db, play_b, 600, "finished")
    add_rating(db, "b", True, 8, "great")
    text = build_taste_context(db)
    assert "Title a | Chan | liked=- sat=- | skipped at 2:05" in text
    assert "Title b | Chan | liked=y sat=8 | great" in text
    assert "(no taste profile yet)" in text
