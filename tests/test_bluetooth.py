"""Tests for the Bluetooth connection helpers and monitor."""

from __future__ import annotations

import subprocess
import threading
from types import SimpleNamespace

import pytest

from src.bluetooth import BtMonitor, ensure_connected, is_connected
from src.db import connect, migrate
from src.store.settings import recent_events

MAC = "AA:BB:CC:DD:EE:FF"
CTRL = "11:22:33:44:55:66"


class FakeRun:
    """Scriptable subprocess.run stand-in for the non-interactive calls."""

    def __init__(self, connected=False, raises=None):
        self.connected = connected
        self.raises = raises
        self.calls = []

    def __call__(self, cmd, **kwargs):
        self.calls.append((cmd, kwargs))
        if self.raises:
            raise self.raises
        if cmd[:2] == ["bluetoothctl", "info"]:
            out = f"Device {MAC}\n\tConnected: {'yes' if self.connected else 'no'}\n"
        elif cmd == ["bluetoothctl", "list"]:
            out = f"Controller 99:99:99:99:99:99 builtin [default]\nController {CTRL} usb\n"
        elif cmd[0] == "hciconfig":
            out = f"hci1:\tType: Primary  Bus: USB\n\tBD Address: {CTRL}  ACL MTU: 1021\n"
        else:
            out = ""
        return SimpleNamespace(stdout=out, returncode=0)


class FakeStdout:
    """readline() yields scripted lines, then blocks (hang=True) or hits EOF."""

    def __init__(self, lines, hang, released):
        self._lines = list(lines)
        self._hang = hang
        self._released = released

    def readline(self):
        if self._lines:
            return self._lines.pop(0)
        if self._hang:
            self._released.wait(10)
        return ""


class FakePopen:
    """Callable factory standing in for subprocess.Popen."""

    def __init__(self, lines=(), hang=False):
        self.lines = lines
        self.hang = hang
        self.written = []
        self.killed = False
        self.released = threading.Event()
        self.args = None

    def __call__(self, cmd, **kwargs):
        self.args = (cmd, kwargs)
        self.stdout = FakeStdout(self.lines, self.hang, self.released)
        self.stdin = SimpleNamespace(write=self.written.append, flush=lambda: None)
        return self

    def wait(self, timeout=None):
        if self.hang:
            raise subprocess.TimeoutExpired("bluetoothctl", timeout)
        return 0

    def kill(self):
        self.killed = True
        self.released.set()


OK_LINES = ["Attempting to connect to AA\n", "Connection successful\n"]


@pytest.fixture
def factory(tmp_path):
    path = tmp_path / "bt.db"
    setup = connect(path)
    migrate(setup)
    setup.close()
    return lambda: connect(path)


def bt_events(factory):
    db = factory()
    try:
        return [e for e in recent_events(db) if e["component"] == "bluetooth"]
    finally:
        db.close()


def test_is_connected_parses_output():
    assert is_connected(MAC, FakeRun(connected=True)) is True
    assert is_connected(MAC, FakeRun(connected=False)) is False


def test_is_connected_false_on_timeout_or_missing_binary():
    assert is_connected(MAC, FakeRun(raises=subprocess.TimeoutExpired("x", 15))) is False
    assert is_connected(MAC, FakeRun(raises=FileNotFoundError())) is False


def test_subprocess_calls_use_capture_and_timeout():
    run = FakeRun()
    is_connected(MAC, run)
    kwargs = run.calls[0][1]
    assert kwargs["capture_output"] and kwargs["text"] and kwargs["timeout"] == 15


def test_ensure_connected_success_line_returns_true():
    popen = FakePopen(OK_LINES)
    run = FakeRun()
    assert ensure_connected(MAC, "hci1", run, popen) is True
    assert popen.args[0] == ["bluetoothctl"]
    assert "".join(popen.written) == f"select {CTRL}\nconnect {MAC}\nquit\n"
    assert any(c[0] == ["hciconfig", "hci1"] for c in run.calls)
    assert popen.killed is False


def test_ensure_connected_failure_line_returns_false():
    popen = FakePopen(["Failed to connect: org.bluez.Error.Failed\n"])
    assert ensure_connected(MAC, "hci1", FakeRun(), popen) is False


def test_ensure_connected_not_available_returns_false():
    popen = FakePopen(["Device AA:BB:CC:DD:EE:FF not available\n"])
    assert ensure_connected(MAC, "hci1", FakeRun(), popen) is False


def test_ensure_connected_deadline_kills_process():
    popen = FakePopen(["Attempting to connect\n"], hang=True)
    assert ensure_connected(MAC, "hci1", FakeRun(), popen, timeout_s=0.2) is False
    assert popen.killed is True


def test_ensure_connected_false_when_binary_missing():
    assert ensure_connected(MAC, "hci1", FakeRun(raises=FileNotFoundError())) is False


def test_ensure_connected_false_when_popen_missing():
    def no_popen(*a, **k):
        raise FileNotFoundError()

    assert ensure_connected(MAC, "hci1", FakeRun(), no_popen) is False


def test_invalid_mac_rejected():
    with pytest.raises(ValueError):
        ensure_connected("not-a-mac", "hci1", FakeRun(), FakePopen())
    with pytest.raises(ValueError):
        BtMonitor("AA:BB:CC", "hci1")
    assert ensure_connected(MAC.lower(), "hci1", FakeRun(), FakePopen(OK_LINES)) is True


def test_monitor_unconfigured_when_no_mac(factory):
    mon = BtMonitor("", "hci1", db_factory=factory, run=FakeRun())
    mon.check_once()
    assert mon.last_state == "unconfigured"


def test_status_reports_disconnected(factory):
    popen = FakePopen(["Failed to connect\n"])
    mon = BtMonitor(MAC, "hci1", db_factory=factory, run=FakeRun(connected=False), popen=popen)
    mon.check_once()
    assert mon.last_state == "disconnected"
    assert popen.args is not None  # retry attempted


def test_monitor_reconnects_and_logs_only_on_change(factory):
    run = FakeRun(connected=False)
    mon = BtMonitor(MAC, "hci1", db_factory=factory, run=run, popen=FakePopen(OK_LINES))
    mon.check_once()
    assert mon.last_state == "connected"
    run.connected = True
    mon.check_once()
    assert len(bt_events(factory)) == 1
    run.connected = False
    mon._popen = FakePopen(["Failed to connect\n"])
    mon.check_once()
    assert mon.last_state == "disconnected"
    assert len(bt_events(factory)) == 2


def test_monitor_start_stop():
    mon = BtMonitor("", "hci1", interval_s=0.01, run=FakeRun())
    assert mon.last_state == "unconfigured"
    mon.start()
    thread = mon._thread
    assert thread is not None and thread.is_alive()
    mon.stop()
    assert not thread.is_alive()
    assert mon._reported == "unconfigured"  # check_once ran at least once
