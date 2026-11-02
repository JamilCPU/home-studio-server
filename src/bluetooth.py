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
CONNECT_TIMEOUT_S = 30

_MAC = re.compile(r"^[0-9A-F]{2}(:[0-9A-F]{2}){5}$", re.IGNORECASE)
_ADAPTER = re.compile(r"^hci\d+$")

State = Literal["connected", "disconnected", "unconfigured"]


def _busctl(
    run: Callable[..., Any], args: list[str], timeout_s: float = TIMEOUT_S
) -> Any | None:
    """Run busctl (D-Bus, no root); return the result, or None on timeout / OS error."""
    try:
        return run(["busctl", *args], capture_output=True, text=True, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        logger.warning("busctl timed out")
    except OSError as exc:  # includes FileNotFoundError
        logger.warning("busctl unavailable: %s", exc)
    return None


def _validate_mac(mac: str) -> None:
    if not _MAC.match(mac):
        raise ValueError(f"invalid bluetooth MAC address: {mac!r}")


def _adapter_path(adapter: str) -> str:
    if not _ADAPTER.match(adapter):
        raise ValueError(f"invalid bluetooth adapter name: {adapter!r}")
    return f"/org/bluez/{adapter}"


def _device_path(mac: str, adapter: str) -> str:
    return f"{_adapter_path(adapter)}/dev_{mac.upper().replace(':', '_')}"


def is_connected(
    mac: str, adapter: str = "hci1", run: Callable[..., Any] = subprocess.run
) -> bool:
    """Device1.Connected on this adapter's copy of the device (a missing device is False)."""
    _validate_mac(mac)
    result = _busctl(run, ["get-property", "org.bluez", _device_path(mac, adapter),
                           "org.bluez.Device1", "Connected"])
    if result is None or result.returncode != 0:
        return False
    return (result.stdout or "").strip() == "b true"


def ensure_connected(
    mac: str, adapter: str = "hci1", run: Callable[..., Any] = subprocess.run
) -> bool:
    """Power the adapter on, ask BlueZ to connect over it, and report the outcome."""
    _validate_mac(mac)
    powered = _busctl(run, ["set-property", "org.bluez", _adapter_path(adapter),
                            "org.bluez.Adapter1", "Powered", "b", "true"])
    if powered is None or powered.returncode != 0:
        logger.warning("could not power on %s", adapter)
    # Connect blocks until BlueZ replies. The subprocess timeout sits above busctl's own
    # --timeout so busctl reports the failure first.
    connected = _busctl(run, ["--timeout=20", "call", "org.bluez", _device_path(mac, adapter),
                              "org.bluez.Device1", "Connect"], CONNECT_TIMEOUT_S)
    if connected is None or connected.returncode != 0:
        logger.warning("bluetooth connect failed on %s", adapter)
    return is_connected(mac, adapter, run)


class BtMonitor:
    def __init__(
        self,
        mac: str,
        adapter: str = "hci1",
        interval_s: float = 30,
        db_factory: Callable[[], sqlite3.Connection] | None = None,
        run: Callable[..., Any] = subprocess.run,
    ) -> None:
        if mac:
            _validate_mac(mac)
        _adapter_path(adapter)
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
        elif is_connected(
            self._mac, self._adapter, self._run
        ) or ensure_connected(self._mac, self._adapter, self._run):
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
