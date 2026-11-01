"""Restart mpv when it dies, with exponential backoff, and swap it into the engine."""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from collections.abc import Callable
from contextlib import closing
from typing import Any

from src.store.settings import log_event

logger = logging.getLogger(__name__)

COMPONENT = "player"
BASE_DELAY_S = 2.0
MAX_DELAY_S = 60.0
POLL_S = 0.5


class MpvSupervisor:
    """Watches the engine's mpv; a dead one is closed and replaced after a backoff.

    The delay starts at 2 s and doubles per restart up to 60 s. It drops back to
    2 s once an mpv has stayed up for 60 s.
    """

    def __init__(
        self,
        engine: Any,
        mpv: Any,
        mpv_factory: Callable[[], Any],
        db_factory: Callable[[], sqlite3.Connection],
        *,
        base_delay_s: float = BASE_DELAY_S,
        max_delay_s: float = MAX_DELAY_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._engine = engine
        self._mpv = mpv
        self._factory = mpv_factory
        self._db_factory = db_factory
        self._base = base_delay_s
        self._max = max_delay_s
        self._clock = clock
        self._delay = base_delay_s
        self._started_at = clock()
        self._next_attempt: float | None = None

    @property
    def mpv(self) -> Any:
        return self._mpv

    def step(self) -> bool:
        """One supervision check; True when a new mpv was swapped in."""
        now = self._clock()
        if not getattr(self._mpv, "dead", False):
            if self._delay > self._base and now - self._started_at >= self._max:
                self._delay = self._base
            return False
        if self._next_attempt is None:
            self._next_attempt = now + self._delay
            logger.warning("mpv is dead; restarting in %.0f s", self._delay)
            return False
        if now < self._next_attempt:
            return False
        self._next_attempt = None
        delay, self._delay = self._delay, min(self._delay * 2, self._max)
        return self._restart(delay)

    def _restart(self, waited: float) -> bool:
        self._close(self._mpv)
        try:
            new = self._factory()
        except Exception as exc:  # noqa: BLE001 - keep supervising
            logger.exception("mpv restart failed")
            self._log("error", f"mpv restart failed: {type(exc).__name__}: {exc}"[:300])
            return False
        self._engine.replace_mpv(new)
        self._mpv = new
        self._started_at = self._clock()
        logger.info("mpv restarted after %.0f s", waited)
        self._log("info", "mpv restarted")
        return True

    def run_forever(self, stop_event: threading.Event, poll_s: float = POLL_S) -> None:
        while not stop_event.is_set():
            try:
                self.step()
            except Exception:  # noqa: BLE001
                logger.exception("mpv supervisor step failed")
            stop_event.wait(poll_s)

    def close(self) -> None:
        self._close(self._mpv)

    @staticmethod
    def _close(mpv: Any) -> None:
        close = getattr(mpv, "close", None)
        if close is None:
            return
        try:
            close()
        except Exception:  # noqa: BLE001
            logger.exception("could not close mpv")

    def _log(self, level: str, message: str) -> None:
        try:
            with closing(self._db_factory()) as conn:
                log_event(conn, COMPONENT, level, message)
        except sqlite3.Error:
            logger.exception("could not log player event")
