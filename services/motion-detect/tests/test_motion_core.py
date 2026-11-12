"""motion_core 的离线测试（无 recamera_ext / 无设备）。

运行：python3 -m pytest test_motion_core.py -q   （在 motion-daemon 目录下）
"""
import numpy as np
import pytest

from motion_core import MotionCore, retention_seconds


def gray(value, shape=(90, 160)):
    return np.full(shape, value, dtype=np.uint8)


def eval_default(det, now, **overrides):
    params = dict(baseline_gap_ms=1000, baseline_frames=5, current_frames=2,
                  sq_diff_threshold=400, min_changed_ratio=0.005)
    params.update(overrides)
    return det.evaluate(now, **params)


def feed_static(det, n=14, value=100, step=0.2):
    for i in range(n):
        det.push(i * step, gray(value))
    return (n - 1) * step


class TestWarmup:
    def test_insufficient_baseline_returns_none(self):
        det = MotionCore()
        for i in range(4):
            det.push(i * 0.2, gray(100))
        assert eval_default(det, 4 * 0.2) is None

    def test_enough_samples_returns_result(self):
        det = MotionCore()
        now = feed_static(det)
        result = eval_default(det, now)
        assert result is not None
        assert result["active"] is False
        assert result["changed_ratio"] == 0.0


class TestDetection:
    def test_static_scene_no_motion(self):
        det = MotionCore()
        now = feed_static(det)
        assert eval_default(det, now)["active"] is False

    def test_scene_change_triggers_motion(self):
        det = MotionCore()
        feed_static(det, value=100)
        for k in range(3):                    # 变亮 40 -> 平方差 1600 > 400
            det.push(10.0 + k * 0.2, gray(140))
        result = eval_default(det, 10.4)
        assert result["active"] is True
        assert result["changed_ratio"] == pytest.approx(1.0)
        assert result["score"] > 0.0
        assert result["changed_pixels"] == 90 * 160

    def test_gap_selects_older_baseline(self):
        det = MotionCore()
        feed_static(det, value=100)           # 0..2.6s 都是 100
        for k in range(4):                    # 2.8..3.4s 变成 140
            det.push(2.8 + k * 0.2, gray(140))
        now = 3.4
        with_gap = eval_default(det, now, baseline_gap_ms=1000)
        no_gap = eval_default(det, now, baseline_gap_ms=0)
        assert with_gap["changed_ratio"] > no_gap["changed_ratio"]

    def test_threshold_suppresses_small_change(self):
        det = MotionCore()
        feed_static(det, value=100)
        for k in range(3):                    # 灰度差 10 -> 平方差 100
            det.push(10.0 + k * 0.2, gray(110))
        assert eval_default(det, 10.4)["active"] is False
        assert eval_default(det, 10.4, sq_diff_threshold=50)["active"] is True

    def test_ratio_gate_filters_tiny_changes(self):
        det = MotionCore()
        feed_static(det, value=100)
        for k in range(3):
            img = gray(100)
            img[10:13, 20:26] = 220           # 18 个像素 ≈ 0.125%
            det.push(10.0 + k * 0.2, img)
        # 像素差异很大（平方差 14400 > 400）但占比 0.125% < 0.5% -> 不报
        assert eval_default(det, 10.4)["active"] is False
        # 放宽占比门限 -> 报
        assert eval_default(det, 10.4, min_changed_ratio=0.001)["active"] is True


class TestTrim:
    def test_trim_drops_old_samples(self):
        det = MotionCore()
        for i in range(10):                   # pts 0..1.8
            det.push(i * 0.2, gray(100))
        det.trim(2.0, retention_s=1.0)
        assert len(det) == 5
        assert min(p for p, _ in det.samples) >= 1.0


class TestDownscale:
    def test_geometry_and_ownership(self):
        det = MotionCore(analysis_width=160)
        y = np.full((1080, 1920), 128, dtype=np.uint8)
        small = det.downscale(y)
        assert small.shape == (90, 160)
        assert small.dtype == np.uint8
        assert small.flags["OWNDATA"]
        assert int(small[0, 0]) == 128        # 均值保灰度

    def test_block_mean_value(self):
        det = MotionCore(analysis_width=16)
        y = np.zeros((20, 20), dtype=np.uint8)
        y[:, 10:] = 100                       # 右半 100 -> 输出右半 100
        small = det.downscale(y)
        assert small.shape == (16, 16)
        # 两条降采样路径（cv2 INTER_AREA / numpy 块均值）在边界列映射不同，
        # 只断言路径无关的性质：左区全 0、右区全 100。
        assert small[0, 0] == 0 and small[0, 15] == 100
        assert small[0, 1] == 0 and small[0, 14] == 100

    def test_non_divisible_source(self):
        det = MotionCore(analysis_width=100)
        y = np.full((722, 1280), 77, dtype=np.uint8)
        small = det.downscale(y)
        assert small.shape == (56, 100)       # ah = round(722*100/1280) = 56
        assert int(small[0, 0]) == 77


class TestRetention:
    def test_covers_gap_and_windows(self):
        r = retention_seconds(200, 1000, 5, 2)
        assert 1.0 + 0.2 * 11 <= r <= 1.0 + 0.2 * 12 + 0.001
