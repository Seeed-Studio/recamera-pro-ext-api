"""Unit tests for the pure reductions in `depth_map.py`.

No camera, no NPU, no app -- just numpy in, numbers out.
"""
import base64
import struct
import zlib

import numpy as np
import pytest

import depth_map as dm


class Info:
    """Stand-in for kit's LetterboxInfo (same four attributes it exposes)."""

    def __init__(self, scale, pad_w, pad_h, orig_w, orig_h):
        self.scale = scale
        self.pad_w = pad_w
        self.pad_h = pad_h
        self.orig_w = orig_w
        self.orig_h = orig_h


# 1280x720 letterboxed into 256x256: scale 0.2, content 256x144, 56 px bars.
WIDE = Info(scale=0.2, pad_w=0, pad_h=56, orig_w=1280, orig_h=720)


# --------------------------------------------------------------------------- #
# valid region / coordinate mapping
# --------------------------------------------------------------------------- #
def test_valid_region_excludes_the_letterbox_bars():
    assert dm.valid_region((256, 256), WIDE) == (0, 56, 256, 200)


def test_valid_region_degenerate_info_keeps_whole_map():
    assert dm.valid_region((256, 256), Info(0, 0, 0, 0, 0)) == (0, 0, 256, 256)


def test_to_original_inverts_the_letterbox():
    assert dm.to_original(0, 56, WIDE) == (0.0, 0.0)
    x, y = dm.to_original(256, 200, WIDE)
    assert (round(x), round(y)) == (1280, 720)


# --------------------------------------------------------------------------- #
# statistics / proximity
# --------------------------------------------------------------------------- #
def test_frame_stats_percentiles():
    v = np.arange(101, dtype=np.float32)
    st = dm.frame_stats(v)
    assert st["min"] == 0.0 and st["max"] == 100.0
    assert st["mean"] == pytest.approx(50.0)
    assert st["p5"] == pytest.approx(5.0)
    assert st["p95"] == pytest.approx(95.0)


def test_frame_stats_ignores_nan_and_empty():
    st = dm.frame_stats(np.array([1.0, np.nan, 3.0]))
    assert st["min"] == 1.0 and st["max"] == 3.0
    assert dm.frame_stats(np.zeros(0)) == {
        "min": 0.0, "max": 0.0, "mean": 0.0, "p5": 0.0, "p95": 0.0}


def test_proximity_is_a_clipped_p5_p95_remap():
    d = np.array([[0.0, 5.0, 50.0, 95.0, 100.0]], dtype=np.float32)
    p = dm.proximity(d, 5.0, 95.0)
    assert p.tolist() == [[0.0, 0.0, 0.5, 1.0, 1.0]]


def test_proximity_of_a_flat_map_is_all_zero():
    p = dm.proximity(np.full((4, 4), 7.0), 7.0, 7.0)
    assert p.shape == (4, 4) and not p.any()


def test_proximity_is_scale_and_offset_free():
    """The map is relative: an affine change of the raw units must not move it."""
    d = np.random.RandomState(0).rand(32, 32).astype(np.float32)
    a = dm.proximity(d, *np.percentile(d, (5, 95)))
    e = d * 3.7 + 11.0
    b = dm.proximity(e, *np.percentile(e, (5, 95)))
    assert np.allclose(a, b, atol=1e-5)


# --------------------------------------------------------------------------- #
# grid
# --------------------------------------------------------------------------- #
def _ramp(h=120, w=160):
    """Proximity that grows left to right: the right column is the nearest."""
    return np.tile(np.linspace(0.0, 1.0, w, dtype=np.float32), (h, 1))


def test_grid_shape_and_full_cover():
    cells = dm.grid_cells(_ramp(), rows=3, cols=4)
    assert len(cells) == 3 and all(len(r) == 4 for r in cells)
    # cut points tile the extent exactly: no gap, no overlap
    assert [c["x0"] for c in cells[0]] == [0, 40, 80, 120]
    assert [c["x1"] for c in cells[0]] == [40, 80, 120, 160]
    assert [r[0]["y0"] for r in cells] == [0, 40, 80]
    assert cells[-1][-1]["y1"] == 120


def test_grid_means_increase_left_to_right_and_match_rows():
    cells = dm.grid_cells(_ramp(), rows=3, cols=4)
    means = [[round(c["mean"], 3) for c in row] for row in cells]
    assert means[0] == means[1] == means[2]          # ramp has no vertical structure
    assert means[0] == sorted(means[0]) and means[0][0] < means[0][-1]


def test_grid_near_uses_the_percentile_not_the_mean():
    cells = dm.grid_cells(_ramp(), rows=1, cols=2, near_percentile=95.0)
    right = cells[0][1]
    assert right["near"] > right["mean"]
    low = dm.grid_cells(_ramp(), rows=1, cols=2, near_percentile=50.0)[0][1]
    assert low["near"] < right["near"]


def test_grid_never_collapses_a_cell_when_asked_for_more_cells_than_pixels():
    cells = dm.grid_cells(np.zeros((2, 2), np.float32), rows=4, cols=4)
    assert len(cells) == 4 and all(len(r) == 4 for r in cells)
    for row in cells:
        for c in row:
            assert c["x1"] > c["x0"] and c["y1"] > c["y0"]


