"""Single entry point for composing reCamera AI workflow services."""
from __future__ import annotations

from dataclasses import dataclass
import threading
from typing import Any, Callable, Iterable, Mapping, Optional

from .capabilities import Capabilities, capabilities as discover_capabilities
from .errors import AdapterError, KitError, ModelLoadError
from .diagnostics import get_logger


log = get_logger("device")


@dataclass(frozen=True)
class CloseReport:
    """Result of closing resources owned by a :class:`Device`."""

    closed: int
    errors: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        """Whether every registered resource closed successfully."""

        return not self.errors


# Explicit name used at the package root.  ``CloseReport`` remains an alias in
# this module for compatibility with early adopters of ``kit.device``.
DeviceCloseReport = CloseReport


class Device:
    """Capability-aware factory and lifetime owner for AI services.

    ``Device`` does not make HTTP CGI the media/data plane.  Camera frames,
    result injection, RGA, and RKNN use their native adapters; the CGI backend
    remains only a temporary control-plane fallback for settings.

    Factories are injectable for tests and vendor backends.  Resources returned
    by this object are closed in reverse creation order when the device context
    exits.
    """

    def __init__(
        self,
        capability_snapshot: Capabilities,
        *,
        factories: Optional[Mapping[str, Callable[..., Any]]] = None,
    ) -> None:
        self.capabilities = capability_snapshot
        self._factories = dict(factories or {})
        self._owned: list[Any] = []
        self._state = threading.Condition(threading.RLock())
        self._active_creations = 0
        self._creator_threads: dict[int, int] = {}
        self._closing = False
        self._closing_owner: int | None = None
        self._closed = False
        self._close_report: Optional[CloseReport] = None

    @classmethod
    def open(
        cls,
        *,
        refresh: bool = True,
        require_verified: Iterable[str] = (),
        factories: Optional[Mapping[str, Callable[..., Any]]] = None,
    ) -> "Device":
        """Discover the device and optionally require negotiated capabilities.

        Current firmware discovery is filesystem/environment based, so socket
        presence remains ``UNKNOWN`` rather than verified.  Passing a name in
        ``require_verified`` therefore fails closed until a native handshake
        reports that capability.
        """

        snapshot = discover_capabilities(refresh=refresh)
        for name in require_verified:
            snapshot.require(name)
        return cls(snapshot, factories=factories)

    def _ensure_open(self) -> None:
        with self._state:
            if self._closing or self._closed:
                raise AdapterError(
                    "Device has already been closed",
                    operation="device.resource",
                    code="device_closed",
                )

    def _factory(self, name: str, default: Callable[..., Any]) -> Callable[..., Any]:
        return self._factories.get(name, default)

    def _create_owned(self, factory: Callable[..., Any], *args, **kwargs) -> Any:
        """Create and register a resource as one close-synchronized action."""

        with self._state:
            if self._closing or self._closed:
                raise AdapterError(
                    "Device has already been closed",
                    operation="device.resource",
                    code="device_closed",
                )
            self._active_creations += 1
            thread_id = threading.get_ident()
            self._creator_threads[thread_id] = (
                self._creator_threads.get(thread_id, 0) + 1)
        try:
            resource = factory(*args, **kwargs)
            with self._state:
                if (callable(getattr(resource, "close", None)) or
                        callable(getattr(resource, "release", None))):
                    self._owned.append(resource)
            return resource
        finally:
            with self._state:
                self._active_creations -= 1
                thread_id = threading.get_ident()
                remaining = self._creator_threads.get(thread_id, 0) - 1
                if remaining > 0:
                    self._creator_threads[thread_id] = remaining
                else:
                    self._creator_threads.pop(thread_id, None)
                if self._active_creations == 0:
                    self._state.notify_all()

    def frame_source(self, **kwargs):
        """Create the selected camera frame source.

        On extension firmware this is the dma-buf broker.  The existing RTSP
        decoder remains a compatibility fallback and is reported through the
        capability snapshot rather than disguised as zero-copy.
        """

        self._ensure_open()
        from .adapters.registry import select_frame_source
        factory = self._factory("frame_source", select_frame_source)
        try:
            return self._create_owned(factory, **kwargs)
        except KitError:
            raise
        except Exception as exc:
            log.exception("could not create frame source")
            raise AdapterError(
                f"could not create frame source: {exc}",
                operation="device.frame_source",
            ) from exc

    def result_publisher(self, kind: str = "ws", **kwargs):
        """Create a result publisher.

        ``kind="osd"`` explicitly selects native result ingress; merely seeing
        its socket never changes the existing software-overlay default.
        """

        self._ensure_open()
        from .adapters.registry import select_result_sink
        factory = self._factory("result_publisher", select_result_sink)
        try:
            return self._create_owned(factory, kind=kind, **kwargs)
        except KitError:
            raise
        except Exception as exc:
            log.exception("could not create result publisher kind=%s", kind)
            raise AdapterError(
                f"could not create result publisher {kind!r}: {exc}",
                operation="device.result_publisher",
                details={"kind": kind},
            ) from exc

    def result_batch_publisher(
        self,
        kind: str = "ws",
        *,
        model_to_pixel=None,
        **kwargs,
    ):
        """Create a typed :class:`kit.ai.ResultBatchPublisher`.

        The underlying legacy/native sink is owned by this ``Device`` and will
        be closed with it.  The wrapper converts explicit coordinate spaces and
        uses the sink's checked path, so synchronous native rejection cannot be
        reported as a successful :class:`~kit.ai.PublishReport`.
        """

        self._ensure_open()
        from .ai import ResultBatchPublisher

        sink = self.result_publisher(kind=kind, **kwargs)
        return ResultBatchPublisher(sink, model_to_pixel=model_to_pixel)

    def image_ops(self, **kwargs):
        """Open the public RGA image-operation context."""

        self._ensure_open()
        from .media import RgaContext
        factory = self._factory("image_ops", RgaContext)
        try:
            return self._create_owned(factory, **kwargs)
        except KitError:
            raise
        except Exception as exc:
            log.exception("could not create image-operation context")
            raise AdapterError(
                f"could not create image-operation context: {exc}",
                operation="device.image_ops",
            ) from exc

    def rknn_session(self, model, **kwargs):
        """Open a typed RKNN session using the managed service when selected.

        appmgr injects the service endpoint only for a manifest-v2 scheduled
        NPU application.  Direct/developer callers keep the existing guarded
        local RKNN session unless they pass an explicit factory.
        """

        self._ensure_open()
        from .runtime import (
            RemoteRknnSession,
            RknnSession,
            configured_inference_socket,
        )
        default_factory = (
            RemoteRknnSession if configured_inference_socket() else RknnSession
        )
        factory = self._factory("rknn_session", default_factory)
        try:
            return self._create_owned(factory, model, **kwargs)
        except KitError:
            raise
        except Exception as exc:
            log.exception("could not create RKNN session")
            raise ModelLoadError(
                f"could not create RKNN session: {exc}",
                operation="device.rknn_session",
            ) from exc

    def control(self, **kwargs):
        """Create the device-settings control plane.

        Control is the only compatibility surface that may use entry.cgi.  It
        is not used for frame transport, RGA, inference, or result delivery.
        """

        self._ensure_open()
        from .adapters.registry import select_control
        factory = self._factory("control", select_control)
        try:
            return self._create_owned(factory, **kwargs)
        except KitError:
            raise
        except Exception as exc:
            log.exception("could not create device control")
            raise AdapterError(
                f"could not create device control: {exc}",
                operation="device.control",
            ) from exc

    def close(self) -> CloseReport:
        """Close every owned resource in reverse order; never stop midway."""

        with self._state:
            if self._closed:
                return CloseReport(0)
            if self._creator_threads.get(threading.get_ident(), 0):
                raise AdapterError(
                    "a resource factory cannot close its owning Device",
                    operation="device.close",
                    code="reentrant_close",
                )
            if self._closing:
                if self._closing_owner == threading.get_ident():
                    raise AdapterError(
                        "Device close cannot recursively wait for itself",
                        operation="device.close",
                        code="reentrant_close",
                    )
                while not self._closed:
                    self._state.wait()
                return self._close_report or CloseReport(0)
            self._closing = True
            self._closing_owner = threading.get_ident()
            try:
                while self._active_creations:
                    self._state.wait()
            except BaseException:
                self._closing = False
                self._closing_owner = None
                self._state.notify_all()
                raise
            resources, self._owned = list(reversed(self._owned)), []
        errors: list[str] = []
        closed = 0
        control_flow: Optional[BaseException] = None
        for resource in resources:
            closer = getattr(resource, "close", None)
            if not callable(closer):
                closer = getattr(resource, "release", None)
            if not callable(closer):
                continue
            try:
                closer()
                closed += 1
            except BaseException as exc:
                message = f"{type(resource).__name__}: {exc}"
                errors.append(message)
                log.exception("resource close failed resource=%s",
                              type(resource).__name__)
                # Continue closing the remaining resources, but never convert a
                # KeyboardInterrupt/SystemExit/internal stop signal into a
                # successful CloseReport.
                if not isinstance(exc, Exception) and control_flow is None:
                    control_flow = exc
        report = CloseReport(closed=closed, errors=tuple(errors))
        with self._state:
            self._close_report = report
            self._closed = True
            self._closing = False
            self._closing_owner = None
            self._state.notify_all()
        if control_flow is not None:
            raise control_flow
        return report

    @property
    def close_report(self) -> Optional[CloseReport]:
        """Report produced by the first close, including context-manager exit.

        ``__exit__`` cannot return a report because Python reserves its return
        value for exception suppression.  Applications that use ``with`` can
        inspect this property after the block to detect best-effort cleanup
        failures.  It is ``None`` until closing begins.
        """

        with self._state:
            return self._close_report

    def __enter__(self) -> "Device":
        self._ensure_open()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        try:
            self.close()
        except BaseException as cleanup_error:
            if exc is None:
                raise
            log.error(
                "Device cleanup failed while preserving body exception",
                exc_info=(
                    type(cleanup_error),
                    cleanup_error,
                    cleanup_error.__traceback__,
                ),
            )
        return None


__all__ = ["CloseReport", "Device", "DeviceCloseReport"]
