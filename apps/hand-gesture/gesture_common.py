"""MediaPipe 手势识别四级管线的纯 numpy/cv2 实现（reCamera Pro 设备可运行）。

严格对齐三方参考：
  - RobotXTeam/sscma-example-sg200x 的 C++ 移植（hand_detector.cc /
    hand_landmarker.cc / gesture_recognizer.cc，逐行对齐 MediaPipe 计算器）
  - MediaPipe 官方 tflite 图内语义（embedder 的归一化在图内完成，
    原点 = 掌根 6 点 [0,1,5,9,13,17] 均值）
  - 本模块的 embedder/classifier 为 tflite 权重的 numpy 精确复刻
    （kit 托管推理只支持单输入 uint8 图像模型，向量模型在 CPU 跑反而更快）。

四级：
  1. palm detect   —— SSD 2016 anchors, 192x192, sigmoid score + NMS
  2. landmark      —— 旋转 ROI warp 224x224, 21 关键点投影回原图
  3. embedder      —— (63,) 归一化关键点 -> (128,) 嵌入（numpy）
  4. classifier    —— (128,) -> (8,) 手势概率（numpy）
"""
from __future__ import annotations

import math

import cv2
import numpy as np

GESTURES = ["None", "Closed_Fist", "Open_Palm", "Pointing_Up",
            "Thumb_Down", "Thumb_Up", "Victory", "ILoveYou"]

# 21 关键点连线（仅供可视化/前端参考）
HAND_CONNECTIONS = [
    (0, 1), (1, 2), (2, 3), (3, 4),
    (0, 5), (5, 6), (6, 7), (7, 8),
    (5, 9), (9, 10), (10, 11), (11, 12),
    (9, 13), (13, 14), (14, 15), (15, 16),
    (13, 17), (17, 18), (18, 19), (19, 20),
    (0, 17),
]

PALM_BASE_IDX = [0, 1, 5, 9, 13, 17]   # embedder 图内 GATHER 的 6 个掌根点
DET_SIZE = 192                          # palm 检测输入边长
LM_SIZE = 224                           # landmark 输入边长


# ---------------------------------------------------------------- 工具
def normalize_radians(a: float) -> float:
    """归一化到 (-pi, pi]（RectTransformationCalculator::NormalizeRadians）。"""
    return a - 2.0 * math.pi * math.floor((a + math.pi) / (2.0 * math.pi))


def letterbox_rgb(rgb: np.ndarray, size: int):
    """等比缩放居中黑边（MediaPipe BORDER_ZERO 约定）。

    Returns (letterboxed_uint8, scale, pad_left, pad_top)。
    """
    h, w = rgb.shape[:2]
    s = size / max(h, w)
    nh, nw = int(round(h * s)), int(round(w * s))
    resized = cv2.resize(rgb, (nw, nh), interpolation=cv2.INTER_LINEAR)
    out = np.zeros((size, size, 3), np.uint8)
    pl, pt = (size - nw) // 2, (size - nh) // 2
    out[pt:pt + nh, pl:pl + nw] = resized
    return out, s, pl, pt


# ---------------------------------------------------------------- 1. palm SSD
def _calc_scale(min_s: float, max_s: float, i: int, n: int) -> float:
    if n == 1:
        return (min_s + max_s) * 0.5
    return min_s + (max_s - min_s) * i / (n - 1)


def generate_anchors() -> np.ndarray:
    """MediaPipe SsdAnchorsCalculator：2016 个锚点中心（归一化坐标）。

    strides=[8,16,16,16]，相同 stride 的层合并为一组；fixed_anchor_size=True
    所以只需中心点。顺序必须与模型输出 2016 行一致。
    """
    strides = [8, 16, 16, 16]
    num_strides = 4
    anchors = []
    layer = 0
    while layer < num_strides:
        last = layer
        while last < num_strides and strides[last] == strides[layer]:
            last += 1
        pairs = []
        for sub in range(layer, last):
            scale = _calc_scale(0.1484375, 0.75, sub, num_strides)
            pairs.append(scale)          # aspect_ratio=1.0
            nxt = 1.0 if sub == num_strides - 1 else _calc_scale(
                0.1484375, 0.75, sub + 1, num_strides)
            pairs.append(math.sqrt(scale * nxt))   # interpolated, ar=1.0
        stride = strides[layer]
        fm = int(math.ceil(DET_SIZE / stride))
        for y in range(fm):
            for x in range(fm):
                cx = (x + 0.5) / fm
                cy = (y + 0.5) / fm
                for _ in pairs:
                    anchors.append((cx, cy))
        layer = last
    anchors = np.asarray(anchors, np.float32)
    assert anchors.shape == (2016, 2), anchors.shape
    return anchors


