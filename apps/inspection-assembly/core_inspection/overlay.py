"""在帧上画框与判定结果。只有预览路径用到，判定逻辑不依赖它。"""
from __future__ import annotations

import numpy as np

from .rules import Verdict
from .types import Detection

# 6 类固定配色，与 evaluation/smoke_cpu_onnx.py 一致，便于两处截图对照。
COLORS = ((60, 180, 75), (245, 130, 48), (0, 130, 200),
          (240, 50, 230), (255, 225, 25), (70, 240, 240))
OK_COLOR = (80, 200, 80)
NG_COLOR = (60, 60, 220)
MISSING_COLOR = (40, 160, 255)      # 缺件 ROI 用橙色，与 NG 边框区分


def draw(frame_bgr: np.ndarray, detections: list[Detection],
         verdict: Verdict | None = None, header: str = "") -> np.ndarray:
    """返回叠加后的副本。归一化框在这里才乘回像素坐标。"""
    import cv2

    canvas = frame_bgr.copy()
    height, width = canvas.shape[:2]
    for det in detections:
        x1 = int((det.cx - det.w / 2) * width)
        y1 = int((det.cy - det.h / 2) * height)
        x2 = int((det.cx + det.w / 2) * width)
        y2 = int((det.cy + det.h / 2) * height)
        color = COLORS[det.class_id % len(COLORS)]
        cv2.rectangle(canvas, (x1, y1), (x2, y2), color, 2)
        label = f"{det.class_name} {det.score:.2f}"
        (text_w, text_h), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX,
                                              0.5, 1)
        cv2.rectangle(canvas, (x1, max(0, y1 - text_h - 6)),
                      (x1 + text_w + 4, y1), color, -1)
        cv2.putText(canvas, label, (x1 + 2, max(text_h, y1 - 4)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (20, 20, 20), 1,
                    cv2.LINE_AA)
    if verdict is not None:
        # 缺件的期望 ROI 单独画出来：现场看画面就能知道"哪个工位空了"，
        # 不用去翻 MQTT payload。
        for item in verdict.missing:
            x1 = int(item.item.roi[0] * width)
            y1 = int(item.item.roi[1] * height)
            x2 = int(item.item.roi[2] * width)
            y2 = int(item.item.roi[3] * height)
            cv2.rectangle(canvas, (x1, y1), (x2, y2), MISSING_COLOR, 2)
            cv2.putText(canvas, f"MISSING {item.item.name}", (x1 + 2, y2 - 6),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, MISSING_COLOR, 1,
                        cv2.LINE_AA)
        color = NG_COLOR if verdict.is_ng else OK_COLOR
        cv2.rectangle(canvas, (0, 0), (width - 1, height - 1), color, 4)
        banner = f"{verdict.verdict}  {header}".strip()
        cv2.rectangle(canvas, (0, 0), (width, 28), color, -1)
        cv2.putText(canvas, banner, (8, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.6,
                    (255, 255, 255), 2, cv2.LINE_AA)
    elif header:
        cv2.putText(canvas, header, (8, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.6,
                    (255, 255, 255), 2, cv2.LINE_AA)
    return canvas


def encode_jpeg(frame_bgr: np.ndarray, quality: int = 75) -> bytes:
    import cv2

    ok, buffer = cv2.imencode(".jpg", frame_bgr,
                              [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)])
    if not ok:
        raise RuntimeError("JPEG encode failed")
    return buffer.tobytes()
