"""Decode YOLO-World sigmoid scores and box distances (DFL is in the graph)."""
import numpy as np

from kit.runtime.postprocess.detect import nms


def detection_result(box, cls, score, term):
    return {"box": box, "cls": cls, "cls_name": term.label,
            "prompt": term.prompt, "score": score,
            "display_label": f"{term.label} {score:.1%} ({', '.join(f'{v:.0f}' for v in box)})"}


def postprocess(outputs, info, terms, conf=0.25, iou=0.45, max_detections=40):
    if not terms:
        return []
    if len(outputs) not in (6, 9):
        raise ValueError("expected 3 YOLO-World score/distance head pairs")
    step = len(outputs) // 3
    boxes, scores, classes = [], [], []
    for level, grid in enumerate((80, 40, 20)):
        probs = np.asarray(outputs[level * step], dtype=np.float32)
        distances = np.asarray(outputs[level * step + 1], dtype=np.float32)
        if probs.shape != (1, 80, grid, grid) or distances.shape != (1, 4, grid, grid):
            raise ValueError(f"unexpected YOLO-World head shape at stride {640 // grid}")
        probs = probs[0, :len(terms)].reshape(len(terms), -1)
        best = probs.max(axis=0)
        keep = np.flatnonzero(np.isfinite(best) & (best >= conf) & (best <= 1))
        if not keep.size:
            continue
        cls = probs[:, keep].argmax(axis=0)
        dist = distances[0].reshape(4, -1)[:, keep]
        x, y = keep % grid + 0.5, keep // grid + 0.5
        stride = 640 / grid
        xyxy = np.stack((x - dist[0], y - dist[1], x + dist[2], y + dist[3]), axis=1) * stride
        valid = np.isfinite(xyxy).all(axis=1)
        boxes.append(xyxy[valid])
        scores.append(best[keep][valid])
        classes.append(cls[valid])
    if not boxes:
        return []
    boxes, scores, classes = map(np.concatenate, (boxes, scores, classes))
    boxes[:, [0, 2]] = np.clip((boxes[:, [0, 2]] - info.pad_w) / info.scale, 0, info.orig_w)
    boxes[:, [1, 3]] = np.clip((boxes[:, [1, 3]] - info.pad_h) / info.scale, 0, info.orig_h)
    valid = (boxes[:, 2] > boxes[:, 0]) & (boxes[:, 3] > boxes[:, 1])
    boxes, scores, classes = boxes[valid], scores[valid], classes[valid]
    selected = []
    for cls in np.unique(classes):
        members = np.flatnonzero(classes == cls)
        selected.extend(members[nms(boxes[members], scores[members], iou)])
    selected.sort(key=lambda k: float(scores[k]), reverse=True)
    results = []
    for k in selected[:max_detections]:
        cls, score = int(classes[k]), float(scores[k])
        box = [round(float(v), 2) for v in boxes[k]]
        results.append(detection_result(box, cls, score, terms[cls]))
    return results
