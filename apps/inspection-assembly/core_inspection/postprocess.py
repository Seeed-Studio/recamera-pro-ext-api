"""YOLOX 无锚点解码 + NMS。纯 numpy，四个平台共用，避免各平台后处理漂移。

YOLOX ONNX（`--decode_in_inference False` 导出）输出形状 (1, N, 5 + num_classes)，
最后一维为 [cx, cy, w, h, obj, cls0..clsK]，cx/cy/w/h 处于「网格单位」，需要
乘以各自 stride 才回到输入图像像素坐标。
"""
from __future__ import annotations

import numpy as np

DEFAULT_STRIDES = (8, 16, 32)


def make_grid(input_h: int, input_w: int,
              strides: tuple[int, ...] = DEFAULT_STRIDES
              ) -> tuple[np.ndarray, np.ndarray]:
    """返回 (grids[N,2], expanded_strides[N,1])，顺序与 YOLOX head 拼接顺序一致。"""
    grids = []
    expanded = []
    for stride in strides:
        hsize, wsize = input_h // stride, input_w // stride
        yv, xv = np.meshgrid(np.arange(hsize), np.arange(wsize), indexing="ij")
        grid = np.stack((xv, yv), axis=2).reshape(-1, 2)
        grids.append(grid)
        expanded.append(np.full((grid.shape[0], 1), stride, dtype=np.float32))
    return (np.concatenate(grids, axis=0).astype(np.float32),
            np.concatenate(expanded, axis=0))


def decode_outputs(raw: np.ndarray, input_h: int, input_w: int,
                   strides: tuple[int, ...] = DEFAULT_STRIDES) -> np.ndarray:
    """把 (N, 5+C) 的原始输出解码成输入图像像素坐标下的 (N, 5+C)。"""
    outputs = np.asarray(raw, dtype=np.float32)
    if outputs.ndim == 3:
        if outputs.shape[0] != 1:
            raise ValueError(f"expected batch-1 output, got shape {outputs.shape}")
        outputs = outputs[0]
    if outputs.ndim != 2 or outputs.shape[1] < 6:
        raise ValueError(f"unexpected YOLOX output shape {outputs.shape}")
    grids, expanded_strides = make_grid(input_h, input_w, strides)
    if grids.shape[0] != outputs.shape[0]:
        raise ValueError(
            f"grid size {grids.shape[0]} does not match output rows "
            f"{outputs.shape[0]}; check input_size/strides")
    decoded = outputs.copy()
    decoded[:, 0:2] = (outputs[:, 0:2] + grids) * expanded_strides
    decoded[:, 2:4] = np.exp(outputs[:, 2:4]) * expanded_strides
    return decoded


def xywh_to_xyxy(boxes: np.ndarray) -> np.ndarray:
    """中心点宽高 -> 左上右下。"""
    boxes = np.asarray(boxes, dtype=np.float32)
    out = np.empty_like(boxes)
    out[:, 0] = boxes[:, 0] - boxes[:, 2] / 2.0
    out[:, 1] = boxes[:, 1] - boxes[:, 3] / 2.0
    out[:, 2] = boxes[:, 0] + boxes[:, 2] / 2.0
    out[:, 3] = boxes[:, 1] + boxes[:, 3] / 2.0
    return out


def nms(boxes_xyxy: np.ndarray, scores: np.ndarray, iou_threshold: float) -> list[int]:
    """单类别贪心 NMS，返回保留下标（按 score 降序）。"""
    boxes_xyxy = np.asarray(boxes_xyxy, dtype=np.float32)
    scores = np.asarray(scores, dtype=np.float32)
    if boxes_xyxy.size == 0:
        return []
    x1, y1, x2, y2 = boxes_xyxy.T
    areas = np.maximum(0.0, x2 - x1) * np.maximum(0.0, y2 - y1)
    order = scores.argsort()[::-1]
    keep: list[int] = []
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
        union = areas[i] + areas[rest] - inter
        iou = np.where(union > 0, inter / np.maximum(union, 1e-12), 0.0)
        order = rest[iou <= iou_threshold]
    return keep


