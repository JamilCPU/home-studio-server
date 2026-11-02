"""Tests for the Home Studio entry point: CLI, service wiring and account import."""

from __future__ import annotations

import json
import threading
import time
from contextlib import closing
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from src.__main__ import (
    ServiceFactories,
    _serve_runtime,
    _start_background,
    _stop_background,
    build_parser,
    build_runtime,
    build_services,
    chapters_hook,
    import_account,
    main,
)
from src.api.app import create_app
from src.db import connect, migrate
from src.models import AppConfig
from src.sources.ytdlp import FeedItem, SourceAuthError, SourceError
from src.store.agent import latest_taste
from src.store import queue
from src.store.settings import imported, latest_event
from src.store.videos import Chapter, Video, get_video, upsert_video
from tests.agent.fakes import FakeClaude
from tests.player.fakes import FakeMpv

TASTE_JSON = json.dumps({"summary": "Likes hardware talks", "likes": ["hardware"], "dislikes": []})


def vid(video_id: str, chapters=()) -> Video:
    return Video(
        video_id=video_id, title=f"Title {video_id}", channel_name="Chan", channel_id="UCx",
        description=None, duration_s=600, published_at=None, tags=(), category=None,
        chapters=chapters, thumbnail_url=None, view_count=None, like_count=None, transcript=None,
    )


class FakeYt:
    def __init__(self, feeds=None, feed_errors=None, chapters=()) -> None:
        self.feeds = feeds or {}
        self.feed_errors = feed_errors or {}
        self.chapters = chapters
        self.feed_calls: list[tuple[str, int]] = []
        self.searches: list[str] = []
        self.meta_calls: list[str] = []

    def subscribed_channels(self) -> list[FeedItem]:
        return [FeedItem("", "Chan A", "UCa", "Chan A"), FeedItem("", "Chan B", "UCb", "Chan B")]

    def account_feed(self, kind: str, limit: int = 200) -> list[FeedItem]:
        self.feed_calls.append((kind, limit))
        if kind in self.feed_errors:
            raise self.feed_errors[kind]
        return [FeedItem(v, f"Title {v}", "UCx", "Chan") for v in self.feeds.get(kind, [])]

    def metadata(self, video_id: str) -> Video:
        self.meta_calls.append(video_id)
        return vid(video_id, self.chapters)

    def search(self, query: str, n: int = 20) -> list[str]:
        self.searches.append(query)
        return ["aaaaaaaaaaa"]


class Recorder:
    """Factory double that records how often it was called."""

    def __init__(self, value) -> None:
        self.value = value
        self.calls = 0

    def __call__(self, *args):
        self.calls += 1
        return self.value


class FakeBt:
    last_state = "unconfigured"

    def __init__(self) -> None:
        self.running = False

    def start(self) -> None:
        self.running = True

    def stop(self) -> None:
        self.running = False


class SlowMpv(FakeMpv):
    def wait_event(self, timeout: float):
        time.sleep(0.01)
        return super().wait_event(timeout)


@pytest.fixture
def db(tmp_path):
    conn = connect(tmp_path / "t.db")
    migrate(conn)
    yield conn
    conn.close()


def _factories(yt, data_api_factory, claude=None) -> ServiceFactories:
    return ServiceFactories(
        mpv=lambda config: FakeMpv(),
        ytdlp=lambda config: yt,
        data_api=data_api_factory,
        claude=lambda config: claude or FakeClaude([]),
        bt=lambda config, db_factory: FakeBt(),
    )


def test_cli_parses_commands():
    parser = build_parser()
    assert parser.parse_args(["serve"]).command == "serve"
    assert parser.parse_args(["import-account"]).command == "import-account"
    legacy = parser.parse_args(["migrate-legacy"])
    assert (legacy.command, legacy.data_dir) == ("migrate-legacy", "data")
    assert parser.parse_args(["migrate-legacy", "--data-dir", "old"]).data_dir == "old"
    assert parser.parse_args(["--config", "x.yaml", "serve"]).config == "x.yaml"
    with pytest.raises(SystemExit):
        parser.parse_args(["curate"])


