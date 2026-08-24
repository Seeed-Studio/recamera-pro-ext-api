"""Bounded thread-safe input queue with explicit backpressure outcomes.

This queue may connect producer and consumer threads, but it does not make
:class:`~kit.workflow.Pipeline` concurrent and does not schedule NPU work.  Its
capacity, wait timeouts and drop policy are explicit so a live-camera producer
cannot accumulate unbounded latency or silently discard frames.
"""

from __future__ import annotations

import math
import threading
import time
from collections import deque
from dataclasses import dataclass
from enum import Enum
from numbers import Integral, Real
from typing import Any, Generic, Iterator, TypeVar

from ..errors import ConfigurationError
from .node import WorkflowError


T = TypeVar("T")


class DropPolicy(str, Enum):
    """Behavior when an :class:`InputQueue` has reached capacity."""

    BLOCK = "block"
    DROP_OLDEST = "drop_oldest"
    DROP_NEWEST = "drop_newest"


class PutStatus(str, Enum):
    """Exact disposition of one queue ``put`` attempt."""

    ENQUEUED = "enqueued"
    DROPPED_OLDEST = "dropped_oldest"
    DROPPED_NEWEST = "dropped_newest"


class QueueError(WorkflowError):
    """Base class for typed bounded-input queue failures."""

    default_code = "workflow_queue_error"


class QueueClosedError(QueueError):
    """An operation cannot continue because the queue is closed and drained."""

    default_code = "workflow_queue_closed"


class QueueTimeoutError(QueueError, TimeoutError):
    """A blocking consumer timed out waiting for an input item."""

    default_code = "workflow_queue_timeout"


class WorkflowBackpressureError(QueueError, TimeoutError):
    """A ``BLOCK`` producer timed out while the bounded queue remained full."""

    default_code = "workflow_backpressure_timeout"


# Descriptive alias for integrations that name the failure after its timeout
# behavior.  It is the same public exception type, not a second hierarchy.
BackpressureTimeoutError = WorkflowBackpressureError


@dataclass(frozen=True, slots=True)
class PutResult(Generic[T]):
    """Non-exception result of one accepted or deliberately dropped put.

    ``dropped_item`` is the evicted oldest item for ``DROP_OLDEST`` and the
    rejected offered item for ``DROP_NEWEST``.  It may legitimately be ``None``;
    callers should use ``status``/``dropped`` to determine disposition.
    """

    status: PutStatus
    size_after: int
    waited_seconds: float = 0.0
    dropped_item: T | None = None

    @property
    def accepted(self) -> bool:
        """Whether the offered item entered the queue."""

        return self.status is not PutStatus.DROPPED_NEWEST

    @property
    def dropped(self) -> bool:
        """Whether either an old or the offered item was discarded."""

        return self.status is not PutStatus.ENQUEUED


