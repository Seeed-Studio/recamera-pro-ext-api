"""Public exception hierarchy for the reCamera Python AI kit.

The kit is expected to run unattended on an embedded device.  A caller must be
able to distinguish a bad model/input from a transient transport failure or a
busy hardware resource without parsing human-readable strings.  Every public
exception therefore carries a stable ``code`` and an ``operation`` while its
message remains useful in logs.

The hierarchy is intentionally small.  Backend-specific details (an errno, an
HTTP status, or a native SDK return code) belong in ``details`` and should not
become new application-facing exception classes.
"""
from __future__ import annotations

import copy
from collections.abc import Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping, Optional


class _FrozenSequence(Sequence[Any]):
    """Immutable sequence that still compares naturally with JSON lists.

    Subclassing ``list`` is insufficient: ``list.append(value, item)`` can
    bypass overridden mutation methods.  Composition avoids that escape hatch
    while preserving iteration, indexing and ``== [..]`` compatibility.
    """

    __slots__ = ("_items",)

    def __init__(self, items) -> None:
        self._items = tuple(items)

    def __getitem__(self, index):
        return self._items[index]

    def __len__(self) -> int:
        return len(self._items)

    def __iter__(self):
        return iter(self._items)

    def __repr__(self) -> str:
        return repr(list(self._items))

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Sequence) and not isinstance(other, (str, bytes, bytearray)):
            return tuple(self._items) == tuple(other)
        return False

    @staticmethod
    def _immutable(*_args, **_kwargs):
        raise TypeError("error detail sequences are read-only")

    append = _immutable
    clear = _immutable
    extend = _immutable
    insert = _immutable
    pop = _immutable
    remove = _immutable
    reverse = _immutable
    sort = _immutable


def _freeze(value: Any) -> Any:
    """Recursively freeze a copied JSON-like diagnostic value."""

    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item)
                                 for key, item in value.items()})
    if isinstance(value, list):
        return _FrozenSequence(_freeze(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: Any) -> Any:
    """Return ordinary JSON-compatible containers for serialization."""

    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, _FrozenSequence)):
        return [_thaw(item) for item in value]
    return value


@dataclass(frozen=True)
class ErrorContext:
    """Machine-readable context attached to :class:`KitError`.

    ``details`` is copied and made read-only so code catching an exception can
    safely pass the context to another thread or serialize it for diagnostics.
    Values should be JSON-compatible and must not contain credentials.
    """

    operation: str
    code: str
    retryable: bool
    details: Mapping[str, Any]


class KitError(Exception):
    """Base class for all documented kit failures.

    Parameters:
        message: Human-readable diagnosis.  It is safe to show this to an app
            developer, but it is not a stable value for program logic.
        operation: Stable name of the operation that failed, for example
            ``"model.load"`` or ``"frame.acquire"``.
        code: Stable short error code.  Subclasses provide a useful default.
        retryable: Whether retrying later *may* succeed without changing input.
        details: Optional non-secret backend diagnostics.
    """

    default_code = "kit_error"

    def __init__(
        self,
        message: str,
        *,
        operation: str = "unknown",
        code: Optional[str] = None,
        retryable: bool = False,
        details: Optional[Mapping[str, Any]] = None,
    ) -> None:
        super().__init__(str(message))
        copied = copy.deepcopy(dict(details or {}))
        self.context = ErrorContext(
            operation=str(operation),
            code=str(code or self.default_code),
            retryable=bool(retryable),
            details=_freeze(copied),
        )

    @property
    def operation(self) -> str:
        """Stable operation name associated with the failure."""

        return self.context.operation

    @property
    def code(self) -> str:
        """Stable error code suitable for application branching."""

        return self.context.code

    @property
    def retryable(self) -> bool:
        """Whether a later retry may succeed without changing the request."""

        return self.context.retryable

    @property
    def details(self) -> Mapping[str, Any]:
        """Read-only, non-secret backend details."""

        return self.context.details

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible representation for status endpoints."""

        return {
            "type": type(self).__name__,
            "message": str(self),
            "operation": self.operation,
            "code": self.code,
            "retryable": self.retryable,
            "details": _thaw(self.details),
        }


class ConfigurationError(KitError, ValueError):
    """An application manifest, option, or model declaration is invalid."""

    default_code = "invalid_configuration"


class AdapterError(KitError, RuntimeError):
    """A frame, result, control, audio, or image backend failed."""

    default_code = "adapter_error"


class ImageOperationError(AdapterError):
    """RGA or software image transformation failed."""

    default_code = "image_operation_failed"


class TransportError(AdapterError):
    """A local socket, HTTP compatibility endpoint, or subprocess failed."""

    default_code = "transport_error"


class DeviceControlError(AdapterError):
    """A device-control request was rejected or returned invalid state."""

    default_code = "device_control_error"


class CapabilityError(KitError):
    """A required device capability is absent or has not been verified."""

    default_code = "capability_unavailable"


class ResourceBusyError(KitError, RuntimeError):
    """A hardware resource is owned by another workflow."""

    default_code = "resource_busy"


class ResourceTimeoutError(ResourceBusyError, TimeoutError):
    """A resource did not become safe to acquire before the deadline."""

    default_code = "resource_timeout"


class InferenceError(KitError, RuntimeError):
    """Base class for model loading, input validation, and inference errors."""

    default_code = "inference_error"


class ModelLoadError(InferenceError):
    """An RKNN model could not be loaded or its runtime could not initialize."""

    default_code = "model_load_failed"


class InputValidationError(InferenceError, ValueError):
    """An inference input does not match the model/session contract."""

    default_code = "invalid_inference_input"


class BufferReleasedError(KitError, RuntimeError):
    """A borrowed or explicitly released image buffer was accessed."""

    default_code = "buffer_released"


def wrap_error(
    exc: BaseException,
    error_type: type[KitError],
    message: str,
    *,
    operation: str,
    code: Optional[str] = None,
    retryable: bool = False,
    details: Optional[Mapping[str, Any]] = None,
) -> KitError:
    """Create a typed public error while retaining the backend exception.

    Use it as ``raise wrap_error(...) from exc``.  Keeping ``__cause__`` gives
    detailed tracebacks to developers without exposing backend-specific types
    as part of the public API.
    """

    error = error_type(
        message,
        operation=operation,
        code=code,
        retryable=retryable,
        details=details,
    )
    error.__cause__ = exc
    return error


__all__ = [
    "AdapterError",
    "BufferReleasedError",
    "CapabilityError",
    "ConfigurationError",
    "DeviceControlError",
    "ErrorContext",
    "InferenceError",
    "ImageOperationError",
    "InputValidationError",
    "KitError",
    "ModelLoadError",
    "ResourceBusyError",
    "ResourceTimeoutError",
    "TransportError",
    "wrap_error",
]
