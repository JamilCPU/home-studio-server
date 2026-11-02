"""Queue-driven playback engine: drives mpv, logs every play, handles transport controls."""

from __future__ import annotations

import logging
import sqlite3
import threading
from contextlib import closing
from dataclasses import dataclass, replace
from typing import Callable, Literal

from src.player.mpv import Mpv, MpvDied
from src.store import queue
from src.store.queue import QueueItem
from src.store.settings import log_event
from src.store.videos import close_open_plays, end_play, get_video, start_play

logger = logging.getLogger(__name__)

COMPONENT = "player"
RESTART_THRESHOLD_S = 5.0
MAX_CONSECUTIVE_ERRORS = 3
BROKEN_MESSAGE = f"SourceBroken: {MAX_CONSECUTIVE_ERRORS} consecutive playback errors"
OK_MESSAGE = "ok: playing"
WATCH_URL = "https://www.youtube.com/watch?v={}"


@dataclass(frozen=True)
class NowPlaying:
    state: Literal["playing", "paused", "idle"]
    item: QueueItem | None
    position_s: float
    duration_s: float | None


class PlaybackEngine:
    def __init__(
        self,
        db_factory: Callable[[], sqlite3.Connection],
        mpv: Mpv,
        on_started: Callable[[str], None] | None = None,
    ) -> None:
        self._db_factory = db_factory
        self._mpv = mpv
        self._on_started = on_started
        self._lock = threading.Lock()
        self._item: QueueItem | None = None
        self._play_id: int | None = None
        self._errors = 0  # consecutive playback errors / load failures
        self._log_ok = True  # log "ok: playing" on the next successful start
        self._hook_thread: threading.Thread | None = None

    # -- public API ---------------------------------------------------------------

    def recover(self) -> None:
        """After a restart: 'playing' items go back to the queue front, open plays end."""
        with self._lock, closing(self._db_factory()) as conn:
            if self._item is not None:
                return  # this engine is already playing: nothing was left over
            requeued = queue.requeue_playing(conn)
            closed = close_open_plays(conn)
        if requeued or closed:
            logger.info("recovered %d playing item(s), closed %d open play(s)", requeued, closed)

    def play_next(self) -> QueueItem | None:
        """Start the next queued item, ending whatever is playing as skipped."""
        with self._lock, closing(self._db_factory()) as conn:
            self._end_current(conn, "skipped")
            return self._advance(conn)

    def next(self) -> QueueItem | None:
        return self.play_next()

    def play_item(self, item_id: int) -> QueueItem | None:
        """Jump to queued item `item_id`, ending whatever is playing as skipped.

        Raises LookupError (changing nothing) if that item is not in the queue."""
        with self._lock, closing(self._db_factory()) as conn:
            if item_id not in {i.id for i in queue.upcoming(conn)}:
                raise LookupError(f"queue item {item_id} not queued")
            self._end_current(conn, "skipped")
            return self._play_item(conn, queue.take(conn, item_id))

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
            return self._play_item(conn, item)

    def toggle_pause(self) -> None:
        """Pause or resume; when idle, start the queue (does nothing if it is empty)."""
        with self._lock:
            if self._item is not None:
                self._mpv.set_pause(not self._mpv.paused())
                return
            with closing(self._db_factory()) as conn:
                if queue.upcoming(conn):
                    self._advance(conn)

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

    def replace_mpv(self, mpv: Mpv) -> None:
        """Swap in a fresh mpv (after a restart); a current item goes back to the queue."""
        with self._lock, closing(self._db_factory()) as conn:
            self._requeue_current(conn)
            self._mpv = mpv

    def handle_event(self, evt: dict) -> None:
        event = evt.get("event")
        if event == "mpv-died":
            self._handle_died()
        elif event == "file-loaded":
            self._handle_loaded()
        elif event == "end-file":
            # stop/redirect/quit come from our own actions (replacing or stopping a load).
            if evt.get("reason") in ("eof", "error"):
                self._handle_end(evt["reason"])

    def _handle_loaded(self) -> None:
        """mpv resolved and opened the stream: playback really started."""
        with self._lock:
            if self._item is None:
                return
            self._errors = 0
            if not self._log_ok:
                return
            self._log_ok = False
            with closing(self._db_factory()) as conn:
                log_event(conn, COMPONENT, "info", OK_MESSAGE)

    def _handle_end(self, reason: str) -> None:
        with self._lock, closing(self._db_factory()) as conn:
            if self._item is None:
                return
            if reason == "eof":
                self._errors = 0
                self._end_current(conn, "finished", self._duration() or self._position())
                self._advance(conn)
                return
            item = self._item
            self._end_current(conn, "error")
            log_event(conn, COMPONENT, "error", f"playback failed: {item.video.title}")
            logger.error("mpv reported a playback error for %s", item.video.title)
            if self._count_error(conn, item):
                self._mpv.stop()
                return
            self._advance(conn)

    def _count_error(self, conn: sqlite3.Connection, item: QueueItem) -> bool:
        """Count a failure; at the limit put ``item`` back at the front and report broken."""
        self._errors += 1
        self._log_ok = True
        if self._errors < MAX_CONSECUTIVE_ERRORS:
            return False
        queue.mark(conn, item.id, "queued")  # it was popped first, so it stays first
        log_event(conn, COMPONENT, "error", BROKEN_MESSAGE)
        logger.error("%s; stopped advancing", BROKEN_MESSAGE)
        return True

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

    def _handle_died(self) -> None:
        """mpv is gone: record the error, keep the item queued, go idle (no restart here)."""
        with self._lock, closing(self._db_factory()) as conn:
            self._requeue_current(conn)
            log_event(conn, COMPONENT, "error", "mpv process died")
            logger.error("mpv process died")

    def _requeue_current(self, conn: sqlite3.Connection) -> None:
        """End the current play as an error and put its item back in the queue."""
        item = self._item
        self._end_current(conn, "error", 0.0)
        if item is not None:
            queue.mark(conn, item.id, "queued")

    def _advance(self, conn: sqlite3.Connection) -> QueueItem | None:
        return self._play_item(conn, queue.pop_next(conn))

    def _play_item(self, conn: sqlite3.Connection, item: QueueItem | None) -> QueueItem | None:
        """Play `item`; a track that fails to load is logged as an error and the next is tried,
        until MAX_CONSECUTIVE_ERRORS failures in a row stop the advance."""
        while item is not None:
            status = self._start(conn, item)
            if status == "ok":
                return item
            if status == "died":
                return None  # no stop() on a dead mpv
            if self._count_error(conn, item):
                break
            item = queue.pop_next(conn)
        self._mpv.stop()
        return None

    def _start(self, conn: sqlite3.Connection, item: QueueItem) -> str:
        """Load `item` into mpv; engine state is only set once the load succeeded."""
        play_id = start_play(conn, item.video.video_id, item.id)
        try:
            self._mpv.load(WATCH_URL.format(item.video.video_id))
            self._mpv.set_pause(False)  # mpv keeps `pause` across loadfile
        except Exception as exc:
            died = isinstance(exc, MpvDied)
            end_play(conn, play_id, 0.0, "error")
            if died:
                queue.mark(conn, item.id, "queued")
            log_event(conn, COMPONENT, "error", f"load failed: {item.video.title}: {exc}")
            logger.error("could not load %s: %s", item.video.video_id, exc)
            return "died" if died else "failed"
        self._play_id = play_id
        self._item = item
        self._run_started_hook(item)
        return "ok"

    def _run_started_hook(self, item: QueueItem) -> None:
        """Run ``on_started`` on its own thread (never under the lock), then refresh the item."""
        if self._on_started is None:
            return
        thread = threading.Thread(
            target=self._started_hook, args=(item,), name="on-started", daemon=True
        )
        self._hook_thread = thread
        thread.start()

    def _started_hook(self, item: QueueItem) -> None:
        try:
            self._on_started(item.video.video_id)  # type: ignore[misc]
            with closing(self._db_factory()) as conn:
                video = get_video(conn, item.video.video_id)
        except Exception:
            logger.exception("on_started hook failed for %s", item.video.video_id)
            return
        if video is None:
            return
        with self._lock:
            if self._item is not None and self._item.id == item.id:
                self._item = replace(self._item, video=video)
