"""Durable asynchronous operations and the lightweight App Center event bus.

The HTTP request thread only validates an action and enqueues it.  A single
worker executes mutations in order; this matches appmgr's existing process-wide
busy gate while keeping uploads/install/restart from holding an nginx request
open.  Operation history is diagnostic state under ``APPMGR_DIR``.  Queued or
running records found after a daemon restart are marked failed because Python
callables cannot be reconstructed safely from a journal.
"""
from __future__ import annotations

import json
import os
import queue
import tempfile
import threading
import time
import uuid
from typing import Callable, Optional

from . import paths


TERMINAL = frozenset(("succeeded", "failed"))
ACTIVE = frozenset(("queued", "running"))
MAX_HISTORY = 200


class OperationBusyError(RuntimeError):
    """The bounded mutation queue cannot safely accept another callback."""


class EventCapacityError(RuntimeError):
    """The bounded SSE fan-out already has its maximum live subscribers."""


class EventBus:
    """Bounded fan-out used by ``GET /api/app-center/v1/events`` SSE clients."""

    def __init__(self, subscriber_depth: int = 64,
                 max_subscribers: Optional[int] = None):
        self._lock = threading.Lock()
        self._subscribers: set[queue.Queue] = set()
        self._subscriber_depth = max(4, int(subscriber_depth))
        self._max_subscribers = max(
            1, int(paths.MAX_SSE_SUBSCRIBERS if max_subscribers is None
                   else max_subscribers))
        self._sequence = 0

    def publish(self, kind: str, **payload) -> dict:
        with self._lock:
            self._sequence += 1
            event = {
                "id": self._sequence,
                "type": str(kind),
                "ts": time.time(),
                **payload,
            }
            subscribers = list(self._subscribers)
        for target in subscribers:
            try:
                target.put_nowait(event)
            except queue.Full:
                # Events only invalidate the front-end cache; retain the newest
                # signal instead of allowing a slow browser to grow memory.
                try:
                    target.get_nowait()
                except queue.Empty:
                    pass
                try:
                    target.put_nowait(event)
                except queue.Full:
                    pass
        return event

    def subscribe(self) -> queue.Queue:
        target: queue.Queue = queue.Queue(maxsize=self._subscriber_depth)
        with self._lock:
            if len(self._subscribers) >= self._max_subscribers:
                raise EventCapacityError(
                    "App Center event stream connection limit reached")
            self._subscribers.add(target)
        return target

    def unsubscribe(self, target: queue.Queue) -> None:
        with self._lock:
            self._subscribers.discard(target)


class OperationManager:
    """One-worker durable operation registry."""

    def __init__(self, journal: Optional[str] = None,
                 event_bus: Optional[EventBus] = None,
                 queue_depth: Optional[int] = None):
        self.journal = journal or paths.operation_state_file()
        self.events = event_bus or EventBus()
        self._lock = threading.RLock()
        self._max_pending = max(
            1, int(paths.MAX_PENDING_OPERATIONS if queue_depth is None
                   else queue_depth))
        # Reserve one internal slot for the close sentinel. Public submission
        # is still capped at _max_pending below.
        self._queue: queue.Queue = queue.Queue(maxsize=self._max_pending + 1)
        self._worker: Optional[threading.Thread] = None
        self._closed = False
        self._records = self._load()
        interrupted = False
        now = time.time()
        for record in self._records:
            if record.get("status") in ACTIVE:
                record.update({
                    "status": "failed",
                    "finished_at": now,
                    "updated_at": now,
                    "error": "appmgr restarted before the operation completed",
                    "message": "interrupted by appmgr restart",
                    "progress": {"percent": 100},
                })
                interrupted = True
        if interrupted:
            self._save()

    def _load(self) -> list[dict]:
        try:
            with open(self.journal) as source:
                value = json.load(source)
        except (FileNotFoundError, OSError, ValueError):
            return []
        records = value.get("operations") if isinstance(value, dict) else None
        if not isinstance(records, list):
            return []
        return [dict(item) for item in records if isinstance(item, dict)][-MAX_HISTORY:]

    def _save(self) -> None:
        directory = os.path.dirname(self.journal)
        os.makedirs(directory, mode=0o700, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".operations.", dir=directory)
        try:
            payload = {"schema_version": 1, "operations": self._records[-MAX_HISTORY:]}
            with os.fdopen(fd, "w") as output:
                json.dump(payload, output, indent=2, sort_keys=True)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.journal)
            temporary = None
        finally:
            if temporary:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass

    def _ensure_worker(self) -> None:
        if self._worker is not None and self._worker.is_alive():
            return
        if self._closed:
            raise RuntimeError("operation manager is closed")
        self._worker = threading.Thread(
            target=self._run, daemon=True, name="appmgr-operations")
        self._worker.start()

    def submit(self, kind: str, app_id: Optional[str],
               callback: Callable[[], object]) -> dict:
        now = time.time()
        record = {
            "id": uuid.uuid4().hex,
            "type": str(kind),
            "app_id": app_id,
            "status": "queued",
            "progress": {"percent": 0},
            "message": "queued",
            "error": None,
            "created_at": now,
            "updated_at": now,
        }
        with self._lock:
            if self._closed:
                raise OperationBusyError("operation manager is closed")
            if app_id is not None:
                active = next((item for item in reversed(self._records)
                               if item.get("app_id") == app_id
                               and item.get("status") in ACTIVE), None)
                if active is not None:
                    raise OperationBusyError(
                        "operation %s is already active for app %s" %
                        (active.get("id"), app_id))
            if self._queue.qsize() >= self._max_pending:
                raise OperationBusyError(
                    "operation queue capacity reached; retry after an active operation finishes")
            self._records.append(record)
            self._records = self._records[-MAX_HISTORY:]
            try:
                self._save()
                self._ensure_worker()
                self._queue.put_nowait((record["id"], callback))
            except Exception:
                self._records = [item for item in self._records
                                 if item.get("id") != record["id"]]
                self._save()
                raise
        self.events.publish("operation", operation=dict(record))
        return dict(record)

    def _update(self, operation_id: str, **fields) -> Optional[dict]:
        with self._lock:
            found = None
            for record in self._records:
                if record.get("id") == operation_id:
                    record.update(fields)
                    record["updated_at"] = time.time()
                    found = dict(record)
                    break
            if found is not None:
                self._save()
        if found is not None:
            self.events.publish("operation", operation=found)
        return found

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            operation_id, callback = item
            self._update(operation_id, status="running",
                         message="running", started_at=time.time(),
                         progress={"percent": 10})
            try:
                result = callback()
            except BaseException as exc:
                # Do not leak a traceback or arbitrary repr through the public
                # API.  The concrete exception class plus message is actionable
                # and is also recorded in appmgr's normal audit/log paths.
                message = "%s: %s" % (type(exc).__name__, exc)
                self._update(operation_id, status="failed", error=message,
                             message="failed", finished_at=time.time(),
                             progress={"percent": 100})
            else:
                self._update(operation_id, status="succeeded", error=None,
                             result=result, message="completed",
                             finished_at=time.time(),
                             progress={"percent": 100})

    def list(self) -> list[dict]:
        with self._lock:
            return [dict(item) for item in reversed(self._records)]

    def active_for(self, app_id: str) -> Optional[dict]:
        with self._lock:
            for item in reversed(self._records):
                if item.get("app_id") == app_id and item.get("status") in ACTIVE:
                    return dict(item)
        return None

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            worker = self._worker
            if worker is not None:
                self._queue.put_nowait(None)
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=2.0)
