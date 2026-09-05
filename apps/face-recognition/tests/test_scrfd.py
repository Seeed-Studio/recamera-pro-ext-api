"""SCRFD decode contract. No rknn, no device: the nine raw tensors are built here.

The load-bearing claim is that nothing indexes the model outputs by POSITION —
the RKNN toolkit is free to reorder them — so every test shuffles the nine
tensors before handing them over.
"""
import random

import numpy as np
import pytest

import scrfd
from kit.runtime.preprocess import LetterboxInfo

NET = 640
ROWS = {8: 12800, 16: 3200, 32: 800}
COLS = {"scores": 1, "bboxes": 4, "kps": 10}


def blank_outputs():
    """Nine zeroed tensors keyed by (stride, kind), as the graph would emit."""
    out = {}
    for s, n in ROWS.items():
        for kind, c in COLS.items():
            out[(s, kind)] = np.zeros((n, c), dtype=np.float32)
    return out


def flatten(blocks, shuffle=True, seed=0):
    items = list(blocks.values())
    if shuffle:
        random.Random(seed).shuffle(items)
    return items


def anchor_index(stride, col, row, anchor=0):
    fw = NET // stride
    return (row * fw + col) * scrfd.NUM_ANCHORS + anchor


def plant(blocks, stride, col, row, score, half_cells, kps_offsets=None):
    """Write one face at grid cell (col,row); box half-side = half_cells*stride."""
    i = anchor_index(stride, col, row)
    blocks[(stride, "scores")][i, 0] = score
    blocks[(stride, "bboxes")][i, :] = half_cells
    offs = kps_offsets or [(-0.5, -0.5), (0.5, -0.5), (0.0, 0.0),
                           (-0.4, 0.5), (0.4, 0.5)]
    for k, (dx, dy) in enumerate(offs):
        blocks[(stride, "kps")][i, k * 2] = dx
        blocks[(stride, "kps")][i, k * 2 + 1] = dy
    return i


IDENTITY = LetterboxInfo(scale=1.0, pad_w=0.0, pad_h=0.0, orig_w=NET, orig_h=NET)


class TestMapOutputsByShape:
    def test_shape_mapping_is_order_independent(self):
        blocks = blank_outputs()
        for seed in range(5):
            mapped = scrfd.map_outputs_by_shape(flatten(blocks, seed=seed))
            for s, n in ROWS.items():
                assert mapped[s]["scores"].shape == (n, 1)
                assert mapped[s]["bboxes"].shape == (n, 4)
                assert mapped[s]["kps"].shape == (n, 10)

    def test_row_count_picks_the_stride(self):
        """12800/3200/800 rows -> stride 8/16/32, largest first."""
        blocks = blank_outputs()
        blocks[(8, "scores")][0, 0] = 0.9
        blocks[(32, "scores")][0, 0] = 0.8
        mapped = scrfd.map_outputs_by_shape(flatten(blocks, seed=3))
        assert mapped[8]["scores"][0, 0] == pytest.approx(0.9)
        assert mapped[32]["scores"][0, 0] == pytest.approx(0.8)

    def test_wrong_output_count_is_an_error(self):
        with pytest.raises(ValueError):
            scrfd.map_outputs_by_shape(flatten(blank_outputs())[:8])

    def test_unknown_column_count_is_an_error(self):
        outs = flatten(blank_outputs(), shuffle=False)
        outs[0] = np.zeros((800, 7), dtype=np.float32)
        with pytest.raises(ValueError):
            scrfd.map_outputs_by_shape(outs)


class TestDecode:
    def test_box_and_landmarks_land_where_the_anchor_says(self):
        blocks = blank_outputs()
        plant(blocks, 32, col=4, row=6, score=0.9, half_cells=2.0)
        dets = scrfd.decode(flatten(blocks, seed=1), IDENTITY,
                            conf_thres=0.5, iou_thres=0.4, model_size=NET)
        assert len(dets) == 1
        ax, ay = (4 + 0.5) * 32, (6 + 0.5) * 32          # 144, 208
        half = 2.0 * 32                                   # 64 px
        assert dets[0]["box"] == pytest.approx(
            [ax - half, ay - half, ax + half, ay + half])
        assert dets[0]["score"] == pytest.approx(0.9)
        # kps deltas are in STRIDE units, same as the box deltas.
        assert dets[0]["kps"][0] == pytest.approx((ax - 0.5 * 32, ay - 0.5 * 32))
        assert dets[0]["kps"][2] == pytest.approx((ax, ay))

    def test_letterbox_padding_is_removed(self):
        """640x480 camera -> 640 letterbox pads 80 px top/bottom; decode undoes it."""
        info = LetterboxInfo(scale=1.0, pad_w=0.0, pad_h=80.0,
                             orig_w=640, orig_h=480)
        blocks = blank_outputs()
        plant(blocks, 32, col=4, row=6, score=0.9, half_cells=2.0)
        d = scrfd.decode(flatten(blocks, seed=2), info, conf_thres=0.5)[0]
        assert d["box"] == pytest.approx([80.0, 144.0 - 80.0, 208.0, 272.0 - 80.0])
        assert all(0 <= p[1] <= 480 for p in d["kps"])

    def test_confidence_threshold_drops_weak_anchors(self):
        blocks = blank_outputs()
        plant(blocks, 32, col=4, row=6, score=0.9, half_cells=2.0)
        plant(blocks, 32, col=14, row=6, score=0.3, half_cells=2.0)
        assert len(scrfd.decode(flatten(blocks), IDENTITY, conf_thres=0.5)) == 1
        assert len(scrfd.decode(flatten(blocks), IDENTITY, conf_thres=0.2)) == 2

    def test_nms_merges_the_same_face_across_strides(self):
        """One face fires on stride 16 and 32; NMS keeps the higher score once."""
        blocks = blank_outputs()
        plant(blocks, 32, col=4, row=6, score=0.9, half_cells=2.0)
        plant(blocks, 16, col=9, row=13, score=0.7, half_cells=4.0)
        raw = scrfd.decode(flatten(blocks), IDENTITY, conf_thres=0.5,
                           iou_thres=0.99)
        merged = scrfd.decode(flatten(blocks), IDENTITY, conf_thres=0.5,
                              iou_thres=0.3)
        assert len(raw) == 2
        assert len(merged) == 1
        assert merged[0]["score"] == pytest.approx(0.9)

    def test_results_are_score_descending(self):
        blocks = blank_outputs()
        plant(blocks, 32, col=2, row=2, score=0.6, half_cells=1.0)
        plant(blocks, 32, col=14, row=6, score=0.95, half_cells=1.0)
        plant(blocks, 32, col=8, row=15, score=0.75, half_cells=1.0)
        scores = [d["score"] for d in scrfd.decode(flatten(blocks), IDENTITY,
                                                   conf_thres=0.5)]
        assert scores == sorted(scores, reverse=True)

    def test_empty_when_nothing_fires(self):
        assert scrfd.decode(flatten(blank_outputs()), IDENTITY,
                            conf_thres=0.5) == []


class TestNms:
    def test_keeps_the_best_of_an_overlapping_pair(self):
        boxes = np.array([[0, 0, 10, 10], [1, 1, 11, 11], [50, 50, 60, 60]],
                         dtype=np.float32)
        scores = np.array([0.5, 0.9, 0.7], dtype=np.float32)
        assert scrfd.nms(boxes, scores, 0.3) == [1, 2]

    def test_empty_input(self):
        assert scrfd.nms(np.zeros((0, 4), np.float32),
                         np.zeros((0,), np.float32), 0.5) == []
