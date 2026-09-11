"""Planarity scoring: synthetic plane vs synthetic sphere.

The two fixtures are the two cases the depth head exists to separate — a flat
sheet (printed photo / phone screen) and a bulging surface (a face) — reduced to
geometry so the assertions do not depend on any model.
"""
import numpy as np
import pytest
from concurrent.futures import ThreadPoolExecutor
import weakref

import depth_liveness as dl


SIZE = 256
BOX = (64, 64, 128, 128)          # x, y, w, h — centre half of the map


def _grid(size=SIZE):
    y, x = np.mgrid[0:size, 0:size].astype(np.float64)
    return (2 * x / (size - 1) - 1.0), (2 * y / (size - 1) - 1.0)


def synthetic_plane(tilt_x=0.7, tilt_y=-0.3, offset=5.0, size=SIZE):
    x, y = _grid(size)
    return tilt_x * x + tilt_y * y + offset


def synthetic_sphere(radius=1.6, size=SIZE):
    """Depth of a sphere cap over the grid, plus a mild tilt.

    The tilt makes the test meaningful: a scorer that looked at raw depth range
    instead of the plane residual would rank this the same as the plane.
    """
    x, y = _grid(size)
    r2 = np.clip(radius ** 2 - x ** 2 - y ** 2, 0.0, None)
    return np.sqrt(r2) + 0.4 * x - 0.2 * y


def test_plane_is_planar():
    res = dl.planarity_from_depth(synthetic_plane(), BOX)
    assert res["residual_ratio"] < 1e-9
    assert res["planarity"] == pytest.approx(1.0)
    assert res["relief"] < 1e-8
    assert res["score"] == pytest.approx(0.0)


def test_sphere_is_not_planar():
    res = dl.planarity_from_depth(synthetic_sphere(), BOX)
    # Residual is normalised by the frame's p95-p5 depth range, so the absolute
    # number is small; what matters is that it clears the full-scale constant.
    assert res["residual_ratio"] > 0.01
    assert res["planarity"] < 0.95
    assert res["score"] > 0.05
    assert res["relief"] > res["residual_ratio"]


def test_sphere_scores_more_live_than_plane():
    flat = dl.planarity_from_depth(synthetic_plane(), BOX)
    bump = dl.planarity_from_depth(synthetic_sphere(), BOX)
    assert bump["score"] > flat["score"]
    assert bump["relief"] > flat["relief"]


def test_scale_and_sign_invariance():
    """Relative-depth heads have arbitrary scale/offset and may invert sign."""
    d = synthetic_sphere()
    base = dl.planarity_from_depth(d, BOX)
    for factor, shift in ((37.0, -12.0), (-1.0, 0.0), (0.01, 1000.0)):
        alt = dl.planarity_from_depth(d * factor + shift, BOX)
        assert alt["residual_ratio"] == pytest.approx(base["residual_ratio"], rel=1e-6)
        assert alt["relief"] == pytest.approx(base["relief"], rel=1e-6)


def test_tilt_invariance():
    """A photo held at an angle is still a plane."""
    for tx, ty in ((0.0, 0.0), (3.0, 0.0), (-2.0, 5.0)):
        res = dl.planarity_from_depth(synthetic_plane(tx, ty), BOX)
        assert res["planarity"] == pytest.approx(1.0)


def test_bbox_is_mapped_from_image_space():
    """A bbox measured on a 640x640 frame lands on the same 256x256 region."""
    d = synthetic_sphere()
    native = dl.planarity_from_depth(d, BOX)
    scaled = dl.planarity_from_depth(
        d, tuple(v * 640 / SIZE for v in BOX), image_size=(640, 640)
    )
    assert scaled["box"] == native["box"]
    assert scaled["residual_ratio"] == pytest.approx(native["residual_ratio"], rel=1e-9)


def test_constant_depth_is_planar():
    res = dl.planarity_from_depth(np.full((SIZE, SIZE), 3.0), BOX)
    assert res["planarity"] == 1.0
    assert res["score"] == 0.0


def test_too_few_samples_returns_nan():
    res = dl.planarity_from_depth(synthetic_sphere(size=8), (2, 2, 3, 3))
    assert res["n_samples"] < dl.MIN_SAMPLES
    assert np.isnan(res["planarity"])
    assert np.isnan(res["score"])


def test_nan_holes_are_ignored():
    d = synthetic_plane()
    d[100:110, 100:110] = np.nan
    res = dl.planarity_from_depth(d, BOX)
    assert res["n_samples"] < 96 * 96
    assert res["planarity"] == pytest.approx(1.0)


@pytest.mark.parametrize("bad", [(0, 0, 0, 10), (0, 0, 10, -1)])
def test_invalid_bbox_raises(bad):
    with pytest.raises(ValueError):
        dl.planarity_from_depth(synthetic_plane(), bad)


def test_non_2d_depth_raises():
    with pytest.raises(ValueError):
        dl.planarity_from_depth(np.zeros((2, 8, 8)), BOX)


def test_depth_flatness_without_model_is_none():
    dl.set_model(None)
    assert dl.available() is False
    assert dl.depth_flatness(np.zeros((64, 64, 3), np.uint8), (8, 8, 40, 40)) is None


def test_depth_flatness_with_fake_model():
    class FakeDepthModel:
        input_size = SIZE

        def infer(self, x):
            assert x.shape == (1, SIZE, SIZE, 3) and x.dtype == np.uint8
            return [synthetic_sphere()[None]]

    dl.set_model(FakeDepthModel())
    try:
        frame = np.zeros((480, 640, 3), np.uint8)
        res = dl.depth_flatness(frame, (160, 120, 480, 360))
        assert res is not None
        assert 0.0 <= res["planarity"] <= 1.0
        assert res["score"] == pytest.approx(1.0 - res["planarity"])
        assert res["score"] > 0.0
    finally:
        dl.set_model(None)


