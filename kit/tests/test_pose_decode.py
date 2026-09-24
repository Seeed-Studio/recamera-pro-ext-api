"""Pin the pose keypoint decode to the Ultralytics definition.

Ultralytics ``Pose.kpts_decode`` computes ``(raw * 2 + (anchors - 0.5)) *
stride`` with ``anchors = grid + 0.5``, and the box decode measures DFL
distances from the same anchor.  A raw keypoint offset of 0.25 therefore
lands exactly on the anchor centre, which is also the centre of a box with
equal left/right and top/bottom distances.  The shape-equivalence fixtures
invert our own decoder, so they cannot catch a formula error on their own.
"""
import numpy as np

from kit.runtime.postprocess.pose import N_KPT, _decode_pose

GRID = 20
STRIDE = 640 // GRID
COL, ROW = 7, 11


def _outputs():
    box = np.full((1, 64, GRID, GRID), -20.0, dtype=np.float32)
    for side in range(4):
        box[0, side * 16 + 2, ROW, COL] = 20.0      # every side = 2 cells
    cls = np.full((1, 1, GRID, GRID), -20.0, dtype=np.float32)
    cls[0, 0, ROW, COL] = 20.0
    kpt = np.zeros((1, N_KPT * 3, GRID, GRID), dtype=np.float32)
    kpt[0, 0::3, ROW, COL] = 0.25
    kpt[0, 1::3, ROW, COL] = 0.25
    kpt[0, 2::3, ROW, COL] = 20.0
    return [box, cls, kpt]


def test_keypoint_offset_quarter_lands_on_anchor_centre():
    xyxy, scores, kpts = _decode_pose(_outputs(), conf_thres=0.5)
    assert scores.shape == (1,)
    centre = ((COL + 0.5) * STRIDE, (ROW + 0.5) * STRIDE)
    box_centre = ((xyxy[0, 0] + xyxy[0, 2]) / 2, (xyxy[0, 1] + xyxy[0, 3]) / 2)
    np.testing.assert_allclose(box_centre, centre, atol=1e-3)
    np.testing.assert_allclose(kpts[0, :, 0], centre[0], atol=1e-3)
    np.testing.assert_allclose(kpts[0, :, 1], centre[1], atol=1e-3)
