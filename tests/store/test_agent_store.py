"""Tests for the agent request / taste store."""

from __future__ import annotations

from src.store import agent, queue
from src.store.videos import add_rating
from tests.store.test_queue import add_video


def test_request_status_transitions(db):
    rid = agent.create_request(db, "find stuff", "user", "now")
    req = agent.get_request(db, rid)
    assert (req.status, req.kind, req.play, req.finished_at) == ("pending", "user", "now", None)
    agent.set_status(db, rid, "running")
    assert agent.get_request(db, rid).finished_at is None
    agent.set_status(db, rid, "done", summary="queued 3")
    req = agent.get_request(db, rid)
    assert (req.status, req.summary, req.error) == ("done", "queued 3", None)
    assert req.finished_at is not None
    rid2 = agent.create_request(db, "x", "morning", "end")
    agent.set_status(db, rid2, "failed", error="cli missing")
    assert agent.get_request(db, rid2).error == "cli missing"
    assert agent.get_request(db, 999) is None


def test_list_requests_newest_first_with_limit(db):
    made = [agent.create_request(db, f"p{i}", "user", "end") for i in range(3)]
    assert [r.id for r in agent.list_requests(db, limit=2)] == [made[2], made[1]]


def test_queued_for_request(db):
    rid = agent.create_request(db, "p", "user", "end")
    queue.enqueue(db, add_video(db, "a"), "agent", agent_request_id=rid)
    queue.enqueue(db, add_video(db, "b"), "user")
    items = agent.queued_for_request(db, rid)
    assert [i.video.video_id for i in items] == ["a"]
    assert items[0].agent_request_id == rid


def test_taste_versions_latest_wins(db):
    assert agent.latest_taste(db) is None
    agent.save_taste(db, "old", ["a"], ["b"], "legacy")
    new_id = agent.save_taste(db, "new", ["x", "y"], [], "agent")
    taste = agent.latest_taste(db)
    assert (taste.id, taste.summary, taste.source) == (new_id, "new", "agent")
    assert taste.likes == ("x", "y") and taste.dislikes == ()


def test_corrections_pending_then_applied(db):
    c1 = agent.add_correction(db, "less news")
    c2 = agent.add_correction(db, "more hardware")
    assert agent.pending_corrections(db) == [(c1, "less news"), (c2, "more hardware")]
    agent.mark_corrections_applied(db, [c1])
    assert agent.pending_corrections(db) == [(c2, "more hardware")]


def test_ratings_since_last_taste(db):
    vid = add_video(db, "a")
    add_rating(db, vid, True, None, None)
    assert agent.ratings_since_last_taste(db) == 1  # no profile: all ratings
    agent.save_taste(db, "s", [], [], "agent")
    assert agent.ratings_since_last_taste(db) == 0
    add_rating(db, vid, False, None, None)
    add_rating(db, vid, None, 5, None)
    assert agent.ratings_since_last_taste(db) == 2
