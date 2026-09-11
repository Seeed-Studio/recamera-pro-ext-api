"""Host-only fd transport/mapping tests; memfd is explicitly fake DMA memory."""
import array
import copy
import ctypes
import errno
import os
import socket
import struct

import numpy as np
import pytest

from kit.runtime import _inference_shared as shared
from kit.runtime._inference_protocol import (
    MAX_TENSOR_BYTES, MAX_TENSORS, ProtocolError, recv_message, send_message,
)


@pytest.fixture
def memfd():
    owned = []

    def create(size=4096):
        if hasattr(os, "memfd_create"):
            fd = os.memfd_create("inferenced-host-test", os.MFD_CLOEXEC)
        else:
            # uv's Python 3.11 build omits os.memfd_create on this Linux host;
            # call the same libc API rather than skip actual SCM_RIGHTS tests.
            native = ctypes.CDLL(None, use_errno=True).memfd_create
            native.argtypes = [ctypes.c_char_p, ctypes.c_uint]
            native.restype = ctypes.c_int
            fd = native(b"inferenced-host-test", 1)  # MFD_CLOEXEC
            if fd < 0:
                raise OSError(ctypes.get_errno(), "memfd_create")
        os.ftruncate(fd, size)
        owned.append(fd)
        return fd

    yield create
    for fd in owned:
        try:
            os.close(fd)
        except OSError as exc:
            assert exc.errno == errno.EBADF


@pytest.fixture
def sockets():
    left, right = socket.socketpair()
    left.settimeout(1)
    right.settimeout(1)
    try:
        yield left, right
    finally:
        left.close()
        right.close()


def assert_closed(*fds):
    for fd in fds:
        with pytest.raises(OSError) as caught:
            os.fstat(fd)
        assert caught.value.errno == errno.EBADF


def open_fds():
    # /proc/self/fd also lists the transient fd used by listdir itself. Exclude
    # that already-closed entry so a subsequently received fd cannot hide in it.
    result = set()
    for entry in os.listdir("/proc/self/fd"):
        try:
            os.fstat(int(entry))
        except OSError as exc:
            assert exc.errno == errno.EBADF
        else:
            result.add(entry)
    return result


def descriptor():
    return {
        "version": shared.SHARED_IO_VERSION,
        "token": "0123456789abcdef0123456789abcdef",
        "input": {"size": 32, "offset": 16, "shape": [1, 2, 3, 3],
                  "strides": [32, 16, 3, 1], "dtype": "uint8"},
        "outputs": [{"size": 40, "offset": 8, "shape": [1, 2, 3],
                     "strides": [40, 20, 4], "dtype": "float32"}],
    }


def test_scm_rights_multi_fd_roundtrip_and_no_inherited_descriptors(memfd, sockets):
    sender, receiver = sockets
    original = [memfd(), memfd(), memfd()]
    for index, fd in enumerate(original):
        os.pwrite(fd, bytes([31 + index]), 0)
        os.set_inheritable(fd, True)
    shared.send_fds(sender, original)
    received = shared.recv_fds(receiver, len(original))
    try:
        assert len(set(original + received)) == 6
        assert all(not os.get_inheritable(fd) for fd in received)
        assert [os.pread(fd, 1, 0) for fd in received] == [b"\x1f", b"\x20", b"\x21"]
        os.pwrite(received[1], b"z", 0)
        assert os.pread(original[1], 1, 0) == b"z"
    finally:
        for fd in received:
            os.close(fd)
    assert all(os.fstat(fd).st_size == 4096 for fd in original)


def test_legacy_tensor_frame_then_fd_record_then_next_frame_keeps_boundaries(memfd, sockets):
    sender, receiver = sockets
    tensor = np.arange(24, dtype=np.float32).reshape(2, 3, 4)
    original = [memfd(), memfd()]
    send_message(sender, {"op": "open_shared_io", "request_id": 8}, [tensor])
    shared.send_fds(sender, original)
    send_message(sender, {"op": "infer_shared", "sequence": 1})

    header, arrays = recv_message(receiver)
    assert header["request_id"] == 8
    np.testing.assert_array_equal(arrays[0], tensor)
    received = shared.recv_fds(receiver, 2)
    try:
        next_header, next_arrays = recv_message(receiver)
        assert next_header["op"] == "infer_shared"
        assert next_header["sequence"] == 1
        assert next_arrays == []
    finally:
        for fd in received:
            os.close(fd)


@pytest.mark.parametrize("count", [0, -1, MAX_TENSORS + 1, True, 1.5, "2"])
def test_receiver_rejects_invalid_expected_count_without_reading(count, sockets):
    sender, receiver = sockets
    sender.sendall(b"X")
    with pytest.raises(ProtocolError):
        shared.recv_fds(receiver, count)
    assert receiver.recv(1) == b"X"


