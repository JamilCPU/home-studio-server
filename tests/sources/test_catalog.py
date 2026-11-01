"""Tests for the catalog cache."""

from __future__ import annotations

import pytest

from src.sources import catalog
from src.sources.data_api import QuotaExceeded
from src.sources.ytdlp import SourceAuthError, SourceError
from src.store.settings import recent_events
from src.store.videos import Chapter, Video, get_video, upsert_video


def vid(video_id, chapters=(), transcript=None):
    return Video(
        video_id=video_id, title=f"T{video_id}", channel_name=None, channel_id=None,
        description=None, duration_s=60, published_at=None, tags=(), category=None,
        chapters=chapters, thumbnail_url=None, view_count=None, like_count=None,
        transcript=transcript,
    )


class FakeApi:
    def __init__(self, exc=None):
        self.calls, self.exc = [], exc

    def video_details(self, ids):
        self.calls.append(list(ids))
        if self.exc:
            raise self.exc
        return [vid(i) for i in ids]


class FakeYt:
    def __init__(self, fail=None, text="hello world. " * 400, chapters=()):
        self.meta_calls, self.tx_calls = [], 0
        self.fail, self.text, self.chapters = fail or {}, text, chapters

    def metadata(self, video_id):
        self.meta_calls.append(video_id)
        if video_id in self.fail:
            raise self.fail[video_id]
        return vid(video_id, chapters=self.chapters)

    def transcript(self, video_id, lang="en"):
        self.tx_calls += 1
        return self.text


def test_catalog_skips_known_ids(db):
    upsert_video(db, vid("known"))
    api = FakeApi()
    out = catalog.ensure_videos(db, ["new1", "known", "new2"], api, FakeYt())
    assert api.calls == [["new1", "new2"]]
    assert [v.video_id for v in out] == ["new1", "known", "new2"]
    assert get_video(db, "new1") is not None


def test_catalog_falls_back_on_quota(db):
    yt = FakeYt(fail={"b": SourceError("gone")})
    out = catalog.ensure_videos(db, ["a", "b", "c"], FakeApi(QuotaExceeded("x")), yt)
    assert yt.meta_calls == ["a", "b", "c"]
    assert [v.video_id for v in out] == ["a", "c"]


def test_catalog_fallback_propagates_auth_error(db):
    yt = FakeYt(fail={"a": SourceAuthError("cookies")})
    with pytest.raises(SourceAuthError):
        catalog.ensure_videos(db, ["a"], FakeApi(QuotaExceeded("x")), yt)


def test_transcript_cached_second_call_no_fetch(db):
    upsert_video(db, vid("v"))
    yt = FakeYt()
    first = catalog.ensure_transcript(db, "v", yt, max_chars=100)
    second = catalog.ensure_transcript(db, "v", yt, max_chars=100)
    assert first == second and len(first) <= 100
    assert yt.tx_calls == 1
    assert get_video(db, "v").transcript == first


def test_transcript_none_not_cached(db):
    upsert_video(db, vid("v"))
    yt = FakeYt(text=None)
    assert catalog.ensure_transcript(db, "v", yt) is None
    assert get_video(db, "v").transcript is None


def test_ensure_chapters_fills_empty(db):
    upsert_video(db, vid("v"))
    yt = FakeYt(chapters=(Chapter("Intro", 0.0),))
    catalog.ensure_chapters(db, "v", yt)
    assert get_video(db, "v").chapters == (Chapter("Intro", 0.0),)


def test_ensure_chapters_noop_when_present(db):
    upsert_video(db, vid("v", chapters=(Chapter("X", 1.0),)))
    yt = FakeYt()
    catalog.ensure_chapters(db, "v", yt)
    assert yt.meta_calls == []


def test_ensure_chapters_swallows_source_error(db):
    upsert_video(db, vid("v"))
    catalog.ensure_chapters(db, "v", FakeYt(fail={"v": SourceError("boom")}))
    assert get_video(db, "v").chapters == ()
    assert len(recent_events(db)) == 1
