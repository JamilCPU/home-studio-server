"""Fakes and fixtures for the REST API tests (real migrated SQLite, fake everything else)."""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass, field

import pytest
from fastapi.testclient import TestClient

from src.api.app import Deps, create_app
from src.db import connect, migrate
from src.models import AppConfig
from src.player.engine import NowPlaying
from src.sources.data_api import QuotaExceeded
from src.store import queue
from src.store.videos import Chapter, Video, upsert_video


def make_video(video_id: str, duration_s: int | None = 300, **kw) -> Video:
    base = dict(
        video_id=video_id, title=f"Title {video_id}", channel_name="Some Channel",
        channel_id="UCx", description=None, duration_s=duration_s, published_at=None,
        tags=(), category=None, chapters=(), thumbnail_url=f"https://img/{video_id}.jpg",
        view_count=None, like_count=None, transcript=None,
    )
    base.update(kw)
    return Video(**base)


class FakeEngine:
    """Engine stand-in: plays from the real queue table, clamps seeks like the real one."""

    def __init__(self, db_factory) -> None:
        self._db_factory = db_factory
        self.calls: list[tuple] = []
        self.item = None
        self.paused = False
        self.position = 0.0
        self.raise_exc: Exception | None = None

    def _call(self, name: str, *args) -> None:
        self.calls.append((name, *args))
        if self.raise_exc is not None:
            raise self.raise_exc

    def _duration(self) -> float | None:
        return float(self.item.video.duration_s) if self.item and self.item.video.duration_s else None

    def play_next(self):
        self._call("play_next")
        with closing(self._db_factory()) as conn:
            self.item = queue.pop_next(conn)
        self.position = 0.0
        return self.item

    def next(self):
        return self.play_next()

    def previous(self):
        self._call("previous")
        return self.item

    def toggle_pause(self) -> None:
        self._call("toggle_pause")
        if self.item is not None:
            self.paused = not self.paused

    def seek(self, position_s: float) -> None:
        self._call("seek", position_s)
        if self.item is None:
            return
        duration = self._duration()
        upper = max(duration - 1, 0) if duration is not None else position_s
        self.position = min(max(position_s, 0), upper)

    def skip(self, delta_s: float) -> None:
        self._call("skip", delta_s)
        self.seek(self.position + delta_s)

    def stop(self) -> None:
        self._call("stop")
        self.item = None

    def now(self) -> NowPlaying:
        if self.raise_exc is not None:
            raise self.raise_exc
        if self.item is None:
            return NowPlaying("idle", None, 0.0, None)
        return NowPlaying("paused" if self.paused else "playing", self.item, self.position,
                          self._duration())


@dataclass
class FakeWorker:
    submitted: list[int] = field(default_factory=list)
    refreshes: int = 0

    def submit(self, request_id: int) -> None:
        self.submitted.append(request_id)

    def submit_taste_refresh(self) -> None:
        self.refreshes += 1


@dataclass
class FakeBt:
    last_state: str = "connected"


class FakeCatalog:
    """Videos the fake sources know about."""

    def __init__(self) -> None:
        self.videos: dict[str, Video] = {}

    def add(self, video: Video) -> Video:
        self.videos[video.video_id] = video
        return video


class FakeDataApi:
    def __init__(self, catalog: FakeCatalog) -> None:
        self._catalog = catalog
        self.search_results: list[str] = []
        self.quota_exceeded = False
        self.searches: list[str] = []

    def search(self, query: str, max_results: int = 25) -> list[str]:
        self.searches.append(query)
        if self.quota_exceeded:
            raise QuotaExceeded("over")
        return list(self.search_results)

    def video_details(self, ids: list[str]) -> list[Video]:
        if self.quota_exceeded:
            raise QuotaExceeded("over")
        return [self._catalog.videos[i] for i in ids if i in self._catalog.videos]


class FakeYtDlp:
    def __init__(self, catalog: FakeCatalog) -> None:
        self._catalog = catalog
        self.search_results: list[str] = []
        self.searches: list[str] = []
        self.search_exc: Exception | None = None

    def search(self, query: str, n: int = 20) -> list[str]:
        self.searches.append(query)
        if self.search_exc is not None:
            raise self.search_exc
        return list(self.search_results)

    def metadata(self, video_id: str) -> Video:
        from src.sources.ytdlp import SourceError

        if video_id not in self._catalog.videos:
            raise SourceError(f"no video {video_id}")
        return self._catalog.videos[video_id]


@dataclass
class Harness:
    client: TestClient
    deps: Deps
    engine: FakeEngine
    worker: FakeWorker
    bt: FakeBt
    data_api: FakeDataApi | None
    ytdlp: FakeYtDlp
    catalog: FakeCatalog
    run_now_calls: list[int]

    def conn(self):
        return closing(self.deps.db_factory())

    def stored(self, video: Video) -> Video:
        with self.conn() as conn:
            upsert_video(conn, video)
        return video


def _build(tmp_path, *, with_data_api: bool = True) -> Harness:
    path = tmp_path / "api.db"
    with closing(connect(path)) as conn:
        migrate(conn)

    def db_factory():
        return connect(path)

    catalog = FakeCatalog()
    engine = FakeEngine(db_factory)
    worker = FakeWorker()
    bt = FakeBt()
    data_api = FakeDataApi(catalog) if with_data_api else None
    ytdlp = FakeYtDlp(catalog)
    run_now_calls: list[int] = []

    def scheduler_run_now() -> int:
        run_now_calls.append(77)
        return 77

    config = AppConfig(cookies_path=str(tmp_path / "cookies.txt"), output_device_label="Echo")
    deps = Deps(
        db_factory=db_factory, engine=engine, worker=worker, data_api=data_api, ytdlp=ytdlp,
        bt=bt, scheduler_run_now=scheduler_run_now, config=config,
    )
    client = TestClient(create_app(deps), raise_server_exceptions=False)
    return Harness(client, deps, engine, worker, bt, data_api, ytdlp, catalog, run_now_calls)


@pytest.fixture
def api(tmp_path) -> Harness:
    return _build(tmp_path)


@pytest.fixture
def api_no_data_api(tmp_path) -> Harness:
    return _build(tmp_path, with_data_api=False)


@pytest.fixture
def chaptered_video() -> Video:
    return make_video("abcdefghijk", chapters=(Chapter("Intro", 0.0), Chapter("Main", 42.5)))
