"""Home Studio CLI: ``serve``, ``import-account`` and ``migrate-legacy``.

Run as ``python -m src <command>`` (or the ``home-studio`` script).
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
import threading
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any

from src.agent.claude_cli import ClaudeError, make_runner
from src.agent.request import AUTH_EVENT_MESSAGE, run_request
from src.agent.taste import refresh_taste, seed_taste_from_imports
from src.agent.worker import AgentWorker
from src.api.app import Deps, create_app
from src.bluetooth import BtMonitor
from src.config import load_config
from src.db import connect, migrate
from src.legacy import migrate_legacy
from src.models import AppConfig
from src.player.engine import PlaybackEngine
from src.player.mpv import MpvIpc
from src.player.supervisor import MpvSupervisor
from src.scheduler import MorningScheduler
from src.sources import catalog
from src.sources.data_api import DataApi
from src.sources.ytdlp import FeedItem, SourceAuthError, SourceError, YtDlp
from src.store.settings import imported, log_event, record_import

logger = logging.getLogger("home_studio")

IMPORT_FEEDS = ("liked", "history", "recommended", "subscriptions", "watch_later")
# yt-dlp feed -> account_imports.kind
IMPORT_KIND = {
    "liked": "liked",
    "history": "history",
    "recommended": "recommended",
    "subscriptions": "subscription",
    "watch_later": "playlist",
}
FEED_LIMIT = 200
MAX_IMPORT_VIDEOS = 300
JOIN_TIMEOUT_S = 5.0


# -- service construction ---------------------------------------------------------


def _default_mpv(config: AppConfig) -> Any:
    return MpvIpc(Path(config.mpv_socket), Path(config.cookies_path))


def _default_ytdlp(config: AppConfig) -> Any:
    return YtDlp(config.cookies_path)


def _default_data_api(config: AppConfig, db_factory: Callable[[], sqlite3.Connection]) -> Any:
    return DataApi(config.youtube_api_key, db_factory, ceiling=config.quota_ceiling)


def _default_claude(config: AppConfig) -> Any:
    return make_runner(config.claude_command, config.claude_timeout_s)


def _default_bt(config: AppConfig, db_factory: Callable[[], sqlite3.Connection]) -> Any:
    return BtMonitor(config.bt_speaker_mac, config.bt_adapter, db_factory=db_factory)


@dataclass(frozen=True)
class ServiceFactories:
    """How each external-facing component is built; tests swap in fakes."""

    mpv: Callable[[AppConfig], Any] = _default_mpv
    ytdlp: Callable[[AppConfig], Any] = _default_ytdlp
    data_api: Callable[[AppConfig, Callable[[], sqlite3.Connection]], Any] = _default_data_api
    claude: Callable[[AppConfig], Any] = _default_claude
    bt: Callable[[AppConfig, Callable[[], sqlite3.Connection]], Any] = _default_bt


@dataclass(frozen=True)
class Runtime:
    """Everything ``serve`` starts and stops, plus the API dependencies."""

    deps: Deps
    engine: PlaybackEngine
    supervisor: MpvSupervisor
    worker: AgentWorker
    monitor: Any
    scheduler: MorningScheduler


def chapters_hook(
    db_factory: Callable[[], sqlite3.Connection], ytdlp: Any
) -> Callable[[str], None]:
    """``on_before_play``: fill missing chapters on the hook's own connection."""

    def hook(video_id: str) -> None:
        with closing(db_factory()) as conn:
            catalog.ensure_chapters(conn, video_id, ytdlp)

    return hook


def _db_factory(config: AppConfig) -> Callable[[], sqlite3.Connection]:
    path = Path(config.db_path)
    with closing(connect(path)) as conn:
        migrate(conn)
    return lambda: connect(path)


def _sources(config: AppConfig, factories: ServiceFactories, db_factory) -> tuple[Any, Any]:
    """(data_api or None without an API key, ytdlp)."""
    data_api = factories.data_api(config, db_factory) if config.youtube_api_key else None
    if data_api is None:
        logger.info("no YouTube API key: searching and fetching with yt-dlp only")
    return data_api, factories.ytdlp(config)


def build_runtime(config: AppConfig, factories: ServiceFactories | None = None) -> Runtime:
    factories = factories or ServiceFactories()
    db_factory = _db_factory(config)
    data_api, ytdlp = _sources(config, factories, db_factory)
    claude = factories.claude(config)
    mpv = factories.mpv(config)
    engine = PlaybackEngine(db_factory, mpv, on_before_play=chapters_hook(db_factory, ytdlp))
    supervisor = MpvSupervisor(engine, mpv, lambda: factories.mpv(config), db_factory)
    run_fn = partial(run_request, claude=claude, data_api=data_api, ytdlp=ytdlp)
    worker = AgentWorker(
        db_factory, run_fn, engine, lambda conn: refresh_taste(conn, claude=claude)
    )
    monitor = factories.bt(config, db_factory)

    def daily_import() -> None:
        with closing(db_factory()) as conn:
            counts = import_account(conn, ytdlp=ytdlp, data_api=data_api, claude=claude)
        logger.info("daily account import: %s", counts)

    scheduler = MorningScheduler(
        db_factory,
        run_request_fn=partial(run_fn, source="morning"),
        engine=engine,
        tz=config.timezone,
        import_fn=daily_import,
    )
    deps = Deps(
        db_factory=db_factory,
        engine=engine,
        worker=worker,
        data_api=data_api,
        ytdlp=ytdlp,
        bt=monitor,
        scheduler_run_now=scheduler.run_now,
        config=config,
    )
    return Runtime(deps, engine, supervisor, worker, monitor, scheduler)


