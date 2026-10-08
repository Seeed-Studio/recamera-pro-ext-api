"""RKNN Lite person detector (YOLOv8n head, COCO class 0).

The model was converted with ``mean_values=0`` / ``std_values=255``, so the NPU
does the normalization and this module hands it raw uint8 NHWC RGB. That is not
a micro-optimization: converting a 640x640x3 frame to float32 on the CPU costs
more than the inference call itself on this board.

Two head layouts are decoded, because two ONNX exports are in use.

``ultralytics`` -- one ``(1, 84, 8400)`` output with DFL already applied inside
the graph. :func:`decode_person_head` handles it, and it is deliberately the
same NumPy postprocess the generic platform uses: one decoder for both
platforms means a coordinate bug cannot hide on one of them.

``zoo`` -- ``airockchip/rknn_model_zoo``'s export. Nine outputs, three per
stride: a 64-channel box distribution, an 80-channel sigmoid class map, and a
1-channel class-score sum. DFL and the final concat were cut out of the graph
because RKNPU2 has no int8 kernel for the transposed softmax;
:func:`decode_zoo_head` does that arithmetic here. The cost is bounded by
filtering on the person channel *first* and running DFL only on the surviving
anchors -- typically a handful out of 8400 -- so this decode is cheaper than
the ultralytics one despite doing more.

Which decoder runs is decided by the shape of what the runtime hands back, not
by configuration. A model and a decoder that disagree produce plausible-looking
wrong boxes rather than an exception, so it must not be possible to pair them
by editing a config file.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass

import numpy as np

from .letterbox import LetterboxTransform, xyxy_to_frame_norm

PERSON_CLASS_ID = 0
PERSON_CLASS_NAME = "person"


@dataclass
class Detection:
    """One post-NMS detection. ``box`` is xyxy in frame_norm units."""

    box: list[float]
    score: float
    label: str = PERSON_CLASS_NAME


def nms(boxes: np.ndarray, scores: np.ndarray, iou_threshold: float) -> list[int]:
    """Greedy NMS on xyxy boxes. Returns kept indices, highest score first."""
    if len(boxes) == 0:
        return []
    x1, y1, x2, y2 = boxes[:, 0], boxes[:, 1], boxes[:, 2], boxes[:, 3]
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
        iou = inter / np.maximum(areas[i] + areas[rest] - inter, 1e-9)
        order = rest[iou <= iou_threshold]
    return keep


def decode_person_head(
    raw: np.ndarray,
    tf: LetterboxTransform,
    conf_threshold: float,
    iou_threshold: float,
) -> list[Detection]:
    """Decode a (1, 4+nc, N) or (1, N, 4+nc) YOLOv8 head into frame_norm boxes."""
    pred = np.squeeze(raw, axis=0) if raw.ndim == 3 else raw
    if pred.shape[0] < pred.shape[1]:
        pred = pred.T  # (channels, anchors) -> (anchors, channels)
    if pred.shape[1] < 5:
        return []

    scores = pred[:, 4 + PERSON_CLASS_ID]
    mask = scores >= conf_threshold
    if not np.any(mask):
        return []
    kept, kept_scores = pred[mask, :4], scores[mask]
    if kept_scores.size > MAX_PRE_NMS:
        top = np.argpartition(kept_scores, -MAX_PRE_NMS)[-MAX_PRE_NMS:]
        kept, kept_scores = kept[top], kept_scores[top]

    cx, cy, w, h = kept[:, 0], kept[:, 1], kept[:, 2], kept[:, 3]
    boxes = np.stack([cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2], axis=1)

    detections: list[Detection] = []
    for i in nms(boxes, kept_scores, iou_threshold):
        bbox = xyxy_to_frame_norm(tuple(boxes[i]), tf)
        if bbox[2] <= 0.0 or bbox[3] <= 0.0:
            continue  # fully clipped by the frame edge
        detections.append(
            Detection(
                box=[
                    float(bbox[0] - bbox[2] / 2),
                    float(bbox[1] - bbox[3] / 2),
                    float(bbox[0] + bbox[2] / 2),
                    float(bbox[1] + bbox[3] / 2),
                ],
                score=float(kept_scores[i]),
            )
        )
    return detections


DFL_BINS = 16

# Anchors kept before NMS, highest score first. At the deployed threshold
# (0.35) a frame yields single digits and this never engages. It exists for the
# AP sweep, which thresholds at 0.001 to trace the full precision/recall curve
# and would otherwise hand a quadratic NMS several thousand boxes; COCO scores
# the top 100 detections per image, so the cut cannot move the metric.
MAX_PRE_NMS = 1000


def _branch_layout(box_tensor: np.ndarray) -> bool:
    """True if this branch came back NHWC, decided from the box tensor.

    RKNN Lite normally hands back the graph's own NCHW, but the layout is a
    runtime option and reading NHWC as NCHW does not fail -- it silently
    reinterprets channels as grid rows and every box lands somewhere plausible
    and wrong.

    The box tensor is the only one that can answer this. Its 64 channels never
    equal a YOLOv8 grid side (80, 40, 20), whereas the class map has 80
    channels and the stride-8 grid is 80x80: for that tensor the two layouts
    are indistinguishable by shape. So the layout is decided once per branch,
    from the box tensor, and applied to the class map of the same branch.
    """
    if box_tensor.ndim != 4:
        raise ValueError(f"expected a 4-D head output, got shape {box_tensor.shape}")
    if box_tensor.shape[1] == 4 * DFL_BINS:
        return False
    if box_tensor.shape[3] == 4 * DFL_BINS:
        return True
    raise ValueError(
        f"no axis of {box_tensor.shape} has {4 * DFL_BINS} box channels"
    )


def _as_nchw(array: np.ndarray, nhwc: bool) -> np.ndarray:
    return np.transpose(array, (0, 3, 1, 2)) if nhwc else array


def _dfl_distances(dist_logits: np.ndarray) -> np.ndarray:
    """``(4*bins, k)`` logits -> ``(4, k)`` expected distances, in grid units.

    Distribution Focal Loss regresses each of the four box edges as a softmax
    over ``DFL_BINS`` discrete distances; the prediction is that distribution's
    mean. Upstream does this with torch, which this platform has no reason to
    install for one softmax.
    """
    reshaped = dist_logits.reshape(4, DFL_BINS, -1).astype(np.float32)
    reshaped -= reshaped.max(axis=1, keepdims=True)  # stabilize before exp
    weights = np.exp(reshaped)
    weights /= weights.sum(axis=1, keepdims=True)
    return np.tensordot(np.arange(DFL_BINS, dtype=np.float32), weights, axes=([0], [1]))


def decode_zoo_head(
    outputs: list[np.ndarray],
    tf: LetterboxTransform,
    conf_threshold: float,
    iou_threshold: float,
    input_size: int = 640,
) -> list[Detection]:
    """Decode the rknn_model_zoo YOLOv8 head into ``frame_norm`` boxes.

    ``outputs`` is 3 or 2 tensors per stride, in the order the graph declares
    them: box distribution, class map, and -- in the 3-per-stride export -- a
    class-score sum. The sum exists so an all-class decoder can reject anchors
    with one comparison instead of eighty. This detector only wants person, so
    it thresholds the person channel directly, which is both cheaper and
    tighter, and the sum is ignored.
    """
    # Always three strides. The export has either two tensors per stride or
    # three, so the count of tensors per stride is what varies -- deriving the
    # stride count instead (len // 2 for the six-output export) silently reads
    # a class map as a box distribution.
    if len(outputs) % 3:
        raise ValueError(f"unexpected zoo head output count: {len(outputs)}")
    branches = 3
    per_branch = len(outputs) // branches

    boxes_all: list[np.ndarray] = []
    scores_all: list[np.ndarray] = []
    for branch in range(branches):
        raw_dist = outputs[per_branch * branch]
        nhwc = _branch_layout(raw_dist)
        dist = _as_nchw(raw_dist, nhwc)[0]
        cls = _as_nchw(outputs[per_branch * branch + 1], nhwc)[0]
        grid_h, grid_w = cls.shape[1], cls.shape[2]
        # The class map already has sigmoid applied inside the graph
        # (ConvSigmoid in the build log), so this is a probability.
        person = cls[PERSON_CLASS_ID]
        rows, cols = np.nonzero(person >= conf_threshold)
        if rows.size == 0:
            continue

        # DFL runs on the surviving anchors only. On a typical frame that is
        # single digits out of 8400, which is the whole reason this decode is
        # affordable in Python at all.
        distances = _dfl_distances(dist[:, rows, cols])
        stride = input_size / grid_h
        cx = cols.astype(np.float32) + 0.5
        cy = rows.astype(np.float32) + 0.5
        boxes_all.append(
            np.stack(
                [
                    (cx - distances[0]) * stride,
                    (cy - distances[1]) * stride,
                    (cx + distances[2]) * stride,
                    (cy + distances[3]) * stride,
                ],
                axis=1,
            )
        )
        scores_all.append(person[rows, cols].astype(np.float32))

    if not boxes_all:
        return []
    boxes = np.concatenate(boxes_all)
    scores = np.concatenate(scores_all)
    if scores.size > MAX_PRE_NMS:
        top = np.argpartition(scores, -MAX_PRE_NMS)[-MAX_PRE_NMS:]
        boxes, scores = boxes[top], scores[top]

    detections: list[Detection] = []
    for i in nms(boxes, scores, iou_threshold):
        bbox = xyxy_to_frame_norm(tuple(boxes[i]), tf)
        if bbox[2] <= 0.0 or bbox[3] <= 0.0:
            continue  # fully clipped by the frame edge
        detections.append(
            Detection(
                box=[
                    float(bbox[0] - bbox[2] / 2),
                    float(bbox[1] - bbox[3] / 2),
                    float(bbox[0] + bbox[2] / 2),
                    float(bbox[1] + bbox[3] / 2),
                ],
                score=float(scores[i]),
            )
        )
    return detections


# --------------------------------------------------------------------------- #
# reCamera Pro (RV1126B) runtime wrapper.
#
# Everything above this line is a verbatim copy of
# ``edge-security-kit/platforms/rknn/esk_rknn/rknn_yolo.py`` up to (but not
# including) its ``RKNNPersonDetector``. That class owns an ``RKNNLite`` handle
# it loaded itself; here the kit has already loaded the model from
# ``manifest.json`` and hands it over as ``self.models.det``, so the wrapper
# below adapts to that handle instead of opening a second copy of a 4 MB model
# on a 2 GB board.
# --------------------------------------------------------------------------- #

import os
import re


class PersonDetector:
    """Person-only YOLOv8 detector over a kit-owned RKNN model handle.

    ``model`` is anything with ``infer(uint8_NHWC) -> list[np.ndarray]``: the
    kit's ``_ModelHandle`` in production, a stub in the host tests.
    """

    def __init__(self, model, conf_threshold=0.35, iou_threshold=0.45, input_size=640):
        self.model = model
        self.conf_threshold = float(conf_threshold)
        self.iou_threshold = float(iou_threshold)
        self.input_size = int(input_size)
        self.last_inference_ms = 0.0
        self._head = None
        self.runtime_version = librknnrt_version()

    @property
    def backend(self) -> str:
        return f"rknn-lite2-{self.runtime_version}"

    @property
    def head(self) -> str:
        if self._head is None:
            raise RuntimeError("head layout is only known after the first inference")
        return self._head

    def infer(self, rgb_canvas: np.ndarray) -> list:
        if rgb_canvas.dtype != np.uint8:
            raise ValueError("RKNN input must stay uint8; the model normalizes")
        batched = np.ascontiguousarray(rgb_canvas[None])
        started = time.perf_counter()
        outputs = self.model.infer(batched)
        self.last_inference_ms = (time.perf_counter() - started) * 1000.0
        if outputs is None:
            raise RuntimeError("RKNN inference returned None")
        return [np.asarray(o) for o in outputs]

    def detect(self, rgb_canvas: np.ndarray, tf: LetterboxTransform) -> list:
        outputs = self.infer(rgb_canvas)
        if len(outputs) == 1:
            self._head = "ultralytics"
            return decode_person_head(
                outputs[0], tf, self.conf_threshold, self.iou_threshold
            )
        self._head = "zoo"
        return decode_zoo_head(
            outputs, tf, self.conf_threshold, self.iou_threshold, self.input_size
        )


# On reCamera Pro the runtime lives in the read-only firmware partition; the
# RK3588 candidate list would find nothing. The version string inside the binary
# is what has to match the conversion toolkit, and the SONAME lies about it on
# both boards, so it is read out of the file rather than off the filename.
_LIBRKNNRT_CANDIDATES = (
    "/oem/usr/lib/librknnrt.so",
    "/usr/lib/librknnrt.so",
    "/userdata/sdk/lib/librknnrt.so",
)


def librknnrt_version() -> str:
    pattern = re.compile(rb"librknnrt version:\s*([0-9][0-9A-Za-z._-]*)")
    seen = set()
    for name in _LIBRKNNRT_CANDIDATES:
        real = os.path.realpath(name)
        if real in seen or not os.path.exists(real):
            continue
        seen.add(real)
        try:
            with open(real, "rb") as handle:
                match = pattern.search(handle.read())
        except OSError:
            continue
        if match:
            return match.group(1).decode("ascii", "replace")
    return "unknown"
