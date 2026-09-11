"""Client sessions for the reCamera multi-model inference daemon.

``RemoteRknnSession`` mirrors the small public surface of :class:`RknnSession`
but never imports ``rknnlite`` and never acquires the device NPU lease.  The
platform daemon owns the managed applications' RKNN contexts. Applications use
validated tensor messages or negotiated private DMA buffers over a Unix socket;
the built-in IPC model remains a separate, coordinated driver client.
"""

from __future__ import annotations

import hashlib
import itertools
import os
import socket
import threading
import time
import uuid
from typing import Any, Mapping, Optional

import numpy as np

from kit.errors import (
    CapabilityError,
    InferenceError,
    InputValidationError,
    ModelLoadError,
    ResourceBusyError,
    TransportError,
)

from ._inference_protocol import ProtocolError, recv_message, send_message
from ._inference_shared import SHARED_IO_VERSION, SharedIOClient, dma_buf_sync, recv_fds
from .engine import InferenceStats, ModelSpec


DEFAULT_INFERENCE_SOCKET = "/run/recamera/inferenced.sock"
INFERENCE_SOCKET_ENV = "RECAMERA_INFERENCE_SERVICE_SOCK"
_LEGACY_INFERENCE_SOCKET_ENV = "RECAMERA_INFERENCE_SERVICE"


def configured_inference_socket() -> Optional[str]:
    """Return the appmgr-authorized service endpoint, if one was injected.

    Managed applications receive ``RECAMERA_INFERENCE_SERVICE_SOCK``.  The
    older, briefly documented variable remains a read-only compatibility alias
    so developer images made during the API transition continue to run.
    Merely having the default socket on disk never opts a hand-launched process
    into the service.
    """

    value = os.environ.get(INFERENCE_SOCKET_ENV)
    if value is None:
        value = os.environ.get(_LEGACY_INFERENCE_SOCKET_ENV)
    value = str(value or "").strip()
    return value or None


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _spec_payload(spec: ModelSpec) -> dict:
    def tensors(values):
        return [
            {
                "name": item.name,
                "shape": list(item.shape),
                "dtype": item.dtype,
                "layout": item.layout,
            }
            for item in values
        ]

    return {
        "name": spec.name,
        "core_mask": spec.core_mask,
        "inputs": tensors(spec.inputs),
        "outputs": tensors(spec.outputs),
    }


def _raise_remote(error: object, *, operation: str) -> None:
    if not isinstance(error, Mapping):
        raise TransportError(
            "inference service returned an invalid error object",
            operation=operation,
            code="invalid_service_response",
        )
    code = str(error.get("code") or "inference_service_error")
    message = str(error.get("message") or code)
    retryable = bool(error.get("retryable"))
    details = error.get("details")
    if not isinstance(details, Mapping):
        details = {}
    cls: type[InferenceError]
    if code in {"model_load_failed", "model_not_found", "model_digest_mismatch"}:
        cls = ModelLoadError
    elif code in {"invalid_input", "invalid_request", "protocol_error"}:
        cls = InputValidationError
    elif code in {"resource_busy", "queue_full", "memory_budget_exceeded"}:
        raise ResourceBusyError(
            message,
            operation=operation,
            code=code,
            retryable=retryable,
            details=details,
        )
    elif code in {"unauthorized", "authorization_pending"}:
        raise CapabilityError(
            message,
            operation=operation,
            code=code,
            retryable=retryable,
            details=details,
        )
    else:
        cls = InferenceError
    raise cls(
        message,
        operation=operation,
        code=code,
        retryable=retryable,
        details=details,
    )


