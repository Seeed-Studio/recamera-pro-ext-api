"""Conservative device-capability discovery for Python AI applications.

There is an important distinction between *seeing a socket path* and proving
that its protocol, version, permissions, and limits are usable.  The legacy
boolean attributes are retained for adapter selection, while ``details`` makes
that confidence explicit.  New applications should call :meth:`require`
before relying on a capability that needs a negotiated contract.
"""
from __future__ import annotations

import os
import stat
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping, Optional

from .errors import CapabilityError


class CapabilityStatus(str, Enum):
    """Confidence in a device capability."""

    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"
    DEGRADED = "degraded"


@dataclass(frozen=True)
class Capability:
    """One named, versioned capability and its negotiated limits.

    ``source`` states how the information was obtained.  A filesystem probe is
    deliberately reported as ``UNKNOWN`` when a path exists, because no
    protocol handshake has occurred yet.
    """

    name: str
    status: CapabilityStatus
    version: Optional[int] = None
    limits: Mapping[str, Any] = field(default_factory=dict)
    source: str = "unknown"
    reason: Optional[str] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", str(self.name))
        object.__setattr__(self, "status", CapabilityStatus(self.status))
        object.__setattr__(self, "limits",
                           MappingProxyType(dict(self.limits or {})))

    @property
    def usable(self) -> bool:
        """Whether the capability has been positively verified."""

        return self.status is CapabilityStatus.AVAILABLE


_LEGACY_NAMES = {
    "frame_broker": "frame",
    "result_ingress": "result.ingress",
    "audio_broker": "audio",
    "control_api": "control",
    "probe": "probe",
}


@dataclass(frozen=True)
class Capabilities:
    """Snapshot of device capabilities.

    The five booleans preserve the original adapter-registry API.  They mean
    only that a legacy probe selected that backend; they do *not* imply that a
    versioned handshake succeeded.  ``get``/``require`` expose the richer and
    safer contract for new code.
    """

    frame_broker: bool = False
    result_ingress: bool = False
    audio_broker: bool = False
    control_api: bool = False
    probe: bool = False
    details: Mapping[str, Capability] = field(default_factory=dict)

    def __post_init__(self) -> None:
        normalized = dict(self.details or {})
        for attr, name in _LEGACY_NAMES.items():
            if name not in normalized:
                selected = bool(getattr(self, attr))
                normalized[name] = Capability(
                    name=name,
                    status=(CapabilityStatus.UNKNOWN if selected
                            else CapabilityStatus.UNAVAILABLE),
                    source="legacy",
                    reason=("legacy probe selected the backend but did not "
                            "negotiate its protocol"
                            if selected else "legacy probe did not find it"),
                )
        object.__setattr__(self, "details", MappingProxyType(normalized))

    def get(self, name: str) -> Capability:
        """Return a capability, or an explicit ``UNKNOWN`` record."""

        key = str(name)
        found = self.details.get(key)
        if found is not None:
            return found
        return Capability(
            name=key,
            status=CapabilityStatus.UNKNOWN,
            source="not-reported",
            reason="the device did not report this capability",
        )

    def require(
        self,
        name: str,
        *,
        min_version: Optional[int] = None,
        allow_degraded: bool = False,
    ) -> Capability:
        """Return a verified capability or raise :class:`CapabilityError`.

        A filesystem-only ``UNKNOWN`` result is rejected.  This fail-closed
        behavior prevents applications from treating a stale socket file as a
        valid frame broker or NPU lease service.
        """

        cap = self.get(name)
        accepted = cap.status is CapabilityStatus.AVAILABLE
        if allow_degraded and cap.status is CapabilityStatus.DEGRADED:
            accepted = True
        if accepted and (min_version is None or
                         (cap.version is not None and
                          cap.version >= int(min_version))):
            return cap
        reason = cap.reason or f"status is {cap.status.value}"
        if min_version is not None:
            if cap.version is None:
                reason = f"{reason}; negotiated version is unknown, need >= {min_version}"
            else:
                reason = f"{reason}; version={cap.version}, need >= {min_version}"
        raise CapabilityError(
            f"required capability {name!r} is not verified: {reason}",
            operation="capabilities.require",
            details={
                "capability": str(name),
                "status": cap.status.value,
                "version": cap.version,
                "min_version": min_version,
                "source": cap.source,
            },
        )


def _socket_path(env_name: str, default: str) -> str:
    return os.environ.get(env_name, default)


def frame_socket_path() -> str:
    """Frame-broker path used for diagnostics (native routing is fixed)."""

    return _socket_path("RECAMERA_FRAME_SOCK", "/run/recamera/frame.sock")