def build_services(config: AppConfig, factories: ServiceFactories | None = None) -> Deps:
    """Wire every component and return the API dependencies (nothing is started)."""
    return build_runtime(config, factories).deps


# -- account import ---------------------------------------------------------------


def _import_key(video_id: str | None, channel_id: str | None) -> str:
    return video_id or f"channel:{channel_id}"


def _record_new(conn: sqlite3.Connection, kind: str, items: list[FeedItem]) -> None:
    """Record feed items not already imported under ``kind``."""
    seen = {_import_key(r["video_id"], r["channel_id"]) for r in imported(conn, kind)}
    for item in items:
        key = _import_key(item.video_id or None, item.channel_id)
        if key in seen:
            continue
        seen.add(key)
        record_import(
            conn, kind, video_id=item.video_id or None, channel_id=item.channel_id,
            title=item.title,
        )


def _fetch_feed(ytdlp: Any, feed: str) -> list[FeedItem]:
    try:
        return ytdlp.account_feed(feed, limit=FEED_LIMIT)
    except SourceAuthError:
        raise
    except SourceError as exc:
        logger.warning("could not import %s: %s", feed, exc)
        return []


def _seed(conn: sqlite3.Connection, claude: Any) -> int:
    try:
        return int(seed_taste_from_imports(conn, claude=claude) is not None)
    except ClaudeError as exc:
        logger.warning("could not seed the taste profile: %s", exc)
        return 0


def import_account(conn: sqlite3.Connection, *, ytdlp: Any, data_api: Any, claude: Any) -> dict:
    """Pull the account's channels and feeds into ``account_imports``; returns counts.

    Then fetches metadata for up to 300 imported videos (yt-dlp paces its own calls)
    and seeds the taste profile if there is none. SourceAuthError is logged and raised.
    """
    try:
        return _import(conn, ytdlp, data_api, claude)
    except SourceAuthError:
        log_event(conn, "ytdlp", "error", AUTH_EVENT_MESSAGE)
        raise


def _import(conn: sqlite3.Connection, ytdlp: Any, data_api: Any, claude: Any) -> dict:
    channels = ytdlp.subscribed_channels()
    _record_new(conn, "subscription", channels)
    counts: dict[str, int] = {"channels": len(channels)}
    video_ids: list[str] = []
    for feed in IMPORT_FEEDS:
        items = _fetch_feed(ytdlp, feed)
        _record_new(conn, IMPORT_KIND[feed], items)
        counts[feed] = len(items)
        video_ids.extend(i.video_id for i in items if i.video_id)
    wanted = list(dict.fromkeys(video_ids))[:MAX_IMPORT_VIDEOS]
    counts["videos"] = len(catalog.ensure_videos(conn, wanted, data_api, ytdlp))
    counts["taste_seeded"] = _seed(conn, claude)
    return counts


# -- commands ---------------------------------------------------------------------


def _start_background(runtime: Runtime, stop: threading.Event) -> list[threading.Thread]:
    loops = (
        ("engine-loop", runtime.engine.run_forever),
        ("mpv-supervisor", runtime.supervisor.run_forever),
        ("morning-scheduler", runtime.scheduler.run_forever),
    )
    threads = [threading.Thread(target=fn, args=(stop,), name=n, daemon=True) for n, fn in loops]
    runtime.worker.start()
    runtime.monitor.start()
    for thread in threads:
        thread.start()
    return threads


def _stop_background(
    runtime: Runtime, stop: threading.Event, threads: list[threading.Thread]
) -> None:
    stop.set()
    for step in (runtime.worker.stop, runtime.monitor.stop):
        try:
            step()
        except Exception:  # noqa: BLE001 - keep shutting down
            logger.exception("shutdown step failed")
    for thread in threads:
        thread.join(timeout=JOIN_TIMEOUT_S)
    runtime.supervisor.close()


def _cmd_serve(config: AppConfig) -> int:
    import uvicorn

    runtime = build_runtime(config)
    stop = threading.Event()
    threads = _start_background(runtime, stop)
    try:
        uvicorn.run(create_app(runtime.deps), host=config.api_host, port=config.api_port)
    finally:
        _stop_background(runtime, stop, threads)
    return 0


def _cmd_import_account(config: AppConfig, factories: ServiceFactories | None = None) -> int:
    factories = factories or ServiceFactories()
    db_factory = _db_factory(config)
    data_api, ytdlp = _sources(config, factories, db_factory)
    with closing(db_factory()) as conn:
        try:
            counts = import_account(
                conn, ytdlp=ytdlp, data_api=data_api, claude=factories.claude(config)
            )
        except SourceAuthError as exc:
            print(f"YouTube sign-in required (cookies expired?): {exc}", file=sys.stderr)
            return 1
    for name, value in counts.items():
        print(f"{name}: {value}")
    return 0


def _cmd_migrate_legacy(config: AppConfig, data_dir: str) -> int:
    with closing(_db_factory(config)()) as conn:
        result = migrate_legacy(conn, Path(data_dir))
    for name, value in result.items():
        print(f"{name}: {value}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="home-studio", description="Home Studio server")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    parser.add_argument("--config", default="config.yaml", help="path to config.yaml")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("serve", help="run the player, agent, scheduler and REST API")
    sub.add_parser("import-account", help="import the YouTube account's feeds and channels")
    legacy = sub.add_parser("migrate-legacy", help="import old feedback and interest profile")
    legacy.add_argument("--data-dir", default="data", help="directory with the legacy JSON files")
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    config = load_config(Path(args.config))
    if args.command == "serve":
        code = _cmd_serve(config)
    elif args.command == "import-account":
        code = _cmd_import_account(config)
    else:
        code = _cmd_migrate_legacy(config, args.data_dir)
    if code:
        sys.exit(code)


if __name__ == "__main__":
    main()
