"""
RKNN inference engine for reCamera Pro (Rockchip RV1126B).

`RknnModel` is the only NPU handle in the kit: `kit.app.App._load_model` builds
one per declared model, and every vision app goes through it. It has two
interchangeable backends.

**ctypes (default)** -- `kit.runtime.ctypes_rknn.CtypesRknnModel` drives
`librknnrt.so` directly. This is the default because `RKNNLite.inference()`
leaves one set of reference cycles per call, holding that call's ctypes
buffers: RSS grows **43.8 kB per call** on a nine-output YOLOv8 graph --
~2.2 MB/min at 18.8 fps, an OOM in hours on a 2 GB board. The memory is cyclic
garbage rather than a C-level leak (a periodic `gc.collect()` returns RSS to
the same byte count every time, and `--gc-every 5` in the repro drops the rate
269x), and `librknnrt` itself is clean -- driving the identical sequence
without the wrapper is flat over 6465 iterations with no `gc` at all. The other
valid fix is periodic `gc.collect()` in every inference loop; it costs ~23% on
inference latency, which is why this kit bypasses the wrapper instead.

The swap is numerically a no-op, and that was measured rather than argued: over
64 inputs (60 real frames plus black/white/grey/noise) all 576 output tensors
came back **bit-identical**, and the decoded post-NMS boxes matched to 0.0 in
both corner position and score.

**rknnlite** -- the original `RKNNLite` wrapper, kept as the retreat. Select it
with `ESK_RKNN_BACKEND=rknnlite`. It works, and grows RSS unless the caller runs
`gc.collect()` periodically; it exists so a board whose `librknnrt.so` does not
match the ctypes prototypes has somewhere to go, and so the growth can be
re-measured without reinstalling an old package. When the ctypes backend is
requested but cannot initialise, the fall back to rknnlite is **printed**, never
silent -- a quiet downgrade would make the growth look like it came back on its
own.

Only dependencies: numpy, plus rknnlite for the non-default backend. No
onnxruntime / torch.

librknnrt.so lives in /oem/usr/lib on the reCamera Pro; rknnlite dlopen's it
from /usr/lib, so a symlink is required once:
    ln -sf /oem/usr/lib/librknnrt.so /usr/lib/librknnrt.so
(The ctypes backend tries both paths itself and needs no symlink.)
"""
from __future__ import annotations

import os
from typing import List

import numpy as np

DEFAULT_BACKEND = "ctypes"


def _requested_backend() -> str:
    value = (os.environ.get("ESK_RKNN_BACKEND") or "").strip().lower()
    if value in ("ctypes", "rknnlite"):
        return value
    if value:
        print(f"[kit.runtime.engine] ignoring ESK_RKNN_BACKEND={value!r} "
              f"(expected 'ctypes' or 'rknnlite')", flush=True)
    return DEFAULT_BACKEND


class RknnLiteModel:
    """The original rknn_toolkit_lite2 path. Works, and accumulates 43.8 kB/call
    of cyclic garbage unless the caller collects periodically."""

    backend = "rknnlite"

    def __init__(self, path: str, core_mask: int | None = None):
        from rknnlite.api import RKNNLite

        self.path = path
        self._rknn = RKNNLite()

        ret = self._rknn.load_rknn(path)
        if ret != 0:
            raise RuntimeError(f"load_rknn failed for {path!r}: ret={ret}")

        # RV1126B is single-core NPU; pass core_mask only when explicitly given.
        if core_mask is not None:
            ret = self._rknn.init_runtime(core_mask=core_mask)
        else:
            ret = self._rknn.init_runtime()
        if ret != 0:
            raise RuntimeError(f"init_runtime failed for {path!r}: ret={ret}")

    def infer(self, input_uint8: np.ndarray) -> List[np.ndarray]:
        arr = np.asarray(input_uint8)
        if arr.ndim == 3:
            arr = np.expand_dims(arr, 0)
        if arr.dtype != np.uint8:
            arr = arr.astype(np.uint8)
        return self._rknn.inference(inputs=[arr])

    def release(self) -> None:
        try:
            self._rknn.release()
        except Exception:
            pass


class RknnModel:
    """Load an .rknn model and run inference on uint8 NHWC input.

    A thin front for whichever backend is selected. The surface is unchanged
    from the rknnlite-only version -- `path`, `infer`, `release`, and the
    context manager -- because every app in this repo is written against it and
    the backend swap has to be invisible to all of them.
    """

    def __init__(self, path: str, core_mask: int | None = None):
        self.path = path
        self._impl = None
        backend = _requested_backend()

        if backend == "ctypes":
            try:
                from kit.runtime.ctypes_rknn import CtypesRknnModel

                self._impl = CtypesRknnModel(path, core_mask=core_mask)
            except Exception as exc:  # noqa: BLE001 -- reported, then degraded
                print(f"[kit.runtime.engine] ctypes backend unavailable for "
                      f"{path!r} ({type(exc).__name__}: {exc}); falling back to "
                      f"rknnlite, which accumulates ~43.8 kB per inference "
                      f"of gc-collectable cycles",
                      flush=True)

        if self._impl is None:
            self._impl = RknnLiteModel(path, core_mask=core_mask)

        self.backend = self._impl.backend

    def infer(self, input_uint8: np.ndarray) -> List[np.ndarray]:
        """
        Run one forward pass.

        `input_uint8` is a uint8 array shaped [1, H, W, 3] (NHWC) or [H, W, 3].
        Normalization (mean=0/std=255) is baked into the model, so feed RAW
        uint8 RGB pixels -- do NOT divide by 255.

        Returns the list of raw output tensors (numpy arrays), float32, shaped
        by the graph's declared output dims. Identical on both backends.
        """
        return self._impl.infer(input_uint8)

    def describe(self) -> dict:
        """Backend/graph detail when the impl offers it -- diagnostics only."""
        describe = getattr(self._impl, "describe", None)
        return describe() if describe else {"backend": self.backend,
                                            "path": self.path}

    def release(self) -> None:
        self._impl.release()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.release()
