"""Deferred model-sized RGB for the explicitly selected DMA inference path."""
from __future__ import annotations

import threading

from kit.errors import BufferReleasedError


class DeferredModelImage:
    """Keep a camera lease local until synchronous RGA has finished reading it.

    Mapping creates the same independent RGB array as the legacy adapter. Once
    mapped, future model calls use that array, including application edits.
    Unmapped frames can instead prepare a private RKNN DMA input directly.
    """

    def __init__(self, materialize, prepare):
        self._materialize = materialize
        self._prepare = prepare
        self._array = None
        self._active = True
        self._closed = False
        self._lock = threading.RLock()

    @property
    def released(self):
        with self._lock:
            return self._closed or (not self._active and self._array is None)

    def _check(self):
        if self.released:
            raise BufferReleasedError("model frame lease has ended", operation="frame.preprocess")

    def map(self):
        with self._lock:
            self._check()
            if self._array is None:
                self._array = self._materialize()
            return self._array

    def prepare(self, descriptor):
        with self._lock:
            self._check()
            if self._array is not None:
                # Do not discard edits to Frame.data or PreparedInput.data.
                return False
            return self._prepare(descriptor)

    def use_lease(self, call, *args):
        """Read the original camera frame, serialized with expiry and RGA.

        A materialized RGB copy can outlive this lease, but never grants more
        time to crop the original borrowed NV12 buffer.
        """
        with self._lock:
            if self._closed or not self._active:
                raise BufferReleasedError(
                    "camera frame lease has ended", operation="frame.crop")
            return call(*args)

    def expire(self):
        with self._lock:
            self._active = False
            self._materialize = self._prepare = None

    def release(self):
        with self._lock:
            self._closed = True
            self._array = None
            self.expire()