def result_socket_path() -> str:
    """Result-ingress path used for diagnostics (native routing is fixed)."""

    return _socket_path("RECAMERA_RESULT_SOCK", "/run/recamera/result-in.sock")


def audio_socket_path() -> str:
    """Audio-broker path used for diagnostics (native routing is fixed)."""

    return _socket_path("RECAMERA_AUDIO_SOCK", "/run/recamera/audio.sock")


def probe_socket_path() -> str:
    """Observability path used for diagnostics (native routing is fixed)."""

    return _socket_path("RECAMERA_PROBE_SOCK", "/run/recamera/probe.sock")


def _env_bool(name: str) -> bool:
    return str(os.environ.get(name, "")).strip().lower() in {
        "1", "true", "yes", "on",
    }


def _filesystem_capability(name: str, path: str) -> tuple[bool, Capability]:
    try:
        mode = os.stat(path).st_mode
    except FileNotFoundError:
        mode = None
    except OSError as exc:
        return False, Capability(
            name=name,
            status=CapabilityStatus.UNAVAILABLE,
            source="filesystem",
            reason=f"cannot stat endpoint {path}: {exc}",
            limits={"endpoint": path},
        )
    if mode is not None and stat.S_ISSOCK(mode):
        return True, Capability(
            name=name,
            status=CapabilityStatus.UNKNOWN,
            source="filesystem",
            reason=(f"endpoint {path} exists, but no version/capability "
                    "handshake has been performed"),
            limits={"endpoint": path},
        )
    reason = (f"endpoint {path} does not exist" if mode is None else
              f"endpoint {path} exists but is not a Unix socket")
    return False, Capability(
        name=name,
        status=CapabilityStatus.UNAVAILABLE,
        source="filesystem",
        reason=reason,
        limits={"endpoint": path},
    )


def probe_capabilities() -> Capabilities:
    """Perform side-effect-free legacy discovery.

    This function does not connect to any endpoint.  Consequently an existing
    socket is recorded as ``UNKNOWN`` rather than ``AVAILABLE``.  A future
    native capability getter can replace these records with negotiated version
    and limit data without changing the public Python API.
    """

    frame, frame_cap = _filesystem_capability("frame", frame_socket_path())
    result, result_cap = _filesystem_capability(
        "result.ingress", result_socket_path())
    audio, audio_cap = _filesystem_capability("audio", audio_socket_path())
    probe, probe_cap = _filesystem_capability("probe", probe_socket_path())

    result_forced = _env_bool("RECAMERA_RESULT_INGRESS")
    if result_forced and not result:
        result = True
        result_cap = Capability(
            name="result.ingress",
            status=CapabilityStatus.UNKNOWN,
            source="environment",
            reason="forced by RECAMERA_RESULT_INGRESS; not handshaken",
        )

    control = _env_bool("RECAMERA_CONTROL_API")
    control_cap = Capability(
        name="control",
        status=(CapabilityStatus.UNKNOWN if control
                else CapabilityStatus.UNAVAILABLE),
        source="environment",
        reason=("forced by RECAMERA_CONTROL_API; not handshaken" if control
                else "RECAMERA_CONTROL_API is not enabled"),
    )

    return Capabilities(
        frame_broker=frame,
        result_ingress=result,
        audio_broker=audio,
        control_api=control,
        probe=probe,
        details={
            "frame": frame_cap,
            "result.ingress": result_cap,
            "audio": audio_cap,
            "control": control_cap,
            "probe": probe_cap,
        },
    )


_CACHED: Optional[Capabilities] = None


def capabilities(refresh: bool = False) -> Capabilities:
    """Return a process-cached capability snapshot.

    This spelling is retained inside ``kit.capabilities`` and in the legacy
    adapter registry.  New application code should import
    :func:`get_capabilities`; unlike the old top-level ``kit.capabilities()``
    spelling, it cannot collide with Python's ``kit.capabilities`` submodule.
    """

    global _CACHED
    if refresh or _CACHED is None:
        _CACHED = probe_capabilities()
    return _CACHED


def get_capabilities(refresh: bool = False) -> Capabilities:
    """Return the cached device capability snapshot.

    ``get_capabilities`` is the stable package-level spelling.  A function
    cannot safely share the name ``capabilities`` with its Python submodule:
    importing another public class that depends on the submodule would replace
    ``kit.capabilities`` with that module object.
    """

    return capabilities(refresh=refresh)


__all__ = [
    "Capabilities",
    "Capability",
    "CapabilityStatus",
    "audio_socket_path",
    "capabilities",
    "get_capabilities",
    "frame_socket_path",
    "probe_capabilities",
    "probe_socket_path",
    "result_socket_path",
]
