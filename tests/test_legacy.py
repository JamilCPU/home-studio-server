"""Tests for the one-time legacy JSON import."""

from __future__ import annotations

import json

from src.legacy import migrate_legacy
from src.store.agent import latest_taste, save_taste
from src.store.videos import get_video


def write(tmp_path, name, data):
    (tmp_path / name).write_text(json.dumps(data), encoding="utf-8")


def seed(tmp_path):
    ts = "2026-01-01T00:00:00"
    write(
        tmp_path,
        "feedback.json",
        [
            {"video_id": "v1", "title": "One", "rating": "thumbs_up", "comment": "great",
             "timestamp": ts},
            {"video_id": "v2", "title": "Two", "rating": "thumbs_down", "comment": "",
             "timestamp": ts},
            {"video_id": "v3", "title": "Three", "rating": "neutral", "comment": "meh",
             "timestamp": ts},
            {"video_id": "v4", "title": "Four", "rating": "neutral", "comment": "",
             "timestamp": ts},
            {"video_id": "v5", "rating": "weird", "comment": "x"},
            {"video_id": "v6", "rating": "thumbs_up"},
        ],
    )
    write(
        tmp_path,
        "interest_profile.json",
        {"topics": {"a": 9, "b": 5, "c": 7, "d": 3, "e": 1, "f": 8, "bad": -4}},
    )


def ratings(conn):
    return {
        r["video_id"]: (r["liked"], r["notes"])
        for r in conn.execute("SELECT * FROM ratings").fetchall()
    }


def test_legacy_maps_ratings(db, tmp_path):
    seed(tmp_path)
    counts = migrate_legacy(db, tmp_path)
    assert counts == {"ratings": 4, "skipped": 2, "taste": 1}
    got = ratings(db)
    assert got["v1"] == (1, "great")
    assert got["v2"] == (0, None)
    assert got["v3"] == (None, "meh")
    assert got["v6"] == (1, None)
    assert "v4" not in got and "v5" not in got
    assert get_video(db, "v1").title == "One"
    assert get_video(db, "v6").title == ""
    taste = latest_taste(db)
    assert taste.source == "legacy"
    assert taste.summary == "Interested in: a, f, c, b, d"
    assert taste.likes == ("a", "f", "c", "b", "d", "e")
    assert taste.dislikes == ("bad",)


def test_legacy_missing_files_skipped(db, tmp_path):
    assert migrate_legacy(db, tmp_path) == {"ratings": 0, "skipped": 0, "taste": 0}


def test_legacy_is_idempotent(db, tmp_path):
    seed(tmp_path)
    migrate_legacy(db, tmp_path)
    before = db.execute("SELECT COUNT(*) FROM ratings").fetchone()[0]
    again = migrate_legacy(db, tmp_path)
    assert again["ratings"] == 0 and again["taste"] == 0
    assert db.execute("SELECT COUNT(*) FROM ratings").fetchone()[0] == before
    assert db.execute("SELECT COUNT(*) FROM taste_profiles").fetchone()[0] == 1


def test_legacy_not_blocked_by_non_legacy_taste(db, tmp_path):
    seed(tmp_path)
    save_taste(db, "agent", [], [], "agent")
    assert migrate_legacy(db, tmp_path)["taste"] == 1


def test_legacy_empty_topics_saves_no_taste(db, tmp_path):
    write(tmp_path, "interest_profile.json", {"topics": {}})
    assert migrate_legacy(db, tmp_path)["taste"] == 0
    write(tmp_path, "interest_profile.json", {"topics": {"a": 0, "b": 0}})
    assert migrate_legacy(db, tmp_path)["taste"] == 0
    assert latest_taste(db) is None


def test_legacy_coerces_odd_values_and_normalizes_timestamps(db, tmp_path):
    write(
        tmp_path,
        "feedback.json",
        [
            {"video_id": "n1", "title": 42, "rating": "thumbs_up", "comment": 7,
             "timestamp": "2026-01-01T05:00:00+05:00"},
            {"video_id": "n2", "rating": ["bad"], "comment": "x"},
            {"video_id": "n3", "rating": "thumbs_up", "timestamp": "garbage"},
            {"video_id": "n4", "rating": "thumbs_up", "timestamp": "2026-02-03T04:05:06"},
        ],
    )
    counts = migrate_legacy(db, tmp_path)
    assert counts["ratings"] == 3 and counts["skipped"] == 1
    assert get_video(db, "n1").title == "42"
    created = {
        r["video_id"]: (r["created_at"], r["notes"])
        for r in db.execute("SELECT * FROM ratings").fetchall()
    }
    assert created["n1"] == ("2026-01-01T00:00:00+00:00", "7")
    assert created["n4"][0] == "2026-02-03T04:05:06+00:00"
    assert created["n3"][0].startswith("1970-01-01")
