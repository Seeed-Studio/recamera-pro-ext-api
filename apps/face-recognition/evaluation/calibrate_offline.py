#!/usr/bin/env python3
"""Offline liveness feature extraction over public anti-spoofing datasets.

Produces the same JSONL rows the device's ``liveness_capture`` command writes,
so `tools/fit_liveness.py` consumes the output unchanged. Everything runs on
fp32 onnxruntime with the SAME preprocessing code the device uses
(`scrfd.decode`, `liveness.crop_minifas`, `depth_liveness.planarity_from_depth`),
so the only thing that differs from a device capture is the camera and the
fp16 NPU quantisation.

What is real and what is synthetic
----------------------------------
* texture (`P_tex_v2`, `P_tex_v1se`) and depth (`depth_planarity`,
  `depth_score`) are genuine per-image measurements.
* `motion_residual` / `correlation` are NOT a live-subject measurement. Each
  still image is re-detected `--jitter` times under a small random translation
  and brightness change; the resulting five-point series is exactly what the
  detector produces for a *static* subject (a print or a screen held still), so
  the distribution measured here is the detector's own noise. That is the number
  `liveness_motion_noise_floor` must sit above — it says nothing about a real
  face's micro-motion, which cannot be obtained from stills.
* `EAR` is always null and `blink` always false: a single frame has no blink.

Usage
-----
    uv run --with onnxruntime --with opencv-python-headless --with numpy \
        python calibrate_offline.py --dataset nuaa --limit 400 \
        --out data/rows-nuaa.jsonl
"""
from __future__ import annotations

import argparse
import json
import random
import re
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import cv2
import numpy as np

HERE = Path(__file__).resolve().parent
APP = HERE.parent
sys.path.insert(0, str(APP))

import scrfd                                    # noqa: E402
import liveness as lv                           # noqa: E402
import liveness_temporal as lt                  # noqa: E402
import depth_liveness as dl                     # noqa: E402
import onnxruntime as ort                       # noqa: E402

DET_ONNX = APP / "models" / "convert" / "det_500m_640.onnx"
FACE_REC_API = Path("/Users/harvest/project/face_rec_api")
TEX_V2_ONNX = FACE_REC_API / "models" / "onnx" / "liveness_minifasnet.onnx"
TEX_V1SE_ONNX = FACE_REC_API / "models" / "onnx" / "liveness_minifasnet_v1se.onnx"
MIDAS_ONNX = HERE / "data" / "midas" / "models" / "model-small_opset19.onnx"

DET_SIZE = 640
MIDAS_SIZE = 256


# --------------------------------------------------------------------------- #
# Preprocessing (verbatim from evaluation/fp32_ref.py so the boxes match)
# --------------------------------------------------------------------------- #
class _LetterboxInfo:
    __slots__ = ("scale", "pad_w", "pad_h", "orig_w", "orig_h")

    def __init__(self, scale, pad_w, pad_h, orig_w, orig_h):
        self.scale, self.pad_w, self.pad_h = scale, pad_w, pad_h
        self.orig_w, self.orig_h = orig_w, orig_h


def letterbox(rgb: np.ndarray, size: int = DET_SIZE):
    h, w = rgb.shape[:2]
    s = min(size / w, size / h)
    nw, nh = int(round(w * s)), int(round(h * s))
    r = cv2.resize(rgb, (nw, nh))
    c = np.full((size, size, 3), 128, np.uint8)
    pw, ph = (size - nw) // 2, (size - nh) // 2
    c[ph:ph + nh, pw:pw + nw] = r
    return c, _LetterboxInfo(s, pw, ph, w, h)


class Detector:
    def __init__(self, path: Path):
        self.s = ort.InferenceSession(str(path), providers=["CPUExecutionProvider"])
        self.name = self.s.get_inputs()[0].name

    def __call__(self, bgr: np.ndarray, conf: float = 0.5) -> List[dict]:
        rgb = bgr[..., ::-1].copy()
        lb, info = letterbox(rgb)
        x = ((lb.astype(np.float32) - 127.5) / 128.0).transpose(2, 0, 1)[None]
        outs = self.s.run(None, {self.name: x})
        return scrfd.decode(outs, info, conf_thres=conf, iou_thres=0.4,
                            model_size=DET_SIZE)


