"""ArcFace 5-point alignment: the similarity transform and the 112x112 chip."""
import numpy as np
import pytest

import align


def rot_scale_shift(pts, deg, s, tx, ty):
    th = np.deg2rad(deg)
    R = np.array([[np.cos(th), -np.sin(th)],
                  [np.sin(th), np.cos(th)]], dtype=np.float32)
    return (pts @ R.T) * s + np.array([tx, ty], dtype=np.float32)


class TestUmeyama:
    def test_recovers_a_known_similarity(self):
        """src = R,s,t applied to the template -> umeyama must invert it."""
        src = rot_scale_shift(align.ARCFACE_DST, deg=17.0, s=1.8,
                              tx=250.0, ty=-40.0).astype(np.float32)
        M = align.umeyama(src, align.ARCFACE_DST)[:2, :]
        back = (np.c_[src, np.ones(5, np.float32)] @ M.T)
        assert back == pytest.approx(align.ARCFACE_DST, abs=1e-2)

    def test_identity_when_src_is_the_template(self):
        M = align.umeyama(align.ARCFACE_DST, align.ARCFACE_DST)[:2, :]
        assert M == pytest.approx(np.array([[1, 0, 0], [0, 1, 0]], np.float32),
                                  abs=1e-3)

    def test_degenerate_input_yields_nan_not_an_exception(self):
        """All five points coincident: rank-0 covariance, same as skimage."""
        src = np.zeros((5, 2), dtype=np.float32)
        assert np.isnan(align.umeyama(src, align.ARCFACE_DST)).all()

    def test_intermediate_math_stays_in_float32(self):
        """★Do not upcast★ -- a float64 path shifts warped pixels by 1 LSB,
        which a quantized embedder turns into a ~0.0025 cosine drift against
        already-enrolled vectors. Pinning the f32/f64 difference proves the
        input dtype is actually what drives the math."""
        src = rot_scale_shift(align.ARCFACE_DST, 11.0, 1.3, 90.0, 20.0)
        m32 = align.umeyama(src.astype(np.float32), align.ARCFACE_DST)
        m64 = align.umeyama(src.astype(np.float64),
                            align.ARCFACE_DST.astype(np.float64))
        assert m32 == pytest.approx(m64, abs=1e-4)
        assert not np.array_equal(m32, m64)


class TestAlignFace:
    def _frame(self, w=640, h=480):
        yy, xx = np.mgrid[0:h, 0:w]
        img = np.stack([(xx % 256), (yy % 256), ((xx + yy) % 256)], axis=-1)
        return img.astype(np.uint8)

    def test_output_is_112x112_rgb_uint8(self):
        frame = self._frame()
        kps = align.ARCFACE_DST + np.array([200.0, 150.0], np.float32)
        chip = align.align_face(frame, kps)
        assert chip.shape == (112, 112, 3)
        assert chip.dtype == np.uint8

    def test_template_landmarks_reproduce_the_top_left_crop(self):
        """Landmarks already AT the template -> the warp is a pure identity, so
        the chip is the image's own top-left 112x112 corner."""
        frame = self._frame()
        chip = align.align_face(frame, align.ARCFACE_DST)
        assert np.array_equal(chip, frame[:112, :112])

    def test_a_rotated_face_is_warped_back_upright(self):
        """The landmarks of a scaled/rotated/shifted face map onto the template
        positions in the chip -- that is what makes two crops comparable."""
        frame = self._frame()
        src = rot_scale_shift(align.ARCFACE_DST, deg=20.0, s=1.7,
                              tx=260.0, ty=170.0).astype(np.float32)
        M = align.umeyama(src, align.ARCFACE_DST)[:2, :]
        mapped = np.c_[src, np.ones(5, np.float32)] @ M.T
        assert mapped == pytest.approx(align.ARCFACE_DST, abs=1e-2)
        assert align.align_face(frame, src).shape == (112, 112, 3)

    def test_accepts_a_list_of_tuples(self):
        frame = self._frame()
        kps = [(float(x), float(y)) for x, y in align.ARCFACE_DST + 100.0]
        assert align.align_face(frame, kps).shape == (112, 112, 3)
