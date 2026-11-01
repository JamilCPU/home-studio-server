"""Queue-driven playback engine: drives mpv, logs every play, handles transport controls."""

from __future__ import annotations

import logging
import sqlite3
import threading
from contextlib import closing
from dataclasses import dataclass
from typing import Callable, Literal

from src.player.mpv import Mpv
from src.store import queue
from src.store.queue import QueueItem
from src.store.settings import log_event
from src.store.videos import end_play, start_play

logger = logging.getLogger(__name__)

COMPONENT = "player"
RESTART_THRESHOLD_S = 5.0
WATCH_URL = "https://www.youtube.com/watch?v={}"


@dataclass(frozen=True)
class NowPlaying:
    state: Literal["playing", "paused", "idle"]
    item: QueueItem | None
    position_s: float
    duration_s: float | None


class PlaybackEngine:
    def __init__(self, db_factory: Callable[[], sqlite3.Connection], mpv: Mpv) -> None:
        self._db_factory = db_factory
        self._mpv = mpv
        self._lock = threading.Lock()
        self._item: QueueItem | None = None
        self._play_id: int | None = None

    # -- public API ---------------------------------------------------------------

    def play_next(self) -> QueueItem | None:
        """Start the next queued item, ending whatever is playing as skipped."""
        with self._lock, closing(self._db_factory()) as conn:
            self._end_current(conn, "skipped")
            return self._advance(conn)

    def next(self) -> QueueItem | None:
        return self.play_next()

    def previous(self) -> QueueItem | None:
        """Restart the current item if past 5 s, otherwise go back to the previous one."""
        with self._lock, closing(self._db_factory()) as conn:
            if self._item is not None and self._position() > RESTART_THRESHOLD_S:
                self._mpv.seek_absolute(0)
                return self._item
            seconds = self._position()
            item = queue.previous(conn)
            if item is None:
                return self._item
            self._end_current(conn, "skipped", seconds)
            self._start(conn, item)
            return item

    def toggle_pause(self) -> None:
        with self._lock:
            if self._item is not None:
                self._mpv.set_pause(not self._mpv.paused())

    def seek(self, position_s: float) -> None:
        with self._lock:
            self._seek_clamped(position_s)

    def skip(self, delta_s: float) -> None:
        with self._lock:
            self._seek_clamped(self._position() + delta_s)

    def stop(self) -> None:
        """Stop playback; the current item goes back to the front of the queue."""
        with self._lock, closing(self._db_factory()) as conn:
            item = self._item
            self._end_current(conn, "stopped")
            if item is not None:
                queue.mark(conn, item.id, "queued")
            self._mpv.stop()

    def now(self) -> NowPlaying:
        with self._lock:
            if self._item is None:
                return NowPlaying("idle", None, 0.0, None)
            state = "paused" if self._mpv.paused() else "playing"
            return NowPlaying(state, self._item, self._position(), self._duration())

    def handle_event(self, evt: dict) -> None:
        if evt.get("event") != "end-file":
            return
        reason = evt.get("reason")
        # stop/redirect/quit come from our own actions (replacing or stopping a load).
        if reason not in ("eof", "error"):
            return
        with self._lock, closing(self._db_factory()) as conn:
            if self._item is None:
                return
            if reason == "eof":
                self._end_current(conn, "finished", self._duration() or self._position())
            else:
                title = self._item.video.title
                self._end_current(conn, "error")
                log_event(conn, COMPONENT, "error", f"playback failed: {title}")
                logger.error("mpv reported a playback error for %s", title)
            self._advance(conn)

    def run_forever(self, stop_event: threading.Event) -> None:
        while not stop_event.is_set():
            evt = self._mpv.wait_event(1.0)
            if evt is None:
                continue
            try:
                self.handle_event(evt)
            except Exception:
                logger.exception("failed to handle mpv event %r", evt)

    # -- internals (call with the lock held) -------------------------------------

    def _position(self) -> float:
        return self._mpv.position() or 0.0

    def _duration(self) -> float | None:
        return self._mpv.duration() or (self._item.video.duration_s if self._item else None)

    def _seek_clamped(self, target: float) -> None:
        if self._item is None:
            return
        duration = self._duration()
        upper = max(duration - 1, 0) if duration is not None else target
        self._mpv.seek_absolute(min(max(target, 0), upper))

    def _end_current(
        self, conn: sqlite3.Connection, reason: str, seconds: float | None = None
    ) -> None:
        if self._play_id is not None:
            end_play(conn, self._play_id, self._position() if seconds is None else seconds, reason)
        self._play_id = None
        self._item = None

    def _advance(self, conn: sqlite3.Connection) -> QueueItem | None:
        item = queue.pop_next(conn)
        if item is None:
            self._mpv.stop()
            return None
        self._start(conn, item)
        return item

    def _start(self, conn: sqlite3.Connection, item: QueueItem) -> None:
        self._play_id = start_play(conn, item.video.video_id, item.id)
        self._item = item
        self._mpv.load(WATCH_URL.format(item.video.video_id))
