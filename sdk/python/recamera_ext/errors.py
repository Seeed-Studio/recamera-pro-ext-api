"""Typed exceptions for :mod:`recamera_ext`.

The native ABI reports a small, stable error-code set.  Historically the
Python wrapper formatted those values into generic ``RuntimeError`` strings,
which made callers parse text and caused the iterator API to hide transport
failures as an ordinary ``StopIteration``.  This module keeps the old Python
exception *families* (``RuntimeError``, ``OSError`` and ``ValueError``) while
adding machine-readable attributes:

``code``
    The negotiated :class:`ErrorCode`, or ``None`` for a local/unknown error.
``rc``
    The exact integer returned by the C ABI.  Open functions normally report a
    positive code through ``int *err``; operation functions return its negative.
``operation``
    The native operation that failed, such as ``"rc_ext_frame_next"``.
``detail``
    Optional local context that is safe to show in diagnostics.

Compatibility matters here.  For example, ``BusyError`` remains catchable as a
``RuntimeError``, ``LibraryLoadError`` remains catchable as an ``OSError``, and
``ResultTooLarge`` remains catchable as a ``ValueError``.
"""

from __future__ import annotations

from enum import IntEnum
from typing import Dict, Optional, Type

__all__ = [
    "ErrorCode",
    "RecameraError",
    "RecameraRuntimeError",
    "VersionError",
    "AuthenticationError",
    "AuthError",
    "BusyError",
    "ResourceBusyError",
    "FormatError",
    "BackpressureError",
    "RateLimitError",
    "InternalError",
    "UnknownNativeError",
    "CapabilityUnavailableError",
    "BufferReleasedError",
    "HandleClosedError",
    "AcquireTimeoutError",
    "FrameTimeoutError",
    "LibraryLoadError",
    "ResultTooLarge",
    "error_from_rc",
]


class ErrorCode(IntEnum):
    """Frozen extension-API error codes (``docs/api/spec.md`` section 1.3)."""

    OK = 0
    EVERSION = 1
    EAUTH = 2
    EBUSY = 3
    EFORMAT = 4
    EBACKPRESSURE = 5
    ERATELIMIT = 6
    EINTERNAL = 7


_DESCRIPTIONS = {
    ErrorCode.EVERSION: "client and server API versions do not overlap",
    ErrorCode.EAUTH: "authentication or source identity was rejected",
    ErrorCode.EBUSY: "the requested endpoint or resource is busy",
    ErrorCode.EFORMAT: "the request or wire payload has an invalid format",
    ErrorCode.EBACKPRESSURE: "the consumer was disconnected for backpressure",
    ErrorCode.ERATELIMIT: "the request exceeded an endpoint rate limit",
    ErrorCode.EINTERNAL: "the server or transport reported an internal error",
}


class RecameraError(Exception):
    """Base class carrying structured extension-API failure information."""

    def __init__(
        self,
        message: Optional[str] = None,
        *,
        operation: Optional[str] = None,
        code: Optional[ErrorCode] = None,
        rc: Optional[int] = None,
        detail: Optional[str] = None,
    ) -> None:
        self.operation = operation
        self.code = code
        self.rc = None if rc is None else int(rc)
        self.detail = detail
        if message is None:
            message = self._format_message()
        super().__init__(message)

    @property
    def code_value(self) -> Optional[int]:
        """Numeric error code, including unknown native return values."""

        if self.code is not None:
            return int(self.code)
        if self.rc is not None and self.rc != 0:
            return abs(self.rc)
        return None

    @property
    def retryable(self) -> bool:
        """Whether retrying later is generally reasonable.

        This is a hint, not a retry policy.  Backpressure requires releasing old
        leases first, and an internal error may still be permanent.
        """

        return self.code in {
            ErrorCode.EBUSY,
            ErrorCode.EBACKPRESSURE,
            ErrorCode.ERATELIMIT,
            ErrorCode.EINTERNAL,
        } or isinstance(self, AcquireTimeoutError)

    def _format_message(self) -> str:
        head = f"{self.operation} failed" if self.operation else type(self).__name__
        parts = [head]
        if self.code is not None:
            desc = _DESCRIPTIONS.get(self.code)
            label = f"{self.code.name}({int(self.code)})"
            parts.append(f"{label}: {desc}" if desc else label)
        elif self.rc not in (None, 0):
            parts.append(f"unknown native error {abs(int(self.rc))}")
        if self.rc is not None:
            parts.append(f"rc={self.rc}")
        if self.detail:
            parts.append(str(self.detail))
        return "; ".join(parts)


