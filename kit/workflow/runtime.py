"""Deterministic synchronous workflow pipeline runtime.

``Pipeline`` executes exactly one stage after another in the calling thread.
It is not an async DAG executor, thread pool, rkipc lease manager or NPU
scheduler.  Deadline and cancellation checks occur at stage boundaries; a
callable that needs mid-stage cancellation must cooperate through the supplied
context.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, replace
from numbers import Real
from typing import Any

from ..errors import ConfigurationError
from ..diagnostics import get_logger
from .node import (
    Stage,
    StageError,
    WorkflowCancelled,
    WorkflowCleanupError,
    WorkflowClosedError,
    WorkflowContext,
    WorkflowError,
    WorkflowResourceError,
    WorkflowTimeout,
)


_LOG = get_logger("workflow.runtime")


@dataclass(frozen=True, slots=True)
class StageCloseFailure:
    """One ordinary exception raised while closing a named stage."""

    stage: str
    error: Exception

    @property
    def message(self) -> str:
        """Stable human-readable error summary."""

        return f"{type(self.error).__name__}: {self.error}"


@dataclass(frozen=True, slots=True)
class WorkflowCloseReport:
    """Outcome of one reverse-order pipeline close pass.

    Ordinary close exceptions are retained in ``failures`` after all remaining
    stages have been attempted.  A direct :class:`BaseException` control-flow
    signal is re-raised after cleanup and therefore never appears as a
    successful report.  Repeated ``close`` calls return the original outcome
    with ``already_closed=True`` and do not call a closer twice.
    """

    attempted: int
    closed: int
    skipped: int
    failures: tuple[StageCloseFailure, ...] = ()
    already_closed: bool = False

    @property
    def ok(self) -> bool:
        """Whether every attempted closer completed without an exception."""

        return not self.failures

    @property
    def errors(self) -> tuple[str, ...]:
        """Compact ``stage: exception`` summaries for status reporting."""

        return tuple(f"{failure.stage}: {failure.message}" for failure in self.failures)

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible close summary."""

        return {
            "attempted": self.attempted,
            "closed": self.closed,
            "skipped": self.skipped,
            "errors": list(self.errors),
            "already_closed": self.already_closed,
            "ok": self.ok,
        }


def _configuration_error(message: str, field_name: str) -> ConfigurationError:
    return ConfigurationError(
        message,
        operation="workflow.configure",
        details={"field": field_name},
    )


def _time_value(value: Any, field_name: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, Real):
        raise _configuration_error(f"{field_name} must be a real number", field_name)
    result = float(value)
    if not math.isfinite(result) or result < 0.0:
        raise _configuration_error(
            f"{field_name} must be finite and non-negative", field_name
        )
    return result


def _lifecycle_locks(stages: Iterable[Stage]) -> list[threading.RLock]:
    """Return each stage lifecycle lock once in deterministic lock order."""

    lifecycles = {id(stage._lifecycle): stage._lifecycle for stage in stages}
    return [lifecycles[key].lock for key in sorted(lifecycles)]


def _claim_stages(stages: tuple[Stage, ...], owner: object) -> None:
    locks = _lifecycle_locks(stages)
    for lock in locks:
        lock.acquire()
    try:
        for stage in stages:
            lifecycle = stage._lifecycle
            if lifecycle.closed:
                raise _configuration_error(
                    f"stage {stage.name!r} has already been closed", "stages"
                )
            if lifecycle.owner is not None:
                raise _configuration_error(
                    f"stage {stage.name!r} is already owned by another pipeline",
                    "stages",
                )
        for stage in stages:
            stage._lifecycle.owner = owner
    finally:
        for lock in reversed(locks):
            lock.release()


