"""Minimal production workflow primitives for reCamera Python applications.

The package intentionally exposes a synchronous sequential pipeline and a
separate bounded thread-safe input queue.  It does not claim to be an async DAG
engine, an NPU scheduler, an rkipc resource lease, or a substitute for appmgr's
device-ownership transition.
"""

from .node import (
    CancellationToken,
    ResourceKind,
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
from .queue import (
    BackpressureTimeoutError,
    DropPolicy,
    InputQueue,
    PutResult,
    PutStatus,
    QueueClosedError,
    QueueError,
    QueueStats,
    QueueTimeoutError,
    WorkflowBackpressureError,
)
from .runtime import CloseReport, Pipeline, StageCloseFailure, WorkflowCloseReport


__all__ = [
    "BackpressureTimeoutError",
    "CancellationToken",
    "CloseReport",
    "DropPolicy",
    "InputQueue",
    "Pipeline",
    "PutResult",
    "PutStatus",
    "QueueClosedError",
    "QueueError",
    "QueueStats",
    "QueueTimeoutError",
    "ResourceKind",
    "Stage",
    "StageCloseFailure",
    "StageError",
    "WorkflowBackpressureError",
    "WorkflowCancelled",
    "WorkflowCleanupError",
    "WorkflowCloseReport",
    "WorkflowClosedError",
    "WorkflowContext",
    "WorkflowError",
    "WorkflowResourceError",
    "WorkflowTimeout",
]
