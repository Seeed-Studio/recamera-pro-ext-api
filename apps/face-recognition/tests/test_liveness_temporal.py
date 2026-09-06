"""Temporal liveness: motion residual, blink state machine, fusion.

All synthetic — five-point sequences and EAR sequences are constructed by hand,
so every assertion is about the algebra and the state machine rather than about
a model. No rknn, no frames.
"""
import math

import numpy as np
import pytest

import liveness_temporal as lt

# A plausible five-point face (SCRFD order: eyes, nose, mouth corners) at ~120 px
BASE = np.array([
    [100.0, 100.0],     # left eye
    [160.0, 100.0],     # right eye
    [130.0, 135.0],     # nose
    [108.0, 170.0],     # left mouth
    [152.0, 170.0],     # right mouth
])
FACE_PX = 120.0


def _cfg(**kw):
    c = lt.LivenessConfig()
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def _similarity(pts, scale=1.0, deg=0.0, tx=0.0, ty=0.0):
    r = math.radians(deg)
    m = np.array([[math.cos(r), -math.sin(r)], [math.sin(r), math.cos(r)]])
    return (pts @ m.T) * scale + np.array([tx, ty])


def _feed(seq, cfg=None, dt=0.1, face_px=FACE_PX):
    """Push a sequence of five-point sets, return the LAST motion tuple."""
    cfg = cfg or _cfg()
    st = lt.LivenessState()
    out = (None, None, None)
    for i, pts in enumerate(seq):
        out = lt.update_motion(st, i * dt, pts, face_px, cfg)
    return st, out


class TestMotionResidual:
    def test_pure_rigid_motion_leaves_almost_no_residual(self):
        """★The whole point★ a photo waved at the lens is a rigid object: the
        similarity fit explains it and nothing survives."""
        seq = [_similarity(BASE, scale=1.0 + 0.02 * i, deg=3.0 * i,
                           tx=5.0 * i, ty=-2.0 * i) for i in range(8)]
        _st, (rms, corr, score) = _feed(seq)
        assert rms is not None
        assert rms < 1e-6
        assert score is None          # below the noise floor -> no evidence

    def test_static_face_plus_detector_noise_stays_at_the_noise_floor(self):
        rng = np.random.default_rng(7)
        seq = [BASE + rng.normal(0.0, 0.25, BASE.shape) for _ in range(10)]
        _st, (rms, corr, score) = _feed(seq)
        # 0.25 px jitter on a 120 px face is ~0.002 in normalized units
        assert 0.0 < rms < 0.006
        assert abs(corr) < 0.5        # white noise: uncorrelated pair to pair

    def test_local_deformation_survives_the_similarity_fit(self):
        """A mouth opening is not a similarity transform of the previous face,
        so it is exactly what the residual is supposed to keep."""
        seq = []
        for i in range(8):
            p = BASE.copy()
            p[3:, 1] += 6.0 * i        # mouth corners drop: a yawn
            seq.append(p)
        _st, (rms, corr, score) = _feed(seq)
        assert rms > 0.01
        assert score is not None and score > 0.0

    def test_deformation_beats_rigid_motion_of_the_same_magnitude(self):
        rigid = [_similarity(BASE, tx=6.0 * i, ty=6.0 * i) for i in range(8)]
        deform = []
        for i in range(8):
            p = BASE.copy()
            p[3:, 1] += 6.0 * i
            deform.append(p)
        _s1, (r_rigid, _c, _m) = _feed(rigid)
        _s2, (r_def, _c2, _m2) = _feed(deform)
        assert r_def > 100 * r_rigid

    def test_residual_is_scale_free(self):
        """Same gesture on a 2x bigger face must give the same residual."""
        small, big = [], []
        for i in range(6):
            p = BASE.copy()
            p[3:, 1] += 4.0 * i
            small.append(p)
            big.append(p * 2.0)
        _s1, (r_small, _a, _b) = _feed(small, face_px=FACE_PX)
        _s2, (r_big, _c, _d) = _feed(big, face_px=2 * FACE_PX)
        assert r_small == pytest.approx(r_big, rel=1e-9)

    def test_too_few_observations_report_nothing(self):
        st = lt.LivenessState()
        cfg = _cfg()
        assert lt.update_motion(st, 0.0, BASE, FACE_PX, cfg) == (None, None, None)
        assert lt.update_motion(st, 0.1, BASE, FACE_PX, cfg) == (None, None, None)

    def test_the_window_is_time_based_not_frame_based(self):
        """A timestamp gap evicts the history: two observations 5 s apart are
        not a 5 s-long window of evidence."""
        cfg = _cfg(motion_window_sec=1.0)
        st = lt.LivenessState()
        for i in range(6):
            lt.update_motion(st, i * 0.1, BASE, FACE_PX, cfg)
        assert len(st.keypoints) == 6
        lt.update_motion(st, 60.0, BASE, FACE_PX, cfg)
        assert len(st.keypoints) == 1
        assert st.motion_residual is None

    def test_degenerate_coincident_points_do_not_raise(self):
        seq = [np.zeros((5, 2)) for _ in range(5)]
        _st, out = _feed(seq)
        assert out == (None, None, None)


