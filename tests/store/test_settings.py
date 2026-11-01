"""Tests for the settings / quota / status store."""

from __future__ import annotations

from dataclasses import replace

import pytest

from src.store import settings
from src.store.settings import MorningSettings


def test_get_morning_defaults(db):
    s = settings.get_morning(db)
    assert s.enabled is True and s.wake_time == "07:45" and s.count == 3
    assert s.days == (0, 1, 2, 3, 4, 5, 6)
    assert s.topics == ("technology", "AI", "hardware", "engineering", "news/politics")


@pytest.mark.parametrize("bad", ["7:45", "25:00", "07:60", "0745", ""])
def test_save_morning_rejects_bad_time(db, bad):
    with pytest.raises(ValueError):
        settings.save_morning(db, replace(settings.get_morning(db), wake_time=bad))
    assert settings.get_morning(db).wake_time == "07:45"


@pytest.mark.parametrize(
    "change",
    [{"days": (7,)}, {"days": (-1,)}, {"count": 0}, {"count": 7}, {"topics": ()}],
)
def test_save_morning_rejects_bad_fields(db, change):
    with pytest.raises(ValueError):
        settings.save_morning(db, replace(settings.get_morning(db), **change))


def test_save_morning_roundtrip(db):
    new = MorningSettings(
        enabled=False, wake_time="06:30", days=(1, 3), topics=("space", "music"), count=5
    )
    settings.save_morning(db, new)
    assert settings.get_morning(db) == new


def test_quota_accumulates_per_day(db):
    assert settings.quota_used(db, "2026-11-01") == 0
    assert settings.add_quota(db, "2026-11-01", 100) == 100
    assert settings.add_quota(db, "2026-11-01", 1) == 101
    assert settings.add_quota(db, "2026-11-02", 5) == 5
    assert settings.quota_used(db, "2026-11-01") == 101


def test_events_latest_and_recent(db):
    assert settings.latest_event(db, "player") is None
    settings.log_event(db, "player", "info", "started")
    settings.log_event(db, "agent", "warn", "slow")
    settings.log_event(db, "player", "error", "boom")
    level, message, created_at = settings.latest_event(db, "player")
    assert (level, message) == ("error", "boom") and created_at
    assert [r["message"] for r in settings.recent_events(db, limit=2)] == ["boom", "slow"]


def test_record_import_and_imported(db):
    settings.record_import(db, "subscription", channel_id="UC1", title="Chan")
    settings.record_import(db, "liked", video_id="v1")
    subs = settings.imported(db, "subscription")
    assert len(subs) == 1 and subs[0]["channel_id"] == "UC1"
    assert [r["video_id"] for r in settings.imported(db, "liked")] == ["v1"]