class CountingDepthModel:
    input_size = SIZE

    def __init__(self):
        self.calls = 0
        self.inputs = []
        self.failures = 0
        self.invalid_outputs = 0
        self.output_refs = []

    def infer(self, x):
        self.calls += 1
        self.inputs.append(x.copy())
        if self.failures:
            self.failures -= 1
            raise RuntimeError("temporary depth inference failure")
        if self.invalid_outputs:
            self.invalid_outputs -= 1
            return [np.zeros(3)]
        output = synthetic_sphere()
        self.output_refs.append(weakref.ref(output))
        return [output[None]]


@pytest.fixture
def counted_depth(monkeypatch):
    model = CountingDepthModel()
    monkeypatch.setattr(dl, "_MODEL", model)
    return model


def _textured_frame():
    yy, xx = np.mgrid[:480, :640]
    return np.stack((xx % 256, yy % 256, (xx + yy) % 256), axis=-1).astype(np.uint8)


def test_scope_reuses_one_depth_map_with_unchanged_pixels_and_distinct_roi_scores(counted_depth):
    frame = _textured_frame()
    boxes = ((80, 60, 240, 240), (320, 160, 560, 420))
    expected = [dl.depth_flatness(frame, box) for box in boxes]
    with dl.reuse_frame_depth(frame):
        actual = [dl.depth_flatness(frame, box) for box in boxes]
        assert counted_depth.calls == 3  # two legacy calls, one shared inference
    assert actual == expected
    assert actual[0]["box"] != actual[1]["box"]
    ys = np.linspace(0, 479, SIZE).astype(np.int32)
    xs = np.linspace(0, 639, SIZE).astype(np.int32)
    expected_input = frame[ys][:, xs][None].astype(np.uint8)
    for value in counted_depth.inputs:
        np.testing.assert_array_equal(value, expected_input)
    # The scope releases the successful map without waiting for another frame.
    assert all(ref() is None for ref in counted_depth.output_refs)


def test_each_frame_scope_runs_again_even_if_camera_reuses_the_same_array(counted_depth):
    frame = _textured_frame()
    for _ in range(2):
        with dl.reuse_frame_depth(frame):
            dl.depth_flatness(frame, (80, 60, 240, 240))
            dl.depth_flatness(frame, (320, 160, 560, 420))
        frame[:] = 0
    assert counted_depth.calls == 2
    assert np.any(counted_depth.inputs[0])
    assert not np.any(counted_depth.inputs[1])


def test_different_frame_or_model_never_uses_the_scoped_map(counted_depth, monkeypatch):
    frame = _textured_frame()
    other = frame.copy()
    replacement = CountingDepthModel()
    with dl.reuse_frame_depth(frame):
        dl.depth_flatness(frame, (80, 60, 240, 240))
        dl.depth_flatness(other, (80, 60, 240, 240))
        dl.depth_flatness(other, (80, 60, 240, 240))
        assert counted_depth.calls == 3
        monkeypatch.setattr(dl, "_MODEL", replacement)
        dl.depth_flatness(frame, (80, 60, 240, 240))
        dl.depth_flatness(frame, (80, 60, 240, 240))
        assert replacement.calls == 1


@pytest.mark.parametrize("failure", ["failures", "invalid_outputs"])
def test_failed_inference_or_invalid_output_does_not_block_same_frame_retry(counted_depth, failure):
    frame = _textured_frame()
    setattr(counted_depth, failure, 1)
    with dl.reuse_frame_depth(frame):
        with pytest.raises((RuntimeError, ValueError)):
            dl.depth_flatness(frame, (80, 60, 240, 240))
        assert dl.depth_flatness(frame, (320, 160, 560, 420)) is not None
        dl.depth_flatness(frame, (80, 60, 240, 240))
    assert counted_depth.calls == 2


def test_missing_model_and_invalid_box_still_skip_inference(counted_depth, monkeypatch):
    frame = _textured_frame()
    with dl.reuse_frame_depth(frame):
        assert dl.depth_flatness(frame, (1, 2, 1, 3)) is None
        assert counted_depth.calls == 0
        monkeypatch.setattr(dl, "_MODEL", None)
        assert dl.depth_flatness(frame, (80, 60, 240, 240)) is None
        monkeypatch.setattr(dl, "_MODEL", counted_depth)
        assert dl.depth_flatness(frame, (80, 60, 240, 240)) is not None
    assert counted_depth.calls == 1


def test_scope_does_not_retain_the_frame(counted_depth):
    frame = _textured_frame()
    ref = weakref.ref(frame)
    with dl.reuse_frame_depth(frame):
        dl.depth_flatness(frame, (80, 60, 240, 240))
        del frame
        assert ref() is None


def test_scope_is_reset_on_exception_and_does_not_leak_to_other_threads(counted_depth):
    frame = _textured_frame()
    with pytest.raises(RuntimeError, match="leave loop"):
        with dl.reuse_frame_depth(frame):
            dl.depth_flatness(frame, (80, 60, 240, 240))
            with ThreadPoolExecutor(max_workers=1) as executor:
                for _ in range(2):
                    executor.submit(dl.depth_flatness, frame, (80, 60, 240, 240)).result()
            assert counted_depth.calls == 3
            raise RuntimeError("leave loop")
    dl.depth_flatness(frame, (80, 60, 240, 240))
    assert counted_depth.calls == 4
