"""reCamera Pro（Rockchip RV1126B NPU）后端。实现 `core_inspection.detector.Detector` 协议。

`.rknn` 在 x86 主机用 rknn-toolkit2 2.3.2 转（`platforms/rknn/rknn_convert_yolox.py`），
设备侧只装 `rknn_toolkit_lite2` 的 `RKNNLite` 运行时，与 `/oem/usr/lib/librknnrt.so`
同版本。

预处理与 CPU / TensorRT / Hailo 共用 `core_inspection.postprocess.letterbox`：
YOLOX 导出无 mean/std 归一化，转换时 `rknn.config` 用 mean 0 / std 1，所以这里
直接把 letterbox 之后的 HWC uint8 传给 NPU（`pass_through=False` 走 toolkit 的
恒等归一化）。后处理同样共用 `postprocess`，四个平台不各写一份解码。
"""
from __future__ import annotations

import time
from pathlib import Path

import numpy as np

from core_inspection.classes import DEEPPCB6_CLASSES
from core_inspection.detector import DetectorError
from core_inspection.hashing import sha256_of
from core_inspection.postprocess import DEFAULT_STRIDES, letterbox, postprocess
from core_inspection.types import Detection


class RknnDetector:
    accelerator = "rknn"

    def __init__(self, model_path: str | Path,
                 classes: tuple[str, ...] = DEEPPCB6_CLASSES,
                 input_size: tuple[int, int] = (640, 640),
                 score_threshold: float = 0.25,
                 nms_threshold: float = 0.45,
                 onnx_sha256: str = "",
                 core_mask: int | None = None,
                 strides: tuple[int, ...] = DEFAULT_STRIDES) -> None:
        from rknnlite.api import RKNNLite

        self.model_path = Path(model_path)
        if not self.model_path.exists():
            raise DetectorError(f"model not found: {self.model_path}")
        self.classes = tuple(classes)
        self.input_size = (int(input_size[0]), int(input_size[1]))
        self.score_threshold = float(score_threshold)
        self.nms_threshold = float(nms_threshold)
        self.strides = tuple(strides)
        self.artifact_sha256 = sha256_of(self.model_path)
        self.onnx_sha256 = onnx_sha256
        self.last_inference_ms = 0.0
        self._closed = False

        self._rknn = RKNNLite()
        ret = self._rknn.load_rknn(str(self.model_path))
        if ret != 0:
            raise DetectorError(f"load_rknn failed: {ret}")
        # RV1126B 只有单核 NPU，不传 core_mask（RKNNLite 默认 AUTO）。
        ret = (self._rknn.init_runtime(core_mask=core_mask)
               if core_mask is not None else self._rknn.init_runtime())
        if ret != 0:
            raise DetectorError(f"init_runtime failed: {ret}")

    def warmup(self, iterations: int = 3) -> None:
        blank = np.zeros((self.input_size[1], self.input_size[0], 3), dtype=np.uint8)
        for _ in range(iterations):
            self.detect(blank)

    def preprocess(self, frame_bgr: np.ndarray) -> tuple[np.ndarray, float]:
        padded, ratio = letterbox(frame_bgr, self.input_size)
        # RKNNLite 的 set_inputs 要求 4 维 NHWC，补上 batch 维。
        return np.ascontiguousarray(padded[None]), ratio

    def detect(self, frame_bgr: np.ndarray) -> list[Detection]:
        if self._closed:
            raise DetectorError("detector already closed")
        height, width = frame_bgr.shape[:2]
        tensor, ratio = self.preprocess(frame_bgr)
        start = time.perf_counter()
        outputs = self._rknn.inference(inputs=[tensor], data_format=["nhwc"])
        self.last_inference_ms = (time.perf_counter() - start) * 1000.0
        if not outputs:
            raise DetectorError("rknn inference returned no outputs")
        raw = np.asarray(outputs[0], dtype=np.float32)
        return postprocess(raw, self.input_size, ratio, self.classes,
                           self.score_threshold, self.nms_threshold,
                           orig_size=(width, height), strides=self.strides)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._rknn.release()
        finally:
            self._rknn = None
