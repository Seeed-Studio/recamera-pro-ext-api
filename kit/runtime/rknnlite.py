"""RKNNLite compatibility backend with bounded cyclic-buffer retention.

RKNNLite 2.3.2 leaves ctypes input/output buffers in reference cycles. Python's
automatic collector counts objects, not their backing bytes: a handful of ASR
outputs can retain hundreds of MiB before it runs. Collect after each vendor
call in the process that owns the runtime, including failed calls and teardown.
The default image ctypes/DMA backend does not use this wrapper or pay this cost.

This does not change tensor ownership, dtype, shape, layout or backend selection.
Live outputs remain valid across subsequent calls and release. Native context
ownership/serialization still belongs to the session or inference daemon.
"""

from __future__ import annotations

import gc

import numpy as np


class RknnLiteRuntime:
    """The vendor interface with explicit collection at synchronous boundaries."""

    backend = "rknnlite"

    def __init__(self, *args, **kwargs):
        # Keep host imports independent of the target-only vendor wheel.
        from rknnlite.api import RKNNLite

        self._runtime = RKNNLite(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_runtime"), name)

    def init_runtime(self, *args, **kwargs):
        try:
            return self._runtime.init_runtime(*args, **kwargs)
        finally:
            gc.collect()

    def inference(self, *args, **kwargs):
        outputs = None
        try:
            outputs = self._runtime.inference(*args, **kwargs)
            if outputs is None:
                return None
            # Detach from the vendor's ctypes-backed arrays before collecting.
            # Otherwise the live return value keeps its buffer cycle reachable;
            # dropping it after an RPC/ASR segment leaves the last large output
            # waiting for another inference/collection. Owned NumPy arrays are
            # reclaimed by reference counting as soon as their consumer finishes.
            # Non-array metadata (e.g. get_frame_id=True) is passed through.
            return [np.array(value, copy=True, order="K")
                    if isinstance(value, np.ndarray) else value
                    for value in outputs]
        finally:
            outputs = None
            # Also covers None/error returns. Do not disable automatic GC or
            # tune process-global thresholds, which affect unrelated workloads.
            gc.collect()

    def release(self):
        try:
            result = self._runtime.release()
            if result in (None, 0):
                # RKNNLite.release destroys the context but keeps the model's
                # bytes on the wrapper. Clear only after successful teardown;
                # failed handles must remain intact for quarantine/retry.
                self._runtime.rknn_data = None
            return result
        finally:
            gc.collect()
