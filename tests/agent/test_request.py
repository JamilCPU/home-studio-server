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


_DEFAULT = object()


def run(db, claude, api=_DEFAULT, yt=None, play="end", prompt="learn ROS 2"):
    rid = create_request(db, prompt, "user", play)
    data_api = FakeApi(["a", "b", "c"]) if api is _DEFAULT else api
    ids = run_request(db, rid, claude=claude, data_api=data_api, ytdlp=yt or FakeYt())
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


def test_data_api_search_error_falls_back_to_ytdlp(db):
    class BadKey(FakeApi):
        def search(self, query, max_results=25):
            raise RuntimeError("HttpError 400: API key not valid")

    yt = FakeYt(["a"])
    claude = FakeClaude([plan(), shortlist("a"), picks(("a", "r"))])
    rid, ids = run(db, claude, api=BadKey(), yt=yt)
    assert yt.searched == ["ros 2"]
    assert len(ids) == 1
    assert get_request(db, rid).status == "done"


def test_unexpected_exception_marks_failed_without_raising(db):
    class Boom:
        def search(self, q, n=25):
            raise RuntimeError("boom")

    rid, ids = run(db, FakeClaude([plan()]), api=None, yt=Boom())
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


# -- pick quality (final review I6) -------------------------------------------------


def test_shortlist_prompt_has_taste_and_count(db):
    save_taste(db, "Loves long hardware deep dives", ["hardware"], [], "user")
    claude = FakeClaude([plan(10), shortlist("a"), picks(("a", "r"))])
    run(db, claude)
    assert "Loves long hardware deep dives" in claude.prompts[1]
    assert "at least 10" in claude.prompts[1]
    assert "at most 10 video ids" in claude.prompts[1]  # max(8, count), up to 12


def test_shortlist_cap_follows_count(db):
    ids = [f"v{i:02d}" for i in range(15)]
    api = FakeApi(ids)
    claude = FakeClaude([plan(10), shortlist(*ids), picks(*[(i, "r") for i in ids])])
    _, queued = run(db, claude, api=api)
    shortlisted = [i for i in ids if f"{i} | Title {i}" in claude.prompts[2]]
    assert len(shortlisted) == 10
    assert len(queued) == 10


def test_shortlist_prompt_default_cap_is_8(db):
    claude = FakeClaude([plan(2), shortlist("a"), picks(("a", "r"))])
    run(db, claude)
    assert "at most 8 video ids" in claude.prompts[1]


class DurApi(FakeApi):
    def __init__(self, durations):
        super().__init__(list(durations))
        self.durations = durations

    def video_details(self, ids):
        return [vid(i, self.durations[i]) for i in ids]


def test_short_live_and_unknown_length_videos_dropped(db):
    api = DurApi({"long": 1200, "short": 90, "live": 0, "unknown": None, "edge": 180})
    claude = FakeClaude([plan(), shortlist("long"), picks(("long", "r"))])
    run(db, claude, api=api)
    assert "long | Title long" in claude.prompts[1]
    assert "edge | Title edge" in claude.prompts[1]
    for dropped in ("short", "live", "unknown"):
        assert f"{dropped} | Title {dropped}" not in claude.prompts[1]


@pytest.mark.parametrize("prompt", ["some short videos about GPUs", "a funny clip"])
def test_short_videos_kept_when_asked_for(db, prompt):
    api = DurApi({"long": 1200, "short": 90})
    claude = FakeClaude([plan(), shortlist("short"), picks(("short", "r"))])
    run(db, claude, api=api, prompt=prompt)
    assert "short | Title short" in claude.prompts[1]


def test_queued_and_playing_videos_excluded(db):
    for v in ("a", "b"):
        upsert_video(db, vid(v))
    enqueue(db, "a", "user")
    playing = enqueue(db, "b", "user")
    mark(db, playing, "playing")
    claude = FakeClaude([plan(), shortlist("c"), picks(("c", "r"))])
    run(db, claude)
    assert "a | Title a" not in claude.prompts[1]
    assert "b | Title b" not in claude.prompts[1]
    assert "c | Title c" in claude.prompts[1]


def test_account_imports_offered_before_search_results(db):
    from src.store.settings import record_import

    for v in ("liked1", "wl1", "rec1", "hist1", "played1", "nodata"):
        if v != "nodata":
            upsert_video(db, vid(v))
    record_import(db, "liked", video_id="liked1")
    record_import(db, "playlist", video_id="wl1")
    record_import(db, "recommended", video_id="rec1")
    record_import(db, "history", video_id="hist1")
    record_import(db, "liked", video_id="played1")
    record_import(db, "liked", video_id="nodata")  # not in videos: no fetch, no quota
    end_play(db, start_play(db, "played1", None), 60, "finished")
    claude = FakeClaude([plan(), shortlist("a"), picks(("a", "r"))])
    run(db, claude)
    lines = [ln.split(" | ")[0] for ln in claude.prompts[1].splitlines() if " | Title " in ln]
    assert lines[:3] == ["liked1", "wl1", "rec1"]
    assert "hist1" not in lines and "played1" not in lines and "nodata" not in lines
    assert lines[3:] == ["a", "b", "c"]


def test_account_import_candidates_capped_at_20(db):
    from src.store.settings import record_import

    for i in range(25):
        upsert_video(db, vid(f"imp{i:02d}"))
        record_import(db, "liked", video_id=f"imp{i:02d}")
    claude = FakeClaude([plan(), shortlist("a"), picks(("a", "r"))])
    run(db, claude)
    assert sum(f"imp{i:02d} | Title" in claude.prompts[1] for i in range(25)) == 20


@pytest.mark.parametrize("count, target, want_count, want_target", [
    (3.0, 45.0, 3, "45"), (2.6, 30.4, 3, "30"),
])
def test_float_count_and_target_accepted(db, count, target, want_count, want_target):
    raw = json.dumps({"queries": ["x"], "count": count, "target_minutes": target})
    claude = FakeClaude([raw, shortlist("a"), picks(("a", "r"))])
    rid, _ = run(db, claude)
    assert get_request(db, rid).status == "done"
    assert f"at most {want_count} videos" in claude.prompts[2]
    assert f"listening time in minutes: {want_target}." in claude.prompts[2]


def test_transcripts_stop_after_first_auth_error(db):
    class CountingYt(FakeYt):
        def __init__(self):
            super().__init__()
            self.tx_calls = []

        def transcript(self, video_id, lang="en"):
            self.tx_calls.append(video_id)
            raise SourceAuthError("sign in")

    yt = CountingYt()
    claude = FakeClaude([plan(), shortlist("a", "b", "c"), picks(("a", "r"))])
    rid, _ = run(db, claude, yt=yt)
    assert yt.tx_calls == ["a"]
    assert claude.prompts[2].count("(no transcript)") == 3
    assert get_request(db, rid).status == "done"