def test_build_services_without_api_key_uses_ytdlp_search(tmp_path):
    yt = FakeYt()
    data_api_factory = Recorder(object())
    config = AppConfig(db_path=str(tmp_path / "s.db"), youtube_api_key="")
    deps = build_services(config, factories=_factories(yt, data_api_factory))

    assert deps.data_api is None
    assert data_api_factory.calls == 0
    assert deps.ytdlp is yt
    client = TestClient(create_app(deps), raise_server_exceptions=False)
    resp = client.get("/api/search", params={"q": "rust"})
    assert resp.status_code == 200
    assert [r["video_id"] for r in resp.json()["data"]["results"]] == ["aaaaaaaaaaa"]
    assert yt.searches == ["rust"]
    with closing(deps.db_factory()) as conn:  # the database was migrated
        assert conn.execute("SELECT COUNT(*) FROM queue_items").fetchone()[0] == 0


def test_build_services_with_api_key_builds_data_api(tmp_path):
    data_api = object()
    data_api_factory = Recorder(data_api)
    config = AppConfig(db_path=str(tmp_path / "s.db"), youtube_api_key="k")
    deps = build_services(config, factories=_factories(FakeYt(), data_api_factory))
    assert deps.data_api is data_api
    assert data_api_factory.calls == 1


def test_import_account_records_and_seeds(db):
    yt = FakeYt(
        feeds={
            "liked": ["l1", "shared"],
            "history": ["h1", "shared"],
            "recommended": ["r1"],
            "subscriptions": ["s1"],
            "watch_later": ["w1"],
        }
    )
    claude = FakeClaude([TASTE_JSON])
    counts = import_account(db, ytdlp=yt, data_api=None, claude=claude)

    assert [kind for kind, _ in yt.feed_calls] == [
        "liked", "history", "recommended", "subscriptions", "watch_later",
    ]
    assert all(limit == 200 for _, limit in yt.feed_calls)
    assert [r["channel_id"] for r in imported(db, "subscription") if r["video_id"] is None] == [
        "UCa", "UCb",
    ]
    assert [r["video_id"] for r in imported(db, "liked")] == ["l1", "shared"]
    assert [r["video_id"] for r in imported(db, "playlist")] == ["w1"]
    assert "s1" in [r["video_id"] for r in imported(db, "subscription")]
    assert sorted(yt.meta_calls) == sorted(["l1", "shared", "h1", "r1", "s1", "w1"])
    assert get_video(db, "h1") is not None
    assert latest_taste(db).summary == "Likes hardware talks"
    assert counts == {
        "channels": 2, "liked": 2, "history": 2, "recommended": 1, "subscriptions": 1,
        "watch_later": 1, "videos": 6, "taste_seeded": 1,
    }


def test_import_account_again_adds_no_duplicates(db):
    yt = FakeYt(feeds={"liked": ["l1"]})
    import_account(db, ytdlp=yt, data_api=None, claude=FakeClaude([TASTE_JSON]))
    counts = import_account(db, ytdlp=yt, data_api=None, claude=FakeClaude([]))
    assert len(imported(db, "liked")) == 1
    assert len(imported(db, "subscription")) == 2
    assert counts["taste_seeded"] == 0  # a profile already exists


def test_import_account_caps_ensure_videos_at_300(db):
    yt = FakeYt(feeds={"liked": [f"v{i}" for i in range(200)],
                       "history": [f"h{i}" for i in range(200)]})
    import_account(db, ytdlp=yt, data_api=None, claude=FakeClaude([TASTE_JSON]))
    assert len(yt.meta_calls) == 300


