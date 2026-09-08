"""Typed RKNN inference sessions for the RV1126B NPU.

``RknnSession`` is the new public interface.  ``RknnModel`` remains as a
backwards-compatible subclass for existing applications.  Both acquire NPU
ownership *before* constructing ``RKNNLite``, mark the shared broker lease ready
only after native initialization, check that ownership is still alive before
every inference, and release the final lease reference only after every
protected RKNN context has been destroyed.
"""
from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, List, Mapping, Optional, Sequence

import numpy as np

from kit.errors import (
    ConfigurationError,
    InferenceError,
    InputValidationError,
    ModelLoadError,
)
from kit.diagnostics import get_logger
from kit.resources import ExternalNpuLease, ResourceLease


log = get_logger("ai.rknn")

# If a vendor runtime refuses/fails to destroy a context, releasing the
# process-level NPU lock would let another workflow enter the driver while the
# old context may still own hardware.  Keep strong references until a later
# successful retry (or process exit, when the kernel closes the flock fd).
_RELEASE_QUARANTINE: dict[object, tuple[Any, ResourceLease, str]] = {}


def _reject_managed_scheduled_local_runtime() -> None:
    """Fence the legacy in-process runtime inside a scheduled app launch.

    A scheduled application has been admitted to the platform-owned inference
    daemon.  Taking rkipc's exclusive external lease from that same process
    would bypass the daemon's queue and can enter RKNN concurrently with its
    resident contexts.  Environment variables are not a security boundary, but
    this guard makes the public Kit API fail closed for accidental misuse; the
    device/supervisor sandbox must separately prevent direct ``rknnlite`` use by
    untrusted code.
    """

    mode = str(os.environ.get("RECAMERA_NPU_MODE") or "").strip().lower()
    if mode != "scheduled":
        return
    endpoint = str(
        os.environ.get("RECAMERA_INFERENCE_SERVICE_SOCK")
        or os.environ.get("RECAMERA_INFERENCE_SERVICE")
        or ""
    ).strip()
    raise ConfigurationError(
        "managed scheduled-NPU applications must use RemoteRknnSession, "
        "Device.rknn_session(), or the App model factory",
        operation="model.configure",
        code="scheduled_npu_requires_remote",
        details={"inference_service": endpoint or None},
    )


@dataclass(frozen=True)
class TensorSpec:
    """Expected tensor name, shape, dtype, and layout.

    A shape dimension of ``-1`` accepts any positive size.  Layout is metadata
    used for validation/documentation; the RKNN runtime receives arrays in the
    supplied order and does not transpose them implicitly.
    """

    name: str
    shape: tuple[int, ...]
    dtype: str = "uint8"
    layout: str = "NHWC"

    def __post_init__(self) -> None:
        name = str(self.name).strip()
        if not name:
            raise ConfigurationError(
                "TensorSpec.name must not be empty",
                operation="model.spec",
            )
        try:
            shape = tuple(int(item) for item in self.shape)
        except (TypeError, ValueError) as exc:
            raise ConfigurationError(
                f"invalid tensor shape: {self.shape!r}",
                operation="model.spec",
                details={"tensor": name, "shape": repr(self.shape)},
            ) from exc
        if not shape or any(item == 0 or item < -1 for item in shape):
            raise ConfigurationError(
                f"invalid tensor shape: {shape!r}",
                operation="model.spec",
                details={"tensor": name, "shape": list(shape)},
            )
        try:
            dtype = np.dtype(self.dtype).name
        except (TypeError, ValueError) as exc:
            raise ConfigurationError(
                f"invalid dtype for tensor {name!r}: {self.dtype!r}",
                operation="model.spec",
                details={"tensor": name, "dtype": repr(self.dtype)},
            ) from exc
        layout = str(self.layout).strip().upper()
        if not layout:
            raise ConfigurationError(
                f"layout for tensor {name!r} must not be empty",
                operation="model.spec",
                details={"tensor": name},
            )
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "shape", shape)
        object.__setattr__(self, "dtype", dtype)
        object.__setattr__(self, "layout", layout)

    def validate(self, value: np.ndarray, operation: str) -> np.ndarray:
        """Validate one array and return it unchanged."""

        arr = np.asarray(value)
        expected_dtype = np.dtype(self.dtype)
        if arr.dtype != expected_dtype:
            raise InputValidationError(
                f"tensor {self.name!r} has dtype {arr.dtype}, expected {expected_dtype}",
                operation=operation,
                details={
                    "tensor": self.name,
                    "actual_dtype": str(arr.dtype),
                    "expected_dtype": str(expected_dtype),
                },
            )
        if arr.ndim != len(self.shape) or any(
            (expected == -1 and actual <= 0)
            or (expected != -1 and actual != expected)
            for actual, expected in zip(arr.shape, self.shape)
        ):
            raise InputValidationError(
                f"tensor {self.name!r} has shape {arr.shape}, expected {self.shape}",
                operation=operation,
                details={
                    "tensor": self.name,
                    "actual_shape": list(arr.shape),
                    "expected_shape": list(self.shape),
                    "layout": self.layout,
                },
            )
        return arr


