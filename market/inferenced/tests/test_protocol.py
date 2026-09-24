import json
import socket
import struct
import threading
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pytest

from kit.runtime._inference_protocol import MAX_TENSORS, ProtocolError, recv_message, send_message


@pytest.mark.parametrize("count", [16, 17, 32, 64])
@pytest.mark.parametrize("header", [
    {"op": "infer", "request_id": 8},
    {"ok": True, "request_id": 8},
])
def test_tensor_count_roundtrip_through_expanded_limit(count, header):
    left, right = socket.socketpair()
    values = [np.full((1, 2), index, dtype=np.float32) for index in range(count)]
    with left, right:
        left.settimeout(1)
        right.settimeout(1)
        # Drain concurrently like the real daemon: many small sendall calls
        # can fill a Unix socket's packet budget even with tiny tensor data.
        with ThreadPoolExecutor(max_workers=1) as pool:
            sent = pool.submit(send_message, left, header, values)
            received, tensors = recv_message(right)
            sent.result(timeout=2)
    assert all(received[key] == value for key, value in header.items())
    assert len(tensors) == count
    for expected, actual in zip(values, tensors):
        np.testing.assert_array_equal(actual, expected)


def test_sender_rejects_over_limit_before_writing_any_message():
    left, right = socket.socketpair()
    value = np.zeros((1,), dtype=np.uint8)
    with left, right:
        left.settimeout(1)
        right.settimeout(1)
        with pytest.raises(ProtocolError, match="too many tensors"):
            send_message(left, {"op": "infer"}, [value] * (MAX_TENSORS + 1))
        # A subsequent valid message must not follow a partial rejected header.
        send_message(left, {"op": "status"})
        header, tensors = recv_message(right)
    assert header["op"] == "status"
    assert tensors == []


def test_receiver_rejects_over_limit_before_reading_payload():
    left, right = socket.socketpair()
    header = json.dumps({
        "protocol": 1,
        "tensors": [{"dtype": "|u1", "shape": [1], "nbytes": 1}] * (MAX_TENSORS + 1),
    }).encode()
    with left, right:
        left.settimeout(1)
        right.settimeout(1)
        left.sendall(struct.pack("!I", len(header)) + header)
        with pytest.raises(ProtocolError, match="invalid tensor metadata list"):
            recv_message(right)


def test_tensor_roundtrip_without_base64():
    left, right = socket.socketpair()
    value = np.arange(24, dtype=np.float32).reshape(2, 3, 4)

    thread = threading.Thread(
        target=lambda: send_message(left, {"op": "infer", "request_id": 7}, [value])
    )
    thread.start()
    header, tensors = recv_message(right)
    thread.join(timeout=2)
    left.close()
    right.close()

    assert header["op"] == "infer"
    assert header["request_id"] == 7
    assert len(tensors) == 1
    np.testing.assert_array_equal(tensors[0], value)


def test_sensevoice_ntf_float32_roundtrip_preserves_rank_and_dtype():
    """The ASR encoder contract must cross the UDS without image reshaping."""

    left, right = socket.socketpair()
    value = np.arange(1 * 344 * 560, dtype=np.float32).reshape(1, 344, 560)

    thread = threading.Thread(
        target=lambda: send_message(
            left,
            {
                "op": "infer",
                "model": {
                    "inputs": [{
                        "name": "speech",
                        "shape": [1, 344, 560],
                        "dtype": "float32",
                        "layout": "NTF",
                    }],
                },
            },
            [value],
        )
    )
    thread.start()
    header, tensors = recv_message(right)
    thread.join(timeout=2)
    left.close()
    right.close()

    assert not thread.is_alive()
    assert header["model"]["inputs"][0] == {
        "name": "speech",
        "shape": [1, 344, 560],
        "dtype": "float32",
        "layout": "NTF",
    }
    assert tensors[0].shape == (1, 344, 560)
    assert tensors[0].dtype == np.float32
    np.testing.assert_array_equal(tensors[0], value)


def test_sender_rejects_object_arrays():
    left, right = socket.socketpair()
    try:
        with pytest.raises(ProtocolError, match="dtype"):
            send_message(left, {"op": "infer"}, [np.array([object()], dtype=object)])
    finally:
        left.close()
        right.close()


def test_receiver_rejects_nbytes_shape_mismatch_before_payload_read():
    left, right = socket.socketpair()
    header = json.dumps(
        {
            "protocol": 1,
            "op": "infer",
            "tensors": [{"dtype": "|u1", "shape": [2, 2], "nbytes": 3}],
        }
    ).encode()
    left.sendall(struct.pack("!I", len(header)) + header)
    with pytest.raises(ProtocolError, match="byte count mismatch"):
        recv_message(right)
    left.close()
    right.close()


def test_clean_eof_is_distinct_from_truncated_message():
    left, right = socket.socketpair()
    left.close()
    with pytest.raises(EOFError):
        recv_message(right)
    right.close()