def nms(xyxy: np.ndarray, scores: np.ndarray, iou_thres: float) -> list:
    """单类别 NMS，返回保留的下标（score 降序）。"""
    order = np.argsort(scores)[::-1]
    areas = np.maximum(0, xyxy[:, 2] - xyxy[:, 0]) * \
        np.maximum(0, xyxy[:, 3] - xyxy[:, 1])
    keep, suppressed = [], np.zeros(len(xyxy), bool)
    for i in range(len(order)):
        idx = order[i]
        if suppressed[idx]:
            continue
        keep.append(int(idx))
        rest = order[i + 1:]
        rest = rest[~suppressed[rest]]
        if rest.size == 0:
            continue
        xx1 = np.maximum(xyxy[idx, 0], xyxy[rest, 0])
        yy1 = np.maximum(xyxy[idx, 1], xyxy[rest, 1])
        xx2 = np.minimum(xyxy[idx, 2], xyxy[rest, 2])
        yy2 = np.minimum(xyxy[idx, 3], xyxy[rest, 3])
        inter = np.maximum(0, xx2 - xx1) * np.maximum(0, yy2 - yy1)
        ovr = inter / (areas[idx] + areas[rest] - inter + 1e-9)
        suppressed[rest[ovr > iou_thres]] = True
    return keep


class PalmDetector:
    """hand_detector_fp16.rknn 的后处理（192 letterbox + 2016 锚点解码）。"""

    def __init__(self, conf_thres: float = 0.5, nms_thres: float = 0.3,
                 max_hands: int = 2):
        self.anchors = generate_anchors()
        self.conf = conf_thres
        self.nms = nms_thres
        self.max_hands = max_hands

    def postprocess(self, outputs, frame_w: int, frame_h: int,
                    scale: float, pad_l: int, pad_t: int):
        """outputs: rknn 原始输出列表。返回归一化坐标手掌列表。

        每项: {"bbox": [x1,y1,x2,y2] 归一化, "kpts": (7,2) 归一化, "score": f}
        """
        outs = [np.asarray(o, np.float32) for o in outputs]
        boxes = next(o for o in outs if o.size == 2016 * 18).reshape(-1, 18)
        scores = next(o for o in outs if o.size == 2016).reshape(-1)

        scores = 1.0 / (1.0 + np.exp(-np.clip(scores, -100, 100)))
        sel = np.nonzero(scores >= self.conf)[0]
        if sel.size == 0:
            return []

        b = boxes[sel]
        ax = self.anchors[sel, 0]
        ay = self.anchors[sel, 1]
        cx = b[:, 0] / DET_SIZE + ax
        cy = b[:, 1] / DET_SIZE + ay
        w = b[:, 2] / DET_SIZE
        h = b[:, 3] / DET_SIZE
        # 18 = cx,cy,w,h + 7 个掌点 (x,y) 交错 -> (N,7,2)
        kpts = np.stack([b[:, 4::2] / DET_SIZE + ax[:, None],
                         b[:, 5::2] / DET_SIZE + ay[:, None]], axis=-1)

        xyxy = np.stack([cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2], -1)
        keep = nms(xyxy, scores[sel], self.nms)

        # letterbox 逆变换：letterbox 坐标系 -> 原图归一化坐标
        palms = []
        for li in keep[:self.max_hands]:
            bx = xyxy[li].copy()
            kp = kpts[li].copy()
            # letterbox 内归一化 -> 原图归一化
            lb_w = frame_w * scale / DET_SIZE   # letterbox 内内容区占比
            lb_h = frame_h * scale / DET_SIZE
            off_x = pad_l / DET_SIZE
            off_y = pad_t / DET_SIZE
            bx[[0, 2]] = (bx[[0, 2]] - off_x) / lb_w
            bx[[1, 3]] = (bx[[1, 3]] - off_y) / lb_h
            kp[..., 0] = (kp[..., 0] - off_x) / lb_w
            kp[..., 1] = (kp[..., 1] - off_y) / lb_h
            palms.append({"bbox": bx, "kpts": kp,
                          "score": float(scores[sel][li])})
        return palms


# ---------------------------------------------------------------- 2. landmark
def compute_rotation(palm_kpts: np.ndarray, img_w: int, img_h: int) -> float:
    """腕(0)->中指根(2) 方向到 90° 的旋转角（图像 Y 轴向下，dy 取负）。"""
    x0, y0 = palm_kpts[0, 0] * img_w, palm_kpts[0, 1] * img_h
    x1, y1 = palm_kpts[2, 0] * img_w, palm_kpts[2, 1] * img_h
    return normalize_radians(math.pi / 2 - math.atan2(-(y1 - y0), x1 - x0))