def test_import_account_only_fetches_new_videos(db):
    upsert_video(db, vid("l1"))
    yt = FakeYt(feeds={"liked": ["l1", "l2"]})
    counts = import_account(db, ytdlp=yt, data_api=None, claude=FakeClaude([TASTE_JSON]))
    assert yt.meta_calls == ["l2"]
    assert counts["videos"] == 1


def test_import_account_logs_ok_event(db):
    yt = FakeYt(feeds={"liked": ["l1", "l2"]})
    import_account(db, ytdlp=yt, data_api=None, claude=FakeClaude([TASTE_JSON]))
    assert latest_event(db, "import")[:2] == ("info", "ok: 2 new videos")
    assert latest_event(db, "ytdlp")[:2] == ("info", "ok: account import")


def test_import_account_logs_error_event(db):
    class Broken(FakeYt):
        def subscribed_channels(self):
            raise RuntimeError("feed parser broke")

    with pytest.raises(RuntimeError):
        import_account(db, ytdlp=Broken(), data_api=None, claude=FakeClaude([]))
    assert latest_event(db, "import")[:2] == ("error", "RuntimeError: feed parser broke")


def test_import_account_skips_failing_feed(db):
    yt = FakeYt(feeds={"liked": ["l1"]}, feed_errors={"recommended": SourceError("nope")})
    counts = import_account(db, ytdlp=yt, data_api=None, claude=FakeClaude([TASTE_JSON]))
    assert counts["recommended"] == 0
    assert counts["liked"] == 1


def test_import_account_auth_error_logged_and_raised(db):
    yt = FakeYt(feed_errors={"liked": SourceAuthError("sign in to confirm")})
    with pytest.raises(SourceAuthError):
        import_account(db, ytdlp=yt, data_api=None, claude=FakeClaude([]))
    assert latest_event(db, "ytdlp")[0] == "error"
    assert latest_event(db, "import")[0] == "error"


def test_chapters_hook_fills_chapters_on_own_connection(tmp_path):
    path = tmp_path / "h.db"
    with closing(connect(path)) as conn:
        migrate(conn)
        upsert_video(conn, vid("abc"))
    yt = FakeYt(chapters=(Chapter("Intro", 0.0),))
    chapters_hook(lambda: connect(path), yt)("abc")
    with closing(connect(path)) as conn:
        assert get_video(conn, "abc").chapters == (Chapter("Intro", 0.0),)


def _play_and_wait(engine):
    engine.play_next()
    engine._hook_thread.join(timeout=5)


def test_engine_hook_fills_now_playing_and_caches_no_chapters(tmp_path):
    from src.player.engine import PlaybackEngine

    path = tmp_path / "h.db"
    with closing(connect(path)) as conn:
        migrate(conn)
        upsert_video(conn, vid("abc"))
        upsert_video(conn, vid("def"))
        for v in ("abc", "def", "def"):
            queue.enqueue(conn, v, "user")
    factory = lambda: connect(path)  # noqa: E731
    yt = FakeYt(chapters=(Chapter("Intro", 0.0),))
    engine = PlaybackEngine(factory, FakeMpv(), on_started=chapters_hook(factory, yt))
    _play_and_wait(engine)
    assert engine.now().item.video.chapters == (Chapter("Intro", 0.0),)
    yt.chapters = ()
    _play_and_wait(engine)  # def: no chapters found
    _play_and_wait(engine)  # def again: not fetched a second time
    assert yt.meta_calls == ["abc", "def"]


def test_main_migrate_legacy(tmp_path, capsys):
    config_file = tmp_path / "config.yaml"
    config_file.write_text(yaml.dump({"db": {"path": "data/x.db"}}), encoding="utf-8")
    legacy_dir = tmp_path / "legacy"
    legacy_dir.mkdir()
    main(["--config", str(config_file), "migrate-legacy", "--data-dir", str(legacy_dir)])
    assert "ratings" in capsys.readouterr().out
    assert Path(tmp_path / "data" / "x.db").exists()