class MiniFAS:
    """MiniFASNet head. Canonical input: 80x80 BGR, float 0-255, NO normalization
    (face_rec_api src/liveness.py :27-31)."""

    def __init__(self, path: Path):
        self.s = ort.InferenceSession(str(path), providers=["CPUExecutionProvider"])
        self.name = self.s.get_inputs()[0].name

    def infer(self, crop_bgr_u8: np.ndarray):
        x = crop_bgr_u8.astype(np.float32).transpose(2, 0, 1)[None]
        return self.s.run(None, {self.name: x})


class MiDaS:
    """MiDaS v2.1 small, opset19. ImageNet normalisation is INSIDE the graph
    (evaluation/depth-model-selection.md), so the graph takes RGB in [0, 1]."""

    def __init__(self, path: Path):
        self.s = ort.InferenceSession(str(path), providers=["CPUExecutionProvider"])
        self.name = self.s.get_inputs()[0].name

    def depth(self, bgr: np.ndarray) -> np.ndarray:
        rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        r = cv2.resize(rgb, (MIDAS_SIZE, MIDAS_SIZE))
        x = (r.astype(np.float32) / 255.0).transpose(2, 0, 1)[None]
        return np.squeeze(np.asarray(self.s.run(None, {self.name: x})[0]))


# --------------------------------------------------------------------------- #
# Datasets
# --------------------------------------------------------------------------- #
# CASIA-FASD video id -> attack type. Each subject records 12 clips:
#   1, 2, HR_1   genuine (low / normal / high quality)
#   3, 4, HR_2   warped printed photo
#   5, 6, HR_3   cut printed photo (eye holes)
#   7, 8, HR_4   video replay on a screen
CASIA_KIND = {
    "1": "real", "2": "real", "HR_1": "real",
    "3": "print", "4": "print", "HR_2": "print",
    "5": "print", "6": "print", "HR_3": "print",
    "7": "screen", "8": "screen", "HR_4": "screen",
}
_CASIA_RE = re.compile(r"^(\d+)_(HR_\d|\d+)\.avi_(\d+)_(real|fake)\.jpg$")


def collect_casiafasd(root: Path) -> List[Tuple[Path, str, str]]:
    """-> [(path, label, track)] with track = subject#clip."""
    out = []
    for p in sorted(root.glob("*/*/color/*.jpg")):
        m = _CASIA_RE.match(p.name)
        if not m:
            continue
        subj, clip, _frame, tag = m.groups()
        kind = CASIA_KIND.get(clip)
        if kind is None:
            continue
        if (kind == "real") != (tag == "real"):     # filename self-check
            raise ValueError(f"label/clip mismatch: {p.name}")
        out.append((p, kind, f"casia-{subj}-{clip}"))
    return out


def collect_nuaa(root: Path) -> List[Tuple[Path, str, str]]:
    """NUAA Photograph Imposter DB, `raw` (uncropped 640x480 frames).

    ClientRaw = genuine, ImposterRaw = a printed photo re-shot by the same
    webcam. Track = subject directory + session prefix of the filename
    (``0001_00_00_01_*`` -> one continuous recording).
    """
    out = []
    for kind, sub in (("real", "ClientRaw"), ("print", "ImposterRaw")):
        for p in sorted((root / "raw" / sub).glob("*/*.jpg")):
            stem = p.stem.split("_")
            sess = "_".join(stem[:-1]) if len(stem) > 1 else p.parent.name
            out.append((p, kind, f"nuaa-{sub}-{p.parent.name}-{sess}"))
    return out


def collect_flat(root: Path) -> List[Tuple[Path, str, str]]:
    """``root/<label>/<track>/*.jpg`` — for datasets shipped as videos that were
    decoded to frames outside this script (one directory per clip = one track)."""
    out = []
    for lab_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        for track_dir in sorted(p for p in lab_dir.iterdir() if p.is_dir()):
            for img in sorted(track_dir.glob("*.jpg")):
                out.append((img, lab_dir.name, f"{root.name}-{lab_dir.name}-{track_dir.name}"))
    return out