class TestBlink:
    def _run(self, ears, cfg=None):
        cfg = cfg or _cfg()
        st = lt.LivenessState()
        fired = [lt.update_blink(st, e, cfg.ear_threshold,
                                 cfg.blink_min_samples, cfg.blink_max_samples)
                 for e in ears]
        return st, fired

    @pytest.mark.parametrize("n_closed", [1, 2, 3])
    def test_a_one_to_three_sample_dip_is_a_blink(self, n_closed):
        st, fired = self._run([0.30] + [0.10] * n_closed + [0.30])
        assert any(fired)
        assert st.blink_seen is True

    def test_an_overlong_closure_is_not_a_blink(self):
        """Someone with their eyes shut is not blinking, and a face that walked
        out of frame is not blinking either."""
        st, fired = self._run([0.30] + [0.10] * 8 + [0.30])
        assert not any(fired)
        assert st.blink_seen is False

    def test_a_long_closure_cannot_be_salvaged_by_its_tail(self):
        """The counter must not reset mid-closure, or the last three closed
        samples of a ten-sample closure would look like a blink."""
        cfg = _cfg(blink_max_samples=3)
        st, fired = self._run([0.30] + [0.10] * 10 + [0.30], cfg)
        assert not any(fired)

    def test_missing_samples_do_not_reset_a_dip_in_progress(self):
        """None means 'FaceMesh did not run this frame', not 'eye is open'."""
        st, fired = self._run([0.30, 0.10, None, None, 0.10, 0.30])
        assert any(fired)
        assert st.blink_seen is True

    def test_blink_latches_for_the_lifetime_of_the_track(self):
        st, _fired = self._run([0.30, 0.10, 0.30] + [0.30] * 20)
        assert st.blink_seen is True

    def test_a_never_closed_eye_never_fires(self):
        st, fired = self._run([0.30] * 10)
        assert not any(fired)
        assert st.closed_samples == 0


