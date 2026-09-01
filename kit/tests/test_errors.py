from __future__ import annotations

import pytest

from kit.errors import (
    ConfigurationError,
    KitError,
    ResourceTimeoutError,
    TransportError,
    wrap_error,
)


def test_error_has_stable_machine_readable_context():
    error = TransportError(
        "frame socket disconnected",
        operation="frame.acquire",
        retryable=True,
        details={"native_code": 7},
    )
    assert isinstance(error, KitError)
    assert error.operation == "frame.acquire"
    assert error.code == "transport_error"
    assert error.retryable is True
    assert error.as_dict()["details"] == {"native_code": 7}
    with pytest.raises(TypeError):
        error.details["native_code"] = 8


def test_compatibility_exception_bases_are_preserved():
    assert isinstance(ConfigurationError("bad"), ValueError)
    assert isinstance(ResourceTimeoutError("busy"), TimeoutError)


def test_wrap_error_retains_backend_cause():
    backend = OSError(111, "connection refused")
    wrapped = wrap_error(
        backend,
        TransportError,
        "could not connect to frame broker",
        operation="frame.open",
        retryable=True,
    )
    assert wrapped.__cause__ is backend
    assert wrapped.operation == "frame.open"


def test_error_details_are_deeply_detached_and_immutable():
    supplied = {"items": [{"code": 7}], "shape": [1, 2, 3]}
    error = TransportError("bad", details=supplied)
    supplied["items"][0]["code"] = 99
    supplied["shape"].append(4)

    assert error.details["items"][0]["code"] == 7
    assert error.details["shape"] == [1, 2, 3]
    with pytest.raises(TypeError):
        error.details["items"][0]["code"] = 8
    with pytest.raises(TypeError):
        error.details["shape"].append(4)
    assert error.as_dict()["details"] == {
        "items": [{"code": 7}],
        "shape": [1, 2, 3],
    }
