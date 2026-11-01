"""Morning scheduler: builds the wake-up set shortly before wake time and starts it.

It also runs the daily account import at 04:00 local when an import function is given.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from collections.abc import Callable
from contextlib import closing
from datetime import date, datetime, time, timedelta
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
IMPORT_AT = time(4, 0)
IMPORT_WINDOW = timedelta(minutes=10)
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
        import_fn: Callable[[], object] | None = None,
    ) -> None:
        self._db_factory = db_factory
        self._run_request = run_request_fn
        self._engine = engine
        self._tz = ZoneInfo(tz)
        self._clock = clock
        self._last_thread: threading.Thread | None = None
        self._build_thread: threading.Thread | None = None
        self._import_fn = import_fn
        self._import_thread: threading.Thread | None = None
        self._fallback_day: date | None = None

    # -- scheduled ticks ----------------------------------------------------------

    def tick(self, now: datetime) -> list[str]:
        """Do whatever is due at ``now`` (aware datetime); returns the actions taken."""
        local = now.astimezone(self._tz).replace(tzinfo=None)
        with closing(self._db_factory()) as conn:
            actions = self._maybe_import(conn, local)
            settings = get_morning(conn)
            if not settings.enabled or local.weekday() not in settings.days:
                return actions
            hour, minute = (int(p) for p in settings.wake_time.split(":"))
            wake = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
            build_at = wake - BUILD_LEAD
            if build_at <= local < build_at + BUILD_WINDOW:
                actions.extend(self._maybe_build(conn, settings, local.date()))
            if wake <= local < wake + START_WINDOW:
                actions.extend(self._maybe_start(conn, settings, local.date()))
            return actions

    def _maybe_import(self, conn: sqlite3.Connection, local: datetime) -> list[str]:
        """Start the daily account import once per day, in the window after 04:00."""
        if self._import_fn is None:
            return []
        start = datetime.combine(local.date(), IMPORT_AT)
        if not start <= local < start + IMPORT_WINDOW:
            return []
        message = f"imported:{local.date().isoformat()}"
        if event_logged(conn, message) or self._import_running():
            return []
        log_event(conn, COMPONENT, "info", message)
        self._import_thread = threading.Thread(
            target=self._import_worker, name="account-import", daemon=True
        )
        self._import_thread.start()
        return ["imported"]

    def _import_running(self) -> bool:
        return self._import_thread is not None and self._import_thread.is_alive()

    def _import_worker(self) -> None:
        try:
            self._import_fn()  # type: ignore[misc]
        except Exception:
            logger.exception("daily account import failed")

    def _maybe_build(
        self, conn: sqlite3.Connection, settings: MorningSettings, day: date
    ) -> list[str]:
        message = f"built:{day.isoformat()}"
        if event_logged(conn, message) or self._build_running():
            return []
        log_event(conn, COMPONENT, "info", message)
        request_id = create_request(conn, morning_prompt(settings), "morning", "end")
        self._build_thread = threading.Thread(
            target=self._build_worker, args=(request_id, settings), daemon=True
        )
        self._build_thread.start()
        return ["built"]

    def _build_running(self) -> bool:
        threads = (self._build_thread, self._last_thread)
        return any(t is not None and t.is_alive() for t in threads)

    def _build_worker(self, request_id: int, settings: MorningSettings) -> None:
        try:
            with closing(self._db_factory()) as conn:
                self._build(conn, request_id, settings)
        except Exception:
            logger.exception("scheduled morning build failed")

    def _maybe_start(
        self, conn: sqlite3.Connection, settings: MorningSettings, day: date
    ) -> list[str]:
        """Start the morning once; ``started:<day>`` is logged only after it really
        started (or the listener is already playing), so later ticks retry a failure."""
        message = f"started:{day.isoformat()}"
        if event_logged(conn, message):
            return []
        outcome = self._start(conn, settings, day)
        if outcome == "retry":
            return []
        log_event(conn, COMPONENT, "info", message)
        return ["started"] if outcome == "played" else []

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
        self._safe_fallback(conn, settings)
        return "fallback"

    @staticmethod
    def _safe_fallback(conn: sqlite3.Connection, settings: MorningSettings) -> None:
        try:
            fallback_morning(conn, settings.count)
        except Exception:
            logger.exception("fallback morning failed")

    def _start(
        self, conn: sqlite3.Connection, settings: MorningSettings, day: date | None = None
    ) -> str:
        """Put morning items first and play them: "played", "busy" or "retry".

        Something actively playing is left alone ("busy"). A paused item is stopped,
        which puts it back in the queue behind the morning items. With nothing
        morning-sourced queued (build crashed or still running), the fallback set is
        queued first (once per day) so the morning is never silent.
        """
        state = self._engine.now().state
        if state == "playing":
            return "busy"
        if not self._has_morning(conn) and self._fallback_due(day):
            self._safe_fallback(conn, settings)
        if not self._has_morning(conn):
            return "retry"  # leave a paused item alone when there is nothing to play
        if state == "paused":
            self._engine.stop()
        queued = queue.upcoming(conn)
        morning = [i.id for i in queued if i.source in MORNING_SOURCES]
        rest = [i.id for i in queued if i.source not in MORNING_SOURCES]
        queue.reorder(conn, morning + rest)
        return "played" if self._engine.play_next() is not None else "retry"

    @staticmethod
    def _has_morning(conn: sqlite3.Connection) -> bool:
        return any(i.source in MORNING_SOURCES for i in queue.upcoming(conn))

    def _fallback_due(self, day: date | None) -> bool:
        """The start-time fallback runs once per day (always for a manual run)."""
        if day is None:
            return True
        if self._fallback_day == day:
            return False
        self._fallback_day = day
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
                self._start(conn, settings)
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