class Pipeline:
    """Linearly owned stage order with a synchronous execution loop.

    ``Pipeline | Stage`` and ``Stage | Stage`` return a new pipeline; execution
    order is always left to right.  ``run_one`` processes one value, while
    ``run`` lazily yields one result for each input in order.  ``timeout`` is a
    fresh relative deadline per item; ``deadline`` is an optional absolute
    monotonic deadline shared by the invocation.  When both are provided the
    earlier boundary wins.

    Composition transfers stage ownership into the returned pipeline.  The
    source pipeline becomes closed and cannot run or close those stages.  This
    move-like rule deliberately prevents the same closer-backed ``Stage`` from
    being shared by two independently closed pipelines.

    A pipeline owns only stage *close calls*.  Resource objects in
    :class:`WorkflowContext` remain caller-owned, and this class performs no
    hidden concurrency, acquisition, scheduling or retry.
    """

    def __init__(
        self,
        stages: Iterable[Stage] = (),
        *,
        clock: Callable[[], float] = time.monotonic,
        logger: logging.Logger | None = None,
        _claim: bool = True,
    ) -> None:
        try:
            normalized = tuple(stages)
        except TypeError as exc:
            error = _configuration_error("stages must be iterable", "stages")
            raise error from exc
        if any(not isinstance(stage, Stage) for stage in normalized):
            raise _configuration_error("pipeline entries must be Stage objects", "stages")
        names = [stage.name for stage in normalized]
        if len(names) != len(set(names)):
            raise _configuration_error("pipeline stage names must be unique", "stages")
        if not callable(clock):
            raise _configuration_error("clock must be callable", "clock")
        self._stages = normalized
        self._clock = clock
        self._logger = logger or _LOG
        self._owner = object()
        self._state = threading.Condition(threading.RLock())
        self._active_runs = 0
        self._active_threads: dict[int, int] = {}
        self._closing = False
        self._closing_owner: int | None = None
        self._closed = False
        self._transferred = False
        self._close_report: WorkflowCloseReport | None = None
        if _claim:
            _claim_stages(self._stages, self._owner)

    @property
    def stages(self) -> tuple[Stage, ...]:
        """Stages in their exact deterministic execution order."""

        return self._stages

    @property
    def closed(self) -> bool:
        """Whether close has started and no further items may run."""

        with self._state:
            return self._closing or self._closed

    def __len__(self) -> int:
        return len(self._stages)

    def __iter__(self) -> Iterator[Stage]:
        return iter(self._stages)

    def __or__(self, other: Any) -> "Pipeline":
        """Move stages into a new left-to-right composition.

        A closer-backed stage is a linear resource, not a reusable value.  On
        success the source pipeline(s) are marked transferred and only the
        returned pipeline may execute or close the combined stages.
        """

        if isinstance(other, Stage):
            additions = (other,)
            sources = (self,)
        elif isinstance(other, Pipeline):
            additions = other.stages
            sources = (self,) if other is self else (self, other)
        else:
            return NotImplemented
        return self._compose_owned(self.stages + additions, sources)

    def _prepend(self, stage: Stage) -> "Pipeline":
        """Transactionally compose an unowned stage before this pipeline."""

        return self._compose_owned((stage,) + self.stages, (self,))

    def _compose_owned(
        self,
        combined: tuple[Stage, ...],
        sources: tuple["Pipeline", ...],
    ) -> "Pipeline":
        """Atomically validate and transfer all combined stage lifetimes."""

        state_locks = sorted(
            {id(source._state): source._state for source in sources}.values(),
            key=id,
        )
        for state in state_locks:
            state.acquire()
        try:
            for source in sources:
                if source._closing or source._closed:
                    raise WorkflowClosedError(
                        "cannot compose a closed pipeline",
                        operation="workflow.compose",
                    )
                if source._active_runs:
                    raise WorkflowError(
                        "cannot compose a pipeline while it is running",
                        operation="workflow.compose",
                        code="workflow_busy",
                    )

            result = Pipeline(
                combined,
                clock=self._clock,
                logger=self._logger,
                _claim=False,
            )
            lifecycle_locks = _lifecycle_locks(combined)
            for lock in lifecycle_locks:
                lock.acquire()
            try:
                source_owners = {source._owner for source in sources}
                for stage in combined:
                    lifecycle = stage._lifecycle
                    if lifecycle.closed:
                        raise _configuration_error(
                            f"stage {stage.name!r} has already been closed", "stages"
                        )
                    if lifecycle.owner is not None and lifecycle.owner not in source_owners:
                        raise _configuration_error(
                            f"stage {stage.name!r} is owned by another pipeline",
                            "stages",
                        )
                for stage in combined:
                    stage._lifecycle.owner = result._owner
            finally:
                for lock in reversed(lifecycle_locks):
                    lock.release()

            for source in sources:
                source._transferred = True
                source._closed = True
                source._close_report = WorkflowCloseReport(
                    attempted=0,
                    closed=0,
                    skipped=len(source.stages),
                    already_closed=True,
                )
                source._state.notify_all()
            return result
        finally:
            for state in reversed(state_locks):
                state.release()

    def _ensure_open(self) -> None:
        with self._state:
            if self._closing or self._closed:
                suffix = " (ownership transferred)" if self._transferred else ""
                raise WorkflowClosedError(
                    f"pipeline has already been closed{suffix}",
                    operation="workflow.run",
                )

    def _admit_run(self) -> None:
        with self._state:
            if self._closing or self._closed:
                suffix = " (ownership transferred)" if self._transferred else ""
                raise WorkflowClosedError(
                    f"pipeline has already been closed{suffix}",
                    operation="workflow.run",
                )
            self._active_runs += 1
            thread_id = threading.get_ident()
            self._active_threads[thread_id] = self._active_threads.get(thread_id, 0) + 1

    def _finish_run(self) -> None:
        with self._state:
            self._active_runs -= 1
            thread_id = threading.get_ident()
            remaining = self._active_threads.get(thread_id, 0) - 1
            if remaining > 0:
                self._active_threads[thread_id] = remaining
            else:
                self._active_threads.pop(thread_id, None)
            if self._active_runs == 0:
                self._state.notify_all()

    def _details(
        self,
        *,
        item: Any,
        item_index: int,
        started: float,
        stage: Stage | None,
        stage_index: int | None,
        now: float | None = None,
    ) -> dict[str, Any]:
        current = float(self._clock() if now is None else now)
        return {
            "stage": None if stage is None else stage.name,
            "stage_index": stage_index,
            "item_index": item_index,
            "item_type": type(item).__name__,
            "elapsed_ms": max(0.0, (current - started) * 1000.0),
        }

    def _log_error(
        self,
        error: WorkflowError,
        *,
        cause: Exception | None = None,
    ) -> None:
        extra = {
            "event": "workflow_error",
            "operation": error.operation,
            "error_code": error.code,
            **dict(error.details),
        }
        self._logger.error(
            "workflow execution failed",
            extra=extra,
            exc_info=(None if cause is None else
                      (type(cause), cause, cause.__traceback__)),
        )

    def _check_stop(
        self,
        context: WorkflowContext,
        *,
        item: Any,
        item_index: int,
        started: float,
        stage: Stage | None,
        stage_index: int | None,
    ) -> None:
        now = float(self._clock())
        details = self._details(
            item=item,
            item_index=item_index,
            started=started,
            stage=stage,
            stage_index=stage_index,
            now=now,
        )
        if context.cancellation.cancelled:
            details["reason"] = context.cancellation.reason
            error = WorkflowCancelled(
                "workflow item was cancelled",
                operation="workflow.run",
                details=details,
            )
            self._log_error(error)
            raise error
        if context.deadline is not None and now >= context.deadline:
            details["deadline"] = context.deadline
            error = WorkflowTimeout(
                "workflow item deadline expired",
                operation="workflow.run",
                details=details,
            )
            self._log_error(error)
            raise error

    def _check_resources(
        self,
        stage: Stage,
        *,
        context: WorkflowContext,
        item: Any,
        item_index: int,
        stage_index: int,
        started: float,
    ) -> None:
        missing = sorted(
            kind.value
            for kind in stage.requires
            if context.resources.get(kind) is None
        )
        if not missing:
            return
        details = self._details(
            item=item,
            item_index=item_index,
            started=started,
            stage=stage,
            stage_index=stage_index,
        )
        details.update(
            {
                "required_resources": sorted(kind.value for kind in stage.requires),
                "missing_resources": missing,
            }
        )
        error = WorkflowResourceError(
            f"stage {stage.name!r} is missing required resources",
            operation="workflow.stage.resources",
            details=details,
        )
        self._log_error(error)
        raise error

    def run_one(
        self,
        item: Any,
        context: WorkflowContext | None = None,
        *,
        timeout: float | None = None,
        deadline: float | None = None,
        item_index: int = 0,
    ) -> Any:
        """Run one item through every stage in order and return the final value.

        ``timeout`` is relative to this call.  ``deadline`` and a deadline
        already present on ``context`` are absolute values from the pipeline's
        monotonic clock.  Ordinary stage exceptions become :class:`StageError`
        with cause/context; direct ``BaseException`` control flow passes through.
        """

        self._admit_run()
        try:
            return self._run_one_impl(
                item,
                context,
                timeout=timeout,
                deadline=deadline,
                item_index=item_index,
            )
        finally:
            self._finish_run()

    def _run_one_impl(
        self,
        item: Any,
        context: WorkflowContext | None,
        *,
        timeout: float | None,
        deadline: float | None,
        item_index: int,
    ) -> Any:
        if context is None:
            context = WorkflowContext()
        if not isinstance(context, WorkflowContext):
            raise _configuration_error("context must be a WorkflowContext", "context")
        if isinstance(item_index, bool) or not isinstance(item_index, int) or item_index < 0:
            raise _configuration_error("item_index must be non-negative", "item_index")

        started = float(self._clock())
        relative = _time_value(timeout, "timeout")
        explicit = _time_value(deadline, "deadline")
        candidates = [value for value in (context.deadline, explicit) if value is not None]
        if relative is not None:
            candidates.append(started + relative)
        effective_deadline = min(candidates) if candidates else None
        item_context = context.for_item(
            item_index=item_index,
            deadline=effective_deadline,
        )

        current = item
        self._check_stop(
            item_context,
            item=item,
            item_index=item_index,
            started=started,
            stage=None,
            stage_index=None,
        )
        for stage_index, stage in enumerate(self.stages):
            self._check_stop(
                item_context,
                item=item,
                item_index=item_index,
                started=started,
                stage=stage,
                stage_index=stage_index,
            )
            self._check_resources(
                stage,
                context=item_context,
                item=item,
                item_index=item_index,
                stage_index=stage_index,
                started=started,
            )
            try:
                current = stage.invoke(current, item_context)
            except (WorkflowCancelled, WorkflowTimeout) as error:
                self._log_error(error)
                raise
            except Exception as exc:
                details = self._details(
                    item=item,
                    item_index=item_index,
                    started=started,
                    stage=stage,
                    stage_index=stage_index,
                )
                details["cause_type"] = type(exc).__name__
                error = StageError(
                    f"stage {stage.name!r} failed",
                    operation="workflow.stage.execute",
                    details=details,
                )
                self._log_error(error, cause=exc)
                raise error from exc
            self._check_stop(
                item_context,
                item=item,
                item_index=item_index,
                started=started,
                stage=stage,
                stage_index=stage_index,
            )
        return current

    def run(
        self,
        items: Iterable[Any],
        context: WorkflowContext | None = None,
        *,
        timeout: float | None = None,
        deadline: float | None = None,
    ) -> Iterator[Any]:
        """Yield results in input order, applying ``timeout`` freshly per item.

        The iterator is lazy and runs entirely on its consumer's thread.  An
        absolute ``deadline`` applies to every item and can therefore serve as a
        whole-loop boundary; ``timeout`` is recomputed inside each ``run_one``.
        """

        for item_index, item in enumerate(items):
            yield self.run_one(
                item,
                context,
                timeout=timeout,
                deadline=deadline,
                item_index=item_index,
            )

    def close(self) -> WorkflowCloseReport:
        """Drain active calls, then close stages in reverse order exactly once."""

        with self._state:
            if self._closed:
                report = self._close_report or WorkflowCloseReport(0, 0, 0)
                return replace(report, already_closed=True)
            if self._closing:
                if self._closing_owner == threading.get_ident():
                    raise WorkflowError(
                        "pipeline close cannot recursively wait for itself",
                        operation="workflow.close",
                        code="reentrant_close",
                    )
                while not self._closed:
                    self._state.wait()
                report = self._close_report or WorkflowCloseReport(0, 0, 0)
                return replace(report, already_closed=True)
            if self._active_threads.get(threading.get_ident(), 0):
                raise WorkflowError(
                    "a running stage cannot close its own pipeline",
                    operation="workflow.close",
                    code="reentrant_close",
                )
            self._closing = True
            self._closing_owner = threading.get_ident()
            try:
                while self._active_runs:
                    self._state.wait()
            except BaseException:
                self._closing = False
                self._closing_owner = None
                self._state.notify_all()
                raise

        control_flow: list[BaseException] = []
        attempted = 0
        closed = 0
        skipped = 0
        failures: list[StageCloseFailure] = []
        for stage in reversed(self.stages):
            try:
                did_close = stage._close_owned(self._owner)
                if did_close:
                    attempted += 1
                    closed += 1
                else:
                    skipped += 1
            except BaseException as exc:
                attempted += 1
                self._logger.error(
                    "workflow stage close failed",
                    extra={
                        "event": "workflow_close_error",
                        "operation": "workflow.close",
                        "stage": stage.name,
                        "error_type": type(exc).__name__,
                    },
                    exc_info=(type(exc), exc, exc.__traceback__),
                )
                if isinstance(exc, Exception):
                    failures.append(StageCloseFailure(stage.name, exc))
                else:
                    control_flow.append(exc)
        report = WorkflowCloseReport(
            attempted=attempted,
            closed=closed,
            skipped=skipped,
            failures=tuple(failures),
        )
        with self._state:
            self._close_report = report
            self._closed = True
            self._closing = False
            self._closing_owner = None
            self._state.notify_all()

        if control_flow:
            first = control_flow[0]
            for later in control_flow[1:]:
                try:
                    first.add_note(
                        f"additional close control flow: {type(later).__name__}: {later}"
                    )
                except Exception:
                    pass
            raise first
        return report

    @property
    def close_report(self) -> WorkflowCloseReport | None:
        """The first close outcome, including context-manager teardown."""

        with self._state:
            return self._close_report

    def __enter__(self) -> "Pipeline":
        self._ensure_open()
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        try:
            report = self.close()
        except BaseException as cleanup_error:
            if exc is not None:
                self._logger.error(
                    "workflow cleanup failed while preserving body exception",
                    extra={
                        "event": "workflow_close_error",
                        "operation": "workflow.close",
                        "error_type": type(cleanup_error).__name__,
                    },
                    exc_info=(
                        type(cleanup_error),
                        cleanup_error,
                        cleanup_error.__traceback__,
                    ),
                )
                return None
            raise
        if exc is None and report.failures:
            raise WorkflowCleanupError(
                "one or more workflow stages failed to close",
                operation="workflow.close",
                details={"errors": list(report.errors)},
            )
        return None


CloseReport = WorkflowCloseReport


__all__ = [
    "CloseReport",
    "Pipeline",
    "StageCloseFailure",
    "WorkflowCloseReport",
]