COLLECTORS = {"casiafasd": collect_casiafasd, "nuaa": collect_nuaa,
              "flat": collect_flat}


def balanced_sample(items: Sequence[Tuple[Path, str, str]], per_label: int,
                    seed: int) -> List[Tuple[Path, str, str]]:
    """Take `per_label` images per label, spread evenly over that label's tracks.

    Round-robin over tracks rather than a flat shuffle: a flat sample of a
    dataset where one subject contributed 500 frames and another 60 is mostly
    the first subject.
    """
    rng = random.Random(seed)
    by_label: Dict[str, Dict[str, List]] = {}
    for path, label, track in items:
        by_label.setdefault(label, {}).setdefault(track, []).append((path, label, track))
    picked: List[Tuple[Path, str, str]] = []
    for label in sorted(by_label):
        tracks = sorted(by_label[label])
        for t in tracks:
            rng.shuffle(by_label[label][t])
        taken, i = [], 0
        while len(taken) < per_label:
            progressed = False
            for t in tracks:
                bucket = by_label[label][t]
                if i < len(bucket):
                    taken.append(bucket[i])
                    progressed = True
                    if len(taken) >= per_label:
                        break
            if not progressed:
                break
            i += 1
        picked.extend(taken)
    return picked


# --------------------------------------------------------------------------- #
# Static-jitter motion floor
# --------------------------------------------------------------------------- #
def jitter_series(det: Detector, bgr: np.ndarray, n: int, rng: random.Random,
                  max_shift: float, bright: float, conf: float
                  ) -> List[Tuple[float, np.ndarray, float]]:
    """Re-detect the same still image under n small perturbations.

    Returns ``[(ts, kps5, face_px)]``. The first sample is the unperturbed
    image, so the caller can reuse it as the primary detection.
    """
    out: List[Tuple[float, np.ndarray, float]] = []
    h, w = bgr.shape[:2]
    for i in range(n):
        if i == 0:
            img = bgr
        else:
            dx = rng.uniform(-max_shift, max_shift)
            dy = rng.uniform(-max_shift, max_shift)
            gain = 1.0 + rng.uniform(-bright, bright)
            M = np.float32([[1, 0, dx], [0, 1, dy]])
            img = cv2.warpAffine(bgr, M, (w, h), flags=cv2.INTER_LINEAR,
                                 borderMode=cv2.BORDER_REPLICATE)
            img = np.clip(img.astype(np.float32) * gain, 0, 255).astype(np.uint8)
        faces = det(img, conf)
        if not faces:
            out.append((i / 15.0, None, 0.0))
            continue
        f = max(faces, key=lambda d: (d["box"][2] - d["box"][0]) *
                (d["box"][3] - d["box"][1]))
        x1, y1, x2, y2 = f["box"]
        out.append((i / 15.0, np.asarray(f["kps"], dtype=np.float64),
                    min(x2 - x1, y2 - y1)))
    return out


def _eff_scale(bgr: np.ndarray, bbox_xywh, scale: float) -> float:
    """The scale `get_expanded_box` actually applies after its image clamp."""
    h, w = bgr.shape[:2]
    return float(min((h - 1) / bbox_xywh[3], (w - 1) / bbox_xywh[2], scale))