def test_background_starts_and_stops_cleanly(tmp_path):
    mpv = SlowMpv()
    factories = ServiceFactories(
        mpv=lambda config: mpv,
        ytdlp=lambda config: FakeYt(),
        data_api=Recorder(None),
        claude=lambda config: FakeClaude([]),
        bt=lambda config, db_factory: FakeBt(),
    )
    runtime = build_runtime(AppConfig(db_path=str(tmp_path / "s.db")), factories)
    stop = threading.Event()
    threads = _start_background(runtime, stop)
    assert all(t.is_alive() for t in threads)
    assert runtime.monitor.running
    _stop_background(runtime, stop, threads)
    assert not any(t.is_alive() for t in threads)
    assert not runtime.monitor.running
    assert mpv.closed
    assert runtime.worker._thread is None


def _fake_factories(mpv, bt=None) -> ServiceFactories:
    return ServiceFactories(
        mpv=lambda config: mpv,
        ytdlp=lambda config: FakeYt(),
        data_api=Recorder(None),
        claude=lambda config: FakeClaude([]),
        bt=bt or (lambda config, db_factory: FakeBt()),
    )


def test_shutdown_stops_current_play(tmp_path):
    mpv = SlowMpv()
    runtime = build_runtime(AppConfig(db_path=str(tmp_path / "s.db")), _fake_factories(mpv))
    with closing(runtime.deps.db_factory()) as conn:
        upsert_video(conn, vid("abc"))
        queue.enqueue(conn, "abc", "user")
    runtime.engine.play_next()
    stop = threading.Event()
    _stop_background(runtime, stop, _start_background(runtime, stop))
    with closing(runtime.deps.db_factory()) as conn:
        assert conn.execute("SELECT end_reason FROM plays").fetchone()[0] == "stopped"
    assert mpv.closed


def test_start_background_recovers_leftover_playing_item(tmp_path):
    runtime = build_runtime(AppConfig(db_path=str(tmp_path / "s.db")), _fake_factories(SlowMpv()))
    with closing(runtime.deps.db_factory()) as conn:
        upsert_video(conn, vid("abc"))
        item_id = queue.enqueue(conn, "abc", "user")
        queue.mark(conn, item_id, "playing")  # left over from a crash
        conn.execute("INSERT INTO plays (video_id, queue_item_id, started_at) VALUES ('abc', ?, 'x')",
                     (item_id,))
        conn.commit()
    stop = threading.Event()
    _stop_background(runtime, stop, _start_background(runtime, stop))
    with closing(runtime.deps.db_factory()) as conn:
        assert [i.id for i in queue.upcoming(conn)] == [item_id]
        assert conn.execute("SELECT end_reason FROM plays").fetchone()[0] == "stopped"


def test_serve_start_failure_still_cleans_up(tmp_path):
    class BrokenBt(FakeBt):
        def start(self):
            raise RuntimeError("no dbus")

    mpv = SlowMpv()
    runtime = build_runtime(
        AppConfig(db_path=str(tmp_path / "s.db")),
        _fake_factories(mpv, bt=lambda config, db_factory: BrokenBt()),
    )
    served = []
    with pytest.raises(RuntimeError, match="no dbus"):
        _serve_runtime(runtime, lambda app: served.append(app))
    assert served == []
    assert runtime.worker._thread is None
    assert mpv.closed


def test_serve_runs_server_then_cleans_up(tmp_path):
    mpv = SlowMpv()
    runtime = build_runtime(AppConfig(db_path=str(tmp_path / "s.db")), _fake_factories(mpv))
    served = []
    _serve_runtime(runtime, lambda app: served.append(app))
    assert len(served) == 1
    assert mpv.closed and runtime.worker._thread is None


def test_build_runtime_failure_closes_mpv(tmp_path):
    mpv = SlowMpv()

    def broken_bt(config, db_factory):
        raise ValueError("invalid bluetooth MAC address")

    with pytest.raises(ValueError):
        build_runtime(AppConfig(db_path=str(tmp_path / "s.db")), _fake_factories(mpv, broken_bt))
    assert mpv.closed