class RecameraRuntimeError(RecameraError, RuntimeError):
    """Base for runtime failures; preserves ``except RuntimeError`` callers."""


class VersionError(RecameraRuntimeError):
    """Client/server API version ranges do not overlap."""


class AuthenticationError(RecameraRuntimeError):
    """Peer identity, app token, or reserved source id was rejected."""


AuthError = AuthenticationError


class BusyError(RecameraRuntimeError):
    """An endpoint subscription or device resource is currently unavailable."""


ResourceBusyError = BusyError


class FormatError(RecameraRuntimeError):
    """A request, buffer layout, or received wire record is malformed."""


class BackpressureError(RecameraRuntimeError):
    """The server disconnected a consumer that held data for too long."""


class RateLimitError(RecameraRuntimeError):
    """An endpoint rejected work because its rate quota was exceeded."""


class InternalError(RecameraRuntimeError):
    """The server, transport, or native wrapper encountered an internal error."""


class UnknownNativeError(RecameraRuntimeError):
    """A native return code is not part of the frozen public error enum."""


class CapabilityUnavailableError(RecameraRuntimeError):
    """The loaded ``librecamera_ext`` lacks an optional API capability."""


class BufferReleasedError(RecameraRuntimeError):
    """A borrowed dma-buf or probe payload was accessed after release."""


class HandleClosedError(RecameraRuntimeError):
    """An operation was attempted after its owning native handle was closed."""


class AcquireTimeoutError(RecameraError, TimeoutError):
    """A strict ``acquire()`` call reached its timeout without a record."""


# Descriptive compatibility alias for callers that only acquire camera frames.
FrameTimeoutError = AcquireTimeoutError


class LibraryLoadError(RecameraError, OSError):
    """No compatible ``librecamera_ext.so.1`` could be loaded and bound."""


class ResultTooLarge(RecameraError, ValueError):
    """A result datagram would exceed the local conservative wire budget."""


_ERROR_TYPES: Dict[ErrorCode, Type[RecameraRuntimeError]] = {
    ErrorCode.EVERSION: VersionError,
    ErrorCode.EAUTH: AuthenticationError,
    ErrorCode.EBUSY: BusyError,
    ErrorCode.EFORMAT: FormatError,
    ErrorCode.EBACKPRESSURE: BackpressureError,
    ErrorCode.ERATELIMIT: RateLimitError,
    ErrorCode.EINTERNAL: InternalError,
}


def error_from_rc(
    operation: str,
    rc: int,
    *,
    detail: Optional[str] = None,
) -> RecameraRuntimeError:
    """Build the typed exception for a native error return.

    ``rc`` may be a positive ``*err`` value from an ``open`` function or the
    negative value returned by an operation.  Zero is rejected because it means
    success and converting it into an exception almost certainly masks a wrapper
    bug.  Unknown values remain available through ``exc.rc``/``code_value``.
    """

    raw = int(getattr(rc, "value", rc))
    if raw == 0:
        raise ValueError("error_from_rc requires a non-zero native return code")
    try:
        code = ErrorCode(abs(raw))
    except ValueError:
        code = None
    cls: Type[RecameraRuntimeError] = _ERROR_TYPES.get(code, UnknownNativeError)
    return cls(operation=operation, code=code, rc=raw, detail=detail)
