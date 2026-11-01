"""mpv wrapper: the `Mpv` protocol and the JSON-IPC implementation."""

from __future__ import annotations

import json
import logging
import queue
import subprocess
import threading
import time
from itertools import count
from pathlib import Path
from typing import Any, Protocol

logger = logging.getLogger(__name__)

CONNECT_TIMEOUT_S = 5.0
REPLY_TIMEOUT_S = 5.0


class MpvDied(Exception):
    """The mpv process or its socket is gone."""


class MpvCommandError(Exception):
    """mpv answered a command with an error."""


class Mpv(Protocol):
    def load(self, url: str) -> None: ...

    def stop(self) -> None: ...

    def set_pause(self, paused: bool) -> None: ...

    def seek_absolute(self, seconds: float) -> None: ...

    def position(self) -> float | None: ...

    def duration(self) -> float | None: ...

    def paused(self) -> bool: ...

    def wait_event(self, timeout: float) -> dict | None: ...


class MpvIpc:
    """Runs `mpv --idle` and talks newline-delimited JSON over its Unix socket."""

    def __init__(self, socket_path: Path, cookies_path: Path, mpv_bin: str = "mpv") -> None:
        self._socket_path = Path(socket_path)
        self._ids = count(1)
        self._send_lock = threading.Lock()
        self._pending: dict[int, queue.Queue] = {}
        self._events: queue.Queue = queue.Queue()
        self._dead = False
        self._closed = False
        self._socket_path.unlink(missing_ok=True)
        self._proc = subprocess.Popen(
            [
                mpv_bin,
                "--idle=yes",
                "--no-video",
                "--no-terminal",
                f"--input-ipc-server={self._socket_path}",
                "--ytdl-format=bestaudio",
                f"--ytdl-raw-options=cookies={cookies_path}",
            ]
        )
        self._sock = self._connect()
        threading.Thread(target=self._read_loop, daemon=True, name="mpv-ipc-reader").start()

    def _connect(self):
        from socket import AF_UNIX, SOCK_STREAM, socket  # AF_UNIX is missing on Windows

        deadline = time.monotonic() + CONNECT_TIMEOUT_S
        while True:
            sock = socket(AF_UNIX, SOCK_STREAM)
            try:
                sock.connect(str(self._socket_path))
                return sock
            except OSError:
                sock.close()
                if time.monotonic() >= deadline or self._proc.poll() is not None:
                    raise
                time.sleep(0.1)

    def _read_loop(self) -> None:
        """Route replies to their waiting request and everything else to the event queue."""
        try:
            for line in self._sock.makefile("r", encoding="utf-8"):
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(msg, dict):
                    continue
                waiter = self._pending.pop(msg["request_id"], None) if "request_id" in msg else None
                if waiter is not None:
                    waiter.put(msg)
                elif "event" in msg:
                    self._events.put(msg)
        except OSError:
            logger.warning("mpv socket closed")
        finally:
            self._mark_dead()

    def _mark_dead(self) -> None:
        """Fail every waiting command now and tell the engine through a sentinel event."""
        self._dead = True
        for request_id in list(self._pending):
            waiter = self._pending.pop(request_id, None)
            if waiter is not None:
                waiter.put(MpvDied("mpv exited"))
        self._events.put({"event": "mpv-died"})

    def _command(self, *args: Any, check: bool = True) -> dict:
        if self._dead:
            raise MpvDied("mpv exited")
        request_id = next(self._ids)
        waiter: queue.Queue = queue.Queue(maxsize=1)
        self._pending[request_id] = waiter
        if self._dead:  # the reader died between the check and registering
            self._pending.pop(request_id, None)
            raise MpvDied("mpv exited")
        payload = json.dumps({"command": list(args), "request_id": request_id}) + "\n"
        try:
            with self._send_lock:
                self._sock.sendall(payload.encode("utf-8"))
            reply = waiter.get(timeout=REPLY_TIMEOUT_S)
        except OSError as exc:
            self._pending.pop(request_id, None)
            raise MpvDied(str(exc)) from exc
        except queue.Empty:
            self._pending.pop(request_id, None)
            raise TimeoutError(f"mpv did not answer {args[0]}") from None
        if isinstance(reply, Exception):
            raise reply
        if check and reply.get("error") != "success":
            raise MpvCommandError(f"{args[0]} failed: {reply.get('error')}")
        return reply

    def _property(self, name: str) -> Any:
        reply = self._command("get_property", name, check=False)
        return reply.get("data") if reply.get("error") == "success" else None

    def load(self, url: str) -> None:
        self._command("loadfile", url, "replace")

    def stop(self) -> None:
        self._command("stop")

    def set_pause(self, paused: bool) -> None:
        self._command("set_property", "pause", paused)

    def seek_absolute(self, seconds: float) -> None:
        self._command("seek", seconds, "absolute")

    def position(self) -> float | None:
        return self._property("time-pos")

    def duration(self) -> float | None:
        return self._property("duration")

    def paused(self) -> bool:
        return bool(self._property("pause"))

    def wait_event(self, timeout: float) -> dict | None:
        try:
            return self._events.get(timeout=timeout)
        except queue.Empty:
            return None

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._proc.terminate()
        try:
            self._sock.close()
        except OSError:
            pass