@dataclass(frozen=True)
class ModelSpec:
    """Declared RKNN model contract.

    ``inputs``/``outputs`` may initially be empty for legacy models whose
    metadata is not exported.  New applications should declare them in their
    manifest so invalid dtype/layout/shape fails before entering the driver.
    """

    path: str
    inputs: tuple[TensorSpec, ...] = ()
    outputs: tuple[TensorSpec, ...] = ()
    core_mask: Optional[int] = None
    name: Optional[str] = None

    def __post_init__(self) -> None:
        try:
            path = os.fspath(self.path)
        except TypeError as exc:
            raise ConfigurationError(
                "model path must be path-like",
                operation="model.spec",
                details={"path": repr(self.path)},
            ) from exc
        if not str(path).strip():
            raise ConfigurationError(
                "model path must not be empty",
                operation="model.spec",
            )
        normalized_specs: dict[str, tuple[TensorSpec, ...]] = {}
        for field_name, raw_specs in (
            ("inputs", self.inputs),
            ("outputs", self.outputs),
        ):
            try:
                specs = tuple(raw_specs)
            except TypeError as exc:
                raise ConfigurationError(
                    f"ModelSpec.{field_name} must be an iterable of TensorSpec",
                    operation="model.spec",
                    details={"field": field_name},
                ) from exc
            for index, spec in enumerate(specs):
                if not isinstance(spec, TensorSpec):
                    raise ConfigurationError(
                        f"ModelSpec.{field_name}[{index}] is not a TensorSpec",
                        operation="model.spec",
                        details={
                            "field": field_name,
                            "index": index,
                            "actual_type": type(spec).__name__,
                        },
                    )
            names = [spec.name for spec in specs]
            duplicates = sorted({name for name in names if names.count(name) > 1})
            if duplicates:
                raise ConfigurationError(
                    f"ModelSpec.{field_name} tensor names must be unique",
                    operation="model.spec",
                    details={"field": field_name, "duplicates": duplicates},
                )
            normalized_specs[field_name] = specs
        object.__setattr__(self, "path", path)
        object.__setattr__(self, "inputs", normalized_specs["inputs"])
        object.__setattr__(self, "outputs", normalized_specs["outputs"])


@dataclass(frozen=True)
class InferenceStats:
    """Cumulative session statistics suitable for a health endpoint."""

    calls: int = 0
    failures: int = 0
    total_ms: float = 0.0
    last_ms: float = 0.0

    @property
    def average_ms(self) -> float:
        """Mean successful/failed call duration, or zero before the first call."""

        return self.total_ms / self.calls if self.calls else 0.0


def _default_runtime_factory():
    # Lazy import keeps package inspection and host-side unit tests independent
    # of the aarch64-only rknnlite wheel.
    from rknnlite.api import RKNNLite
    return RKNNLite()