def transform_rect(bbox_norm: np.ndarray, rot: float, img_w: int, img_h: int,
                   shift_y: float = -0.55, scale_factor: float = 2.5):
    """palm bbox -> 旋转 ROI（归一化）。

    ``shift_y`` / ``scale_factor`` 对齐 MediaPipe 的 rect 变换；默认值在
    29 个设备/官方样本上按 landmark 误差和手势标签一致性选择，且可由
    App 的 live 配置覆盖。"""
    x1, y1, x2, y2 = [float(v) for v in bbox_norm]
    width, height = x2 - x1, y2 - y1
    xc, yc = (x1 + x2) / 2, (y1 + y2) / 2
    W, H = float(img_w), float(img_h)

    cos_r, sin_r = math.cos(rot), math.sin(rot)
    SHIFT_X, SHIFT_Y = 0.0, float(shift_y)
    x_shift = (W * width * SHIFT_X * cos_r - H * height * SHIFT_Y * sin_r) / W
    y_shift = (W * width * SHIFT_X * sin_r + H * height * SHIFT_Y * cos_r) / H
    xc += x_shift
    yc += y_shift

    long_side = max(width * W, height * H)
    width, height = long_side / W, long_side / H
    return xc, yc, width * float(scale_factor), height * float(scale_factor), rot


def _rotated_rect_points(cx: float, cy: float, w: float, h: float,
                         angle_deg: float) -> np.ndarray:
    """手算 cv2.RotatedRect(...).points() —— 设备端 slim cv2 没有 RotatedRect 绑定。

    与 OpenCV 源码逐式一致（点序 bl, tl, tr, br），主机侧已与
    cv2.RotatedRect.points() 对比验证（200 组随机参数，差异 <2e-4，
    为 cv2 自身 fp32 舍入量级）。
    """
    a = math.sin(math.radians(angle_deg)) * 0.5
    b = math.cos(math.radians(angle_deg)) * 0.5
    p0 = (cx - a * h - b * w, cy + b * h - a * w)
    p1 = (cx + a * h - b * w, cy - b * h - a * w)
    p2 = (2 * cx - p0[0], 2 * cy - p0[1])
    p3 = (2 * cx - p1[0], 2 * cy - p1[1])
    return np.float32([p0, p1, p2, p3])


def warp_rotated_roi(rgb: np.ndarray, rect, size: int = LM_SIZE) -> np.ndarray:
    """按旋转矩形做透视裁剪（bl,tl,tr,br 约定，与 MediaPipe/C++ 参考一致）。"""
    xc, yc, w, h, rot = rect
    H, W = rgb.shape[:2]
    src = _rotated_rect_points(xc * W, yc * H, w * W, h * H,
                               math.degrees(rot))
    dst = np.float32([[0, size], [0, 0], [size, 0], [size, size]])
    M = cv2.getPerspectiveTransform(src, dst)
    return cv2.warpPerspective(rgb, M, (size, size),
                               flags=cv2.INTER_LINEAR,
                               borderMode=cv2.BORDER_CONSTANT,
                               borderValue=(0, 0, 0))


def landmark_postprocess(outputs, rect, img_w: int, img_h: int):
    """landmarker 4 输出 -> 原图归一化 21 关键点 + handedness + presence。"""
    outs = [np.asarray(o, np.float32).reshape(-1) for o in outputs]
    lm = next(o for o in outs if o.size == 63)
    world = next(o for o in outs if o.size == 63 and o is not lm)
    scalars = [o for o in outs if o.size == 1]
    hand_flag = float(scalars[0][0])
    handed_raw = float(scalars[1][0])

    xc, yc, w, h, rot = rect
    cos_r, sin_r = math.cos(rot), math.sin(rot)
    pts = np.zeros((21, 3), np.float32)
    for i in range(21):
        lx = lm[i * 3 + 0] / LM_SIZE
        ly = lm[i * 3 + 1] / LM_SIZE
        lz = lm[i * 3 + 2] / LM_SIZE / 0.4
        xl, yl = lx - 0.5, ly - 0.5
        pts[i, 0] = (cos_r * xl - sin_r * yl) * w + xc
        pts[i, 1] = (sin_r * xl + cos_r * yl) * h + yc
        pts[i, 2] = lz * w
    handedness = 1.0 / (1.0 + math.exp(-handed_raw)) if handed_raw > -100 else 0.0
    return pts, world.reshape(21, 3), handedness, hand_flag


