"""Workflow stages, context and typed execution failures.

This module defines a deliberately small synchronous workflow contract.  A
``ResourceKind`` declaration means only that the caller must place a concrete
object in :class:`WorkflowContext.resources`; it does not acquire an rkipc
lease, arbitrate the NPU, start worker threads or infer device availability.
"""

from __future__ import annotations

import inspect
import math
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from numbers import Integral, Real
from types import MappingProxyType
from typing import Any

from ..errors import ConfigurationError, KitError
from ..resources import ResourceKind


class WorkflowError(KitError, RuntimeError):
    """Base class for structured synchronous-workflow failures."""

    default_code = "workflow_error"


class WorkflowCancelled(WorkflowError):
    """Execution stopped because its explicit cancellation token was set."""

    default_code = "workflow_cancelled"


class WorkflowTimeout(WorkflowError, TimeoutError):
    """An item exceeded its monotonic deadline at a stage boundary.

    A sequential Python callable cannot be preempted safely.  The runtime checks
    before and after each stage, so a blocking stage is reported immediately
    after it returns; cooperative stages can also inspect the context token and
    deadline themselves.
    """

    default_code = "workflow_timeout"


class WorkflowResourceError(WorkflowError):
    """A stage's declared resource was absent from the explicit context."""

    default_code = "workflow_resource_missing"


class WorkflowClosedError(WorkflowError):
    """Execution was attempted after its owning pipeline was closed."""

    default_code = "workflow_closed"


class WorkflowCleanupError(WorkflowError):
    """One or more stage closers failed during context-manager teardown."""

    default_code = "workflow_cleanup_failed"


class StageError(WorkflowError):
    """A stage callable raised an ordinary :class:`Exception`.

    The original exception is always retained as ``__cause__``.  Process
    control flow deriving directly from :class:`BaseException`, such as
    ``KeyboardInterrupt`` and ``SystemExit``, is never converted to this type.
    """

    default_code = "workflow_stage_failed"


def _configuration_error(message: str, field_name: str) -> ConfigurationError:
    return ConfigurationError(
        message,
        operation="workflow.configure",
        details={"field": field_name},
    )


def _deadline_value(value: Any, field_name: str) -> float | None:
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


class CancellationToken:
    """Thread-safe, cooperative cancellation signal shared by workflow stages.

    ``cancel`` is idempotent and the first non-empty reason wins.  The token
    cannot forcibly interrupt a Python callable; a stage doing long work should
    inspect ``cancelled`` or call :meth:`raise_if_cancelled` at safe points.
    """

    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._reason = ""

    @property
    def cancelled(self) -> bool:
        """Whether cancellation has been requested."""

        return self._event.is_set()

    @property
    def reason(self) -> str:
        """Stable first cancellation reason, or an empty string."""

        with self._lock:
            return self._reason

    def cancel(self, reason: str = "") -> bool:
        """Request cancellation; return ``True`` only for the first request."""

        text = str(reason or "")
        with self._lock:
            if self._event.is_set():
                # An early caller may only know that cancellation is needed,
                # while a later caller knows why.  Preserve the documented
                # first *non-empty* reason without changing idempotent status.
                if not self._reason and text:
                    self._reason = text
                return False
            self._reason = text
            self._event.set()
            return True

    def wait(self, timeout: float | None = None) -> bool:
        """Wait for cancellation using :class:`threading.Event` semantics."""

        if timeout is not None:
            timeout = _deadline_value(timeout, "timeout")
        return self._event.wait(timeout)

    def raise_if_cancelled(
        self,
        *,
        stage: str | None = None,
        item_index: int | None = None,
        elapsed_ms: float = 0.0,
    ) -> None:
        """Raise :class:`WorkflowCancelled` with machine-readable context."""

        if not self.cancelled:
            return
        raise WorkflowCancelled(
            "workflow item was cancelled",
            operation="workflow.run",
            details={
                "stage": stage,
                "item_index": item_index,
                "elapsed_ms": float(elapsed_ms),
                "reason": self.reason,
            },
        )


