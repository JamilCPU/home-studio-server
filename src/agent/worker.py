"""Background worker that runs agent requests one at a time."""

from __future__ import annotations

import logging
import queue
import sqlite3
import threading
import time
from collections.abc import Callable
from contextlib import closing
from typing import Any

from src.agent.taste import should_refresh
from src.store.agent import get_request, set_status
from src.store.settings import log_event

logger = logging.getLogger(__name__)

COMPONENT = "agent"
_STOP = object()
_REFRESH = object()
_MAX_MESSAGE = 300
_JOIN_TIMEOUT_S = 10.0


def _describe(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:_MAX_MESSAGE]


class AgentWorker:
    def __init__(
        self,
        db_factory: Callable[[], sqlite3.Connection],
        run_request_fn: Callable[[sqlite3.Connection, int], list[int]],
        engine: Any,
        refresh_taste_fn: Callable[[sqlite3.Connection], Any],
        should_refresh_fn: Callable[[sqlite3.Connection], bool] = should_refresh,
    ) -> None:
        self._db_factory = db_factory
        self._run_request = run_request_fn
        self._engine = engine
        self._refresh_taste = refresh_taste_fn
        self._should_refresh = should_refresh_fn
        self._queue: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None

    # -- lifecycle ---------------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._reset_stale()
        self._thread = threading.Thread(target=self._loop, name="agent-worker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        thread = self._thread
        if thread is None:
            return
        self._queue.put(_STOP)
        thread.join(timeout=_JOIN_TIMEOUT_S)
        self._thread = None

    def submit(self, request_id: int) -> None:
        self._queue.put(request_id)

    def submit_taste_refresh(self) -> None:
        self._queue.put(_REFRESH)

    def join_idle(self, timeout: float) -> bool:
        """Wait until every queued job is finished. True if drained within timeout."""
        deadline = time.monotonic() + timeout
        with self._queue.all_tasks_done:
            while self._queue.unfinished_tasks:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._queue.all_tasks_done.wait(remaining)
        return True

    # -- internals ---------------------------------------------------------------

    def _reset_stale(self) -> None:
        with closing(self._db_factory()) as conn:
            rows = conn.execute("SELECT id FROM agent_requests WHERE status = 'running'").fetchall()
            for row in rows:
                set_status(conn, row[0], "failed", error="server restarted")

    def _loop(self) -> None:
        while True:
            job = self._queue.get()
            try:
                if job is _STOP:
                    return
                self._handle(job)
            except Exception:  # noqa: BLE001 - the worker thread must never die
                logger.exception("agent worker job failed")
            finally:
                self._queue.task_done()

    def _handle(self, job: object) -> None:
        with closing(self._db_factory()) as conn:
            if job is _REFRESH:
                self._maybe_refresh(conn, force=True)
                return
            self._run_one(conn, int(job))  # type: ignore[call-overload]
            self._maybe_refresh(conn, force=False)

    def _run_one(self, conn: sqlite3.Connection, request_id: int) -> None:
        set_status(conn, request_id, "running")
        try:
            ids = self._run_request(conn, request_id)
        except Exception as exc:  # noqa: BLE001
            logger.exception("agent request %s crashed", request_id)
            set_status(conn, request_id, "failed", error=_describe(exc))
            return
        request = get_request(conn, request_id)
        if ids and request is not None and request.play == "now":
            self._start_if_idle()

    def _start_if_idle(self) -> None:
        try:
            if self._engine.now().state == "idle":
                self._engine.play_next()
        except Exception:  # noqa: BLE001
            logger.exception("could not start playback after agent request")

    def _maybe_refresh(self, conn: sqlite3.Connection, *, force: bool) -> None:
        try:
            if force or self._should_refresh(conn):
                self._refresh_taste(conn)
        except Exception as exc:  # noqa: BLE001
            logger.exception("taste refresh failed")
            log_event(conn, COMPONENT, "error", _describe(exc))
