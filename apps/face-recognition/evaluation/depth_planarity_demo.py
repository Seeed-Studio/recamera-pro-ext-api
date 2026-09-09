"""Demo run of `depth_liveness.planarity_from_depth` on real depth maps.

Inputs are the RKNN PC-simulator depth maps produced during the depth-model
selection (models/depth_out/*.npy, see evaluation/depth-model-selection.md):

  img_probe     evaluation/probe_003301.jpg — a 2D face photo pasted on a flat
                grey mount, i.e. the planar/replay-like case.
  img_realface  the same photo's inset cropped and rescaled to fill the frame,
                i.e. the face-fills-frame case the model was trained for.

A third box over the grey mount of the probe frame is the ground-truth planar
control: that region really is one flat surface.

Run:  uv run --with numpy python evaluation/depth_planarity_demo.py
"""
import os
import sys

import numpy as np

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, APP_DIR)

import depth_liveness as dl  # noqa: E402

OUT = os.path.join(APP_DIR, "models", "depth_out")

# Boxes in the 640x640 source-image coordinates.
FACE_ON_PROBE = (265, 215, 155, 185)     # face inside the pasted 2D photo
GREY_MOUNT = (20, 20, 130, 130)          # flat grey border of the probe frame
FACE_FULL_FRAME = (203, 96, 330, 395)    # same face, cropped to fill the frame

CASES = [
    ("face inside pasted 2D photo", "img_probe", FACE_ON_PROBE),
    ("grey mount (true plane control)", "img_probe", GREY_MOUNT),
    ("same face filling the frame", "img_realface", FACE_FULL_FRAME),
]


def main() -> None:
    for model in ("midas_v21_small_256", "dav2_small_252"):
        print(f"\n=== {model} ===")
        print(f"{'case':34s} {'planarity':>10s} {'relief':>8s} {'score':>7s} "
              f"{'resid/range':>11s} {'n':>7s}")
        for label, image, box in CASES:
            path = os.path.join(OUT, f"{model}__{image}__rknnsim.npy")
            if not os.path.exists(path):
                print(f"{label:34s} MISSING {path}")
                continue
            depth = np.load(path)
            r = dl.planarity_from_depth(depth, box, image_size=(640, 640))
            print(f"{label:34s} {r['planarity']:10.4f} {r['relief']:8.3f} "
                  f"{r['score']:7.4f} {r['residual_ratio']:11.4f} "
                  f"{r['n_samples']:7d}")


if __name__ == "__main__":
    main()