@dataclass(frozen=True, slots=True)
class WorkflowContext:
    """Explicit resources and per-run state passed to each stage.

    ``resources`` is copied into a read-only mapping keyed by
    :class:`~kit.resources.ResourceKind`; a key with value ``None`` is treated
    as unavailable.  The runtime never acquires or releases these objects.
    ``metadata`` is caller-owned descriptive state and is shallow-copied.
    ``deadline`` is an absolute monotonic timestamp for the current item, not a
    wall-clock time.  ``item_index`` is filled by :class:`Pipeline`.
    """

    resources: Mapping[ResourceKind, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)
    cancellation: CancellationToken = field(default_factory=CancellationToken)
    deadline: float | None = None
    item_index: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.resources, Mapping):
            raise _configuration_error("resources must be a mapping", "resources")
        normalized: dict[ResourceKind, Any] = {}
        for raw_kind, resource in self.resources.items():
            try:
                kind = raw_kind if isinstance(raw_kind, ResourceKind) else ResourceKind(raw_kind)
            except (TypeError, ValueError) as exc:
                error = _configuration_error(
                    f"unknown workflow resource: {raw_kind!r}", "resources"
                )
                raise error from exc
            if kind in normalized:
                raise _configuration_error(
                    f"resource {kind.value!r} was provided more than once", "resources"
                )
            normalized[kind] = resource
        if not isinstance(self.metadata, Mapping) or any(
            not isinstance(key, str) for key in self.metadata
        ):
            raise _configuration_error(
                "metadata must be a mapping with string keys", "metadata"
            )
        if not isinstance(self.cancellation, CancellationToken):
            raise _configuration_error(
                "cancellation must be a CancellationToken", "cancellation"
            )
        if self.item_index is not None:
            if isinstance(self.item_index, bool) or not isinstance(self.item_index, Integral):
                raise _configuration_error("item_index must be an integer", "item_index")
            if int(self.item_index) < 0:
                raise _configuration_error(
                    "item_index must be non-negative", "item_index"
                )
            object.__setattr__(self, "item_index", int(self.item_index))
        object.__setattr__(self, "deadline", _deadline_value(self.deadline, "deadline"))
        object.__setattr__(self, "resources", MappingProxyType(normalized))
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))

    def for_item(
        self,
        *,
        item_index: int,
        deadline: float | None,
    ) -> "WorkflowContext":
        """Return a context view for one item, sharing resources and token."""

        return replace(self, item_index=item_index, deadline=deadline)

    def get_resource(self, kind: ResourceKind | str, default: Any = None) -> Any:
        """Return an explicitly supplied resource without acquiring anything."""

        try:
            normalized = kind if isinstance(kind, ResourceKind) else ResourceKind(kind)
        except (TypeError, ValueError) as exc:
            error = _configuration_error(f"unknown resource: {kind!r}", "resource")
            raise error from exc
        resource = self.resources.get(normalized, default)
        return default if resource is None else resource

    def require_resource(self, kind: ResourceKind | str) -> Any:
        """Return one resource or fail closed with ``WorkflowResourceError``."""

        try:
            normalized = kind if isinstance(kind, ResourceKind) else ResourceKind(kind)
        except (TypeError, ValueError) as exc:
            error = _configuration_error(f"unknown resource: {kind!r}", "resource")
            raise error from exc
        resource = self.resources.get(normalized)
        if resource is None:
            raise WorkflowResourceError(
                f"required workflow resource is missing: {normalized.value}",
                operation="workflow.resource.require",
                details={
                    "resource": normalized.value,
                    "item_index": self.item_index,
                },
            )
        return resource

    def remaining(self, clock: Callable[[], float] = time.monotonic) -> float | None:
        """Return non-negative seconds remaining, or ``None`` without deadline."""

        if self.deadline is None:
            return None
        return max(0.0, self.deadline - float(clock()))

    def raise_if_cancelled(self, *, stage: str | None = None, elapsed_ms: float = 0.0) -> None:
        """Delegate a contextual cooperative-cancellation check to the token."""

        self.cancellation.raise_if_cancelled(
            stage=stage,
            item_index=self.item_index,
            elapsed_ms=elapsed_ms,
        )


def _call_mode(function: Callable[..., Any]) -> str:
    try:
        signature = inspect.signature(function)
    except (TypeError, ValueError):
        # C callables without inspectable signatures most commonly consume one
        # value.  They can be wrapped explicitly when context is required.
        return "item"
    marker = object()

    # An explicitly named ``context`` parameter is unambiguous, even when it
    # has a default.  Prefer a keyword for positional-or-keyword parameters so
    # wrappers with ``*args`` cannot accidentally consume the context value.
    context_parameter = signature.parameters.get("context")
    if context_parameter is not None:
        if context_parameter.kind is inspect.Parameter.POSITIONAL_ONLY:
            try:
                signature.bind(marker, marker)
            except TypeError:
                pass
            else:
                return "item_context"
        elif context_parameter.kind in (
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        ):
            try:
                signature.bind(marker, context=marker)
            except TypeError:
                pass
            else:
                return "item_keyword_context"

    # Prefer the smallest valid call.  In particular, ``lambda value,
    # offset=1`` and ``def wrapper(*args)`` are ordinary one-item transforms;
    # their optional/variadic positions must not be hijacked for context.
    candidates = (
        ("item", (marker,), {}),
        ("item_context", (marker, marker), {}),
    )
    for mode, args, kwargs in candidates:
        try:
            signature.bind(*args, **kwargs)
        except TypeError:
            continue
        return mode
    raise _configuration_error(
        "stage callable must accept item or item plus WorkflowContext",
        "callable",
    )