class TestFusion:
    def _state(self, tex, samples, cfg):
        st = lt.LivenessState()
        for _ in range(samples):
            st.update_texture(tex, 1.0)      # alpha 1 -> ema == tex exactly
        return st

    def test_pending_until_three_texture_samples(self):
        cfg = _cfg()
        st = lt.LivenessState()
        for i in range(2):
            st.update_texture(0.99, 1.0)
            out = lt.fuse_liveness(st, 1.0, 0.0, 0.9, None, cfg)
            assert out["decision"] == "pending"
            assert out["reason"] == "insufficient_samples"
        st.update_texture(0.99, 1.0)
        out = lt.fuse_liveness(st, 1.0, 0.0, 0.9, None, cfg)
        assert out["decision"] == "live"

    def test_a_blink_decides_live_immediately(self):
        """Even with texture sitting well under T_spoof: a blink is proof, and
        MiniFAS on a badly-lit real face is not."""
        cfg = _cfg()
        st = self._state(0.10, 3, cfg)
        st.blink_seen = True
        out = lt.fuse_liveness(st, 1.0, 0.0, None, None, cfg)
        assert out["decision"] == "live"
        assert out["reason"] == "blink"
        assert out["blink"] is True
        assert out["score"] == pytest.approx(1.0)

    def test_no_blink_alone_never_produces_a_spoof(self):
        """★The asymmetry★ absence of a blink is not evidence: people stare."""
        cfg = _cfg()
        st = self._state(0.99, 3, cfg)
        out = lt.fuse_liveness(st, 99.0, 0.0, None, None, cfg)
        assert out["blink"] is False
        assert out["decision"] == "live"

    def test_no_motion_before_the_timeout_stays_pending(self):
        cfg = _cfg(timeout_sec=2.0)
        st = self._state(0.99, 3, cfg)
        out = lt.fuse_liveness(st, 1.0, 0.0, None, None, cfg)
        assert out["decision"] == "pending"
        assert out["reason"] == "awaiting_motion"

    def test_at_timeout_texture_decides_without_a_motion_penalty(self):
        """A person standing perfectly still must not be locked out."""
        cfg = _cfg(timeout_sec=2.0)
        st = self._state(0.99, 3, cfg)
        out = lt.fuse_liveness(st, 5.0, 0.0, None, None, cfg)
        assert out["decision"] == "live"
        assert out["reason"] == "timeout_texture"
        # weights renormalised over texture alone -> the texture value itself
        assert out["score"] == pytest.approx(0.99)

    def test_weights_are_renormalised_over_the_present_terms(self):
        cfg = _cfg(w_texture=0.7, w_motion=0.3)
        st = self._state(0.8, 3, cfg)
        out = lt.fuse_liveness(st, 5.0, 0.0, 0.4, None, cfg)
        assert out["score"] == pytest.approx((0.7 * 0.8 + 0.3 * 0.4) / 1.0)

    def test_depth_joins_the_fusion_only_when_enabled(self):
        st = self._state(0.8, 3, _cfg())
        off = lt.fuse_liveness(st, 5.0, 0.0, None, 0.0, _cfg())
        st2 = self._state(0.8, 3, _cfg())
        on = lt.fuse_liveness(st2, 5.0, 0.0, None, 0.0,
                              _cfg(depth_enabled=True))
        assert off["score"] == pytest.approx(0.8)
        assert on["score"] == pytest.approx(0.7 * 0.8 / (0.7 + 0.2))
        assert on.get("depth") == {"score": 0.0}

    def test_a_low_texture_score_is_a_spoof(self):
        cfg = _cfg()
        st = self._state(0.05, 3, cfg)
        out = lt.fuse_liveness(st, 5.0, 0.0, None, None, cfg)
        assert out["decision"] == "spoof"

    def test_hysteresis_holds_a_verdict_inside_the_band(self):
        """A score drifting between T_spoof and T_live must not flap."""
        cfg = _cfg(t_live=0.65, t_spoof=0.45)
        st = self._state(0.90, 3, cfg)
        assert lt.fuse_liveness(st, 5.0, 0.0, None, None, cfg)["decision"] == "live"
        st.texture_ema = 0.55                       # inside the band
        assert lt.fuse_liveness(st, 6.0, 0.0, None, None, cfg)["decision"] == "live"
        st.texture_ema = 0.30                       # below T_spoof
        assert lt.fuse_liveness(st, 7.0, 0.0, None, None, cfg)["decision"] == "spoof"
        st.texture_ema = 0.55                       # back inside the band
        assert lt.fuse_liveness(st, 8.0, 0.0, None, None, cfg)["decision"] == "spoof"
        st.texture_ema = 0.90
        assert lt.fuse_liveness(st, 9.0, 0.0, None, None, cfg)["decision"] == "live"

    def test_a_first_verdict_inside_the_band_stays_pending(self):
        cfg = _cfg()
        st = self._state(0.55, 3, cfg)
        out = lt.fuse_liveness(st, 5.0, 0.0, None, None, cfg)
        assert out["decision"] == "pending"
        assert out["reason"] == "uncertain"

    def test_the_result_object_has_the_documented_shape(self):
        cfg = _cfg()
        st = self._state(0.9, 3, cfg)
        out = lt.fuse_liveness(st, 5.0, 0.0, 0.5, None, cfg)
        assert set(out) == {"score", "texture", "motion", "blink", "decision",
                            "reason"}


class TestTextureEma:
    def test_the_ema_starts_at_the_first_sample(self):
        st = lt.LivenessState()
        st.update_texture(0.8, 0.4)
        assert st.texture_ema == pytest.approx(0.8)
        assert st.texture_samples == 1

    def test_subsequent_samples_are_blended(self):
        st = lt.LivenessState()
        st.update_texture(0.8, 0.4)
        st.update_texture(0.3, 0.4)
        assert st.texture_ema == pytest.approx(0.4 * 0.3 + 0.6 * 0.8)

    def test_a_failed_inference_is_not_a_sample(self):
        st = lt.LivenessState()
        st.update_texture(None, 0.4)
        assert st.texture_samples == 0 and st.texture_ema is None


class TestConfigBinding:
    def test_manifest_keys_map_onto_the_dataclass(self):
        cfg = lt.LivenessConfig.from_config({
            "liveness_min_samples": 5,
            "liveness_t_live": 0.8,
            "liveness_depth_enabled": True,
            "unrelated_key": 1,
        })
        assert cfg.min_samples == 5
        assert cfg.t_live == pytest.approx(0.8)
        assert cfg.depth_enabled is True
        assert cfg.texture_ema_alpha == pytest.approx(0.4)   # untouched default