def test_label_buckets():
    assert dm.label_of(0.9) == "near"
    assert dm.label_of(0.5) == "mid"
    assert dm.label_of(0.1) == "far"
    assert dm.label_of(dm.NEAR_CUT) == "near"
    assert dm.label_of(dm.MID_CUT) == "mid"


# --------------------------------------------------------------------------- #
# ROI config parsing
# --------------------------------------------------------------------------- #
def test_parse_rois_accepts_json_string_list_and_dicts():
    assert dm.parse_rois('[[0.1,0.2,0.3,0.4]]') == [[0.1, 0.2, 0.3, 0.4]]
    assert dm.parse_rois([[0, 0, 1, 1]]) == [[0.0, 0.0, 1.0, 1.0]]
    assert dm.parse_rois([{"x": 0, "y": 0, "w": 0.5, "h": 0.5}]) == [[0, 0, 0.5, 0.5]]


def test_parse_rois_drops_garbage_without_raising():
    assert dm.parse_rois("") == []
    assert dm.parse_rois("not json") == []
    assert dm.parse_rois([[1, 2, 3]]) == []              # wrong arity
    assert dm.parse_rois([[0, 0, 0, 0.5]]) == []         # zero width
    assert dm.parse_rois([["a", "b", "c", "d"]]) == []


# --------------------------------------------------------------------------- #
# debug depth map (publish_map)
# --------------------------------------------------------------------------- #
def _png_pixels(blob):
    """Decode the greyscale PNG the app writes, without PIL."""
    assert blob[:8] == b"\x89PNG\r\n\x1a\n"
    pos, idat, hdr = 8, b"", None
    while pos < len(blob):
        (ln,) = struct.unpack(">I", blob[pos:pos + 4])
        tag = blob[pos + 4:pos + 8]
        body = blob[pos + 8:pos + 8 + ln]
        assert struct.unpack(">I", blob[pos + 8 + ln:pos + 12 + ln])[0] == \
            zlib.crc32(tag + body) & 0xFFFFFFFF, tag
        if tag == b"IHDR":
            hdr = struct.unpack(">IIBBBBB", body)
        elif tag == b"IDAT":
            idat += body
        pos += 12 + ln
    w, h, depth, ctype = hdr[0], hdr[1], hdr[2], hdr[3]
    assert (depth, ctype) == (8, 0)
    raw = zlib.decompress(idat)
    rows = []
    for y in range(h):
        line = raw[y * (w + 1):(y + 1) * (w + 1)]
        assert line[0] == 0                      # filter type None
        rows.append(list(line[1:]))
    return w, h, np.array(rows, dtype=np.uint8)


def test_downsample_keeps_the_gradient_direction():
    small = dm.downsample(_ramp(), 8, 4)
    assert small.shape == (4, 8)
    assert small[0, 0] == 0 and small[0, -1] == 255
    assert list(small[0]) == sorted(small[0])


def test_depth_map_payload_is_a_decodable_png():
    p = dm.depth_map_payload(_ramp())
    assert (p["w"], p["h"], p["format"], p["encoding"]) == (64, 48, "png", "base64")
    w, h, px = _png_pixels(base64.b64decode(p["data"]))
    assert (w, h) == (64, 48) and px.shape == (48, 64)
    assert px[0, 0] == 0 and px[0, -1] == 255


def test_encode_png_round_trips_exact_pixel_values():
    src = np.arange(256, dtype=np.uint8).reshape(16, 16)
    _, _, px = _png_pixels(dm.encode_png_gray(src))
    assert (px == src).all()


def test_encode_png_rejects_a_non_2d_array():
    with pytest.raises(ValueError):
        dm.encode_png_gray(np.zeros((4, 4, 3), np.uint8))


# --------------------------------------------------------------------------- #
# the nearest-rank percentile that replaced np.percentile (device cost)
# --------------------------------------------------------------------------- #
def test_percentile_matches_numpy_nearest_rank():
    rng = np.random.RandomState(3)
    v = rng.rand(997).astype(np.float32)
    for q in (0, 5, 25, 50, 75, 95, 100):
        k = int(q / 100.0 * (v.size - 1) + 0.5)
        assert dm.percentile(v, q) == pytest.approx(np.sort(v)[k])


def test_percentile_stays_within_the_sample_range_and_handles_edges():
    v = np.array([2.0, 9.0, 4.0], dtype=np.float32)
    assert dm.percentile(v, 0) == 2.0
    assert dm.percentile(v, 100) == 9.0
    assert dm.percentile(v, -5) == 2.0        # clipped, not an index error
    assert dm.percentile(v, 500) == 9.0
    assert dm.percentile(np.zeros(0), 50) == 0.0


def test_percentile_does_not_reorder_the_callers_array():
    v = np.array([3.0, 1.0, 2.0], dtype=np.float32)
    dm.percentile(v, 50)
    assert v.tolist() == [3.0, 1.0, 2.0]
