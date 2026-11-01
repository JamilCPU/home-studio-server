"""Keep the Bluetooth speaker connected over a chosen adapter (hci1)."""

from __future__ import annotations

import logging
import re
import sqlite3
import subprocess
import threading
from collections.abc import Callable
from contextlib import closing
from typing import Any, Literal

from src.store.settings import log_event

logger = logging.getLogger(__name__)

COMPONENT = "bluetooth"
TIMEOUT_S = 15
_BD_ADDRESS = re.compile(r"BD Address:\s*([0-9A-Fa-f:]{17})")
_CONTROLLER = re.compile(r"^Controller\s+([0-9A-Fa-f:]{17})", re.MULTILINE)

State = Literal["connected", "disconnected", "unconfigured"]


def _run(run: Callable[..., Any], cmd: list[str], **kwargs: Any) -> str | None:
    """Run a command; return stdout, or None on timeout / missing binary / OS error."""
    try:
        result = run(cmd, capture_output=True, text=True, timeout=TIMEOUT_S, **kwargs)
    except subprocess.TimeoutExpired:
        logger.warning("%s timed out", cmd[0])
    except OSError as exc:  # includes FileNotFoundError
        logger.warning("%s unavailable: %s", cmd[0], exc)
    else:
        return result.stdout or ""
    return None


def is_connected(mac: str, run: Callable[..., Any] = subprocess.run) -> bool:
    out = _run(run, ["bluetoothctl", "info", mac])
    return out is not None and "Connected: yes" in out


def _controller_address(adapter: str, run: Callable[..., Any]) -> str | None:
    """Resolve an adapter name (hci1) to its controller address."""
    hci = _run(run, ["hciconfig", adapter])
    match = _BD_ADDRESS.search(hci or "")
    if match is None:
        return None
    address = match.group(1).upper()
    listing = _run(run, ["bluetoothctl", "list"])
    if listing is not None and address not in {m.upper() for m in _CONTROLLER.findall(listing)}:
        logger.warning("adapter %s (%s) not known to bluetoothctl", adapter, address)
        return None
    return address


def ensure_connected(
    mac: str, adapter: str = "hci1", run: Callable[..., Any] = subprocess.run
) -> bool:
    address = _controller_address(adapter, run)
    if address is None:
        return False
    # `select` only lasts within one bluetoothctl session, so send both commands together.
    out = _run(run, ["bluetoothctl"], input=f"select {address}\nconnect {mac}\nquit\n")
    if out is None:
        return False
    return "Connection successful" in out or is_connected(mac, run)


class BtMonitor:
    def __init__(
        self,
        mac: str,
        adapter: str = "hci1",
        interval_s: float = 30,
        db_factory: Callable[[], sqlite3.Connection] | None = None,
        run: Callable[..., Any] = subprocess.run,
    ) -> None:
        self._mac = mac
        self._adapter = adapter
        self._interval_s = interval_s
        self._db_factory = db_factory
        self._run = run
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_state: State = "unconfigured" if not mac else "disconnected"
        self._reported: State | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="bt-monitor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=TIMEOUT_S + 5)
            self._thread = None

    def check_once(self) -> State:
        if not self._mac:
            state: State = "unconfigured"
        elif is_connected(self._mac, self._run) or ensure_connected(
            self._mac, self._adapter, self._run
        ):
            state = "connected"
        else:
            state = "disconnected"
        self.last_state = state
        self._report(state)
        return state

    def _report(self, state: State) -> None:
        if state == self._reported:
            return
        self._reported = state
        if self._db_factory is None:
            return
        level = "info" if state == "connected" else "warn"
        try:
            with closing(self._db_factory()) as conn:
                log_event(conn, COMPONENT, level, f"bluetooth {state}")
        except sqlite3.Error:
            logger.exception("could not log bluetooth state")

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.check_once()
            except Exception:  # noqa: BLE001 - keep monitoring
                logger.exception("bluetooth check failed")
            self._stop.wait(self._interval_s)
