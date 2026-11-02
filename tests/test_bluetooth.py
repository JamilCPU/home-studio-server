"""Tests for the Bluetooth connection helpers and monitor."""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

from src.bluetooth import BtMonitor, ensure_connected, is_connected
from src.db import connect, migrate
from src.store.settings import recent_events

MAC = "AA:BB:CC:DD:EE:FF"


DEV_PATH = "/org/bluez/hci1/dev_AA_BB_CC_DD_EE_FF"


class FakeRun:
    """Scriptable subprocess.run stand-in for busctl.

    `connected` may be a bool or a list of values returned by successive Connected reads;
    `missing` makes the device object not exist; `connect_rc` is the Connect call's exit code.
    """

    def __init__(self, connected=False, missing=False, connect_rc=0, raises=None, raise_on=None):
        self.reads = list(connected) if isinstance(connected, list) else None
        self.connected = connected
        self.missing = missing
        self.connect_rc = connect_rc
        self.raises = raises
        self.raise_on = raise_on
        self.calls = []

    def __call__(self, cmd, **kwargs):
        self.calls.append((cmd, kwargs))
        if self.raises and (self.raise_on is None or self.raise_on in cmd):
            raise self.raises
        if "get-property" in cmd:
            if self.missing:
                return SimpleNamespace(stdout="", returncode=1)
            value = self.reads.pop(0) if self.reads else self.connected
            return SimpleNamespace(stdout=f"b {'true' if value else 'false'}\n", returncode=0)
        if "Connect" in cmd:
            return SimpleNamespace(stdout="", returncode=self.connect_rc)
        return SimpleNamespace(stdout="", returncode=0)

    def cmds(self):
        return [c[0] for c in self.calls]


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


def test_is_connected_true_and_false():
    run = FakeRun(connected=True)
    assert is_connected(MAC, "hci1", run) is True
    assert run.calls[0][0] == [
        "busctl", "get-property", "org.bluez", DEV_PATH, "org.bluez.Device1", "Connected"]
    assert is_connected(MAC, "hci1", FakeRun(connected=False)) is False


def test_is_connected_uses_requested_adapter():
    run = FakeRun(connected=True)
    is_connected(MAC, "hci0", run)
    assert "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF" in run.calls[0][0]


def test_is_connected_false_when_device_missing():
    assert is_connected(MAC, "hci1", FakeRun(missing=True)) is False


def test_is_connected_false_on_timeout_or_missing_binary():
    assert is_connected(MAC, "hci1", FakeRun(raises=subprocess.TimeoutExpired("x", 15))) is False
    assert is_connected(MAC, "hci1", FakeRun(raises=FileNotFoundError())) is False


def test_subprocess_calls_use_capture_and_timeout():
    run = FakeRun()
    is_connected(MAC, "hci1", run)
    kwargs = run.calls[0][1]
    assert kwargs["capture_output"] and kwargs["text"] and kwargs["timeout"] == 15


def test_ensure_connected_powers_on_then_connects():
    run = FakeRun(connected=[True])
    assert ensure_connected(MAC, "hci1", run) is True
    assert run.cmds() == [
        ["busctl", "set-property", "org.bluez", "/org/bluez/hci1",
         "org.bluez.Adapter1", "Powered", "b", "true"],
        ["busctl", "--timeout=20", "call", "org.bluez", DEV_PATH, "org.bluez.Device1", "Connect"],
        ["busctl", "get-property", "org.bluez", DEV_PATH, "org.bluez.Device1", "Connected"],
    ]
    assert run.calls[1][1]["timeout"] > 20  # outlives busctl's own timeout


def test_ensure_connected_connect_failure_returns_false():
    run = FakeRun(connected=False, connect_rc=1)
    assert ensure_connected(MAC, "hci1", run) is False
    assert any("Connect" in c for c in run.cmds())


def test_ensure_connected_device_missing_returns_false():
    assert ensure_connected(MAC, "hci1", FakeRun(missing=True, connect_rc=1)) is False


def test_ensure_connected_connect_timeout_returns_false():
    run = FakeRun(raises=subprocess.TimeoutExpired("busctl", 30), raise_on="Connect")
    assert ensure_connected(MAC, "hci1", run) is False


def test_ensure_connected_false_when_binary_missing():
    assert ensure_connected(MAC, "hci1", FakeRun(raises=FileNotFoundError())) is False


def test_invalid_mac_rejected():
    with pytest.raises(ValueError):
        ensure_connected("not-a-mac", "hci1", FakeRun())
    with pytest.raises(ValueError):
        is_connected("not-a-mac", "hci1", FakeRun())
    with pytest.raises(ValueError):
        BtMonitor("AA:BB:CC", "hci1")
    assert ensure_connected(MAC.lower(), "hci1", FakeRun(connected=[True])) is True


def test_invalid_adapter_rejected():
    with pytest.raises(ValueError):
        BtMonitor(MAC, "hci1/../x")
    with pytest.raises(ValueError):
        ensure_connected(MAC, "bad adapter", FakeRun())


def test_monitor_unconfigured_when_no_mac(factory):
    mon = BtMonitor("", "hci1", db_factory=factory, run=FakeRun())
    mon.check_once()
    assert mon.last_state == "unconfigured"


def test_status_reports_disconnected(factory):
    run = FakeRun(connected=False, connect_rc=1)
    mon = BtMonitor(MAC, "hci1", db_factory=factory, run=run)
    mon.check_once()
    assert mon.last_state == "disconnected"
    assert any("Connect" in c for c in run.cmds())  # retry attempted


def test_monitor_first_check_powers_adapter_and_reconnects(factory):
    # first read: disconnected; after Connect: connected
    run = FakeRun(connected=[False, True])
    mon = BtMonitor(MAC, "hci1", db_factory=factory, run=run)
    mon.check_once()
    assert mon.last_state == "connected"
    assert "Powered" in run.cmds()[1]
    assert all("/org/bluez/hci1" in " ".join(c) for c in run.cmds())


def test_monitor_skips_reconnect_when_connected_and_logs_only_on_change(factory):
    run = FakeRun(connected=True)
    mon = BtMonitor(MAC, "hci1", db_factory=factory, run=run)
    mon.check_once()
    assert mon.last_state == "connected"
    assert len(run.calls) == 1
    mon.check_once()
    assert len(bt_events(factory)) == 1
    run.connected = False
    run.connect_rc = 1
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
