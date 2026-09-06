#!/usr/bin/env python3
"""Offline threshold/weight fitting for liveness v2. stdlib + numpy only.

Input is the JSONL written by the ``liveness_capture`` command — one row per
face per frame, features only. Concatenate the captures for every label:

    python3 tools/fit_liveness.py real.jsonl print.jsonl screen.jsonl \\
        --max-far 0.01 --output-thresholds thresholds.json

★Split by track, never by row★ consecutive rows from one track are nearly
identical (same person, same lighting, 30 ms apart). A random row-level split
puts near-duplicates on both sides and reports an ROC that the device will not
reproduce. `--holdout` therefore partitions whole (session, track) groups.

★Report both operating points★ Youden's J is the balanced answer; a door does
not want the balanced answer. `--max-far` reports the threshold that holds the
false-accept rate at or below a stated bound, which is the number an access
control install actually configures.

Everything printed is JSON on stdout, so this composes with jq.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Dict, List, Optional, Sequence

import numpy as np

REAL_LABELS = ("real",)
SPOOF_LABELS = ("print", "screen")

DEFAULT_WEIGHTS = {"texture": 0.70, "motion": 0.30, "depth": 0.20,
                   "blink_bonus": 1.0}
DEFAULT_CAL = {"floor": 0.003, "high": 0.020, "corr_low": 0.15,
               "corr_high": 0.65}

FEATURES = ("P_tex_v2", "P_tex_v1se", "motion_residual", "correlation", "EAR",
            "face_px_size")


def _f(v) -> float:
    """A row value as float, with null/absent/garbage becoming NaN."""
    if v is None:
        return float("nan")
    try:
        return float(v)
    except (TypeError, ValueError):
        return float("nan")


def load_jsonl(path: str) -> Dict[str, np.ndarray]:
    """Parse one capture file into parallel arrays.

    ``y`` is 1 for ``real`` and 0 for ``print``/``screen``; rows with any other
    label are dropped, as are unparseable lines (a capture truncated by a power
    cut ends in a partial line). Nulls survive as NaN so the per-feature ROC can
    ignore exactly the rows where that feature is missing, instead of dropping
    the whole row everywhere.
    """
    cols: Dict[str, List] = {k: [] for k in FEATURES}
    cols.update({"y": [], "blink": [], "track": [], "ts": [], "label": []})
    session = os.path.basename(path)
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if not isinstance(row, dict):
                continue
            label = str(row.get("label") or "").strip().lower()
            if label in REAL_LABELS:
                y = 1.0
            elif label in SPOOF_LABELS:
                y = 0.0
            else:
                continue
            for k in FEATURES:
                cols[k].append(_f(row.get(k)))
            cols["y"].append(y)
            cols["blink"].append(1.0 if row.get("blink") else 0.0)
            cols["ts"].append(_f(row.get("ts")))
            cols["label"].append(label)
            # ★Group key★ track ids restart at 1 on every app launch, so a bare
            # track id would merge different people from different sessions.
            cols["track"].append(f"{session}#{label}#{row.get('track_id')}")
    out: Dict[str, np.ndarray] = {}
    out["y"] = np.asarray(cols["y"], dtype=np.float64)
    out["blink"] = np.asarray(cols["blink"], dtype=np.float64)
    out["ts"] = np.asarray(cols["ts"], dtype=np.float64)
    out["track"] = np.asarray(cols["track"], dtype=object)
    out["label"] = np.asarray(cols["label"], dtype=object)
    for k in FEATURES:
        out[k] = np.asarray(cols[k], dtype=np.float64)
    return out


def concat(datasets: Sequence[Dict[str, np.ndarray]]) -> Dict[str, np.ndarray]:
    if not datasets:
        raise SystemExit("no input rows")
    keys = datasets[0].keys()
    return {k: np.concatenate([d[k] for d in datasets]) for k in keys}


# --------------------------------------------------------------------------- #
# ROC
# --------------------------------------------------------------------------- #
def roc_threshold(y: np.ndarray, score: np.ndarray,
                  max_far: float = 0.01) -> Dict:
    """Full-sweep ROC over the unique score values.

    Returns AUC, the Youden-J operating point, and the lowest-threshold point
    whose false-accept rate is <= `max_far`. `score` is oriented so that HIGHER
    means more live; rows where it is not finite are ignored.
    """
    y = np.asarray(y, dtype=np.float64)
    s = np.asarray(score, dtype=np.float64)
    keep = np.isfinite(s) & np.isfinite(y)
    y, s = y[keep], s[keep]
    n_pos, n_neg = float((y == 1).sum()), float((y == 0).sum())
    if n_pos == 0 or n_neg == 0:
        return {"n": int(y.size), "n_real": int(n_pos), "n_spoof": int(n_neg),
                "auc": None, "youden": None, "at_max_far": None,
                "note": "need both classes"}

    thr = np.unique(s)
    # Candidate cut-offs: "accept when score >= t". Include one above the max so
    # the all-reject corner exists.
    cand = np.concatenate([thr, [thr[-1] + 1.0]])
    tpr = np.array([float((s[y == 1] >= t).sum()) / n_pos for t in cand])
    fpr = np.array([float((s[y == 0] >= t).sum()) / n_neg for t in cand])

    order = np.argsort(fpr, kind="stable")
    # numpy 2 renamed trapz; the device may still be on numpy 1.
    trapezoid = getattr(np, "trapezoid", None) or np.trapz
    auc = float(trapezoid(tpr[order], fpr[order]))
    j = tpr - fpr
    ij = int(np.argmax(j))

    ok = np.where(fpr <= float(max_far))[0]
    at_far = None
    if ok.size:
        best = ok[int(np.argmax(tpr[ok]))]
        at_far = {"threshold": float(cand[best]), "tpr": float(tpr[best]),
                  "fpr": float(fpr[best]), "max_far": float(max_far)}

    return {
        "n": int(y.size), "n_real": int(n_pos), "n_spoof": int(n_neg),
        "auc": auc,
        "youden": {"threshold": float(cand[ij]), "tpr": float(tpr[ij]),
                   "fpr": float(fpr[ij]), "j": float(j[ij])},
        "at_max_far": at_far,
    }


# --------------------------------------------------------------------------- #
# Feature construction (must mirror liveness_temporal.py)
# --------------------------------------------------------------------------- #
def normalized_motion(residual, correlation, floor: float, high: float,
                      corr_low: float, corr_high: float) -> np.ndarray:
    """Clipped piecewise-linear motion calibration, vectorised.

    Mirrors `liveness_temporal.normalized_motion`, including the rule that a
    residual at or below the floor yields NaN ("no evidence"), NOT zero.
    """
    r = np.asarray(residual, dtype=np.float64)
    c = np.asarray(correlation, dtype=np.float64)
    m_res = np.clip((r - floor) / max(1e-9, high - floor), 0.0, 1.0)
    c = np.where(np.isfinite(c), c, 0.0)
    m_corr = np.clip((c - corr_low) / max(1e-9, corr_high - corr_low), 0.0, 1.0)
    out = 0.5 * (m_res + m_corr)
    return np.where(np.isfinite(r) & (r > floor), out, np.nan)


def fused_score(tex, motion, blink, depth, weights: Dict[str, float]
                ) -> np.ndarray:
    """Weighted mean over the terms that are present, plus the blink bonus.

    Absent evidence (NaN) is removed from BOTH the numerator and the weight
    sum, which is what "normalize available non-blink weights" means: a still
    face is not penalised for having no motion term.
    """
    tex = np.asarray(tex, dtype=np.float64)
    parts = [(float(weights.get("texture", 0.0)), tex)]
    if motion is not None:
        parts.append((float(weights.get("motion", 0.0)),
                      np.asarray(motion, dtype=np.float64)))
    if depth is not None:
        parts.append((float(weights.get("depth", 0.0)),
                      np.asarray(depth, dtype=np.float64)))
    num = np.zeros_like(tex)
    den = np.zeros_like(tex)
    for w, v in parts:
        ok = np.isfinite(v)
        num = num + np.where(ok, w * np.nan_to_num(v), 0.0)
        den = den + np.where(ok, w, 0.0)
    score = np.where(den > 0, num / np.where(den > 0, den, 1.0), np.nan)
    if blink is not None:
        b = np.asarray(blink, dtype=np.float64) > 0
        score = np.where(b, np.minimum(1.0, score
                                       + float(weights.get("blink_bonus", 0.0))),
                         score)
    return np.clip(score, 0.0, 1.0)


def split_by_track(data: Dict[str, np.ndarray], holdout: float, seed: int):
    """Partition WHOLE tracks into fit/validation sets."""
    tracks = np.unique(data["track"])
    rng = np.random.default_rng(seed)
    rng.shuffle(tracks)
    n_val = int(round(len(tracks) * float(holdout)))
    val = set(tracks[:n_val].tolist())
    mask_val = np.array([t in val for t in data["track"]], dtype=bool)
    return ~mask_val, mask_val


def _subset(data: Dict[str, np.ndarray], mask) -> Dict[str, np.ndarray]:
    return {k: v[mask] for k, v in data.items()}


def report(data: Dict[str, np.ndarray], cal: Dict[str, float],
           weights: Dict[str, float], max_far: float) -> Dict:
    tex_mean = np.nanmean(np.stack([data["P_tex_v2"], data["P_tex_v1se"]]),
                          axis=0)
    motion = normalized_motion(data["motion_residual"], data["correlation"],
                               cal["floor"], cal["high"], cal["corr_low"],
                               cal["corr_high"])
    fused = fused_score(tex_mean, motion, data["blink"], None, weights)
    y = data["y"]
    return {
        "rows": int(y.size),
        "tracks": int(np.unique(data["track"]).size),
        "texture_v2": roc_threshold(y, data["P_tex_v2"], max_far),
        "texture_v1se": roc_threshold(y, data["P_tex_v1se"], max_far),
        "texture_ensemble": roc_threshold(y, tex_mean, max_far),
        "motion_residual": roc_threshold(y, data["motion_residual"], max_far),
        "correlation": roc_threshold(y, data["correlation"], max_far),
        "motion_score": roc_threshold(y, motion, max_far),
        "fused": roc_threshold(y, fused, max_far),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("paths", nargs="+", help="liveness_capture.jsonl file(s)")
    ap.add_argument("--max-far", type=float, default=0.01,
                    help="false-accept bound for the reported operating point")
    ap.add_argument("--weights", default=None,
                    help='JSON, e.g. \'{"texture":0.7,"motion":0.3}\'')
    ap.add_argument("--calibration", default=None,
                    help='JSON, e.g. \'{"floor":0.003,"high":0.02}\'')
    ap.add_argument("--holdout", type=float, default=0.0,
                    help="fraction of TRACKS held out for validation")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--output-thresholds", default=None,
                    help="write the suggested config keys to this JSON file")
    args = ap.parse_args()

    weights = dict(DEFAULT_WEIGHTS)
    if args.weights:
        weights.update(json.loads(args.weights))
    cal = dict(DEFAULT_CAL)
    if args.calibration:
        cal.update(json.loads(args.calibration))

    data = concat([load_jsonl(p) for p in args.paths])
    if data["y"].size == 0:
        raise SystemExit("no usable rows (labels must be real/print/screen)")

    out: Dict = {"inputs": list(args.paths), "weights": weights,
                 "calibration": cal, "max_far": args.max_far}
    out["labels"] = {str(k): int((data["label"] == k).sum())
                     for k in np.unique(data["label"])}
    if args.holdout > 0:
        fit_mask, val_mask = split_by_track(data, args.holdout, args.seed)
        out["fit"] = report(_subset(data, fit_mask), cal, weights, args.max_far)
        out["validation"] = report(_subset(data, val_mask), cal, weights,
                                   args.max_far)
        head = out["fit"]
    else:
        out["all"] = report(data, cal, weights, args.max_far)
        head = out["all"]

    fused = head.get("fused") or {}
    point = fused.get("at_max_far") or fused.get("youden")
    if point:
        t_live = float(point["threshold"])
        suggestion = {
            "liveness_t_live": round(t_live, 4),
            "liveness_t_spoof": round(max(0.0, t_live - 0.20), 4),
            "liveness_motion_noise_floor": cal["floor"],
            "liveness_motion_high": cal["high"],
            "liveness_correlation_low": cal["corr_low"],
            "liveness_correlation_high": cal["corr_high"],
            "liveness_w_texture": weights["texture"],
            "liveness_w_motion": weights["motion"],
        }
        out["suggested_config"] = suggestion
        if args.output_thresholds:
            with open(args.output_thresholds, "w", encoding="utf-8") as fh:
                json.dump(suggestion, fh, indent=2)
                fh.write("\n")

    json.dump(out, sys.stdout, indent=2, default=float)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
