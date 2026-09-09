"""One monotonic clock for the whole open-door path, off unless asked for.

Why a separate module and not `print()` in the loop
---------------------------------------------------
The «face in frame → contact closed» number is produced by three pieces of code
that live in two repositories and three threads: the recognition loop
(`app.py`), the frame channel and the event thread (`inproc_adapter.py`), and
the actuator (`gpio_out.py`). Timing each of them against its own `time.time()`
and correlating the prints afterwards does not survive a clock step and cannot
express «this stage of THIS frame». So: one process-wide monotonic clock, one
JSONL file, one row per frame plus one row per track milestone.

Cost when disabled: `frame()` hands back a null object whose `stage()` /
`add()` / `close()` do nothing, and `event()` returns on a module-level bool.
No allocation, no formatting, no file.

Enable with an environment variable, never with a config key — this is a bench
instrument, not a product feature, and it must be impossible to leave on by
editing `config.json`::

    F1_STAGE_TRACE=/userdata/latbench/stages.jsonl

Rows
----
``{"k": "frame", "i": <frame index>, "t": <monotonic>, "age_ms": …, "<stage>": ms…}``
    One per processed frame. ``age_ms`` is `t_loop − frame.pts`, i.e. how stale
    the frame already was when the loop got to it — the queueing term that no
    per-stage timer can see.
``{"k": "ev", "e": <name>, "tid": …, "i": …, "t": …}``
    A track milestone on the same clock: ``new`` (this track's first ungated
    frame), ``live`` (its liveness verdict first settled to LIVE), ``stable``
    (its embeddings first reached ``min_track_frames``). ``since_new_ms`` on the
    latter two is what the person at the door is actually waiting through.
    The bench adds ``pulse`` from outside, so the trace ends at the contact.

The file is opened once, appended line by line and flushed per row: a bench run
that is killed mid-way must still have every row it already produced.
"""
from __future__ import annotations

import json
import os
import threading
import time

_PATH = os.environ.get("F1_STAGE_TRACE") or ""
ENABLED = bool(_PATH)

_LOCK = threading.Lock()
_FH = None


def _fh():
    global _FH
    if _FH is None:
        _FH = open(_PATH, "a", buffering=1)
    return _FH


def now() -> float:
    """The one clock. Every timestamp in the trace comes from here."""
    return time.monotonic()


def row(**fields) -> None:
    """Append one JSONL row. Never raises: a broken trace must not break a run."""
    if not ENABLED:
        return
    try:
        line = json.dumps(fields, separators=(",", ":"))
    except Exception:                           # noqa: BLE001
        return
    try:
        with _LOCK:
            _fh().write(line + "\n")
    except Exception:                           # noqa: BLE001
        pass


def event(name: str, **fields) -> None:
    """A milestone: `{"k":"ev","e":name,"t":<monotonic>, …}`."""
    if not ENABLED:
        return
    row(k="ev", e=name, t=now(), **fields)


class Frame:
    """Accumulates one frame's stage costs, emitted as a single row on `close()`.

    Used as ``with st.stage("detect"): …`` so an exception cannot lose the row
    or leave a half-open timer behind.
    """

    __slots__ = ("i", "t0", "age_ms", "_ms", "_open", "_t")

    def __init__(self, i: int, pts) -> None:
        self.i = int(i)
        self.t0 = now()
        self.age_ms = (None if not isinstance(pts, (int, float))
                       else round((self.t0 - float(pts)) * 1000.0, 2))
        self._ms = {}
        self._open = None
        self._t = self.t0

    def stage(self, name: str):
        self._open = name
        return self

    def __enter__(self):
        self._t = now()                          # noqa: SLF001
        return self

    def __exit__(self, *exc):
        name, self._open = self._open, None
        if name:
            self.add(name, now() - self._t)      # noqa: SLF001
        return False

    def add(self, name: str, dt: float) -> None:
        self._ms[name] = round(self._ms.get(name, 0.0) + dt * 1000.0, 3)

    def close(self, **extra) -> None:
        row(k="frame", i=self.i, t=self.t0, age_ms=self.age_ms,
            total_ms=round((now() - self.t0) * 1000.0, 3), **self._ms, **extra)


class _Null:
    """Zero-cost stand-in when tracing is off. Same surface, no work."""

    __slots__ = ()

    def stage(self, name):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def add(self, name, dt):
        pass

    def close(self, **extra):
        pass


NULL = _Null()


def frame(i: int, pts):
    """`Frame` when tracing is on, the null object when it is off."""
    return Frame(i, pts) if ENABLED else NULL


__all__ = ["ENABLED", "Frame", "NULL", "event", "frame", "now", "row"]
