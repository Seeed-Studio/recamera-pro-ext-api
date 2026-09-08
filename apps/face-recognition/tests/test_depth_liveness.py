"""Planarity scoring: synthetic plane vs synthetic sphere.

The two fixtures are the two cases the depth head exists to separate — a flat
sheet (printed photo / phone screen) and a bulging surface (a face) — reduced to
geometry so the assertions do not depend on any model.
"""
import numpy as np
import pytest

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