def _ctypes_spec_supported(spec: ModelSpec) -> bool:
    """Return whether the declared contract is safe for the low-level path."""
    if len(spec.inputs) != 1:
        return False
    tensor = spec.inputs[0]
    return (
        tensor.dtype == "uint8"
        and tensor.layout == "NHWC"
        and len(tensor.shape) == 4
        and all(dim > 0 for dim in tensor.shape)
    )


def _runtime_for_spec(spec: ModelSpec, *, legacy_uint8: bool = False):
    """Select a backend before any native model load occurs."""
    choice = str(os.environ.get("ESK_RKNN_BACKEND", "auto")).strip().lower()
    if choice not in {"auto", "rknnlite", "ctypes"}:
        raise ConfigurationError(
            f"unsupported ESK_RKNN_BACKEND value {choice!r}",
            operation="model.backend",
        )
    eligible = _ctypes_spec_supported(spec)
    # An old RknnModel has no metadata.  The ctypes runtime validates the
    # actual graph after init, so it remains a useful compatibility path when
    # its shared library is present.
    legacy = not spec.inputs and legacy_uint8
    if choice == "ctypes":
        if not (eligible or not spec.inputs):
            raise ConfigurationError(
                "ctypes backend requires one static uint8 NHWC input",
                operation="model.backend",
                code="ctypes_unsupported_model",
            )
        from kit.runtime.ctypes_rknn import CtypesRknnModel
        return CtypesRknnModel()
    if choice == "auto" and (eligible or legacy):
        from kit.runtime.ctypes_rknn import CtypesRknnModel, library_path
        if eligible or library_path():
            return CtypesRknnModel()
    return _default_runtime_factory()


