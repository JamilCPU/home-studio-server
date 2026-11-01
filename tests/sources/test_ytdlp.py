"""Tests for the yt-dlp source, driven by a fake YoutubeDL factory."""

from __future__ import annotations

import json

import pytest
from yt_dlp.utils import DownloadError

from src.sources.ytdlp import SourceAuthError, SourceError, YtDlp


class FakeYdl:
    def __init__(self, owner, opts):
        self._owner, self.opts = owner, opts

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, url, download=False):
        self._owner.urls.append(url)
        result = self._owner.info
        if isinstance(result, Exception):
            raise result
        return result

    def urlopen(self, url):
        self._owner.fetched.append(url)
        payload = json.dumps(self._owner.json3[url]).encode()

        class _Resp:
            def read(_self):
                return payload

        return _Resp()


class Factory:
    def __init__(self, info=None, json3=None):
        self.info = info
        self.json3 = json3 or {}
        self.opts: list[dict] = []
        self.urls: list[str] = []
        self.fetched: list[str] = []

    def __call__(self, opts):
        self.opts.append(opts)
        return FakeYdl(self, opts)


def _ytdlp(factory, cookies="missing.txt", **kw):
    return YtDlp(cookies, ydl_factory=factory, sleep=kw.pop("sleep", lambda s: None), **kw)


@pytest.mark.parametrize(
    "kind,url",
    [
        ("liked", ":ytfav"),
        ("history", ":ythistory"),
        ("recommended", ":ytrec"),
        ("subscriptions", ":ytsubs"),
        ("watch_later", ":ytwatchlater"),
    ],
)
def test_feed_kind_maps_to_ytdlp_url(kind, url):
    f = Factory(
        info={"entries": [{"id": "a", "title": "A", "channel_id": "c", "channel": "Ch"}, None]}
    )
    items = _ytdlp(f).account_feed(kind, limit=7)
    assert f.urls == [url]
    assert f.opts[0]["extract_flat"] == "in_playlist"
    assert f.opts[0]["playlistend"] == 7
    assert (items[0].video_id, items[0].channel_name) == ("a", "Ch")
    assert len(items) == 1


def test_subscribed_channels_have_empty_video_id():
    f = Factory(info={"entries": [{"id": "UC1", "title": "Ch", "channel_id": "UC1"}]})
    items = _ytdlp(f).subscribed_channels()
    assert f.urls == ["https://www.youtube.com/feed/channels"]
    assert items[0].video_id == "" and items[0].channel_id == "UC1"


def test_calls_are_paced():
    now = [100.0]
    sleeps: list[float] = []
    f = Factory(info={"entries": []})
    y = YtDlp("x", ydl_factory=f, sleep=sleeps.append, clock=lambda: now[0], min_interval_s=2.0)
    y.search("q")
    y.search("q")
    assert sleeps == [2.0]
    now[0] += 5
    y.search("q")
    assert sleeps == [2.0]


def test_bot_check_raises_source_auth_error():
    f = Factory(info=DownloadError("Sign in to confirm you're not a bot"))
    with pytest.raises(SourceAuthError):
        _ytdlp(f).metadata("abc")


def test_other_errors_raise_source_error():
    f = Factory(info=DownloadError("Video unavailable"))
    with pytest.raises(SourceError) as ei:
        _ytdlp(f).metadata("abc")
    assert not isinstance(ei.value, SourceAuthError)


def test_never_sets_mark_watched(tmp_path):
    cookies = tmp_path / "c.txt"
    cookies.write_text("# x")
    f = Factory(info={"entries": []})
    y = _ytdlp(f, cookies=str(cookies))
    y.search("q")
    y.account_feed("liked")
    y.subscribed_channels()
    assert f.opts
    for o in f.opts:
        assert "mark_watched" not in o
        assert o["cookiefile"] == str(cookies)
        assert o["skip_download"] is True and o["quiet"] is True


def test_cookiefile_omitted_when_missing():
    f = Factory(info={"entries": []})
    _ytdlp(f).search("q")
    assert "cookiefile" not in f.opts[0]


def test_search_uses_ytsearch_and_flat():
    f = Factory(info={"entries": [{"id": "x"}, {"id": "y"}]})
    assert _ytdlp(f).search("hello", n=2) == ["x", "y"]
    assert f.urls == ["ytsearch2:hello"]
    assert f.opts[0]["extract_flat"] == "in_playlist"


def test_metadata_maps_fields():
    f = Factory(
        info={
            "id": "v1", "title": "T", "channel": "Ch", "channel_id": "c1", "description": "d",
            "duration": 61, "upload_date": "20260102", "tags": ["a", "b"],
            "categories": ["Science", "X"], "thumbnail": "http://t", "view_count": 5,
            "like_count": 2,
            "chapters": [{"title": "Intro", "start_time": 0}, {"title": "Two", "start_time": 30.5}],
        }
    )
    v = _ytdlp(f).metadata("v1")
    assert v.published_at == "2026-01-02"
    assert v.category == "Science" and v.tags == ("a", "b")
    assert [(c.title, c.start_s) for c in v.chapters] == [("Intro", 0.0), ("Two", 30.5)]
    assert v.duration_s == 61


def _json3(text):
    return {"events": [{"segs": [{"utf8": text}]}, {"segs": [{"utf8": "\n"}, {"utf8": "more"}]}]}


def test_transcript_prefers_manual():
    info = {
        "subtitles": {"en": [{"ext": "vtt", "url": "m-vtt"}, {"ext": "json3", "url": "m-json"}]},
        "automatic_captions": {"en": [{"ext": "json3", "url": "a-json"}]},
    }
    f = Factory(info=info, json3={"m-json": _json3("manual   text"), "a-json": _json3("auto")})
    assert _ytdlp(f).transcript("v") == "manual text more"
    assert f.fetched == ["m-json"]


def test_transcript_falls_back_to_auto_and_en_prefix():
    info = {"subtitles": {}, "automatic_captions": {"en-US": [{"ext": "json3", "url": "a"}]}}
    f = Factory(info=info, json3={"a": _json3("auto")})
    assert _ytdlp(f).transcript("v") == "auto more"


def test_transcript_none_without_tracks():
    f = Factory(info={"subtitles": {}, "automatic_captions": {}})
    assert _ytdlp(f).transcript("v") is None
    assert f.fetched == []