# -- OAuth account import ---------------------------------------------------------------


class FakeAccount:
    def __init__(self, connected=True, subs=None, liked=None, playlist=None) -> None:
        self._connected = connected
        self.subs = subs if subs is not None else [FeedItem("", "Sub A", "UCa", "Sub A")]
        self.liked_items = liked if liked is not None else [FeedItem("o1", "Liked 1", "UCx", "C")]
        self.playlist = playlist if playlist is not None else [FeedItem("p1", "Pl 1", "UCx", "C")]
        self.calls: list[str] = []

    def connected(self) -> bool:
        return self._connected

    def subscriptions(self):
        self.calls.append("subscriptions")
        return list(self.subs)

    def liked(self):
        self.calls.append("liked")
        return list(self.liked_items)

    def playlist_items(self):
        self.calls.append("playlist_items")
        return list(self.playlist)


def signed_in_cookies(tmp_path) -> str:
    path = tmp_path / "cookies.txt"
    expiry = int(time.time()) + 86400
    path.write_text("# Netscape HTTP Cookie File\n"
                    f".youtube.com\tTRUE\t/\tTRUE\t{expiry}\t__Secure-3PSID\tfake\n")
    return str(path)


def test_oauth_import_records_kinds_and_skips_cookie_feeds_without_cookies(db, tmp_path):
    yt = FakeYt(feeds={"history": ["h1"]})
    account = FakeAccount()
    counts = import_account(db, ytdlp=yt, data_api=None, claude=FakeClaude([TASTE_JSON]),
                            account=account, cookies_path=str(tmp_path / "none.txt"))
    assert account.calls == ["subscriptions", "liked", "playlist_items"]
    assert yt.feed_calls == []  # no cookies: yt-dlp feeds skipped
    assert [r["channel_id"] for r in imported(db, "subscription")] == ["UCa"]
    assert [r["video_id"] for r in imported(db, "liked")] == ["o1"]
    assert [r["video_id"] for r in imported(db, "playlist")] == ["p1"]
    assert counts == {"channels": 1, "liked": 1, "playlists": 1, "history": 0,
                      "recommended": 0, "watch_later": 0, "videos": 2, "taste_seeded": 1}
    messages = [r[0] for r in db.execute(
        "SELECT message FROM status_events WHERE component = 'import' ORDER BY id")]
    assert messages[0] == "cookie feeds skipped: cookies missing"
    assert messages[-1] == ("ok (oauth): 1 channels, 1 liked, 1 playlist videos, "
                            "0 history, 0 recommended, 0 watch_later, 2 new videos")
    assert latest_event(db, "ytdlp") is None


def test_oauth_import_runs_cookie_feeds_when_cookies_ok(db, tmp_path):
    yt = FakeYt(feeds={"history": ["h1"], "recommended": ["r1"], "watch_later": ["w1"],
                       "liked": ["ignored"], "subscriptions": ["ignored2"]})
    counts = import_account(db, ytdlp=yt, data_api=None, claude=FakeClaude([TASTE_JSON]),
                            account=FakeAccount(), cookies_path=signed_in_cookies(tmp_path))
    assert [k for k, _ in yt.feed_calls] == ["history", "recommended", "watch_later"]
    assert [r["video_id"] for r in imported(db, "playlist")] == ["p1", "w1"]
    assert [r["video_id"] for r in imported(db, "history")] == ["h1"]
    assert counts["history"] == counts["recommended"] == counts["watch_later"] == 1
    assert counts["videos"] == 5
    assert latest_event(db, "ytdlp")[:2] == ("info", "ok: account import")