@dataclass(frozen=True, slots=True)
class QueueStats:
    """Atomic snapshot of all queue counters and current state.

    ``put_attempts``/``get_attempts`` include calls rejected after close.
    ``put_waits``/``get_waits`` count operations that encountered a full/empty
    queue and entered the blocking path, including zero-timeout attempts; they
    do not count condition-variable wakeups.  For a queue that never clears
    items, ``enqueued == dequeued + size + dropped_oldest`` always holds.
    """

    capacity: int
    size: int
    high_watermark: int
    closed: bool
    put_attempts: int
    get_attempts: int
    enqueued: int
    dequeued: int
    dropped_oldest: int
    dropped_newest: int
    put_waits: int
    get_waits: int
    put_timeouts: int
    get_timeouts: int
    closed_puts: int
    closed_gets: int

    @property
    def dropped(self) -> int:
        """Total items discarded by either drop policy."""

        return self.dropped_oldest + self.dropped_newest

    @property
    def accepted(self) -> int:
        """Alias for the total number of successfully enqueued items."""

        return self.enqueued

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible metrics mapping."""

        return {
            "capacity": self.capacity,
            "size": self.size,
            "high_watermark": self.high_watermark,
            "closed": self.closed,
            "put_attempts": self.put_attempts,
            "get_attempts": self.get_attempts,
            "enqueued": self.enqueued,
            "dequeued": self.dequeued,
            "dropped_oldest": self.dropped_oldest,
            "dropped_newest": self.dropped_newest,
            "dropped": self.dropped,
            "put_waits": self.put_waits,
            "get_waits": self.get_waits,
            "put_timeouts": self.put_timeouts,
            "get_timeouts": self.get_timeouts,
            "closed_puts": self.closed_puts,
            "closed_gets": self.closed_gets,
        }


def _configuration_error(message: str, field_name: str) -> ConfigurationError:
    return ConfigurationError(
        message,
        operation="workflow.queue.configure",
        details={"field": field_name},
    )


def _timeout(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, Real):
        raise _configuration_error("timeout must be a real number", "timeout")
    result = float(value)
    if not math.isfinite(result) or result < 0.0:
        raise _configuration_error("timeout must be finite and non-negative", "timeout")
    return result


class InputQueue(Generic[T]):
    """A bounded FIFO with precise blocking and latest/oldest drop policies.

    ``BLOCK`` waits for space and raises :class:`WorkflowBackpressureError` on
    timeout.  ``DROP_OLDEST`` atomically evicts the oldest queued item before
    accepting the new one.  ``DROP_NEWEST`` rejects the offered item.  Drop
    policies never hide that decision: every call returns :class:`PutResult`.

    ``close`` wakes all waiters and prevents further puts.  Existing items may
    still be drained; a get on a closed, empty queue raises
    :class:`QueueClosedError`.  Iteration follows that drain-until-closed rule.
    """

    def __init__(
        self,
        capacity: int,
        policy: DropPolicy | str = DropPolicy.BLOCK,
        *,
        clock=time.monotonic,
    ) -> None:
        if isinstance(capacity, bool) or not isinstance(capacity, Integral):
            raise _configuration_error("capacity must be an integer", "capacity")
        if int(capacity) <= 0:
            raise _configuration_error("capacity must be positive", "capacity")
        try:
            normalized_policy = (
                policy if isinstance(policy, DropPolicy) else DropPolicy(policy)
            )
        except (TypeError, ValueError) as exc:
            error = _configuration_error("unknown drop policy", "policy")
            raise error from exc
        if not callable(clock):
            raise _configuration_error("clock must be callable", "clock")

        self.capacity = int(capacity)
        self.policy = normalized_policy
        self._clock = clock
        self._items: deque[T] = deque()
        self._condition = threading.Condition(threading.Lock())
        self._closed = False
        self._high_watermark = 0
        self._put_attempts = 0
        self._get_attempts = 0
        self._enqueued = 0
        self._dequeued = 0
        self._dropped_oldest = 0
        self._dropped_newest = 0
        self._put_waits = 0
        self._get_waits = 0
        self._put_timeouts = 0
        self._get_timeouts = 0
        self._closed_puts = 0
        self._closed_gets = 0

    @property
    def closed(self) -> bool:
        """Whether no further items may be put."""

        with self._condition:
            return self._closed

    def qsize(self) -> int:
        """Return the current item count under the queue lock."""

        with self._condition:
            return len(self._items)

    def empty(self) -> bool:
        """Whether the queue currently contains no items."""

        return self.qsize() == 0

    def full(self) -> bool:
        """Whether the queue is currently at capacity."""

        with self._condition:
            return len(self._items) >= self.capacity

    def _stats_locked(self) -> QueueStats:
        return QueueStats(
            capacity=self.capacity,
            size=len(self._items),
            high_watermark=self._high_watermark,
            closed=self._closed,
            put_attempts=self._put_attempts,
            get_attempts=self._get_attempts,
            enqueued=self._enqueued,
            dequeued=self._dequeued,
            dropped_oldest=self._dropped_oldest,
            dropped_newest=self._dropped_newest,
            put_waits=self._put_waits,
            get_waits=self._get_waits,
            put_timeouts=self._put_timeouts,
            get_timeouts=self._get_timeouts,
            closed_puts=self._closed_puts,
            closed_gets=self._closed_gets,
        )

    def stats(self) -> QueueStats:
        """Return one internally consistent snapshot of all counters."""

        with self._condition:
            return self._stats_locked()

    snapshot = stats

    def _closed_error(self, operation: str, waited_seconds: float = 0.0) -> QueueClosedError:
        return QueueClosedError(
            "input queue is closed",
            operation=operation,
            details={
                "capacity": self.capacity,
                "size": len(self._items),
                "policy": self.policy.value,
                "waited_seconds": waited_seconds,
            },
        )

    def put(self, item: T, timeout: float | None = None) -> PutResult[T]:
        """Put one item according to policy, or raise a typed blocking failure."""

        timeout = _timeout(timeout)
        with self._condition:
            self._put_attempts += 1
            if self._closed:
                self._closed_puts += 1
                raise self._closed_error("workflow.queue.put")

            if len(self._items) < self.capacity:
                self._items.append(item)
                self._enqueued += 1
                self._high_watermark = max(self._high_watermark, len(self._items))
                self._condition.notify_all()
                return PutResult(PutStatus.ENQUEUED, len(self._items))

            if self.policy is DropPolicy.DROP_OLDEST:
                dropped = self._items.popleft()
                self._dropped_oldest += 1
                self._items.append(item)
                self._enqueued += 1
                self._condition.notify_all()
                return PutResult(
                    PutStatus.DROPPED_OLDEST,
                    len(self._items),
                    dropped_item=dropped,
                )

            if self.policy is DropPolicy.DROP_NEWEST:
                self._dropped_newest += 1
                return PutResult(
                    PutStatus.DROPPED_NEWEST,
                    len(self._items),
                    dropped_item=item,
                )

            started = float(self._clock())
            deadline = None if timeout is None else started + timeout
            waited = False
            while len(self._items) >= self.capacity:
                if self._closed:
                    self._closed_puts += 1
                    raise self._closed_error(
                        "workflow.queue.put", max(0.0, float(self._clock()) - started)
                    )
                if not waited:
                    waited = True
                    self._put_waits += 1
                remaining = None if deadline is None else deadline - float(self._clock())
                if remaining is not None and remaining <= 0.0:
                    self._put_timeouts += 1
                    elapsed = max(0.0, float(self._clock()) - started)
                    raise WorkflowBackpressureError(
                        "timed out waiting for input queue capacity",
                        operation="workflow.queue.put",
                        retryable=True,
                        details={
                            "capacity": self.capacity,
                            "size": len(self._items),
                            "policy": self.policy.value,
                            "timeout": timeout,
                            "waited_seconds": elapsed,
                        },
                    )
                self._condition.wait(remaining)

            # A consumer may make space and close the queue before this waiter
            # reacquires the lock.  Closing is a hard admission barrier even
            # when the loop condition has become false in the same wakeup.
            if self._closed:
                self._closed_puts += 1
                raise self._closed_error(
                    "workflow.queue.put", max(0.0, float(self._clock()) - started)
                )
            self._items.append(item)
            self._enqueued += 1
            self._high_watermark = max(self._high_watermark, len(self._items))
            self._condition.notify_all()
            elapsed = max(0.0, float(self._clock()) - started) if waited else 0.0
            return PutResult(
                PutStatus.ENQUEUED,
                len(self._items),
                waited_seconds=elapsed,
            )

    def put_nowait(self, item: T) -> PutResult[T]:
        """Put without waiting; a full BLOCK queue raises backpressure."""

        return self.put(item, timeout=0.0)

    def get(self, timeout: float | None = None) -> T:
        """Remove and return the oldest item, waiting up to ``timeout``."""

        timeout = _timeout(timeout)
        with self._condition:
            self._get_attempts += 1
            started = float(self._clock())
            deadline = None if timeout is None else started + timeout
            waited = False
            while not self._items:
                if self._closed:
                    self._closed_gets += 1
                    raise self._closed_error(
                        "workflow.queue.get", max(0.0, float(self._clock()) - started)
                    )
                if not waited:
                    waited = True
                    self._get_waits += 1
                remaining = None if deadline is None else deadline - float(self._clock())
                if remaining is not None and remaining <= 0.0:
                    self._get_timeouts += 1
                    elapsed = max(0.0, float(self._clock()) - started)
                    raise QueueTimeoutError(
                        "timed out waiting for an input queue item",
                        operation="workflow.queue.get",
                        retryable=True,
                        details={
                            "capacity": self.capacity,
                            "size": 0,
                            "policy": self.policy.value,
                            "timeout": timeout,
                            "waited_seconds": elapsed,
                        },
                    )
                self._condition.wait(remaining)

            item = self._items.popleft()
            self._dequeued += 1
            self._condition.notify_all()
            return item

    def get_nowait(self) -> T:
        """Get without waiting; an empty open queue raises ``QueueTimeoutError``."""

        return self.get(timeout=0.0)

    def close(self) -> QueueStats:
        """Prevent puts, wake every waiter and return the resulting snapshot."""

        with self._condition:
            if not self._closed:
                self._closed = True
                self._condition.notify_all()
            return self._stats_locked()

    def __len__(self) -> int:
        return self.qsize()

    def __iter__(self) -> Iterator[T]:
        """Drain items until the closed queue becomes empty."""

        while True:
            try:
                yield self.get()
            except QueueClosedError:
                return


__all__ = [
    "BackpressureTimeoutError",
    "DropPolicy",
    "InputQueue",
    "PutResult",
    "PutStatus",
    "QueueClosedError",
    "QueueError",
    "QueueStats",
    "QueueTimeoutError",
    "WorkflowBackpressureError",
]
