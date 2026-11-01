"""Tests for SQLite connection setup and migrations."""

from __future__ import annotations

import sqlite3

import pytest

from src.db import connect, migrate


@pytest.fixture
def raw(tmp_path):
    conn = connect(tmp_path / "nested" / "raw.db")
    yield conn
    conn.close()


def test_migrate_fresh_returns_latest(raw):
    assert migrate(raw) == 3


def test_migrate_is_idempotent(raw):
    migrate(raw)
    assert migrate(raw) == 3
    assert raw.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0] == 1


def test_connect_pragmas(raw):
    assert raw.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert raw.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert raw.row_factory is sqlite3.Row


def test_morning_defaults_seeded(db):
    row = db.execute("SELECT * FROM morning_settings").fetchone()
    assert row["wake_time"] == "07:45"
    assert row["count"] == 3


def test_rating_requires_some_field(db):
    db.execute("INSERT INTO videos (video_id, title, first_seen) VALUES ('v', 't', 'now')")
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO ratings (video_id, created_at) VALUES ('v', 'now')")


def test_satisfaction_out_of_range_rejected(db):
    db.execute("INSERT INTO videos (video_id, title, first_seen) VALUES ('v', 't', 'now')")
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO ratings (video_id, satisfaction, created_at) VALUES ('v', 11, 'now')"
        )


def test_foreign_keys_enforced(db):
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO ratings (video_id, liked, created_at) VALUES ('x', 1, 'now')")


def test_new_columns_have_defaults(db):
    cols = {r["name"]: r for r in db.execute("PRAGMA table_info(videos)")}
    assert cols["chapters_checked"]["dflt_value"] == "0"



def test_build_lead_column_defaults_and_range(db):
    assert db.execute("SELECT build_lead_min FROM morning_settings").fetchone()[0] == 5
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("UPDATE morning_settings SET build_lead_min = 61")
