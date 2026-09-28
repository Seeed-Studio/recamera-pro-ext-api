"""Bounded, aging priority scheduler used by :mod:`inferenced.server`."""

from __future__ import annotations

import collections
import itertools
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Deque, Optional


log = logging.getLogger(__name__)
MAX_CONCURRENT_MODELS = 4


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
    cleanup: Optional[Callable[[], None]] = None
    # Cache/context identity, not client identity. None preserves serialization
    # for callers which have not supplied a model concurrency contract.
    concurrency_key: Optional[str] = None
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
    """Bounded independent-context scheduler with per-application fairness.

    Equal-priority clients rotate.  Priority is combined with bounded aging, so
    an interactive request normally wins while a lower-priority stream still
    becomes runnable after waiting. A context occupies at most one worker;
    waiting jobs for that context do not consume the other workers.
    """

    def __init__(
        self,
        *,
        max_pending_per_client: int = 8,
        max_concurrent_models: int = MAX_CONCURRENT_MODELS,
        aging_points_per_second: float = 10.0,
        clock: Callable[[], float] = time.monotonic,
        name: str = "recamera-inferenced",
    ) -> None:
        if max_pending_per_client <= 0:
            raise ValueError("max_pending_per_client must be positive")
        if (type(max_concurrent_models) is not int
                or not 1 <= max_concurrent_models <= MAX_CONCURRENT_MODELS):
            raise ValueError(f"max_concurrent_models must be an integer in 1..{MAX_CONCURRENT_MODELS}")
        self.max_concurrent_models = max_concurrent_models
        self.max_pending_per_client = int(max_pending_per_client)
        self.aging_points_per_second = float(aging_points_per_second)
        self._clock = clock
        self._cv = threading.Condition(threading.RLock())
        self._queues: dict[str, Deque[ScheduledJob]] = {}
        self._rotation: collections.deque[str] = collections.deque()
        self._sequence = itertools.count(1)
        self._stopping = False
        self._active_keys: set[Optional[str]] = set()
        self._threads = [threading.Thread(target=self._run, name=f"{name}-{i}", daemon=True)
                         for i in range(max_concurrent_models)]
        self._thread = self._threads[0]  # Compatibility for internal diagnostics.
        for thread in self._threads:
            thread.start()

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
        deadline = time.monotonic() + 5.0
        for thread in self._threads:
            if thread is not threading.current_thread():
                thread.join(timeout=max(0, deadline - time.monotonic()))

    def status(self):
        with self._cv:
            return {"max_concurrent_models": self.max_concurrent_models,
                    "active_models": len(self._active_keys),
                    "pending_requests": sum(len(q) for q in self._queues.values())}

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
            for job in list(queue):
                if job.deadline <= now:
                    queue.remove(job)
                    job.finish(error=DeadlineExceededError("inference deadline expired in queue"))
            if not queue:
                self._remove_empty(client_id)
                continue

            # Within one client, priority may reorder pending work.  Sequence is
            # the stable FIFO tiebreaker.
            runnable = []
            for job in queue:
                # Also wake for expiry when every context is busy/rate-limited.
                wake_at = job.deadline if wake_at is None else min(wake_at, job.deadline)
                if job.concurrency_key in self._active_keys:
                    continue
                eligible = max(job.enqueued_at, float(job.ready_at()))
                if eligible > now:
                    wake_at = min(wake_at, eligible)
                else:
                    runnable.append(job)
            if not runnable:
                continue
            job = max(runnable, key=lambda item: (item.priority, -item.sequence))
            age = max(0.0, now - job.enqueued_at)
            score = float(job.priority) + min(100.0, age * self.aging_points_per_second)
            candidates.append(
                (score, -rotation_rank.get(client_id, 0), -job.sequence, client_id, job)
            )

        if not candidates:
            return None, wake_at
        _, _, _, client_id, selected = max(candidates)
        self._active_keys.add(selected.concurrency_key)
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
            finally:
                # The worker can now sleep indefinitely. The waiting caller
                # owns its job/result; do not keep the last tensor payload (or
                # a failed job's input closure) alive in this thread's frame.
                cleanup = job.cleanup
                with self._cv:
                    self._active_keys.remove(job.concurrency_key)
                    self._cv.notify_all()
                result = None
                job = None
                if cleanup is not None:
                    try:
                        cleanup()
                    except Exception:
                        log.exception("scheduled job cleanup notification failed")
                cleanup = None


__all__ = [
    "DeadlineExceededError",
    "FairScheduler",
    "QueueFullError",
    "ScheduledJob",
]
