"""Rockchip NPU 后端。实现 `core_waste.classifier.Classifier` 协议。

调用序列与 `platforms/infer_shard.py::RknnBackend` 逐字一致——那份已经在
reCamera Pro（RV1126B）与 radxa（RK3588）上跑完过整个 val 分片
（`evaluation/runs/2026-09-07-recamera-pro`、`2026-09-06-m1c-rk3588-radxa`），
这里只是把它包成 Classifier 协议，不重新发明一遍 RKNNLite 的生命周期。

**输入是原始 uint8 NHWC RGB**：`.rknn` 转换时把 ImageNet mean/std 折进了模型
（`platforms/rknn/rknn_convert_m1c.py` 的 `mean_values` / `std_values`），所以
设备侧不做任何 float 归一化。喂错了不会报错，只会让精度悄悄掉，所以几何与
量纲都由 `crop_rgb_u8` 一处决定，且 `tests/test_rknn_preprocess_parity.py`
按字节比对它与评测脚本 `platforms/prep_val_bundle.py::preprocess_u8_rgb`
的输出——线上跑的必须是被验收过的那套预处理。

`core_mask` 只在显式给出时才传：RV1126B 是单核 NPU，`init_runtime(core_mask=...)`
在这颗芯片上没有意义；RK3588 是三核，AUTO 只会给一核（见
`project_e1_embedding_int8_lessons`），需要多核时由调用方显式指定。
"""
from __future__ import annotations

import time
from pathlib import Path

import numpy as np

from core_waste.classifier import ClassifierError, sha256_of
from core_waste.taxonomy import MATERIAL_CLASSES

#: 与 backends/onnxruntime/classifier.py 同一条几何：短边 resize 256 + 中心裁剪
RESIZE_SHORT_SIDE = 256


def crop_rgb_u8(image_bgr: np.ndarray,
                input_size: tuple[int, int] = (224, 224),
                resize_short_side: int = RESIZE_SHORT_SIDE) -> np.ndarray:
    """HWC uint8 BGR -> HWC uint8 RGB，尺寸 input_size。不做归一化。

    几何与 `backends.onnxruntime.classifier.preprocess_bgr` 完全一致，
    差别只在末尾不除 255、不减均值——那两步在 `.rknn` 里。
    """
    import cv2

    if image_bgr.ndim != 3 or image_bgr.shape[2] != 3:
        raise ClassifierError(f"expected HWC BGR image, got {image_bgr.shape}")
    height, width = image_bgr.shape[:2]
    scale = resize_short_side / min(height, width)
    resized = cv2.resize(image_bgr,
                         (max(1, round(width * scale)),
                          max(1, round(height * scale))),
                         interpolation=cv2.INTER_LINEAR)
    target_w, target_h = input_size
    rh, rw = resized.shape[:2]
    top = max(0, (rh - target_h) // 2)
    left = max(0, (rw - target_w) // 2)
    cropped = resized[top:top + target_h, left:left + target_w]
    if cropped.shape[:2] != (target_h, target_w):
        cropped = cv2.resize(cropped, (target_w, target_h),
                             interpolation=cv2.INTER_LINEAR)
    return np.ascontiguousarray(cropped[:, :, ::-1], dtype=np.uint8)


class RknnClassifier:
    accelerator = "rknn"

    def __init__(self, model_path: str | Path,
                 classes: tuple[str, ...] = MATERIAL_CLASSES,
                 input_size: tuple[int, int] = (224, 224),
                 backbone: str = "efficientnet_lite0",
                 onnx_sha256: str = "",
                 core_mask: str = "") -> None:
        self.model_path = Path(model_path)
        if not self.model_path.exists():
            raise ClassifierError(f"rknn model not found: {self.model_path}")
        self.classes = tuple(classes)
        self.backbone = backbone
        self.input_size = (int(input_size[0]), int(input_size[1]))
        self.onnx_sha256 = onnx_sha256
        #: `.rknn` 自己的 sha256。与 models/SHA256SUMS 里的行对得上，事件里
        #: 报出来才能证明现场跑的是哪一次转换的产物（onnx_sha256 只说明训练
        #: 产物是哪一个，说明不了量化用的是 calib64 还是 calib256+mmse）。
        self.artifact_sha256 = sha256_of(self.model_path)
        self.last_inference_ms = 0.0

        from rknnlite.api import RKNNLite

        self._rknn = RKNNLite(verbose=False)
        if self._rknn.load_rknn(str(self.model_path)) != 0:
            raise ClassifierError(f"load_rknn failed: {self.model_path}")
        if core_mask:
            mask = getattr(RKNNLite, f"NPU_CORE_{core_mask}", None)
            if mask is None:
                raise ClassifierError(f"unknown core_mask: {core_mask!r}")
            ret = self._rknn.init_runtime(core_mask=mask)
        else:
            ret = self._rknn.init_runtime()
        if ret != 0:
            # /dev/rknpu 是 crw------- root root：非 root 跑到这里就会失败，
            # 而错误码本身不解释这一点，所以在这里说清楚。
            raise ClassifierError(
                f"init_runtime failed (ret={ret}) for {self.model_path}. "
                f"/dev/rknpu is root-only, so the process must run as root.")
        self._closed = False

    def warmup(self, iterations: int = 3) -> None:
        blank = np.zeros((self.input_size[1], self.input_size[0], 3),
                         dtype=np.uint8)
        for _ in range(max(0, iterations)):
            self._rknn.inference(inputs=[blank[None]])

    def predict(self, image: np.ndarray) -> np.ndarray:
        crop = crop_rgb_u8(image, self.input_size)
        start = time.perf_counter()
        outputs = self._rknn.inference(inputs=[crop[None]])
        self.last_inference_ms = (time.perf_counter() - start) * 1000.0
        if not outputs:
            raise ClassifierError("rknn inference returned no output tensor")
        logits = np.asarray(outputs[0], dtype=np.float32).reshape(-1)
        if logits.size != len(self.classes):
            raise ClassifierError(
                f"expected {len(self.classes)} logits, got {logits.size}")
        return logits

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._rknn.release()
        except Exception:  # noqa: BLE001 - 释放失败不影响退出码
            pass
