"""Optional, connection-owned DMA tensor transport for local inference.

The version-1 tensor protocol remains unchanged. Only a client that explicitly
requests ``open_shared_io`` receives this one-byte SCM_RIGHTS record, following
the ordinary response. The daemon allocates every buffer; it never imports a
client-supplied address or descriptor. Returned model results remain copies.
"""
from __future__ import annotations

import array
import contextlib
import fcntl
import mmap
import os
import socket
import struct

import numpy as np

from ._inference_protocol import MAX_MESSAGE_BYTES, MAX_TENSOR_BYTES, MAX_TENSORS, ProtocolError

SHARED_IO_VERSION = "rknn-dma-v1"


@contextlib.contextmanager
def dma_buf_sync(fd, *, write=False):
    """Bracket CPU accesses to DMA memory (linux/dma-buf.h)."""
    access = 2 if write else 1
    fcntl.ioctl(fd, 0x40086200, struct.pack("Q", access))
    try:
        yield
    finally:
        fcntl.ioctl(fd, 0x40086200, struct.pack("Q", access | 4))


def send_fds(sock, fds):
    if not 1 <= len(fds) <= MAX_TENSORS:
        raise ProtocolError("invalid shared IO descriptor count")
    rights = array.array("i", fds)
    if sock.sendmsg([b"F"], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, rights)]) != 1:
        raise ProtocolError("incomplete shared IO descriptor record")


def recv_fds(sock, count):
    if type(count) is not int or not 1 <= count <= MAX_TENSORS:
        raise ProtocolError("invalid shared IO descriptor count")
    fds = []
    try:
        data, ancillary, flags, _ = sock.recvmsg(
            1, socket.CMSG_SPACE(MAX_TENSORS * array.array("i").itemsize),
            getattr(socket, "MSG_CMSG_CLOEXEC", 0),
        )
        unexpected = False
        for level, kind, value in ancillary:
            if level != socket.SOL_SOCKET or kind != socket.SCM_RIGHTS:
                unexpected = True
                continue
            ints = array.array("i")
            ints.frombytes(value[:len(value) - len(value) % ints.itemsize])
            fds.extend(ints)
        if unexpected:
            raise ProtocolError("unexpected shared IO ancillary record")
        if data != b"F" or flags & socket.MSG_CTRUNC or len(fds) != count:
            raise ProtocolError("invalid shared IO descriptor record")
        for fd in fds:
            os.set_inheritable(fd, False)
        return fds
    except BaseException:
        for fd in fds:
            os.close(fd)
        raise


def _positive_int(value, label, *, zero=False):
    if type(value) is not int or value < (0 if zero else 1):
        raise ProtocolError(f"invalid shared IO {label}")
    return value


class MappedTensor:
    """A private client mapping; closing it never frees a daemon's RKNN handle."""

    def __init__(self, fd, descriptor, *, output=False):
        self.fd = fd
        self.mapping = None
        self.array = None
        try:
            if not isinstance(descriptor, dict):
                raise ProtocolError("shared IO descriptor must be an object")
            self.descriptor = dict(descriptor)
            self.descriptor["fd"] = fd  # FD numbers only have local meaning.
            self.size = _positive_int(descriptor.get("size"), "size")
            self.offset = _positive_int(descriptor.get("offset", 0), "offset", zero=True)
            if self.size > MAX_TENSOR_BYTES or self.offset > MAX_TENSOR_BYTES:
                raise ProtocolError("shared IO mapping exceeds tensor limit")
            self.shape = tuple(_positive_int(v, "shape") for v in descriptor["shape"])
            self.strides = tuple(_positive_int(v, "stride") for v in descriptor["strides"])
            if not 1 <= len(self.shape) <= 8 or len(self.strides) != len(self.shape):
                raise ProtocolError("invalid shared IO rank")
            self.dtype = np.dtype(descriptor["dtype"])
            expected = np.dtype("float32" if output else "uint8")
            if self.dtype != expected:
                raise ProtocolError("unsupported shared IO dtype")
            # Positive non-overlapping row-major strides, allowing row padding.
            required = self.dtype.itemsize
            for dimension, stride in zip(reversed(self.shape), reversed(self.strides)):
                if stride < required:
                    raise ProtocolError("overlapping shared IO tensor strides")
                required += (dimension - 1) * stride
            if required > self.size:
                raise ProtocolError("shared IO tensor exceeds its allocation")
            if not output and (len(self.shape) != 4 or self.shape[0] != 1):
                raise ProtocolError("shared IO requires one NHWC image")
            self.mapping = mmap.mmap(
                fd, self.offset + self.size,
                flags=mmap.MAP_SHARED,
                prot=mmap.PROT_READ | (0 if output else mmap.PROT_WRITE),
            )
            self.array = np.ndarray(
                self.shape, dtype=self.dtype, buffer=self.mapping,
                offset=self.offset, strides=self.strides,
            )
        except BaseException:
            self.close()
            raise

    def close(self):
        self.array = None
        mapping, self.mapping = self.mapping, None
        try:
            if mapping is not None:
                mapping.close()
        finally:
            fd, self.fd = self.fd, -1
            if fd >= 0:
                os.close(fd)


class SharedIOClient:
    def __init__(self, descriptor, fds):
        self.input = None
        self.outputs = []
        self.sequence = 0
        remaining = list(fds)
        try:
            if not isinstance(descriptor, dict):
                raise ProtocolError("shared IO descriptor must be an object")
            self.token = descriptor.get("token")
            if descriptor.get("version") != SHARED_IO_VERSION:
                raise ProtocolError("unsupported shared IO version")
            if not isinstance(self.token, str) or len(self.token) != 32:
                raise ProtocolError("invalid shared IO token")
            metas = [descriptor["input"], *descriptor["outputs"]]
            if not 2 <= len(metas) <= MAX_TENSORS or len(metas) != len(remaining):
                raise ProtocolError("shared IO descriptor count mismatch")
            if not all(isinstance(meta, dict) for meta in metas):
                raise ProtocolError("shared IO tensor descriptors must be objects")
            if sum(_positive_int(m.get("size"), "size")
                   + _positive_int(m.get("offset", 0), "offset", zero=True)
                   for m in metas) > MAX_MESSAGE_BYTES:
                raise ProtocolError("shared IO allocations exceed message limit")
            self.input = MappedTensor(remaining.pop(0), metas[0])
            for meta in metas[1:]:
                self.outputs.append(MappedTensor(remaining.pop(0), meta, output=True))
        except BaseException:
            self.close()
            raise
        finally:
            for fd in remaining:
                os.close(fd)

    def results(self):
        # Preserve the established ownership contract, including callers that
        # retain results across another infer(), stop, or application reload.
        results = []
        for item in self.outputs:
            with dma_buf_sync(item.fd):
                results.append(np.array(item.array, copy=True, order="C"))
        return results

    def close(self):
        buffers = ([self.input] if self.input is not None else []) + self.outputs
        self.input, self.outputs = None, []
        failure = None
        for item in buffers:
            try:
                item.close()
            except BaseException as exc:
                failure = failure or exc
        if failure is not None:
            raise failure
