"""A writer-preferring barrier between native calls and process-wide GC."""

from contextlib import contextmanager
import threading


class NativeOperationGate:
    """Permit independent native calls, but drain them before GC/maintenance.

    A waiting writer prevents new readers from extending the busy period
    indefinitely. Exclusive acquisition is reentrant for initialization/cleanup;
    callers must never upgrade a shared acquisition to an exclusive one.
    """

    def __init__(self):
        self._cv = threading.Condition()
        self._readers = 0
        self._waiting_writers = 0
        self._writer = None
        self._depth = 0

    @contextmanager
    def shared(self):
        with self._cv:
            while self._writer is not None or self._waiting_writers:
                self._cv.wait()
            self._readers += 1
        try:
            yield
        finally:
            with self._cv:
                self._readers -= 1
                self._cv.notify_all()

    @contextmanager
    def exclusive(self, *, blocking=True):
        owner = threading.get_ident()
        acquired = False
        with self._cv:
            if self._writer == owner:
                self._depth += 1
                acquired = True
            elif blocking or (self._writer is None and not self._readers):
                self._waiting_writers += 1
                try:
                    while self._writer is not None or self._readers:
                        self._cv.wait()
                    self._writer, self._depth = owner, 1
                    acquired = True
                finally:
                    self._waiting_writers -= 1
                    self._cv.notify_all()
        try:
            yield acquired
        finally:
            if acquired:
                with self._cv:
                    self._depth -= 1
                    if not self._depth:
                        self._writer = None
                        self._cv.notify_all()