def test_oauth_import_cookie_auth_error_does_not_fail_import(db, tmp_path):
    yt = FakeYt(feed_errors={"history": SourceAuthError("sign in")})
    counts = import_account(db, ytdlp=yt, data_api=None, claude=FakeClaude([TASTE_JSON]),
                            account=FakeAccount(), cookies_path=signed_in_cookies(tmp_path))
    assert counts["liked"] == 1
    assert latest_event(db, "ytdlp")[0] == "error"
    assert latest_event(db, "import")[1].startswith("ok (oauth):")


def test_oauth_import_dedupes_against_cookie_era_rows(db, tmp_path):
    from src.store.settings import record_import

    record_import(db, "liked", video_id="o1", title="Liked 1")
    record_import(db, "subscription", channel_id="UCa", title="Sub A")
    import_account(db, ytdlp=FakeYt(), data_api=None, claude=FakeClaude([TASTE_JSON]),
                   account=FakeAccount(), cookies_path=str(tmp_path / "none.txt"))
    assert len(imported(db, "liked")) == 1
    assert len(imported(db, "subscription")) == 1


def test_first_oauth_import_refreshes_an_existing_profile(db, tmp_path):
    from src.store.agent import save_taste

    save_taste(db, "cookie-era profile", ["x"], [], "agent")
    refreshed = json.dumps({"summary": "From OAuth", "likes": ["y"], "dislikes": []})
    claude = FakeClaude([refreshed])
    counts = import_account(db, ytdlp=FakeYt(), data_api=None, claude=claude,
                            account=FakeAccount(), cookies_path=str(tmp_path / "none.txt"))
    assert counts["taste_seeded"] == 1
    assert latest_taste(db).summary == "From OAuth"
    again = import_account(db, ytdlp=FakeYt(), data_api=None, claude=FakeClaude([]),
                           account=FakeAccount(), cookies_path=str(tmp_path / "none.txt"))
    assert again["taste_seeded"] == 0  # only the first OAuth import refreshes


def test_disconnected_account_uses_the_cookie_path(db, tmp_path):
    yt = FakeYt(feeds={"liked": ["l1"]})
    account = FakeAccount(connected=False)
    counts = import_account(db, ytdlp=yt, data_api=None, claude=FakeClaude([TASTE_JSON]),
                            account=account, cookies_path=str(tmp_path / "none.txt"))
    assert account.calls == []
    assert counts["channels"] == 2 and "playlists" not in counts


def test_oauth_import_error_is_logged_and_raised(db, tmp_path):
    from src.sources.youtube_oauth import OAuthError

    class Broken(FakeAccount):
        def liked(self):
            raise OAuthError("token refresh failed: unreachable")

    with pytest.raises(OAuthError):
        import_account(db, ytdlp=FakeYt(), data_api=None, claude=FakeClaude([]),
                       account=Broken(), cookies_path=str(tmp_path / "none.txt"))
    assert latest_event(db, "import")[:2] == ("error",
                                              "OAuthError: token refresh failed: unreachable")


def test_build_runtime_wires_youtube_oauth(tmp_path):
    config = AppConfig(db_path=str(tmp_path / "w.db"), cookies_path=str(tmp_path / "c.txt"),
                       youtube_oauth_token_path=str(tmp_path / "secrets" / "o.json"))
    runtime = build_runtime(config, _factories(FakeYt(), Recorder(None)))
    assert runtime.deps.youtube_oauth.state() == "unconfigured"
    configured = AppConfig(db_path=str(tmp_path / "w.db"), cookies_path=str(tmp_path / "c.txt"),
                           youtube_oauth_client_id="fake-id", youtube_oauth_client_secret="fake",
                           youtube_oauth_token_path=str(tmp_path / "secrets" / "o.json"))
    runtime = build_runtime(configured, _factories(FakeYt(), Recorder(None)))
    assert runtime.deps.youtube_oauth.state() == "disconnected"
    client = TestClient(create_app(runtime.deps))
    assert client.get("/api/status").json()["data"]["youtube_account"] == "disconnected"
