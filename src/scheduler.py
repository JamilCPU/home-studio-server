"""Morning scheduler: builds the wake-up set shortly before wake time and starts it."""

from __future__ import annotations

import logging
import sqlite3
import threading
from collections.abc import Callable
from contextlib import closing
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from src.agent.morning import COMPONENT, fallback_morning, morning_prompt
from src.store import queue
from src.store.agent import create_request
from src.store.settings import MorningSettings, get_morning, log_event

logger = logging.getLogger(__name__)

BUILD_LEAD = timedelta(minutes=5)
BUILD_WINDOW = timedelta(minutes=3)
START_WINDOW = timedelta(minutes=10)
TICK_SECONDS = 20
MORNING_SOURCES = ("morning", "fallback")


def event_logged(conn: sqlite3.Connection, message: str) -> bool:
    """True if a scheduler status event with exactly this message exists."""
    row = conn.execute(
        "SELECT 1 FROM status_events WHERE component = ? AND message = ? LIMIT 1",
        (COMPONENT, message),
    ).fetchone()
    return row is not None


class MorningScheduler:
    def __init__(
        self,
        db_factory: Callable[[], sqlite3.Connection],
        *,
        run_request_fn: Callable[[sqlite3.Connection, int], list[int]],
        engine,
        tz: str = "America/Chicago",
        clock: Callable[..., datetime] = datetime.now,
    ) -> None:
        self._db_factory = db_factory
        self._run_request = run_request_fn
        self._engine = engine
        self._tz = ZoneInfo(tz)
        self._clock = clock
        self._last_thread: threading.Thread | None = None

    # -- scheduled ticks ----------------------------------------------------------

    def tick(self, now: datetime) -> list[str]:
        """Do whatever is due at ``now`` (aware datetime); returns the actions taken."""
        local = now.astimezone(self._tz).replace(tzinfo=None)
        with closing(self._db_factory()) as conn:
            settings = get_morning(conn)
            if not settings.enabled or local.weekday() not in settings.days:
                return []
            hour, minute = (int(p) for p in settings.wake_time.split(":"))
            wake = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
            build_at = wake - BUILD_LEAD
            actions: list[str] = []
            if build_at <= local < build_at + BUILD_WINDOW:
                actions.extend(self._maybe_build(conn, settings, local.date()))
            if wake <= local < wake + START_WINDOW:
                actions.extend(self._maybe_start(conn, local.date()))
            return actions

    def _maybe_build(
        self, conn: sqlite3.Connection, settings: MorningSettings, day: date
    ) -> list[str]:
        message = f"built:{day.isoformat()}"
        if event_logged(conn, message):
            return []
        log_event(conn, COMPONENT, "info", message)
        request_id = create_request(conn, morning_prompt(settings), "morning", "end")
        return [self._build(conn, request_id, settings)]

    def _maybe_start(self, conn: sqlite3.Connection, day: date) -> list[str]:
        message = f"started:{day.isoformat()}"
        if event_logged(conn, message):
            return []
        log_event(conn, COMPONENT, "info", message)
        return ["started"] if self._start(conn) else []

    # -- shared steps -------------------------------------------------------------

    def _build(self, conn: sqlite3.Connection, request_id: int, settings: MorningSettings) -> str:
        """Clear stale items, run the agent, fall back if it yields nothing."""
        for item in queue.upcoming(conn):
            if item.source in MORNING_SOURCES:
                queue.remove(conn, item.id)
        try:
            ids = self._run_request(conn, request_id)
        except Exception:
            logger.exception("morning request %s raised", request_id)
            ids = []
        if ids:
            return "built"
        fallback_morning(conn, settings.count)
        return "fallback"

    def _start(self, conn: sqlite3.Connection) -> bool:
        """Put morning items first and play if idle; True when playback was started."""
        if self._engine.now().state != "idle":
            return False
        queued = queue.upcoming(conn)
        morning = [i.id for i in queued if i.source in MORNING_SOURCES]
        if not morning:
            return False
        rest = [i.id for i in queued if i.source not in MORNING_SOURCES]
        queue.reorder(conn, morning + rest)
        self._engine.play_next()
        return True

    # -- manual trigger -----------------------------------------------------------

    def run_now(self) -> int:
        """Build and start a morning set immediately; returns the request id."""
        with closing(self._db_factory()) as conn:
            settings = get_morning(conn)
            request_id = create_request(conn, morning_prompt(settings), "morning", "end")
        thread = threading.Thread(
            target=self._run_now_worker, args=(request_id, settings), daemon=True
        )
        self._last_thread = thread
        thread.start()
        return request_id

    def _run_now_worker(self, request_id: int, settings: MorningSettings) -> None:
        try:
            with closing(self._db_factory()) as conn:
                self._build(conn, request_id, settings)
                self._start(conn)
        except Exception:
            logger.exception("run_now failed")

    # -- loop ---------------------------------------------------------------------

    def run_forever(self, stop_event: threading.Event) -> None:
        while not stop_event.is_set():
            try:
                self.tick(self._clock().astimezone(self._tz))
            except Exception:
                logger.exception("scheduler tick failed")
            stop_event.wait(TICK_SECONDS)