class _StageLifecycle:
    """Mutable ownership cell kept out of the public frozen Stage value."""

    __slots__ = ("closed", "lock", "owner")

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.owner: object | None = None
        self.closed = False


@dataclass(frozen=True, slots=True)
class Stage:
    """One named synchronous transform in a deterministic pipeline.

    ``callable`` may accept either ``item`` or ``(item, context)`` (including a
    keyword-only ``context``).  Its signature is inspected once at construction
    so an internal ``TypeError`` is never mistaken for arity negotiation.
    ``requires`` is declarative and checked before every invocation.  ``closer``
    is optional; otherwise a callable ``close`` attribute on the transform is
    used by :meth:`Pipeline.close`.
    """

    name: str
    callable: Callable[..., Any]
    requires: frozenset[ResourceKind] = frozenset()
    closer: Callable[[], Any] | None = field(default=None, repr=False, compare=False)
    _mode: str = field(init=False, repr=False, compare=False)
    _lifecycle: _StageLifecycle = field(
        default_factory=_StageLifecycle,
        init=False,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise _configuration_error("stage name must be a non-empty string", "name")
        if not callable(self.callable):
            raise _configuration_error("stage callable must be callable", "callable")
        try:
            requirements = frozenset(
                kind if isinstance(kind, ResourceKind) else ResourceKind(kind)
                for kind in self.requires
            )
        except (TypeError, ValueError) as exc:
            error = _configuration_error("stage requires an unknown resource", "requires")
            raise error from exc
        if self.closer is not None and not callable(self.closer):
            raise _configuration_error("stage closer must be callable", "closer")
        object.__setattr__(self, "name", self.name.strip())
        object.__setattr__(self, "requires", requirements)
        object.__setattr__(self, "_mode", _call_mode(self.callable))

    def invoke(self, item: Any, context: WorkflowContext) -> Any:
        """Invoke the transform using its prevalidated context calling mode."""

        if self._mode == "item_context":
            return self.callable(item, context)
        if self._mode == "item_keyword_context":
            return self.callable(item, context=context)
        return self.callable(item)

    def _closer(self) -> Callable[[], Any] | None:
        closer = self.closer
        if closer is None:
            discovered = getattr(self.callable, "close", None)
            closer = discovered if callable(discovered) else None
        return closer

    def _close_owned(self, owner: object) -> bool:
        """Close exactly once for the pipeline holding ``owner``."""

        lifecycle = self._lifecycle
        with lifecycle.lock:
            if lifecycle.owner is not owner:
                raise WorkflowClosedError(
                    f"stage {self.name!r} is not owned by this pipeline",
                    operation="workflow.close",
                    details={"stage": self.name},
                )
            if lifecycle.closed:
                return False
            lifecycle.closed = True
            closer = self._closer()
            if closer is None:
                return False
            # Keep the lifecycle lock while invoking the closer.  This is a
            # one-shot teardown path and prevents a direct close/transfer race.
            closer()
            return True

    def close(self) -> bool:
        """Close an unowned stage exactly once.

        Once a stage is placed in a :class:`Pipeline`, that pipeline owns its
        lifetime and direct closure is rejected.  This avoids a second
        pipeline or caller invalidating a resource during execution.
        """

        lifecycle = self._lifecycle
        with lifecycle.lock:
            if lifecycle.owner is not None:
                raise WorkflowClosedError(
                    f"stage {self.name!r} lifetime is owned by a pipeline",
                    operation="workflow.close",
                    details={"stage": self.name},
                )
            if lifecycle.closed:
                return False
            lifecycle.closed = True
            closer = self._closer()
        if closer is None:
            return False
        closer()
        return True

    def __or__(self, other: Any):
        """Compose ``Stage | Stage`` or ``Stage | Pipeline`` in order."""

        from .runtime import Pipeline

        if isinstance(other, Stage):
            # Pipeline claims all stages only after validating the complete
            # tuple, so a duplicate/owned right operand cannot strand ``self``
            # under an unreachable temporary owner.
            return Pipeline((self, other))
        if isinstance(other, Pipeline):
            return other._prepend(self)
        return NotImplemented


__all__ = [
    "CancellationToken",
    "ResourceKind",
    "Stage",
    "StageError",
    "WorkflowCancelled",
    "WorkflowCleanupError",
    "WorkflowClosedError",
    "WorkflowContext",
    "WorkflowError",
    "WorkflowResourceError",
    "WorkflowTimeout",
]
