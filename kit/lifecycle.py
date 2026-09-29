"""Publication / shutdown admission gate (overlay phase-2 V5-4, V6-2, V7-1, V8-1).

One lock, two admission points, one stop protocol.  The gate exists because a
multi-threaded app publishes from more than one thread while a shutdown request
can arrive at any moment, and "am I still allowed to publish?" must not be a
race:

  * **callback admission** -- the voice state machine asks :meth:`admit_callback`
    immediately before it invokes the application callback.  Once closing is
    set, no new callback starts; a callback that was already admitted runs to
    completion (it is never killed mid-flight).
  * **publication admission** -- the publish entry points ask
    :meth:`admit_publication` before they touch a sink.  Once closing is set,
    no new publication is admitted (V8-1: "zero new publication" means zero new
    publication ADMISSION; a publication that was already admitted may finish).

Both admissions read the same flag under the same lock, so "set closing" cannot
land between them.

Stop protocol (V5-4/V7-1/V8-1 -- the order matters)::

    with lock: mark closing                # the quiescence boundary
    # lock released here -- callbacks in flight must be able to drain
    wait for already-admitted callbacks, bounded by the shared deadline
    stop/join/terminate the owners, then destroy

The bound is the *silence* budget, not the *recycling* budget.  A drain that
overruns the deadline is recorded as an acceptance failure and the caller
proceeds with teardown anyway.  For a voice pipeline that means releasing the
ASR backend on a bounded wait of its own (V7-1: abandon the wait, not the
model): the decode a callback was blocked on is synchronous and uncancellable,
so waiting for it is what would blow the budget, and dropping its result is
what the gate above already guarantees.

Callback execution budget (V7-1/V8-1)
-------------------------------------
A callback is synchronous application code and is never killed mid-flight, so
the only way "2 seconds" can hold is for each callback to be bounded.  The
budget below is what a callback that publishes must fit inside; it is the
reason :class:`kit.app.App` takes one publication lock around the whole
``sink.emit*`` call rather than only around the counter:

    admission + lock wait   <= 0.25 s   (the lock is taken with a retry-free
                                         acquire; a producer that holds it is
                                         in `format + publish` below)
    format + all-channel publish <= 1.0 s
                                         bounded by the payload ceilings: one
                                         envelope <= 64 KiB and
                                         `messages <= formatter limit x
                                         channel count`
    margin                  >= 0.75 s

The lock-wait term is *not* charged to the callback whose admission is being
measured (V7-1); it is listed because it, too, has to fit before the deadline.
A callback that exceeds this budget does not get killed -- it is recorded as a
quiescence failure (see :meth:`PublicationGate.wait_quiescent`) and the caller
recycles the owning process.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional


logger = logging.getLogger(__name__)

# Shared drain budget for one stop request (V5-4/V6-2: "stop 请求后 2 秒").
DEFAULT_QUIESCENCE_SEC = 2.0


class PublicationGate:
    """Serialize callback admission, publication admission and closing.

    The gate is deliberately passive: it never stops a thread, kills a process
    or touches a sink.  Owners do that themselves, after :meth:`request_stop`
    returns.
    """

    def __init__(self, *, name: str = "publication") -> None:
        self.name = name
        self._lock = threading.Lock()
        self._closing = False
        self._callbacks = 0
        self._quiescent = threading.Event()
        self._quiescent.set()
        # Diagnostics: filled by request_stop / wait_quiescent.
        self.stop_requested_at: Optional[float] = None
        self.quiescence_failures = 0

    # -- admission --------------------------------------------------------- #
    def admit_callback(self) -> bool:
        """Reserve one callback slot; False once closing (zero new admission)."""
        with self._lock:
            if self._closing:
                return False
            self._callbacks += 1
            self._quiescent.clear()
            return True

    def release_callback(self) -> None:
        """Release a callback slot reserved by :meth:`admit_callback`."""
        with self._lock:
            if self._callbacks > 0:
                self._callbacks -= 1
            if self._callbacks == 0:
                self._quiescent.set()

    def admit_publication(self) -> bool:
        """True while publications may still enter a sink."""
        with self._lock:
            return not self._closing

    @property
    def closing(self) -> bool:
        with self._lock:
            return self._closing

    @property
    def inflight(self) -> int:
        """Already-admitted callbacks that have not returned yet."""
        with self._lock:
            return self._callbacks

    @property
    def quiescent(self) -> bool:
        """True when no admitted callback is running."""
        return self._quiescent.is_set()

    # -- shutdown ---------------------------------------------------------- #
    def request_stop(self, *, clock: Callable[[], float] = time.monotonic
                     ) -> float:
        """Mark closing under the lock, then RELEASE it.

        Returns the monotonic timestamp of the stop request (the quiescence
        boundary).  Called more than once, the first timestamp is kept.
        """
        with self._lock:
            if self.stop_requested_at is None:
                self.stop_requested_at = clock()
            self._closing = True
            if self._callbacks == 0:
                self._quiescent.set()
            return self.stop_requested_at

    def wait_quiescent(self, timeout: float = DEFAULT_QUIESCENCE_SEC) -> bool:
        """Wait (bounded) for already-admitted callbacks to finish.

        False means the silence budget was exceeded; the caller records it as a
        stop/handoff failure and continues with recycling (V7-1).
        """
        deadline = max(0.0, float(timeout))
        if self._quiescent.wait(deadline):
            return True
        with self._lock:
            self.quiescence_failures += 1
            inflight = self._callbacks
        logger.warning(
            "%s gate: %d already-admitted callback(s) still running %.3fs "
            "after the stop request; recording a quiescence failure and "
            "continuing with recycling", self.name, inflight, deadline)
        return False

    def quiescence_latency(self,
                           clock: Callable[[], float] = time.monotonic
                           ) -> Optional[float]:
        """Seconds from the stop request to the last observed quiescence."""
        if self.stop_requested_at is None or not self._quiescent.is_set():
            return None
        return clock() - self.stop_requested_at