@pytest.mark.parametrize("sent, expected", [(1, 2), (2, 1), (3, 2)])
def test_fd_count_mismatch_closes_all_received_duplicates(memfd, sockets, sent, expected):
    sender, receiver = sockets
    original = [memfd() for _ in range(sent)]
    before = open_fds()
    shared.send_fds(sender, original)
    with pytest.raises(ProtocolError):
        shared.recv_fds(receiver, expected)
    assert open_fds() == before
    assert all(os.fstat(fd).st_size == 4096 for fd in original)


def test_ancillary_truncation_closes_received_and_kernel_dropped_fds(memfd, sockets):
    sender, receiver = sockets
    fd = memfd()
    before = open_fds()
    sender.sendmsg([b"F"], [(socket.SOL_SOCKET, socket.SCM_RIGHTS,
                            array.array("i", [fd] * (MAX_TENSORS + 8)))])
    with pytest.raises(ProtocolError):
        shared.recv_fds(receiver, MAX_TENSORS)
    assert open_fds() == before


@pytest.mark.parametrize("payload", [b"X", b"\x00"])
def test_invalid_record_marker_does_not_leak_received_fds(memfd, sockets, payload):
    sender, receiver = sockets
    fd = memfd()
    before = open_fds()
    sender.sendmsg([payload], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", [fd]))])
    with pytest.raises(ProtocolError):
        shared.recv_fds(receiver, 1)
    assert open_fds() == before


def test_eof_before_fd_record_fails_cleanly(sockets):
    sender, receiver = sockets
    sender.close()
    with pytest.raises(ProtocolError):
        shared.recv_fds(receiver, 1)


def test_fd_cloexec_failure_still_closes_entire_received_batch(memfd, sockets, monkeypatch):
    sender, receiver = sockets
    original = [memfd(), memfd()]
    before = open_fds()
    shared.send_fds(sender, original)
    monkeypatch.setattr(shared.os, "set_inheritable", lambda *_: (_ for _ in ()).throw(OSError("injected")))
    with pytest.raises(OSError, match="injected"):
        shared.recv_fds(receiver, 2)
    assert open_fds() == before


def test_unexpected_ancillary_record_does_not_leak_later_rights(memfd, sockets):
    sender, receiver = sockets
    # Linux attaches SCM_CREDENTIALS before SCM_RIGHTS when the receiver opts
    # in. A rejected ancillary message must still close every received fd.
    receiver.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
    original = [memfd(), memfd()]
    before = open_fds()
    shared.send_fds(sender, original)
    try:
        with pytest.raises(ProtocolError):
            shared.recv_fds(receiver, 2)
        assert open_fds() == before
    finally:
        for leaked in open_fds() - before:
            os.close(int(leaked))


def test_input_mapping_respects_offset_row_padding_and_closes_only_its_owned_fd(memfd):
    original = memfd()
    os.pwrite(original, b"\xa5" * 128, 0)
    owned = os.dup(original)
    mapped = shared.MappedTensor(owned, descriptor()["input"])
    try:
        assert mapped.descriptor["fd"] == owned
        mapped.array[0, 0] = 31
        mapped.array[0, 1] = 97
        contents = os.pread(original, 48, 0)
        assert contents[:16] == b"\xa5" * 16
        assert contents[16:25] == bytes([31]) * 9
        assert contents[25:32] == b"\xa5" * 7
        assert contents[32:41] == bytes([97]) * 9
        assert contents[41:48] == b"\xa5" * 7
    finally:
        mapped.close()
    assert_closed(owned)
    assert os.fstat(original).st_size == 4096
    mapped.close()  # idempotent teardown, including after a fallback


@pytest.mark.parametrize("metadata", [
    None, "not-an-object", [],
    {"size": 0}, {"size": True}, {"offset": -1}, {"offset": True},
    {"size": MAX_TENSOR_BYTES + 1}, {"offset": MAX_TENSOR_BYTES + 1},
    {"shape": []}, {"shape": [1, 2, 3]}, {"shape": [2, 2, 3, 3]},
    {"shape": [1, 2, 3, 0]}, {"shape": [1, 2, 3, 1.5]},
    {"strides": [32, 8, 3, 1]}, {"strides": [32, 16, 3]},
    {"strides": [32, 16, 3, -1]}, {"size": 24},
    {"dtype": "float32"}, {"dtype": "object"}, {"dtype": "bad-dtype"},
])
def test_invalid_tensor_metadata_closes_fd_before_mapping(memfd, metadata):
    fd = memfd()
    meta = descriptor()["input"]
    if isinstance(metadata, dict):
        meta.update(metadata)
    else:
        meta = metadata
    with pytest.raises((ValueError, TypeError, KeyError, AttributeError)):
        shared.MappedTensor(fd, meta)
    assert_closed(fd)


def test_truncated_backing_allocation_rejects_mapping_and_closes_fd(memfd):
    fd = memfd(20)
    with pytest.raises((ValueError, OSError)):
        shared.MappedTensor(fd, descriptor()["input"])
    assert_closed(fd)


@pytest.mark.parametrize("change", ["not-object", "version", "token", "count", "output", "missing"])
def test_invalid_connection_metadata_closes_consumed_and_remaining_fds(memfd, change):
    fds = [memfd(), memfd(), memfd()]
    meta = descriptor()
    meta["outputs"].append(copy.deepcopy(meta["outputs"][0]))
    if change == "not-object":
        meta = None
    elif change == "version":
        meta["version"] = "unsupported"
    elif change == "token":
        meta["token"] = 31
    elif change == "count":
        meta["outputs"].pop()
    elif change == "output":
        meta["outputs"][0]["dtype"] = "uint8"
    elif change == "missing":
        del meta["outputs"]
    with pytest.raises((ValueError, TypeError, KeyError, AttributeError)):
        shared.SharedIOClient(meta, fds)
    assert_closed(*fds)


def test_output_results_are_owned_contiguous_copies_across_reuse_and_close(memfd, monkeypatch):
    # DMA synchronization, if provided, is mocked solely for these host memfds.
    # Never teach production code to treat ENOTTY as successful DMA synchronization.
    monkeypatch.setattr(shared.fcntl, "ioctl", lambda *_a, **_kw: 0)
    fds = [memfd(), memfd()]
    meta = descriptor()
    os.pwrite(fds[1], np.array([1, 2, 3], dtype=np.float32).tobytes(), 8)
    os.pwrite(fds[1], np.array([4, 5, 6], dtype=np.float32).tobytes(), 28)
    client = shared.SharedIOClient(meta, fds)
    try:
        assert client.outputs[0].array.flags.writeable is False
        first = client.results()[0]
        assert first.flags.owndata and first.flags.c_contiguous
        assert not np.shares_memory(first, client.outputs[0].array)
        os.pwrite(fds[1], np.array([91, 92, 93], dtype=np.float32).tobytes(), 8)
        second = client.results()[0]
        np.testing.assert_array_equal(second[0, 0], [91, 92, 93])
        np.testing.assert_array_equal(first, [[[1, 2, 3], [4, 5, 6]]])
    finally:
        client.close()
    assert_closed(*fds)
    np.testing.assert_array_equal(first, [[[1, 2, 3], [4, 5, 6]]])
    np.testing.assert_array_equal(second, [[[91, 92, 93], [4, 5, 6]]])


@pytest.mark.parametrize("write, access", [(False, 1), (True, 2)])
def test_dma_cpu_sync_brackets_access_and_releases_on_body_failure(monkeypatch, write, access):
    operations = []

    def ioctl(fd, request, flags):
        operations.append((fd, request, struct.unpack("Q", flags)[0]))

    monkeypatch.setattr(shared.fcntl, "ioctl", ioctl)
    with pytest.raises(RuntimeError, match="copy interrupted"):
        with shared.dma_buf_sync(9, write=write):
            assert operations == [(9, 0x40086200, access)]
            raise RuntimeError("copy interrupted")
    assert operations == [(9, 0x40086200, access), (9, 0x40086200, access | 4)]


def test_dma_sync_start_failure_never_allows_cpu_access(monkeypatch):
    operations = []

    def ioctl(fd, request, flags):
        operations.append((fd, request, struct.unpack("Q", flags)[0]))
        raise OSError(errno.ENOTTY, "host memory is not DMA")

    monkeypatch.setattr(shared.fcntl, "ioctl", ioctl)
    with pytest.raises(OSError) as caught:
        with shared.dma_buf_sync(9):
            pytest.fail("must not read potentially stale DMA memory")
    assert caught.value.errno == errno.ENOTTY
    assert operations == [(9, 0x40086200, 1)]


def test_dma_sync_end_failure_is_not_silently_ignored(monkeypatch):
    def ioctl(_fd, _request, flags):
        if struct.unpack("Q", flags)[0] & 4:
            raise OSError(errno.EIO, "cache synchronization failed")

    monkeypatch.setattr(shared.fcntl, "ioctl", ioctl)
    with pytest.raises(OSError) as caught:
        with shared.dma_buf_sync(9, write=True):
            pass
    assert caught.value.errno == errno.EIO
