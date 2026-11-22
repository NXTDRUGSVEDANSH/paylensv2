"""
A per-request event channel.

The pipeline already records what it did — attempts, timings, call records —
but only hands it over once the answer is finished. This is the same material
while it is still happening, so a caller can show progress that is measured
rather than animated, and so the recoveries that used to exist only as log
lines (a repaired JSON response, a provider retry) become data on the answer.

Nothing is bound by default. `emit()` is a no-op unless a caller has opened a
channel, which is why the emit calls added to shared modules leave the classic
pipeline exactly as it was.

The channel lives in a ContextVar, so a request running in a worker thread gets
its own and nested calls several modules deep reach it without every function
in between growing a parameter.
"""
from __future__ import annotations

import time
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Callable, Iterator

# kind:
#   stage     — a step of the pipeline starting, finishing or failing
#   recovery  — something went wrong and was repaired without failing the request
#   note      — context worth showing, owed to neither of the above
Sink = Callable[[dict], None]

_current: ContextVar["Channel | None"] = ContextVar("paylens_events", default=None)


class Channel:
    """Collects events, and forwards them to a sink as they happen."""

    def __init__(self, sink: Sink | None = None) -> None:
        self.sink = sink
        self.events: list[dict] = []
        self._t0 = time.time()

    def emit(self, kind: str, stage: str, *, status: str = "info",
             message: str = "", detail: str = "", ms: int = 0) -> dict:
        ev = {"kind": kind, "stage": stage, "status": status, "message": message,
              "detail": detail, "ms": ms,
              "at_ms": int((time.time() - self._t0) * 1000)}
        self.events.append(ev)
        if self.sink is not None:
            try:
                self.sink(ev)
            except Exception:  # noqa: BLE001
                # A consumer that has gone away (a closed SSE connection) must
                # not take the request down with it. The work is still worth
                # finishing: the caller may be polling for the final answer.
                self.sink = None
        return ev

    def of_kind(self, kind: str) -> list[dict]:
        return [e for e in self.events if e["kind"] == kind]


@contextmanager
def bind(channel: Channel) -> Iterator[Channel]:
    token = _current.set(channel)
    try:
        yield channel
    finally:
        _current.reset(token)


def current() -> Channel | None:
    return _current.get()


def emit(kind: str, stage: str, *, status: str = "info", message: str = "",
         detail: str = "", ms: int = 0) -> None:
    ch = _current.get()
    if ch is not None:
        ch.emit(kind, stage, status=status, message=message, detail=detail, ms=ms)


def recovery(stage: str, message: str, detail: str = "") -> None:
    """Something was wrong and was fixed. The user is entitled to know."""
    emit("recovery", stage, status="info", message=message, detail=detail)


@contextmanager
def stage(name: str, message: str = "") -> Iterator[None]:
    """Emit start/done around a step, with its real duration."""
    t0 = time.time()
    emit("stage", name, status="start", message=message)
    try:
        yield
    except Exception as exc:  # noqa: BLE001
        emit("stage", name, status="fail", message=str(exc)[:200],
             ms=int((time.time() - t0) * 1000))
        raise
    emit("stage", name, status="done", ms=int((time.time() - t0) * 1000))