class RemoteRknnSession:
    """One remotely hosted model context.

    Model contexts with the same digest are shared by the daemon while this
    object retains a per-client alias.  Calls on one session are serialized so
    request/response framing cannot interleave.  Calls from different clients
    may be submitted concurrently and are fairly scheduled by the daemon.
    """

    def __init__(
        self,
        model: str | ModelSpec,
        core_mask: Optional[int] = None,
        *,
        socket_path: Optional[str] = None,
        model_sha256: Optional[str] = None,
        verify_model: bool = False,
        memory_mb: int = 64,
        priority: int = 50,
        max_fps: float = 0.0,
        connect_timeout: float = 10.0,
        strict_inputs: bool = True,
        app_id: Optional[str] = None,
        instance_id: Optional[str] = None,
        generation: Optional[int] = None,
        shared_io: bool = True,
    ) -> None:
        self.spec = (
            model
            if isinstance(model, ModelSpec)
            else ModelSpec(path=os.fspath(model), core_mask=core_mask)
        )
        if core_mask is not None and isinstance(model, ModelSpec):
            self.spec = ModelSpec(
                path=model.path,
                inputs=model.inputs,
                outputs=model.outputs,
                core_mask=core_mask,
                name=model.name,
            )
        # The inference daemon is a different process and therefore has its
        # own cwd.  Sending a relative path lets the daemon resolve an app
        # model such as ``models/foo.rknn`` relative to ``/`` (the service
        # cwd), even though the managed application resolved it successfully
        # relative to its installed app directory.  Besides breaking bundled
        # models, that makes path meaning depend on server process state.
        # Canonicalise in the client process before the path crosses the UDS;
        # the server still performs its exact manifest-authorisation lookup
        # and never trusts this value as policy.
        self.path = os.path.realpath(
            os.path.abspath(os.fsdecode(self.spec.path)))
        self.strict_inputs = bool(strict_inputs)
        self.socket_path = (
            socket_path or configured_inference_socket() or DEFAULT_INFERENCE_SOCKET
        )
        self.app_id = app_id or os.environ.get("RECAMERA_APP_ID", "python")
        self.instance_id = instance_id or os.environ.get(
            "RECAMERA_APP_INSTANCE", uuid.uuid4().hex
        )
        raw_generation: object = generation
        if raw_generation is None:
            raw_generation = os.environ.get("RECAMERA_APP_GENERATION", "0")
        try:
            if isinstance(raw_generation, bool):
                raise ValueError
            if isinstance(raw_generation, int):
                self.generation = raw_generation
            elif isinstance(raw_generation, str) and raw_generation.isascii() \
                    and raw_generation.isdecimal():
                self.generation = int(raw_generation)
            else:
                raise ValueError
        except (TypeError, ValueError) as exc:
            raise InputValidationError(
                "generation must be a non-negative integer",
                operation="remote_model.configure",
            ) from exc
        if self.generation < 0:
            raise InputValidationError(
                "generation must be a non-negative integer",
                operation="remote_model.configure",
            )
        if (
            str(os.environ.get("RECAMERA_NPU_MODE") or "").strip().lower()
            == "scheduled"
            and self.generation <= 0
        ):
            raise InputValidationError(
                "managed scheduled-NPU launch is missing RECAMERA_APP_GENERATION",
                operation="remote_model.configure",
                code="missing_managed_generation",
            )
        self._alias = uuid.uuid4().hex
        self._lock = threading.RLock()
        self._request_ids = itertools.count(1)
        self._released = False
        self._transport_broken = False
        self._calls = 0
        self._failures = 0
        self._total_ms = 0.0
        self._last_ms = 0.0
        self._shared_io = None
        self._last_timings_ms = {}

        if not isinstance(memory_mb, int) or isinstance(memory_mb, bool) or memory_mb <= 0:
            raise InputValidationError(
                "memory_mb must be a positive integer",
                operation="remote_model.configure",
            )
        if not isinstance(priority, int) or isinstance(priority, bool) or not 0 <= priority <= 100:
            raise InputValidationError(
                "priority must be an integer in 0..100",
                operation="remote_model.configure",
            )
        try:
            max_fps = float(max_fps)
        except (TypeError, ValueError) as exc:
            raise InputValidationError(
                "max_fps must be numeric",
                operation="remote_model.configure",
            ) from exc
        if not np.isfinite(max_fps) or max_fps < 0:
            raise InputValidationError(
                "max_fps must be finite and non-negative",
                operation="remote_model.configure",
            )

        expected_sha = model_sha256
        if verify_model and expected_sha is None:
            expected_sha = _sha256_file(self.path)
        if expected_sha is not None:
            expected_sha = str(expected_sha).lower()
            if len(expected_sha) != 64 or any(ch not in "0123456789abcdef" for ch in expected_sha):
                raise InputValidationError(
                    "model_sha256 must be 64 lowercase hexadecimal characters",
                    operation="remote_model.configure",
                )

        try:
            if isinstance(connect_timeout, bool):
                raise ValueError
            connect_timeout = float(connect_timeout)
        except (TypeError, ValueError) as exc:
            raise InputValidationError(
                "connect_timeout must be a positive finite number",
                operation="remote_model.configure",
            ) from exc
        if not np.isfinite(connect_timeout) or connect_timeout <= 0:
            raise InputValidationError(
                "connect_timeout must be a positive finite number",
                operation="remote_model.configure",
            )

        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            deadline = time.monotonic() + connect_timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise CapabilityError(
                        "timed out waiting for appmgr inference authorization",
                        operation="inference_service.hello",
                        code="authorization_pending",
                        retryable=True,
                        details={"socket": self.socket_path},
                    )
                self._sock.settimeout(remaining)
                try:
                    self._sock.connect(self.socket_path)
                    hello, _ = self._exchange(
                        {
                            "op": "hello",
                            "request_id": next(self._request_ids),
                            "app_id": self.app_id,
                            "instance_id": self.instance_id,
                            "generation": self.generation,
                        },
                        operation="inference_service.hello",
                    )
                    break
                except CapabilityError as exc:
                    self._sock.close()
                    if exc.code != "authorization_pending":
                        raise
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise
                    time.sleep(min(0.02, remaining))
                    self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self._exchange(
                {
                    "op": "load",
                    "request_id": next(self._request_ids),
                    "alias": self._alias,
                    "path": self.path,
                    "sha256": expected_sha,
                    "memory_mb": memory_mb,
                    "priority": priority,
                    "max_fps": max_fps,
                    "spec": _spec_payload(self.spec),
                },
                operation="remote_model.load",
            )
            if (shared_io and hello.get("capabilities", {}).get("shared_io")
                    == SHARED_IO_VERSION):
                self._exchange(
                    {"op": "open_shared_io", "request_id": next(self._request_ids),
                     "alias": self._alias, "version": SHARED_IO_VERSION},
                    operation="remote_model.shared_io",
                )
        except BaseException:
            try:
                self._sock.close()
            except BaseException:
                pass
            self._released = True
            if self._shared_io is not None:
                self._shared_io.close()
                self._shared_io = None
            raise
        finally:
            # Per-request deadlines below temporarily replace this value.
            if not self._released:
                self._sock.settimeout(None)

    @property
    def released(self) -> bool:
        return self._released

    @property
    def stats(self) -> InferenceStats:
        with self._lock:
            return InferenceStats(
                calls=self._calls,
                failures=self._failures,
                total_ms=self._total_ms,
                last_ms=self._last_ms,
            )

    @property
    def io_transport(self):
        return SHARED_IO_VERSION if self._shared_io is not None else "tensor-v1"

    @property
    def last_timings_ms(self):
        with self._lock:
            return dict(self._last_timings_ms)

    def _exchange(
        self,
        header: Mapping[str, Any],
        tensors=(),
        *,
        operation: str,
        timeout: Optional[float] = None,
    ) -> tuple[dict, list[np.ndarray]]:
        try:
            if timeout is not None:
                self._sock.settimeout(timeout)
            send_message(self._sock, header, tensors)
            response, outputs = recv_message(self._sock)
            if (header.get("op") == "open_shared_io" and response.get("ok") is True
                    and response.get("shared_io") is not None):
                descriptor = response["shared_io"]
                if not isinstance(descriptor, dict) or not isinstance(descriptor.get("outputs"), list):
                    raise ProtocolError("invalid shared IO response")
                fds = recv_fds(self._sock, 1 + len(descriptor["outputs"]))
                try:
                    self._shared_io = SharedIOClient(descriptor, fds)
                except (ValueError, KeyError, TypeError) as exc:
                    raise ProtocolError(f"invalid shared IO mapping: {exc}") from exc
        except (OSError, EOFError, ProtocolError) as exc:
            self._transport_broken = True
            try:
                self._sock.close()
            except OSError:
                pass
            raise TransportError(
                f"inference service transport failed: {exc}",
                operation=operation,
                code="inference_service_unavailable",
                retryable=True,
                details={"socket": self.socket_path},
            ) from exc
        finally:
            if timeout is not None:
                try:
                    self._sock.settimeout(None)
                except OSError:
                    pass
        if response.get("ok") is not True:
            _raise_remote(response.get("error"), operation=operation)
        return response, outputs

    def _ordered_inputs(self, value: Any) -> list[np.ndarray]:
        specs = self.spec.inputs
        if isinstance(value, Mapping):
            if not specs:
                raise InputValidationError(
                    "named inputs require ModelSpec.inputs declarations",
                    operation="remote_model.infer.validate",
                )
            missing = [spec.name for spec in specs if spec.name not in value]
            extras = sorted(set(value) - {spec.name for spec in specs})
            if missing or extras:
                raise InputValidationError(
                    "named inputs do not match the model contract",
                    operation="remote_model.infer.validate",
                    details={"missing": missing, "unexpected": extras},
                )
            arrays = [np.asarray(value[spec.name]) for spec in specs]
        elif isinstance(value, (list, tuple)) and specs and len(specs) > 1:
            arrays = [np.asarray(item) for item in value]
        else:
            arrays = [np.asarray(value)]
        if specs and len(arrays) != len(specs):
            raise InputValidationError(
                f"model expects {len(specs)} inputs, got {len(arrays)}",
                operation="remote_model.infer.validate",
            )
        result: list[np.ndarray] = []
        for index, array in enumerate(arrays):
            spec = specs[index] if index < len(specs) else None
            if array.ndim == 3 and (
                spec is None or (len(spec.shape) == 4 and spec.layout == "NHWC")
            ):
                array = np.expand_dims(array, 0)
            if spec is not None and self.strict_inputs:
                array = spec.validate(array, "remote_model.infer.validate")
            elif not self.strict_inputs and array.dtype != np.uint8:
                array = array.astype(np.uint8)
            result.append(np.ascontiguousarray(array))
        return result

    def infer(self, inputs: Any, *, timeout: float = 30.0) -> list[np.ndarray]:
        return self._infer(inputs, timeout=timeout)

    def infer_prepared(self, prepare, fallback, *, timeout=30.0):
        """Internal Kit hook: prepare this call's private input while locked.

        ``prepare(descriptor)`` synchronously writes the DMA input and returns
        True, or returns False without submitting work to request the ndarray
        fallback. No borrowed camera FD crosses the process boundary.
        """
        return self._infer(None, timeout=timeout, prepare=prepare, fallback=fallback)

    def _infer(self, inputs, *, timeout, prepare=None, fallback=None):
        started = time.monotonic()
        with self._lock:
            if self._released:
                raise InferenceError(
                    "cannot infer with a released remote session",
                    operation="remote_model.infer",
                    code="session_released",
                )
            if self._transport_broken:
                raise TransportError(
                    "the inference service connection is no longer usable",
                    operation="remote_model.infer",
                    code="inference_service_connection_lost",
                    retryable=True,
                    details={"socket": self.socket_path},
                )
            if isinstance(timeout, bool):
                raise InputValidationError(
                    "timeout must be numeric, not bool",
                    operation="remote_model.infer.validate",
                )
            try:
                timeout = float(timeout)
            except (TypeError, ValueError) as exc:
                raise InputValidationError(
                    "timeout must be numeric",
                    operation="remote_model.infer.validate",
                ) from exc
            if not np.isfinite(timeout) or timeout <= 0 or timeout > 300.0:
                raise InputValidationError(
                    "timeout must be finite and in the range (0, 300] seconds",
                    operation="remote_model.infer.validate",
                )
            arrays = self._ordered_inputs(inputs) if prepare is None else None
            request_id = next(self._request_ids)
            self._calls += 1
            try:
                channel = self._shared_io
                ready = False
                if prepare is not None and channel is not None:
                    ready = prepare(dict(channel.input.descriptor))
                if not ready and arrays is None:
                    arrays = self._ordered_inputs(fallback() if fallback is not None else inputs)
                request = {"request_id": request_id, "alias": self._alias,
                           "timeout_ms": max(1, int(timeout * 1000))}
                if channel is not None:
                    if not ready:
                        if (len(arrays) != 1 or arrays[0].shape != channel.input.shape
                                or arrays[0].dtype != channel.input.dtype):
                            raise InputValidationError(
                                "input does not match the model's shared IO contract",
                                operation="remote_model.infer.validate")
                        with dma_buf_sync(channel.input.fd, write=True):
                            np.copyto(channel.input.array, arrays[0], casting="no")
                    channel.sequence += 1
                    request.update(op="infer_shared", token=channel.token,
                                   sequence=channel.sequence)
                    try:
                        response, outputs = self._exchange(
                            request, operation="remote_model.infer", timeout=timeout + 1.0)
                        if (response.get("token") != channel.token
                                or response.get("sequence") != channel.sequence or outputs):
                            raise ProtocolError("shared IO completion does not match request")
                        outputs = channel.results()
                    except BaseException:
                        # Never reuse input/output after an uncertain completion.
                        # The daemon retains its allocation until work has ended.
                        self._transport_broken = True
                        self._sock.close()
                        raise
                else:
                    request["op"] = "infer"
                    response, outputs = self._exchange(
                        request, arrays, operation="remote_model.infer", timeout=timeout + 1.0)
                self._last_timings_ms = dict(response.get("timings_ms") or {})
                if self.spec.outputs and len(outputs) != len(self.spec.outputs):
                    raise InputValidationError(
                        "remote output count does not match ModelSpec",
                        operation="remote_model.infer.outputs",
                        details={
                            "expected_count": len(self.spec.outputs),
                            "actual_count": len(outputs),
                        },
                    )
                if self.spec.outputs and self.strict_inputs:
                    outputs = [
                        spec.validate(value, "remote_model.infer.outputs")
                        for spec, value in zip(self.spec.outputs, outputs)
                    ]
                return outputs
            except BaseException:
                self._failures += 1
                raise
            finally:
                self._last_ms = (time.monotonic() - started) * 1000.0
                self._total_ms += self._last_ms

    def release(self) -> None:
        with self._lock:
            if self._released:
                return
            try:
                if not self._transport_broken:
                    try:
                        self._exchange(
                            {
                                "op": "unload",
                                "request_id": next(self._request_ids),
                                "alias": self._alias,
                            },
                            operation="remote_model.release",
                            timeout=5.0,
                        )
                    except CapabilityError as exc:
                        if exc.code != "unauthorized":
                            raise
                        # Compatibility with an older daemon (or a narrow
                        # revoke/unload race): closing this authenticated socket
                        # makes the server's _drop_client discard only our own
                        # aliases.  Treat revocation as a terminal, successful
                        # local release; it never grants or restores authority.
            finally:
                self._released = True
                self._sock.close()
                if getattr(self, "_shared_io", None) is not None:
                    self._shared_io.close()
                    self._shared_io = None

    close = release

    def __enter__(self) -> "RemoteRknnSession":
        if self._released:
            raise InferenceError(
                "cannot enter a released remote session",
                operation="remote_model.enter",
                code="session_released",
            )
        return self

    def __exit__(self, exc_type, exc, traceback) -> bool:
        try:
            self.release()
        except BaseException:
            if exc is None:
                raise
        return False

    def __del__(self) -> None:
        try:
            self.release()
        except BaseException:
            pass


class RemoteRknnModel(RemoteRknnSession):
    """Compatibility wrapper matching the permissive legacy ``RknnModel``."""

    def __init__(self, path: str, core_mask: Optional[int] = None, **kwargs) -> None:
        kwargs.setdefault("strict_inputs", False)
        super().__init__(path, core_mask=core_mask, **kwargs)


__all__ = [
    "DEFAULT_INFERENCE_SOCKET",
    "INFERENCE_SOCKET_ENV",
    "RemoteRknnModel",
    "RemoteRknnSession",
    "configured_inference_socket",
]