def multiclass_nms(boxes_xyxy: np.ndarray, scores: np.ndarray,
                   iou_threshold: float, score_threshold: float
                   ) -> list[tuple[int, float, np.ndarray]]:
    """逐类别 NMS。scores 形状 (N, C)。返回 [(class_id, score, xyxy)] 按分数降序。"""
    boxes_xyxy = np.asarray(boxes_xyxy, dtype=np.float32)
    scores = np.asarray(scores, dtype=np.float32)
    results: list[tuple[int, float, np.ndarray]] = []
    for class_id in range(scores.shape[1]):
        class_scores = scores[:, class_id]
        mask = class_scores > score_threshold
        if not mask.any():
            continue
        cls_boxes = boxes_xyxy[mask]
        cls_scores = class_scores[mask]
        for index in nms(cls_boxes, cls_scores, iou_threshold):
            results.append((class_id, float(cls_scores[index]), cls_boxes[index]))
    results.sort(key=lambda item: item[1], reverse=True)
    return results


def postprocess(raw: np.ndarray, input_size: tuple[int, int], ratio: float,
                classes: tuple[str, ...], score_threshold: float = 0.25,
                nms_threshold: float = 0.45, orig_size: tuple[int, int] | None = None,
                strides: tuple[int, ...] = DEFAULT_STRIDES) -> list:
    """完整后处理：解码 -> obj*cls 打分 -> 逐类 NMS -> 归一化 Detection 列表。

    `ratio` 是 letterbox 缩放比（原图 -> 输入图）。`orig_size` 为 (width, height)，
    缺省时按 input_size / ratio 推算。
    """
    from .types import Detection  # 局部导入避免循环依赖

    input_w, input_h = input_size
    decoded = decode_outputs(raw, input_h, input_w, strides)
    boxes = xywh_to_xyxy(decoded[:, 0:4])
    scores = decoded[:, 4:5] * decoded[:, 5:]
    if scores.shape[1] != len(classes):
        raise ValueError(
            f"model has {scores.shape[1]} classes but class table has {len(classes)}")

    if orig_size is None:
        orig_w, orig_h = input_w / ratio, input_h / ratio
    else:
        orig_w, orig_h = orig_size
    boxes = boxes / ratio

    detections = []
    for class_id, score, xyxy in multiclass_nms(boxes, scores, nms_threshold,
                                                score_threshold):
        x1 = min(max(float(xyxy[0]), 0.0), orig_w)
        y1 = min(max(float(xyxy[1]), 0.0), orig_h)
        x2 = min(max(float(xyxy[2]), 0.0), orig_w)
        y2 = min(max(float(xyxy[3]), 0.0), orig_h)
        if x2 <= x1 or y2 <= y1:
            continue
        detections.append(Detection(
            class_id=class_id,
            class_name=classes[class_id],
            score=score,
            cx=((x1 + x2) / 2.0) / orig_w,
            cy=((y1 + y2) / 2.0) / orig_h,
            w=(x2 - x1) / orig_w,
            h=(y2 - y1) / orig_h,
        ))
    return detections


def letterbox(image, input_size: tuple[int, int], pad_value: int = 114):
    """等比缩放 + 右下补边，返回 (padded_hwc_uint8, ratio)。需要 cv2。"""
    import cv2

    input_w, input_h = input_size
    height, width = image.shape[:2]
    ratio = min(input_w / width, input_h / height)
    new_w, new_h = int(round(width * ratio)), int(round(height * ratio))
    resized = cv2.resize(image, (new_w, new_h), interpolation=cv2.INTER_LINEAR)
    padded = np.full((input_h, input_w, image.shape[2]), pad_value, dtype=np.uint8)
    padded[:new_h, :new_w] = resized
    return padded, ratio
