from __future__ import annotations

import os
import socket

import pytest

from kit.capabilities import (
    Capabilities,
    Capability,
    CapabilityStatus,
    capabilities,
)
from kit.errors import CapabilityError


def test_legacy_bool_constructor_remains_compatible():
    caps = Capabilities(frame_broker=True)
    assert caps.frame_broker is True
    assert caps.result_ingress is False
    assert caps.get("frame").status is CapabilityStatus.UNKNOWN


def test_require_accepts_only_verified_version():
    caps = Capabilities(details={
        "npu.lease": Capability(
            "npu.lease",
            CapabilityStatus.AVAILABLE,
            version=2,
            limits={"max_owners": 1},
            source="handshake",
        )
    })
    assert caps.require("npu.lease", min_version=2).limits["max_owners"] == 1
    with pytest.raises(CapabilityError) as caught:
        caps.require("npu.lease", min_version=3)
    assert caught.value.details["capability"] == "npu.lease"


def test_regular_file_cannot_impersonate_socket_endpoint(tmp_path, monkeypatch):
    endpoint = tmp_path / "frame.sock"
    endpoint.touch()
    monkeypatch.setenv("RECAMERA_FRAME_SOCK", str(endpoint))
    caps = capabilities(refresh=True)
    assert caps.frame_broker is False
    assert caps.get("frame").status is CapabilityStatus.UNAVAILABLE
    assert "not a Unix socket" in caps.get("frame").reason


def test_socket_presence_is_unknown_until_handshake(tmp_path, monkeypatch):
    endpoint = tmp_path / "frame.sock"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(endpoint))
    monkeypatch.setenv("RECAMERA_FRAME_SOCK", str(endpoint))
    caps = capabilities(refresh=True)
    assert caps.frame_broker is True
    assert caps.get("frame").status is CapabilityStatus.UNKNOWN
    assert caps.get("frame").source == "filesystem"
    with pytest.raises(CapabilityError):
        caps.require("frame")
    server.close()


def test_absent_socket_is_unavailable(monkeypatch):
    monkeypatch.setenv("RECAMERA_FRAME_SOCK", "/definitely/absent/frame.sock")
    caps = capabilities(refresh=True)
    assert caps.frame_broker is False
    assert caps.get("frame").status is CapabilityStatus.UNAVAILABLE


def test_package_level_capability_getter_cannot_be_shadowed_by_submodule():
    # Device imports kit.capabilities internally.  Historically that import
    # changed kit.capabilities from a function into a module at runtime.
    from kit import Device, get_capabilities
    import kit

    assert Device
    assert callable(get_capabilities)
    assert callable(kit.get_capabilities)
    assert kit.get_capabilities() is get_capabilities()
    assert not callable(kit.capabilities)  # the submodule has an honest name