class RknnSession:
    """Own one RKNN runtime context and its process-level NPU lease.

    Parameters:
        model: A model path or :class:`ModelSpec`.
        core_mask: Optional RKNN core mask.  RV1126B normally uses the runtime
            default because it has a single NPU core.
        lease: Resource lease implementation.  The default is
            :class:`~kit.resources.ExternalNpuLease`.
        lease_timeout: Seconds to wait for another Python process to release
            the NPU.  ``None`` selects rkipc's bounded server default (and
            remains an indefinite wait only for an explicit legacy lock).
        runtime_factory: Test/vendor injection point returning an RKNNLite-like
            object.  Applications normally leave it unset.
        strict_inputs: Validate declared TensorSpec contracts without implicit
            dtype conversion.  Defaults to true for this new interface.
    """

    def __init__(
        self,
        model: str | ModelSpec,
        core_mask: Optional[int] = None,
        *,
        lease: Optional[ResourceLease] = None,
        lease_timeout: Optional[float] = 30.0,
        runtime_factory: Optional[Callable[[], Any]] = None,
        strict_inputs: bool = True,
    ) -> None:
        _reject_managed_scheduled_local_runtime()
        self.spec = (model if isinstance(model, ModelSpec)
                     else ModelSpec(path=os.fspath(model), core_mask=core_mask))
        if core_mask is not None and isinstance(model, ModelSpec):
            self.spec = ModelSpec(
                path=model.path,
                inputs=model.inputs,
                outputs=model.outputs,
                core_mask=core_mask,
                name=model.name,
            )
        self.path = self.spec.path
        self.strict_inputs = bool(strict_inputs)
        self._lease = lease or ExternalNpuLease()
        self._runtime = None
        self._released = False
        self._quarantine_key = object()
        self._calls = 0
        self._failures = 0
        self._total_ms = 0.0
        self._last_ms = 0.0
        self._lifecycle = threading.Condition(threading.RLock())
        self._infer_active = False
        self._infer_owner: int | None = None
        self._closing = False
        self._closing_owner: int | None = None
        self._release_failed = False

        try:
            self._lease.acquire(timeout=lease_timeout)
        except BaseException:
            # ResourceLease.release is required to be idempotent.  Calling it
            # also protects against a custom lease that acquired ownership and
            # then raised while reporting/recording it.
            try:
                self._lease.release()
            except BaseException:
                self._quarantine_release()
                log.critical(
                    "NPU lease cleanup failed while propagating acquire error "
                    "model=%s; resource remains quarantined", self.path,
                    exc_info=True)
            raise
        try:
            # Preserve the zero-argument injection hook used by existing
            # tests and vendors.  Only the built-in default participates in
            # backend selection.
            self._runtime = (runtime_factory() if runtime_factory is not None
                             else _runtime_for_spec(
                                 self.spec, legacy_uint8=not self.strict_inputs))
            ret = self._runtime.load_rknn(self.path)
            if ret != 0:
                raise ModelLoadError(
                    f"RKNN load failed for {self.path!r} (ret={ret})",
                    operation="model.load",
                    details={"path": self.path, "native_code": ret},
                )

            if self.spec.core_mask is not None:
                ret = self._runtime.init_runtime(core_mask=self.spec.core_mask)
            else:
                ret = self._runtime.init_runtime()
            if ret != 0:
                raise ModelLoadError(
                    f"RKNN runtime initialization failed for {self.path!r} (ret={ret})",
                    operation="model.init_runtime",
                    details={
                        "path": self.path,
                        "native_code": ret,
                        "core_mask": self.spec.core_mask,
                    },
                )
            self._mark_lease_ready()
        except BaseException as exc:
            # Construction is transactional even for process-control
            # exceptions.  In particular, _GracefulStop, KeyboardInterrupt and
            # SystemExit must release the partially-created native context and
            # NPU lease, then remain the *same* exception seen by the caller.
            self._rollback_initialization()
            if isinstance(exc, ModelLoadError):
                raise
            if isinstance(exc, Exception):
                raise ModelLoadError(
                    f"could not create RKNN runtime for {self.path!r}: {exc}",
                    operation="model.init_runtime",
                    details={"path": self.path},
                ) from exc
            raise
        log.info("RKNN session ready model=%s", self.path)

    @property
    def released(self) -> bool:
        """Whether the native runtime and lease have been released."""

        with self._lifecycle:
            return self._released

    @property
    def stats(self) -> InferenceStats:
        """Return an immutable snapshot of inference timing/counters."""

        with self._lifecycle:
            return InferenceStats(
                calls=self._calls,
                failures=self._failures,
                total_ms=self._total_ms,
                last_ms=self._last_ms,
            )

    def _mark_lease_ready(self) -> None:
        """Publish readiness after RKNN load/init, preserving legacy leases."""

        ready = getattr(self._lease, "ready", None)
        if not callable(ready):
            # Third-party ResourceLease implementations written for the older
            # acquire/release-only protocol remain source compatible.  The
            # default ExternalNpuLease always implements the broker transition.
            log.debug("custom NPU lease has no ready transition model=%s",
                      self.path)
            return
        try:
            ready()
        except Exception as exc:
            raise ModelLoadError(
                f"could not mark the NPU lease ready for {self.path!r}: {exc}",
                operation="model.lease.ready",
                code="npu_lease_ready_failed",
                retryable=True,
                details={"path": self.path},
            ) from exc

    def _ensure_lease_alive(self) -> None:
        """Fail closed when rkipc has revoked/lost the ownership generation."""

        alive = getattr(self._lease, "alive", None)
        if not callable(alive):
            log.debug("custom NPU lease has no liveness check model=%s",
                      self.path)
            return
        try:
            is_alive = alive()
        except Exception as exc:
            raise InferenceError(
                f"could not verify NPU lease liveness for {self.path!r}: {exc}",
                operation="model.infer.lease",
                code="npu_lease_check_failed",
                retryable=True,
                details={"path": self.path},
            ) from exc
        if is_alive is not True:
            raise InferenceError(
                "rkipc revoked the NPU ownership generation; inference is fenced",
                operation="model.infer.lease",
                code="npu_lease_revoked",
                retryable=False,
                details={"path": self.path},
            )

    def _ordered_inputs(self, value: Any) -> list[np.ndarray]:
        specs = self.spec.inputs
        if isinstance(value, Mapping):
            if not specs:
                raise InputValidationError(
                    "named inputs require ModelSpec.inputs declarations",
                    operation="model.infer.validate",
                )
            missing = [spec.name for spec in specs if spec.name not in value]
            extras = sorted(set(value) - {spec.name for spec in specs})
            if missing or extras:
                raise InputValidationError(
                    "named inputs do not match the model contract",
                    operation="model.infer.validate",
                    details={"missing": missing, "unexpected": extras},
                )
            arrays = [np.asarray(value[spec.name]) for spec in specs]
        elif (isinstance(value, (list, tuple)) and specs and len(specs) > 1):
            arrays = [np.asarray(item) for item in value]
        else:
            arrays = [np.asarray(value)]

        if specs and len(arrays) != len(specs):
            raise InputValidationError(
                f"model expects {len(specs)} inputs, got {len(arrays)}",
                operation="model.infer.validate",
                details={"expected_count": len(specs), "actual_count": len(arrays)},
            )

        normalized: list[np.ndarray] = []
        for index, arr in enumerate(arrays):
            spec = specs[index] if index < len(specs) else None
            # Convenience for the ubiquitous image contract: HWC -> NHWC.
            if arr.ndim == 3 and (spec is None or
                                  (len(spec.shape) == 4 and spec.layout == "NHWC")):
                arr = np.expand_dims(arr, 0)
            if spec is not None and self.strict_inputs:
                arr = spec.validate(arr, "model.infer.validate")
            elif not self.strict_inputs and arr.dtype != np.uint8:
                # Historical RknnModel behavior retained only in compatibility
                # mode.  RknnSession never silently changes dtype.
                arr = arr.astype(np.uint8)
            normalized.append(np.ascontiguousarray(arr))
        return normalized

    def infer(self, inputs: Any) -> List[np.ndarray]:
        """Run one synchronous forward pass and return raw output arrays.

        ``inputs`` may be one ndarray, a sequence for a declared multi-input
        model, or a mapping keyed by ``TensorSpec.name``.  Driver exceptions are
        wrapped as :class:`InferenceError` with the original exception retained
        as ``__cause__``.
        """

        thread_id = threading.get_ident()
        try:
            with self._lifecycle:
                if self._infer_owner == thread_id:
                    raise InferenceError(
                        "recursive inference on one RKNN context is unsupported",
                        operation="model.infer",
                        code="reentrant_inference",
                    )
                while self._infer_active and not self._closing:
                    self._lifecycle.wait()
                if (self._released or self._runtime is None or self._closing
                        or self._release_failed):
                    code = ("session_quarantined" if self._release_failed
                            else "session_released")
                    raise InferenceError(
                        "cannot infer while the RKNN session is closing or released",
                        operation="model.infer",
                        code=code,
                    )
                self._ensure_lease_alive()
                self._infer_active = True
                self._infer_owner = thread_id
        except BaseException as exc:
            if isinstance(exc, Exception):
                raise
            # Preserve the pre-existing control-flow cleanup guarantee even if
            # SIGTERM/KeyboardInterrupt lands in the new broker liveness check.
            try:
                self.release()
            except BaseException:
                log.exception(
                    "RKNN cleanup failed while propagating admission "
                    "control-flow exception model=%s", self.path)
            raise

        control_flow = False
        try:
            return self._infer_impl(inputs)
        except BaseException as exc:
            control_flow = not isinstance(exc, Exception)
            raise
        finally:
            with self._lifecycle:
                self._infer_active = False
                self._infer_owner = None
                self._lifecycle.notify_all()
            if control_flow:
                # Only tear down after leaving the active-inference region;
                # release() can now wait safely and cannot destroy a context
                # underneath the native inference call.
                try:
                    self.release()
                except BaseException:
                    log.exception(
                        "RKNN cleanup failed while propagating control-flow "
                        "exception model=%s", self.path)

    def _infer_impl(self, inputs: Any) -> List[np.ndarray]:
        """Inference body executed under the session admission barrier."""

        try:
            arrays = self._ordered_inputs(inputs)
        except Exception:
            # Preserve historical validation/coercion errors.  Only native
            # driver Exceptions below are normalized to InferenceError.
            raise
        except BaseException:
            raise
        started = time.monotonic()
        self._calls += 1
        try:
            outputs = self._runtime.inference(inputs=arrays)
            if outputs is None:
                raise RuntimeError("RKNNLite.inference returned None")
            result = [np.asarray(item) for item in outputs]
            if self.spec.outputs and len(result) != len(self.spec.outputs):
                raise InputValidationError(
                    "RKNN output count does not match ModelSpec",
                    operation="model.infer.outputs",
                    details={
                        "expected_count": len(self.spec.outputs),
                        "actual_count": len(result),
                    },
                )
            if self.spec.outputs and self.strict_inputs:
                result = [
                    spec.validate(item, "model.infer.outputs")
                    for spec, item in zip(self.spec.outputs, result)
                ]
            return result
        except InputValidationError:
            self._failures += 1
            raise
        except Exception as exc:
            self._failures += 1
            log.exception("RKNN inference failed model=%s", self.path)
            raise InferenceError(
                f"RKNN inference failed for {self.path!r}: {exc}",
                operation="model.infer",
                retryable=True,
                details={"path": self.path},
            ) from exc
        except BaseException:
            self._failures += 1
            raise
        finally:
            self._last_ms = (time.monotonic() - started) * 1000.0
            self._total_ms += self._last_ms

    def _release_runtime(self) -> None:
        runtime = self._runtime
        if runtime is not None:
            try:
                result = runtime.release()
                if result not in (None, 0):
                    raise RuntimeError(
                        f"RKNNLite.release returned non-zero status {result!r}")
            except BaseException:
                self._quarantine_release()
                raise
            self._runtime = None

    def _quarantine_release(self) -> None:
        """Retain the runtime/lease so a failed destroy cannot free the lock."""

        _RELEASE_QUARANTINE[self._quarantine_key] = (
            self._runtime,
            self._lease,
            self.path,
        )

    def _clear_release_quarantine(self) -> None:
        _RELEASE_QUARANTINE.pop(self._quarantine_key, None)

    def _rollback_initialization(self) -> None:
        """Best-effort rollback that never masks the construction exception."""

        try:
            self._release_runtime()
        except BaseException:
            # Never unlock while a partially-created native context may still
            # exist.  The construction exception remains primary; this object
            # is retained by the quarantine until process exit.
            self._quarantine_release()
            log.critical(
                "RKNN runtime rollback failed model=%s; NPU lease remains "
                "quarantined", self.path, exc_info=True)
            return
        try:
            self._lease.release()
        except BaseException:
            self._quarantine_release()
            log.critical(
                "NPU lease rollback failed model=%s; resource remains "
                "quarantined", self.path, exc_info=True)
            return
        self._released = True
        self._clear_release_quarantine()

    def release(self) -> None:
        """Destroy the RKNN context and release the NPU guard exactly once."""

        thread_id = threading.get_ident()
        with self._lifecycle:
            if self._released:
                return
            if self._infer_owner == thread_id:
                raise InferenceError(
                    "inference cannot release its own active RKNN context",
                    operation="model.release",
                    code="reentrant_release",
                )
            while self._closing:
                if self._closing_owner == thread_id:
                    raise InferenceError(
                        "RKNN release cannot recursively wait for itself",
                        operation="model.release",
                        code="reentrant_release",
                    )
                self._lifecycle.wait()
                if self._released:
                    return
            self._closing = True
            self._closing_owner = thread_id
            try:
                while self._infer_active:
                    self._lifecycle.wait()
            except BaseException:
                self._closing = False
                self._closing_owner = None
                self._lifecycle.notify_all()
                raise
        try:
            self._release_runtime()
        except Exception as exc:
            with self._lifecycle:
                self._release_failed = True
                self._closing = False
                self._closing_owner = None
                self._lifecycle.notify_all()
            log.critical(
                "RKNN runtime release failed model=%s; retaining NPU lease",
                self.path, exc_info=True)
            raise InferenceError(
                f"RKNN runtime release failed for {self.path!r}: {exc}",
                operation="model.release",
                code="runtime_release_failed",
                retryable=True,
                details={"path": self.path, "lease_retained": True},
            ) from exc
        except BaseException:
            with self._lifecycle:
                self._release_failed = True
                self._closing = False
                self._closing_owner = None
                self._lifecycle.notify_all()
            log.critical(
                "RKNN runtime release interrupted model=%s; retaining NPU lease",
                self.path, exc_info=True)
            raise
        try:
            self._lease.release()
        except Exception as exc:
            self._quarantine_release()
            with self._lifecycle:
                self._release_failed = True
                self._closing = False
                self._closing_owner = None
                self._lifecycle.notify_all()
            log.critical("NPU lease release failed model=%s", self.path,
                         exc_info=True)
            raise InferenceError(
                f"NPU lease release failed for {self.path!r}: {exc}",
                operation="model.release",
                code="lease_release_failed",
                retryable=True,
                details={"path": self.path, "lease_retained": True},
            ) from exc
        except BaseException:
            self._quarantine_release()
            with self._lifecycle:
                self._release_failed = True
                self._closing = False
                self._closing_owner = None
                self._lifecycle.notify_all()
            log.critical("NPU lease release interrupted model=%s", self.path,
                         exc_info=True)
            raise
        with self._lifecycle:
            self._released = True
            self._release_failed = False
            self._closing = False
            self._closing_owner = None
            self._lifecycle.notify_all()
        self._clear_release_quarantine()
        log.info("RKNN session released model=%s", self.path)

    def __enter__(self) -> "RknnSession":
        if self._released:
            raise InferenceError(
                "cannot enter a released RKNN session",
                operation="model.enter",
                code="session_released",
            )
        return self

    def __exit__(self, exc_type, exc, traceback) -> bool:
        try:
            self.release()
        except BaseException:
            if exc is None:
                raise
            # Preserve the error from the with-body.  release() already logged
            # and quarantined the unsafe context/lease pair.
            log.critical(
                "RKNN context cleanup failed while preserving earlier error "
                "model=%s", self.path, exc_info=True)
        return False

    def __del__(self) -> None:
        """Best-effort cleanup when application code forgets ``release``.

        Explicit lifecycle management remains required.  In particular,
        ``release`` quarantines a native context and retains its NPU lease when
        RKNN destruction fails; this finalizer intentionally uses that same
        fail-closed path instead of unlocking an uncertain runtime.
        """

        try:
            self.release()
        except BaseException:
            try:
                log.critical(
                    "RKNN session finalizer could not release model=%s",
                    getattr(self, "path", "<partially-constructed>"),
                    exc_info=True,
                )
            except BaseException:
                pass


class RknnModel(RknnSession):
    """Compatibility wrapper preserving the original permissive input rules.

    Existing applications may continue to pass float/other arrays; they are
    converted to uint8 as before.  New code should use ``RknnSession`` with a
    declared ``ModelSpec`` so dtype/shape mistakes fail explicitly.
    """

    def __init__(self, path: str, core_mask: Optional[int] = None, **kwargs) -> None:
        kwargs.setdefault("strict_inputs", False)
        super().__init__(path, core_mask=core_mask, **kwargs)


__all__ = [
    "InferenceStats",
    "ModelSpec",
    "RknnModel",
    "RknnSession",
    "TensorSpec",
]
