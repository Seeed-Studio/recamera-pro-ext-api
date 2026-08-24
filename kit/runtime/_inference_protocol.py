"""Framed tensor protocol shared by the inference daemon and Python clients.

The transport is an ``AF_UNIX/SOCK_STREAM`` connection.  Every message is a
four-byte big-endian JSON-header length, a UTF-8 JSON object, then the raw bytes
for each tensor described by ``header["tensors"]``.  Tensor bytes are never
embedded as base64, so a 640x640 RGB input does not grow by another third.

This module deliberately contains no RKNN-specific code.  It is small enough to
ship with :mod:`kit` and lets the daemon and client share exactly one parser and
the same size/shape validation rules.
"""

from __future__ import annotations

import json
import math
import socket
import struct
from typing import Any, Iterable, Mapping, Sequence

import numpy as np


PROTOCOL_VERSION = 1
MAX_HEADER_BYTES = 64 * 1024
MAX_TENSORS = 16
MAX_TENSOR_RANK = 8
MAX_TENSOR_BYTES = 64 * 1024 * 1024
MAX_MESSAGE_BYTES = 96 * 1024 * 1024


class ProtocolError(ValueError):
    """The peer sent a malformed or out-of-policy message."""


def _recv_exact(sock: socket.socket, size: int, *, allow_eof: bool = False) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = sock.recv(remaining)
        if not chunk:
            if allow_eof and remaining == size:
                raise EOFError
            raise ProtocolError("connection closed in the middle of a message")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _tensor_meta(value: np.ndarray) -> tuple[np.ndarray, dict[str, Any]]:
    array = np.ascontiguousarray(np.asarray(value))
    if array.dtype.hasobject or array.dtype.kind in {"O", "S", "U", "V"}:
        raise ProtocolError(f"unsupported tensor dtype {array.dtype}")
    if not 1 <= array.ndim <= MAX_TENSOR_RANK:
        raise ProtocolError(f"tensor rank {array.ndim} is outside 1..{MAX_TENSOR_RANK}")
    if any(int(dim) <= 0 for dim in array.shape):
        raise ProtocolError(f"tensor dimensions must be positive: {array.shape!r}")
    if array.nbytes > MAX_TENSOR_BYTES:
        raise ProtocolError(
            f"tensor is too large: {array.nbytes} > {MAX_TENSOR_BYTES} bytes"
        )
    return array, {
        "dtype": array.dtype.str,
        "shape": [int(dim) for dim in array.shape],
        "nbytes": int(array.nbytes),
    }


def _validate_meta(raw: object) -> tuple[np.dtype, tuple[int, ...], int]:
    if not isinstance(raw, Mapping):
        raise ProtocolError("tensor metadata must be an object")
    try:
        dtype = np.dtype(raw["dtype"])
        shape = tuple(int(dim) for dim in raw["shape"])
        nbytes = int(raw["nbytes"])
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise ProtocolError("invalid tensor metadata") from exc
    if dtype.hasobject or dtype.kind in {"O", "S", "U", "V"}:
        raise ProtocolError(f"unsupported tensor dtype {dtype}")
    if not 1 <= len(shape) <= MAX_TENSOR_RANK or any(dim <= 0 for dim in shape):
        raise ProtocolError(f"invalid tensor shape {shape!r}")
    elements = math.prod(shape)
    expected = elements * dtype.itemsize
    if nbytes != expected:
        raise ProtocolError(
            f"tensor byte count mismatch: metadata={nbytes}, expected={expected}"
        )
    if nbytes < 0 or nbytes > MAX_TENSOR_BYTES:
        raise ProtocolError(f"tensor byte count is outside policy: {nbytes}")
    return dtype, shape, nbytes


def send_message(
    sock: socket.socket,
    header: Mapping[str, Any],
    tensors: Iterable[np.ndarray] = (),
) -> None:
    """Send one validated header and zero or more contiguous tensors."""

    arrays: list[np.ndarray] = []
    metas: list[dict[str, Any]] = []
    total = 0
    for value in tensors:
        if len(arrays) >= MAX_TENSORS:
            raise ProtocolError(f"too many tensors (maximum {MAX_TENSORS})")
        array, meta = _tensor_meta(value)
        total += array.nbytes
        if total > MAX_MESSAGE_BYTES:
            raise ProtocolError(
                f"tensor payload is too large: {total} > {MAX_MESSAGE_BYTES} bytes"
            )
        arrays.append(array)
        metas.append(meta)

    message = dict(header)
    if "tensors" in message:
        raise ProtocolError("caller must not supply reserved 'tensors' metadata")
    message["protocol"] = PROTOCOL_VERSION
    message["tensors"] = metas
    try:
        encoded = json.dumps(
            message, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ProtocolError("message header is not JSON serializable") from exc
    if not encoded or len(encoded) > MAX_HEADER_BYTES:
        raise ProtocolError(
            f"header size is outside 1..{MAX_HEADER_BYTES}: {len(encoded)}"
        )

    sock.sendall(struct.pack("!I", len(encoded)))
    sock.sendall(encoded)
    for array in arrays:
        sock.sendall(memoryview(array).cast("B"))


def recv_message(
    sock: socket.socket,
    *,
    max_message_bytes: int = MAX_MESSAGE_BYTES,
) -> tuple[dict[str, Any], list[np.ndarray]]:
    """Receive one message, rejecting malformed sizes before allocating."""

    prefix = _recv_exact(sock, 4, allow_eof=True)
    (header_size,) = struct.unpack("!I", prefix)
    if not 1 <= header_size <= MAX_HEADER_BYTES:
        raise ProtocolError(f"invalid header size {header_size}")
    raw_header = _recv_exact(sock, header_size)
    try:
        header = json.loads(raw_header.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError("invalid JSON header") from exc
    if not isinstance(header, dict):
        raise ProtocolError("message header must be an object")
    if header.get("protocol") != PROTOCOL_VERSION:
        raise ProtocolError(
            f"unsupported protocol {header.get('protocol')!r}; expected {PROTOCOL_VERSION}"
        )
    raw_metas = header.get("tensors")
    if not isinstance(raw_metas, list) or len(raw_metas) > MAX_TENSORS:
        raise ProtocolError("invalid tensor metadata list")

    parsed = [_validate_meta(meta) for meta in raw_metas]
    total = sum(item[2] for item in parsed)
    if total > min(int(max_message_bytes), MAX_MESSAGE_BYTES):
        raise ProtocolError(f"tensor payload exceeds message policy: {total}")

    arrays: list[np.ndarray] = []
    for dtype, shape, nbytes in parsed:
        payload = _recv_exact(sock, nbytes)
        arrays.append(np.frombuffer(payload, dtype=dtype).reshape(shape))
    return header, arrays


__all__ = [
    "MAX_HEADER_BYTES",
    "MAX_MESSAGE_BYTES",
    "MAX_TENSOR_BYTES",
    "MAX_TENSORS",
    "PROTOCOL_VERSION",
    "ProtocolError",
    "recv_message",
    "send_message",
]