# --------------------------------------------------------------------------- #
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dataset", required=True, choices=sorted(COLLECTORS))
    ap.add_argument("--root", required=True, help="extracted dataset directory")
    ap.add_argument("--out", required=True, help="output JSONL")
    ap.add_argument("--limit", type=int, default=400, help="images per label")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--conf", type=float, default=0.4, help="SCRFD confidence")
    ap.add_argument("--jitter", type=int, default=5,
                    help="static re-detections per image for the motion floor")
    ap.add_argument("--max-shift", type=float, default=2.0, help="jitter pixels")
    ap.add_argument("--brightness", type=float, default=0.05, help="jitter gain")
    ap.add_argument("--no-depth", action="store_true")
    ap.add_argument("--progress", type=int, default=100)
    args = ap.parse_args()

    items = COLLECTORS[args.dataset](Path(args.root))
    picked = balanced_sample(items, args.limit, args.seed)
    counts: Dict[str, int] = {}
    for _, label, _ in picked:
        counts[label] = counts.get(label, 0) + 1
    print(f"[{args.dataset}] pool={len(items)} sampled={len(picked)} {counts}",
          flush=True)

    det = Detector(DET_ONNX)
    tex_v2 = MiniFAS(TEX_V2_ONNX)
    tex_v1se = MiniFAS(TEX_V1SE_ONNX)
    midas = None if args.no_depth else MiDaS(MIDAS_ONNX)
    cfg = lt.LivenessConfig()
    rng = random.Random(args.seed)

    n_ok = n_noface = n_err = 0
    t0 = time.time()
    with open(args.out, "w", encoding="utf-8") as fh:
        for idx, (path, label, track) in enumerate(picked):
            bgr = cv2.imread(str(path))
            if bgr is None:
                n_err += 1
                continue
            series = jitter_series(det, bgr, max(1, args.jitter), rng,
                                   args.max_shift, args.brightness, args.conf)
            if series[0][1] is None:
                n_noface += 1
                continue
            kps0, face_px = series[0][1], series[0][2]
            faces = det(bgr, args.conf)
            f = max(faces, key=lambda d: (d["box"][2] - d["box"][0]) *
                    (d["box"][3] - d["box"][1]))
            x1, y1, x2, y2 = f["box"]
            bbox_xywh = (x1, y1, x2 - x1, y2 - y1)

            try:
                p_v2 = lv.real_probability_strict(
                    tex_v2.infer(lv.crop_minifas(bgr, bbox_xywh,
                                                 lv.LIVENESS_CROP_SCALE)))
                p_v1se = lv.real_probability_strict(
                    tex_v1se.infer(lv.crop_minifas(bgr, bbox_xywh,
                                                   lv.LIVENESS_CROP_SCALE_V1SE)))
            except ValueError:
                n_err += 1
                continue

            # Static-jitter motion: the detector's own noise on a still subject.
            state = lt.LivenessState()
            for ts, kps, px in series:
                if kps is None:
                    continue
                lt.update_motion(state, ts, kps, px or face_px, cfg)

            planarity = score = None
            if midas is not None:
                d = midas.depth(bgr)
                r = dl.planarity_from_depth(d, bbox_xywh,
                                            image_size=(bgr.shape[1], bgr.shape[0]))
                planarity = None if not np.isfinite(r["planarity"]) else float(r["planarity"])
                score = None if not np.isfinite(r["score"]) else float(r["score"])

            row = {
                "P_tex_v2": p_v2,
                "P_tex_v1se": p_v1se,
                "motion_residual": state.motion_residual,
                "correlation": state.correlation,
                "EAR": None,
                "blink": False,
                "face_px_size": float(face_px),
                "track_id": track,
                "label": label,
                "ts": float(idx),
                "depth_planarity": planarity,
                "depth_score": score,
                "source": str(path.relative_to(Path(args.root))),
                "det_score": float(f["score"]),
                # `get_expanded_box` clamps the requested scale so the expansion
                # fits the image. When both heads clamp to the same value the
                # ensemble is two views of ONE crop, which the report must say.
                "crop_scale_v2": _eff_scale(bgr, bbox_xywh, lv.LIVENESS_CROP_SCALE),
                "crop_scale_v1se": _eff_scale(bgr, bbox_xywh,
                                              lv.LIVENESS_CROP_SCALE_V1SE),
            }
            fh.write(json.dumps(row) + "\n")
            n_ok += 1
            if args.progress and (idx + 1) % args.progress == 0:
                el = time.time() - t0
                print(f"  {idx + 1}/{len(picked)} ok={n_ok} noface={n_noface} "
                      f"err={n_err} {el:.0f}s ({el / (idx + 1):.2f}s/img)",
                      flush=True)

    print(f"[{args.dataset}] done ok={n_ok} no_face={n_noface} error={n_err} "
          f"elapsed={time.time() - t0:.0f}s -> {args.out}", flush=True)


if __name__ == "__main__":
    main()
