"""Scriptable in-memory stand-in for an mpv process."""

from __future__ import annotations


class FakeMpv:
    """Records calls; position/duration/paused are set by the test, events are pushed."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.pos: float | None = None
        self.dur: float | None = None
        self.is_paused = False
        self.events: list[dict] = []

    def load(self, url: str) -> None:
        self.calls.append(("load", url))
        self.is_paused = False

    def stop(self) -> None:
        self.calls.append(("stop",))

    def set_pause(self, paused: bool) -> None:
        self.calls.append(("set_pause", paused))
        self.is_paused = paused

    def seek_absolute(self, seconds: float) -> None:
        self.calls.append(("seek", seconds))
        self.pos = seconds

    def position(self) -> float | None:
        return self.pos

    def duration(self) -> float | None:
        return self.dur

    def paused(self) -> bool:
        return self.is_paused

    def wait_event(self, timeout: float) -> dict | None:
        return self.events.pop(0) if self.events else None

    def push(self, event: dict) -> None:
        self.events.append(event)

    def calls_named(self, name: str) -> list[tuple]:
        return [c for c in self.calls if c[0] == name]
