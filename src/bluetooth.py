"""Keep the Bluetooth speaker connected over a chosen adapter (hci1)."""

from __future__ import annotations

import logging
import queue
import re
import sqlite3
import subprocess
import threading
import time
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from typing import Any, Literal

from src.store.settings import log_event

logger = logging.getLogger(__name__)

COMPONENT = "bluetooth"
TIMEOUT_S = 15
SYSFS_BLUETOOTH = Path("/sys/class/bluetooth")
_BD_ADDRESS = re.compile(r"BD Address:\s*([0-9A-Fa-f:]{17})")
_CONTROLLER = re.compile(r"^Controller\s+([0-9A-Fa-f:]{17})", re.MULTILINE)

_MAC = re.compile(r"^[0-9A-F]{2}(:[0-9A-F]{2}){5}$", re.IGNORECASE)
_SUCCESS = ("Connection successful",)
_FAILURE = ("Failed to connect", "not available")
_QUIT_WAIT_S = 5

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


def _sysfs_address(adapter: str, sysfs: Path) -> str | None:
    try:
        text = (Path(sysfs) / adapter / "address").read_text(encoding="ascii").strip()
    except OSError:
        return None
    return text.upper() if _MAC.match(text) else None


def _controller_address(
    adapter: str, run: Callable[..., Any], sysfs: Path = SYSFS_BLUETOOTH
) -> str | None:
    """Resolve an adapter name (hci1) to its controller address: hciconfig, else sysfs."""
    match = _BD_ADDRESS.search(_run(run, ["hciconfig", adapter]) or "")
    address = match.group(1).upper() if match else _sysfs_address(adapter, sysfs)
    if address is None:
        logger.warning("could not resolve adapter %s to a controller address", adapter)
        return None
    listing = _run(run, ["bluetoothctl", "list"])
    if listing is not None and address not in {m.upper() for m in _CONTROLLER.findall(listing)}:
        logger.warning("adapter %s (%s) not known to bluetoothctl", adapter, address)
        return None
    return address


def _validate_mac(mac: str) -> None:
    if not _MAC.match(mac):
        raise ValueError(f"invalid bluetooth MAC address: {mac!r}")


def _interactive_connect(
    address: str, mac: str, popen: Callable[..., Any], timeout_s: float
) -> bool:
    """Drive one bluetoothctl session until the asynchronous connect reports a result."""
    try:
        proc = popen(
            ["bluetoothctl"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
    except OSError as exc:
        logger.warning("bluetoothctl unavailable: %s", exc)
        return False
    lines: queue.Queue[str | None] = queue.Queue()

    def reader() -> None:
        try:
            for line in iter(proc.stdout.readline, ""):
                lines.put(line)
        except (OSError, ValueError):
            pass
        lines.put(None)

    threading.Thread(target=reader, name="bt-reader", daemon=True).start()
    result: bool | None = None
    try:
        proc.stdin.write(f"select {address}\nconnect {mac}\n")
        proc.stdin.flush()
        deadline = time.monotonic() + timeout_s
        while result is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                logger.warning("bluetooth connect timed out")
                break
            try:
                line = lines.get(timeout=remaining)
            except queue.Empty:
                continue
            if line is None:
                break
            if any(m in line for m in _SUCCESS):
                result = True
            elif any(m in line for m in _FAILURE):
                result = False
    except OSError as exc:
        logger.warning("bluetoothctl session failed: %s", exc)
    finally:
        _close_session(proc, graceful=result is not None)
    return bool(result)


def _close_session(proc: Any, *, graceful: bool) -> None:
    if graceful:
        try:
            proc.stdin.write("quit\n")
            proc.stdin.flush()
            proc.wait(timeout=_QUIT_WAIT_S)
            return
        except (OSError, subprocess.TimeoutExpired):
            pass
    try:
        proc.kill()
    except OSError:
        logger.exception("could not kill bluetoothctl")


def ensure_connected(
    mac: str,
    adapter: str = "hci1",
    run: Callable[..., Any] = subprocess.run,
    popen: Callable[..., Any] = subprocess.Popen,
    timeout_s: float = TIMEOUT_S,
    sysfs: Path = SYSFS_BLUETOOTH,
) -> bool:
    _validate_mac(mac)
    address = _controller_address(adapter, run, sysfs)
    if address is None:
        return False
    # `select` only lasts within one bluetoothctl session, and `connect` is asynchronous,
    # so keep one interactive session open until it reports the outcome.
    return _interactive_connect(address, mac, popen, timeout_s)


class BtMonitor:
    def __init__(
        self,
        mac: str,
        adapter: str = "hci1",
        interval_s: float = 30,
        db_factory: Callable[[], sqlite3.Connection] | None = None,
        run: Callable[..., Any] = subprocess.run,
        popen: Callable[..., Any] = subprocess.Popen,
        sysfs: Path = SYSFS_BLUETOOTH,
    ) -> None:
        if mac:
            _validate_mac(mac)
        self._popen = popen
        self._mac = mac
        self._adapter = adapter
        self._interval_s = interval_s
        self._db_factory = db_factory
        self._run = run
        self._sysfs = sysfs
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
            self._mac, self._adapter, self._run, self._popen, sysfs=self._sysfs
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
