"""Daemon-only RKNNLite output ownership and coalesced cycle collection.

The local Kit API retains its copying wrapper. Here the service owns vendor
outputs until serialization completes, so it can return them without another
NumPy copy. Collection must also run after a response/job releases its payload;
the next inference is not guaranteed to happen (silence, timeout, disconnect).
"""

from __future__ import annotations

import gc
import threading
import time

from kit.runtime.rknnlite import RknnLiteRuntime


class _ServiceRknnLite:
    backend = "rknnlite"
    collects_output_cycles = False

    def __init__(self, protected: RknnLiteRuntime):
        # Only unwrap the known Kit wrapper, before it loads a native context.
        # Custom runtime_factory implementations retain their own semantics.
        self._runtime = protected._runtime

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_runtime"), name)

    def release(self):
        result = self._runtime.release()
        if result in (None, 0):
            self._runtime.rknn_data = None
        return result


def service_runtime(runtime):
    return _ServiceRknnLite(runtime) if type(runtime) is RknnLiteRuntime else runtime


class DeferredCycleCollector:
    """One pending collection, not one timer/thread per inference.

    The backend serializes collect() with native calls. Notifications need only
    this short state lock; a slow inference never blocks response completion.
    A notification arriving during GC is preserved for a later collection.
    """

    def __init__(self, *, delay=0.5, clock=time.monotonic):
        self._delay = delay
        self._clock = clock
        self._lock = threading.Lock()
        self._due = None

    def request(self):
        with self._lock:
            if self._due is None:
                self._due = self._clock() + self._delay

    def pending(self, *, force=False):
        with self._lock:
            return self._due is not None and (force or self._clock() >= self._due)

    def collect(self, *, force=False):
        with self._lock:
            if self._due is None or (not force and self._clock() < self._due):
                return False
            self._due = None
        try:
            gc.collect()
        except BaseException:
            self.request()
            raise
        return True