# ---------------------------------------------------------------- 3+4. 头部（numpy 复刻）
class GestureHeads:
    """gesture_embedder + canned_gesture_classifier 的 numpy 精确复刻。

    输入为投影回原图的归一化 21 关键点；图内自带归一化
    （掌根 6 点均值原点 + 长边缩放 + 1e-5），与 tflite 逐 op 对齐。
    """

    def __init__(self, npz_path: str):
        z = np.load(npz_path)
        self.w = {k: z[k].astype(np.float32) for k in z.files}

    def _fc(self, x, w_name):
        W = self.w[f"emb::{w_name}/MatMul"]
        b = self.w[f"emb::{w_name}/BiasAdd/ReadVariableOp/resource"]
        return x @ W.T + b

    def embed(self, landmarks: np.ndarray) -> np.ndarray:
        """landmarks: (21,3) 归一化图像坐标 -> (128,) 嵌入。"""
        hand = landmarks.reshape(1, 21, 3).astype(np.float32)
        origin = hand[:, PALM_BASE_IDX, :].mean(axis=1, keepdims=True)
        c = hand - origin                                  # (1,21,3)
        rx = c[..., 0].max(axis=1) - c[..., 0].min(axis=1)
        ry = c[..., 1].max(axis=1) - c[..., 1].min(axis=1)
        scale = np.maximum(rx, ry).reshape(1, 1, 1) + 1e-5
        x = (c / scale).reshape(1, 63)

        # 双干：dense_12 线性 + bn10-folded relu，汇成残差主干
        a = self._fc(x, "dense_12")
        b = np.maximum(x @ self.w["emb::batch_normalization_10/batchnorm/mul_1"].T
                       + self.w["emb::re_lu_10/Relu;batch_normalization_10/batchnorm/add_1"], 0)
        r = a + self._fc(b, "dense_13")
        for i in range(11, 16):                            # 5 个残差块
            h = np.maximum(r * self.w[f"emb::batch_normalization_{i}/batchnorm/mul"]
                           + self.w[f"emb::batch_normalization_{i}/batchnorm/sub"], 0)
            r = r + self._fc(h, f"dense_{i + 3}")
        h = np.maximum(r * self.w["emb::batch_normalization_16/batchnorm/mul"]
                       + self.w["emb::batch_normalization_16/batchnorm/sub"], 0)
        out = h @ self.w["emb::batch_normalization_17/batchnorm/mul_1"].T \
            + self.w["emb::re_lu_17/Relu;batch_normalization_17/batchnorm/add_1"]
        return np.maximum(out, 0).reshape(-1)

    def normalize_landmark_aspect_ratio(self, landmarks: np.ndarray,
                                        image_width: int,
                                        image_height: int) -> np.ndarray:
        """MediaPipe LandmarksToMatrixCalculator 的宽高比修正。

        landmarks 是原图 normalized 坐标；x/y 先按 W/max(W,H)、H/max(W,H)
        缩放到同一物理尺度，再交给 embedder 图内的 palm-base/object
        normalization。宽画幅不做这一步会把手的几何比例横向拉伸。
        """
        a = np.asarray(landmarks, np.float32).reshape(21, 3).copy()
        max_dim = float(max(image_width, image_height))
        a[:, 0] = (a[:, 0] - 0.5) * (float(image_width) / max_dim) + 0.5
        a[:, 1] = (a[:, 1] - 0.5) * (float(image_height) / max_dim) + 0.5
        return a

    def classify_landmarks(self, landmarks: np.ndarray,
                           image_width: int, image_height: int):
        """原图 normalized 21 点 -> 手势；包含官方宽高比前处理。"""
        corrected = self.normalize_landmark_aspect_ratio(
            landmarks, image_width, image_height)
        return self.classify(self.embed(corrected))

    def classify(self, embedding: np.ndarray):
        """(128,) -> (label, confidence, probs[8])。"""
        h = np.maximum(embedding * self.w["cls::batch_normalization_1/batchnorm/mul"]
                       + self.w["cls::batch_normalization_1/batchnorm/sub"], 0)
        logits = h @ self.w["cls::new_classifier/MatMul"].T \
            + self.w["cls::new_classifier/BiasAdd/ReadVariableOp/resource"]
        logits = logits - logits.max()
        e = np.exp(logits)
        probs = e / e.sum()
        idx = int(np.argmax(probs))
        return GESTURES[idx], float(probs[idx]), probs
