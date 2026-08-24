"""Bounded, aging priority scheduler used by :mod:`inferenced.server`."""

from __future__ import annotations

import collections
import itertools
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Deque, Optional


class QueueFullError(RuntimeError):
    """A client exceeded its bounded number of pending inference requests."""


class DeadlineExceededError(TimeoutError):
    """A request expired before the scheduler could start it."""


@dataclass
class ScheduledJob:
    # ``client_id`` owns cancellation when one Unix connection disappears.
    # ``fairness_id`` groups every model connection from the same admitted app
    # generation into one queue so opening more sockets cannot buy more NPU
    # turns.  It defaults to client_id for backwards compatibility.
    client_id: str
    priority: int
    deadline: float
    execute: Callable[[], Any]
    ready_at: Callable[[], float] = lambda: 0.0
    fairness_id: Optional[str] = None
    sequence: int = 0
    enqueued_at: float = field(default_factory=time.monotonic)
    _done: threading.Event = field(default_factory=threading.Event, init=False)
    _result: Any = field(default=None, init=False)
    _error: Optional[BaseException] = field(default=None, init=False)

    def finish(self, result: Any = None, error: Optional[BaseException] = None) -> None:
        self._result = result
        self._error = error
        self._done.set()

    def result(self, timeout: Optional[float] = None) -> Any:
        if not self._done.wait(timeout):
            raise DeadlineExceededError("scheduler result wait timed out")
        if self._error is not None:
            raise self._error
        return self._result


class FairScheduler:
    """Single-driver scheduler with bounded per-client queues.

    Equal-priority clients rotate.  Priority is combined with bounded aging, so
    an interactive request normally wins while a lower-priority stream still
    becomes runnable after waiting.  Only the worker calls ``job.execute``;
    vendor RKNN contexts therefore never infer concurrently.
    """

    def __init__(
        self,
        *,
        max_pending_per_client: int = 8,
        aging_points_per_second: float = 10.0,
        clock: Callable[[], float] = time.monotonic,
        name: str = "recamera-inferenced",
    ) -> None:
        if max_pending_per_client <= 0:
            raise ValueError("max_pending_per_client must be positive")
        self.max_pending_per_client = int(max_pending_per_client)
        self.aging_points_per_second = float(aging_points_per_second)
        self._clock = clock
        self._cv = threading.Condition(threading.RLock())
        self._queues: dict[str, Deque[ScheduledJob]] = {}
        self._rotation: collections.deque[str] = collections.deque()
        self._sequence = itertools.count(1)
        self._stopping = False
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)
        self._thread.start()

    def submit(self, job: ScheduledJob) -> ScheduledJob:
        if not 0 <= int(job.priority) <= 100:
            raise ValueError("priority must be in 0..100")
        with self._cv:
            if self._stopping:
                raise RuntimeError("scheduler is stopping")
            fairness_id = job.fairness_id or job.client_id
            queue = self._queues.get(fairness_id)
            if queue is None:
                queue = collections.deque()
                self._queues[fairness_id] = queue
                self._rotation.append(fairness_id)
            if len(queue) >= self.max_pending_per_client:
                raise QueueFullError(
                    f"application {fairness_id!r} has {len(queue)} pending requests"
                )
            job.sequence = next(self._sequence)
            queue.append(job)
            self._cv.notify_all()
            return job

    def cancel_client(self, client_id: str) -> int:
        with self._cv:
            cancelled = 0
            for fairness_id, queue in list(self._queues.items()):
                retained = collections.deque()
                for job in queue:
                    if job.client_id == client_id:
                        job.finish(error=RuntimeError("client disconnected"))
                        cancelled += 1
                    else:
                        retained.append(job)
                if retained:
                    self._queues[fairness_id] = retained
                else:
                    self._queues.pop(fairness_id, None)
                    try:
                        self._rotation.remove(fairness_id)
                    except ValueError:
                        pass
            self._cv.notify_all()
            return cancelled

    def close(self) -> None:
        with self._cv:
            if self._stopping:
                pass
            self._stopping = True
            for client_id, queue in list(self._queues.items()):
                for job in queue:
                    job.finish(error=RuntimeError("scheduler stopped"))
                self._queues.pop(client_id, None)
            self._rotation.clear()
            self._cv.notify_all()
        if self._thread is not threading.current_thread():
            self._thread.join(timeout=5.0)

    def _remove_empty(self, client_id: str) -> None:
        queue = self._queues.get(client_id)
        if queue:
            return
        self._queues.pop(client_id, None)
        try:
            self._rotation.remove(client_id)
        except ValueError:
            pass

    def _select_locked(self, now: float) -> tuple[Optional[ScheduledJob], Optional[float]]:
        candidates: list[tuple[float, int, int, str, ScheduledJob]] = []
        wake_at: Optional[float] = None
        rotation_rank = {client: index for index, client in enumerate(self._rotation)}
        for client_id in list(self._rotation):
            queue = self._queues.get(client_id)
            if not queue:
                self._remove_empty(client_id)
                continue

            # Expired jobs are completed without entering the driver.  Continue
            # until this client's head is live so one stale request cannot block
            # all later work from the same application.
            while queue and queue[0].deadline <= now:
                queue.popleft().finish(
                    error=DeadlineExceededError("inference deadline expired in queue")
                )
            if not queue:
                self._remove_empty(client_id)
                continue

            # Within one client, priority may reorder pending work.  Sequence is
            # the stable FIFO tiebreaker.
            job = max(queue, key=lambda item: (item.priority, -item.sequence))
            eligible = max(job.enqueued_at, float(job.ready_at()))
            if eligible > now:
                wake_at = eligible if wake_at is None else min(wake_at, eligible)
                continue
            age = max(0.0, now - job.enqueued_at)
            score = float(job.priority) + min(100.0, age * self.aging_points_per_second)
            candidates.append(
                (score, -rotation_rank.get(client_id, 0), -job.sequence, client_id, job)
            )

        if not candidates:
            return None, wake_at
        _, _, _, client_id, selected = max(candidates)
        queue = self._queues[client_id]
        queue.remove(selected)
        # Move the winning client behind its peers for equal-score round robin.
        try:
            self._rotation.remove(client_id)
        except ValueError:
            pass
        if queue:
            self._rotation.append(client_id)
        else:
            self._queues.pop(client_id, None)
        return selected, wake_at

    def _run(self) -> None:
        while True:
            with self._cv:
                while True:
                    if self._stopping:
                        return
                    now = self._clock()
                    job, wake_at = self._select_locked(now)
                    if job is not None:
                        break
                    timeout = None if wake_at is None else max(0.0, wake_at - now)
                    self._cv.wait(timeout=timeout)
            try:
                if job.deadline <= self._clock():
                    raise DeadlineExceededError("inference deadline expired before execution")
                result = job.execute()
            except BaseException as exc:
                job.finish(error=exc)
            else:
                job.finish(result=result)


__all__ = [
    "DeadlineExceededError",
    "FairScheduler",
    "QueueFullError",
    "ScheduledJob",
]
