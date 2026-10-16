"""检测侧的与硬件无关部分：接口协议、YOLOX 解码、NMS、外扩方形裁剪。

和分类那边一样的分工（`core_waste.classifier` 的模块注释）：backend 只做
预处理 + 推理，把**原始输出张量**交出来；解码、NMS、坐标还原、裁剪全在
core 里跑一份。四个平台共用同一份后处理，是「平台后处理漂移」最直接的
对策——HEF / RKNN / TensorRT 各自的算子实现差一点，解码写在各家 SDK 里
就再也对不齐了。

YOLOX 的 ONNX（`--decode_in_inference False` 导出）输出一个
`[1, A, 5 + C]` 张量，A = 8400（640² 下 80²+40²+20²），最后一维是
`[cx, cy, w, h, obj, cls_0..cls_{C-1}]`，其中 cx/cy/w/h 已经是**网格坐标**
——要按 stride 还原：`cx = (cx + grid_x) * stride`。这一步漏掉的话框会全部
挤在左上角，是移植 YOLOX 最常见的坑。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Sequence, runtime_checkable

import numpy as np

#: YOLOX-Tiny / Nano 的三个特征层 stride
YOLOX_STRIDES = (8, 16, 32)


@dataclass(frozen=True)
class Detection:
    """一个检测框。`bbox` 是**原帧像素** xyxy，`score` = obj × cls。"""

    bbox: tuple[float, float, float, float]
    score: float
    class_id: int = 0
    class_name: str = "waste"

    @property
    def area(self) -> float:
        x1, y1, x2, y2 = self.bbox
        return max(0.0, x2 - x1) * max(0.0, y2 - y1)

    def normalized(self, width: int, height: int) -> list[float]:
        x1, y1, x2, y2 = self.bbox
        return [min(max(x1 / width, 0.0), 1.0),
                min(max(y1 / height, 0.0), 1.0),
                min(max(x2 / width, 0.0), 1.0),
                min(max(y2 / height, 0.0), 1.0)]


@runtime_checkable
class Detector(Protocol):
    """检测后端接口。实现只负责 letterbox + 推理，不做解码/NMS。"""

    accelerator: str
    input_size: tuple[int, int]
    onnx_sha256: str
    classes: tuple[str, ...]

    def warmup(self, iterations: int = 3) -> None:
        ...

    def detect(self, image: np.ndarray) -> list[Detection]:
        """输入 HWC uint8 BGR 原帧，返回原帧坐标的框。"""

    def close(self) -> None:
        ...


class DetectorError(RuntimeError):
    """检测后端初始化或推理失败。"""


# ---- letterbox ----------------------------------------------------------
def letterbox_params(src_hw: tuple[int, int],
                     dst_hw: tuple[int, int]) -> tuple[float, int, int]:
    """返回 (ratio, pad_x, pad_y)。等比缩放后贴左上角，右/下补边。

    YOLOX 官方 demo 就是贴左上角（不居中），解码时只要减 pad 再除 ratio。
    这里保留 pad 参数是为了将来换成居中 letterbox 不用改解码。
    """
    ratio = min(dst_hw[0] / src_hw[0], dst_hw[1] / src_hw[1])
    return ratio, 0, 0


def letterbox(image: np.ndarray, dst_hw: tuple[int, int],
              pad_value: int = 114) -> tuple[np.ndarray, float]:
    """HWC uint8 BGR -> (dst_h, dst_w, 3) uint8，返回缩放比。"""
    import cv2

    ratio, _, _ = letterbox_params(image.shape[:2], dst_hw)
    new_w = int(round(image.shape[1] * ratio))
    new_h = int(round(image.shape[0] * ratio))
    canvas = np.full((dst_hw[0], dst_hw[1], 3), pad_value, dtype=np.uint8)
    canvas[:new_h, :new_w] = cv2.resize(image, (new_w, new_h),
                                        interpolation=cv2.INTER_LINEAR)
    return canvas, ratio


# ---- 解码 ---------------------------------------------------------------
def yolox_grids(input_hw: tuple[int, int],
                strides: Sequence[int] = YOLOX_STRIDES):
    """按 stride 生成 (grid[A,2], stride[A,1])，A 是 anchor 总数。"""
    grids, expanded = [], []
    for stride in strides:
        rows, cols = input_hw[0] // stride, input_hw[1] // stride
        yv, xv = np.meshgrid(np.arange(rows), np.arange(cols), indexing="ij")
        grid = np.stack((xv, yv), 2).reshape(-1, 2).astype(np.float32)
        grids.append(grid)
        expanded.append(np.full((grid.shape[0], 1), stride, dtype=np.float32))
    return np.concatenate(grids, 0), np.concatenate(expanded, 0)


def decode_yolox(raw: np.ndarray, input_hw: tuple[int, int], ratio: float,
                 strides: Sequence[int] = YOLOX_STRIDES) -> np.ndarray:
    """`[1, A, 5+C]` 或 `[A, 5+C]` -> `[N, 6]` = xyxy(原帧) + score + class。

    还没做 NMS，也没做置信度阈值——两者都在 `postprocess` 里，方便单独测。
    """
    output = np.asarray(raw, dtype=np.float32)
    if output.ndim == 3:
        if output.shape[0] != 1:
            raise DetectorError(f"batch must be 1, got {output.shape[0]}")
        output = output[0]
    if output.ndim != 2 or output.shape[1] < 6:
        raise DetectorError(f"unexpected detector output shape {raw.shape}")
    grid, stride = yolox_grids(input_hw, strides)
    if grid.shape[0] != output.shape[0]:
        raise DetectorError(
            f"anchor count mismatch: model gives {output.shape[0]}, "
            f"strides {tuple(strides)} at {input_hw} give {grid.shape[0]}")
    cxcy = (output[:, :2] + grid) * stride
    wh = np.exp(output[:, 2:4]) * stride
    scores = output[:, 4:5] * output[:, 5:]
    class_ids = np.argmax(scores, axis=1)
    best = scores[np.arange(scores.shape[0]), class_ids]
    xyxy = np.empty((output.shape[0], 4), dtype=np.float32)
    xyxy[:, 0] = cxcy[:, 0] - wh[:, 0] / 2
    xyxy[:, 1] = cxcy[:, 1] - wh[:, 1] / 2
    xyxy[:, 2] = cxcy[:, 0] + wh[:, 0] / 2
    xyxy[:, 3] = cxcy[:, 1] + wh[:, 1] / 2
    xyxy /= ratio          # letterbox 贴左上角，没有偏移要减
    return np.concatenate(
        [xyxy, best[:, None], class_ids[:, None].astype(np.float32)], axis=1)


# ---- NMS ----------------------------------------------------------------
def nms(boxes: np.ndarray, scores: np.ndarray, iou_threshold: float) -> list[int]:
    """标准 greedy NMS，纯 numpy。返回保留下来的下标，按分数降序。"""
    if len(boxes) == 0:
        return []
    x1, y1, x2, y2 = boxes[:, 0], boxes[:, 1], boxes[:, 2], boxes[:, 3]
    areas = np.maximum(0.0, x2 - x1) * np.maximum(0.0, y2 - y1)
    order = scores.argsort()[::-1]
    keep = []
    while order.size > 0:
        i = int(order[0])
        keep.append(i)
        if order.size == 1:
            break
        rest = order[1:]
        xx1 = np.maximum(x1[i], x1[rest])
        yy1 = np.maximum(y1[i], y1[rest])
        xx2 = np.minimum(x2[i], x2[rest])
        yy2 = np.minimum(y2[i], y2[rest])
        inter = np.maximum(0.0, xx2 - xx1) * np.maximum(0.0, yy2 - yy1)
        iou = inter / np.maximum(areas[i] + areas[rest] - inter, 1e-9)
        order = rest[iou <= iou_threshold]
    return keep


def postprocess(decoded: np.ndarray, frame_hw: tuple[int, int],
                classes: Sequence[str] = ("waste",),
                score_threshold: float = 0.25,
                iou_threshold: float = 0.45,
                max_items: int = 16,
                min_box_px: int = 12) -> list[Detection]:
    """阈值 + 逐类 NMS + 裁到画面内 + 限件数。返回按分数降序的 Detection。"""
    if decoded.size == 0:
        return []
    height, width = frame_hw
    kept = decoded[decoded[:, 4] >= score_threshold]
    if kept.size == 0:
        return []
    results: list[Detection] = []
    for class_id in np.unique(kept[:, 5]).astype(int):
        subset = kept[kept[:, 5].astype(int) == class_id]
        for index in nms(subset[:, :4], subset[:, 4], iou_threshold):
            x1, y1, x2, y2 = subset[index, :4]
            x1 = float(np.clip(x1, 0, width - 1))
            y1 = float(np.clip(y1, 0, height - 1))
            x2 = float(np.clip(x2, x1 + 1, width))
            y2 = float(np.clip(y2, y1 + 1, height))
            if (x2 - x1) < min_box_px or (y2 - y1) < min_box_px:
                continue
            name = (classes[class_id] if class_id < len(classes)
                    else str(class_id))
            results.append(Detection((x1, y1, x2, y2),
                                     float(subset[index, 4]),
                                     int(class_id), name))
    results.sort(key=lambda d: -d.score)
    return results[:max_items]


# ---- 外扩方形裁剪 -------------------------------------------------------
def square_crop(image: np.ndarray, bbox: Sequence[float],
                expand: float = 0.15) -> np.ndarray:
    """按框中心取正方形并外扩，越界处补边缘像素（不是黑边）。

    为什么是**方形**：分类器的预处理是「短边缩到 256 → 中心裁 224」
    （`backends/onnxruntime/classifier.py`）。喂一个瘦长的框进去，中心裁会
    把物体两头切掉，分类分数掉得比背景多一点还厉害。先补成方形，缩放就是
    整体等比，中心裁什么都不切。

    为什么**补边缘像素而不是补黑**：黑边在归一化之后是一片强负值，
    EfficientNet 的第一层会把它当成边缘特征，实测比复制边缘更容易改判。
    """
    import cv2

    height, width = image.shape[:2]
    x1, y1, x2, y2 = (float(v) for v in bbox)
    cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
    side = max(x2 - x1, y2 - y1) * (1.0 + 2 * expand)
    side = max(side, 8.0)
    left = int(round(cx - side / 2))
    top = int(round(cy - side / 2))
    size = int(round(side))

    pad_l = max(0, -left)
    pad_t = max(0, -top)
    pad_r = max(0, left + size - width)
    pad_b = max(0, top + size - height)
    x0, y0 = max(0, left), max(0, top)
    patch = image[y0:min(height, top + size), x0:min(width, left + size)]
    if patch.size == 0:
        raise ValueError(f"empty crop for bbox {tuple(bbox)}")
    if pad_l or pad_t or pad_r or pad_b:
        patch = cv2.copyMakeBorder(patch, pad_t, pad_b, pad_l, pad_r,
                                   cv2.BORDER_REPLICATE)
    return np.ascontiguousarray(patch)


def iou(a: Sequence[float], b: Sequence[float]) -> float:
    ax1, ay1, ax2, ay2 = (float(v) for v in a)
    bx1, by1, bx2, by2 = (float(v) for v in b)
    ix = max(0.0, min(ax2, bx2) - max(ax1, bx1))
    iy = max(0.0, min(ay2, by2) - max(ay1, by1))
    inter = ix * iy
    union = ((ax2 - ax1) * (ay2 - ay1) + (bx2 - bx1) * (by2 - by1) - inter)
    return inter / union if union > 1e-9 else 0.0
