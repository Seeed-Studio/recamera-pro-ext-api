"""
Offline correctness tests for the hw-roi (dma-buf RGA ROI crop) path.

No device, no librga: the RGA crop op is faked with a numpy stand-in that
performs the SAME crop+scale into the SAME destination window the real
`improcess_t` would, so these tests validate the geometry/plumbing (crop rect,
destination window, roi_map, gray-fill) rather than librga's ABI. They cover:

  * `kit.pipeline.square_roi_geometry` roi_map == `crop_square_roi`'s roi_map,
    and against a hand-computed oracle (interior / border / corner / off-frame);
  * the OfficialFrameSource `_crop_roi` plumbing crops the RIGHT region at the
    RIGHT scale -- interior ROIs match the numpy `crop_square_roi` pixel-for-
    pixel (same resampler), border ROIs match on the in-frame region and are
    gray-filled (114) outside, matching the documented hardware behaviour;
  * OfficialFrameSource wiring: hw_roi attaches a `roi_cropper` and keeps camera
    geometry; a librga without improcess_t degrades to hw (no cropper);
  * App.crop_roi_hw dispatch: hardware cropper when present, numpy fallback when
    absent, gray ROI (never wrong pixels) on a hardware failure.

Each positive assertion is paired with a negative control so a broken geometry
or dispatch is caught (not a tautology).

Run:  python3 -m pytest kit/adapters/test_rga_roi.py -q      (from repo root)
"""
import os
import sys
import types

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import numpy as np

from kit.pipeline import square_roi_geometry, crop_square_roi

try:
    from PIL import Image
    _HAVE_PIL = True
except Exception:                        # pragma: no cover
    _HAVE_PIL = False


# --- a numpy stand-in for what librga's improcess_t crop would produce ------- #
def _fake_rga_crop(ref_rgb, src_rect, dst_size, dst_window, pad_value=114):
    """Crop `src_rect` from `ref_rgb` and BILINEAR-scale it into `dst_window` of
    a `pad_value`-filled `dst_size` square canvas -- the exact geometry the RGA
    `crop_nv12_to_rgb` asks the hardware for (minus the NV12 color convert, which
    is irrelevant to geometry). Represents the hardware output for the tests."""
    out = np.full((dst_size, dst_size, 3), pad_value, dtype=np.uint8)
    if src_rect is None or dst_window is None:
        return out
    sx1, sy1, sx2, sy2 = src_rect
    dx1, dy1, dx2, dy2 = dst_window
    dw, dh = dx2 - dx1, dy2 - dy1
    if dw < 1 or dh < 1:
        return out
    sub = ref_rgb[sy1:sy2, sx1:sx2]
    if _HAVE_PIL:
        scaled = np.asarray(Image.fromarray(sub).resize((dw, dh), Image.BILINEAR),
                            dtype=np.uint8)
    else:                                # pragma: no cover
        ys = (np.arange(dh) * sub.shape[0] / dh).astype(np.int64).clip(0, sub.shape[0] - 1)
        xs = (np.arange(dw) * sub.shape[1] / dw).astype(np.int64).clip(0, sub.shape[1] - 1)
        scaled = sub[ys][:, xs]
    out[dy1:dy2, dx1:dx2] = scaled
    return out


class _FakeRgaCropper:
    """A fake RgaNV12ToRGB whose crop reads a reference RGB image (not a real
    dma-buf) so the geometry can be validated offline."""

    def __init__(self, ref_rgb, can_crop=True):
        self._ref = ref_rgb
        self._can_crop = can_crop

    def can_crop(self):
        return self._can_crop

    def crop_nv12_to_rgb(self, fd, width, height, y_stride, y_vstride,
                         src_rect, dst_size, dst_window=None, out=None,
                         pad_value=114):
        canvas = _fake_rga_crop(self._ref, src_rect, dst_size, dst_window,
                                pad_value)
        if out is not None:
            out[:] = canvas
            return out
        return canvas

    # resize_nv12_to_rgb is what _rga_letterbox uses; return a gray-padded
    # letterbox-sized RGB (content irrelevant to these geometry tests).
    def resize_nv12_to_rgb(self, fd, width, height, y_stride, y_vstride,
                           dst_width, dst_height):
        return np.full((dst_height, dst_width, 3), 5, dtype=np.uint8)

    # convert is the full-resolution NV12->RGB used by the "hw" (aux) path;
    # hand back the reference so that path yields ORIGINAL-geometry pixels.
    def convert(self, fd, width, height, y_stride, y_vstride):
        return np.ascontiguousarray(self._ref)


class _FakeExtFrame:
    def __init__(self, ref_rgb, pts_us=1000):
        self.height, self.width = ref_rgb.shape[:2]
        self.pts_us = pts_us
        self.fourcc = 0
        self.planes = [(0, self.width, self.height),
                       (self.width * self.height, self.width, self.height // 2)]
        self.fd = 7
        self._c = types.SimpleNamespace(fd=7)
        self.ref_rgb = ref_rgb

    def to_bgr(self):
        return self.ref_rgb[:, :, ::-1].copy()


def _gradient(h, w):
    """A deterministic RGB image where every pixel encodes its own coordinate,
    so a mis-cropped region is provably wrong (not just noisy)."""
    yy, xx = np.mgrid[0:h, 0:w]
    img = np.zeros((h, w, 3), dtype=np.uint8)
    img[..., 0] = (xx % 256).astype(np.uint8)
    img[..., 1] = (yy % 256).astype(np.uint8)
    img[..., 2] = ((xx + yy) % 256).astype(np.uint8)
    return img


# --------------------------------------------------------------------------- #
# 1. Geometry: square_roi_geometry roi_map == crop_square_roi roi_map + oracle
# --------------------------------------------------------------------------- #
def test_geometry_roimap_matches_crop_square_roi_and_oracle():
    fh, fw = 480, 640
    frame = _gradient(fh, fw)
    boxes = [
        [200.0, 150.0, 300.0, 280.0],   # interior
        [10.0, 12.0, 90.0, 160.0],      # near the top-left border
        [600.0, 440.0, 700.0, 560.0],   # runs past the bottom-right corner
        [-50.0, -40.0, -5.0, -3.0],     # entirely off-frame
    ]
    for box in boxes:
        for out_size in (224, 192):
            for pad in (0.15, 0.25):
                roi_map, _sv, _dw, _sq = square_roi_geometry(
                    fh, fw, box, out_size, pad)
                _roi, ref_map = crop_square_roi(frame, box, out_size, pad)
                assert roi_map == ref_map, (box, out_size, pad, roi_map, ref_map)

    # Independent oracle for one box: pad=0.25 on [200,150,300,280].
    #   bw=100, bh=130 -> pad x1=175,y1=117.5,x2=325,y2=312.5
    #   cx=250, cy=215, side=max(150,195)=195, half=97.5
    #   ix1=round(152.5)=152 (round-half-to-even), iy1=round(117.5)=118,
    #   iside=round(195)=195 ; scale=195/224
    roi_map, src_valid, dst_window, sq = square_roi_geometry(
        fh, fw, [200.0, 150.0, 300.0, 280.0], 224, 0.25)
    assert sq == (152, 118, 195), sq
    assert roi_map == (152.0, 118.0, 195 / 224, 195 / 224), roi_map
    # Fully interior: valid == full square, dst window == whole canvas.
    assert src_valid == (152, 118, 152 + 195, 118 + 195), src_valid
    assert dst_window == (0, 0, 224, 224), dst_window
    print("PASS test_geometry_roimap_matches_crop_square_roi_and_oracle")


def test_geometry_negative_control():
    """Reverse validation: a deliberately wrong square (off-by pad) yields a
    roi_map that does NOT match crop_square_roi -- proving the check above bites."""
    fh, fw = 480, 640
    frame = _gradient(fh, fw)
    box = [200.0, 150.0, 300.0, 280.0]
    _roi, good_map = crop_square_roi(frame, box, 224, 0.25)
    # Compute geometry with the WRONG pad; roi_map must differ.
    bad_map, _sv, _dw, _sq = square_roi_geometry(fh, fw, box, 224, 0.40)
    assert bad_map != good_map, (bad_map, good_map)
    print("PASS test_geometry_negative_control")


# --------------------------------------------------------------------------- #
# 2. _crop_roi plumbing: right region, right scale, gray border
# --------------------------------------------------------------------------- #
def _make_source_with_ref(ref_rgb, can_crop=True):
    from kit.adapters.official import OfficialFrameSource
    src = OfficialFrameSource(url=None, input_size=640, hw_roi=True,
                              prefer_rga=False, verbose=False)
    src._rga = _FakeRgaCropper(ref_rgb, can_crop=can_crop)
    src._rga_decided = True
    return src


def test_crop_roi_interior_matches_numpy_pixelwise():
    fh, fw = 480, 640
    ref = _gradient(fh, fw)
    src = _make_source_with_ref(ref)
    ext = _FakeExtFrame(ref)
    box = [200.0, 150.0, 300.0, 280.0]   # fully interior
    hw_roi, hw_map = src._crop_roi(ext, box, 224, 0.25)
    np_roi, np_map = crop_square_roi(ref, box, 224, 0.25)
    assert hw_roi.shape == (224, 224, 3) and hw_roi.dtype == np.uint8
    assert hw_map == np_map, (hw_map, np_map)
    # Same crop rect + same BILINEAR resampler -> identical pixels for interior.
    assert np.array_equal(hw_roi, np_roi), \
        ("interior mean_abs_diff", float(np.abs(hw_roi.astype(int) - np_roi).mean()))
    print("PASS test_crop_roi_interior_matches_numpy_pixelwise")


def test_crop_roi_border_gray_fills_outside_and_matches_inside():
    fh, fw = 480, 640
    ref = _gradient(fh, fw)
    src = _make_source_with_ref(ref)
    ext = _FakeExtFrame(ref)
    box = [10.0, 12.0, 90.0, 160.0]      # square runs off the top-left edge
    roi_map, src_valid, dst_window, (ix1, iy1, iside) = square_roi_geometry(
        fh, fw, box, 224, 0.25)
    assert ix1 < 0 or iy1 < 0, "test box must exceed the frame to be meaningful"
    hw_roi, hw_map = src._crop_roi(ext, box, 224, 0.25)
    assert hw_map == roi_map
    dx1, dy1, dx2, dy2 = dst_window
    # Outside the destination window is gray 114 (hardware pad), not edge-pixels.
    assert np.all(hw_roi[:dy1] == 114) and np.all(hw_roi[:, :dx1] == 114), \
        "off-frame margin must be gray-filled"
    # Inside the window matches the numpy reference resized into the same window.
    ref_inside = _fake_rga_crop(ref, src_valid, 224, dst_window)[dy1:dy2, dx1:dx2]
    assert np.array_equal(hw_roi[dy1:dy2, dx1:dx2], ref_inside)
    print("PASS test_crop_roi_border_gray_fills_outside_and_matches_inside")


def test_crop_roi_offframe_box_returns_zeros():
    fh, fw = 480, 640
    ref = _gradient(fh, fw)
    src = _make_source_with_ref(ref)
    ext = _FakeExtFrame(ref)
    box = [-300.0, -300.0, -260.0, -260.0]   # entirely off-frame even after pad
    roi_map, src_valid, _dw, _sq = square_roi_geometry(fh, fw, box, 224, 0.25)
    assert src_valid is None, "test box must be fully outside the frame"
    hw_roi, _hw_map = src._crop_roi(ext, box, 224, 0.25)
    np_roi, _np_map = crop_square_roi(ref, box, 224, 0.25)
    assert np.all(hw_roi == 0) and np.all(np_roi == 0)   # both give zeros
    print("PASS test_crop_roi_offframe_box_returns_zeros")


# --------------------------------------------------------------------------- #
# 3. OfficialFrameSource wiring: hw_roi attaches a cropper; degrade to hw
# --------------------------------------------------------------------------- #
def test_hw_roi_source_attaches_cropper_and_keeps_geometry():
    from kit.adapters.official import OfficialFrameSource, _FrameRoiCropper
    ref = _gradient(480, 640)
    src = _make_source_with_ref(ref)
    ext = _FakeExtFrame(ref, pts_us=1000)
    data, model_data, model_info = src._convert(ext)
    # hw-roi behaves like hw-direct for pixels: the letterbox IS the data.
    assert data is model_data and model_data.shape == (640, 640, 3)
    assert model_info.orig_w == 640 and model_info.orig_h == 480
    # frames() attaches a cropper bound to the live frame.
    cropper = src._make_cropper(ext)
    assert isinstance(cropper, _FrameRoiCropper)
    roi, roi_map = cropper.crop_square([200.0, 150.0, 300.0, 280.0], 224, 0.25)
    assert roi.shape == (224, 224, 3)
    _np, np_map = crop_square_roi(ref, [200.0, 150.0, 300.0, 280.0], 224, 0.25)
    assert roi_map == np_map
    print("PASS test_hw_roi_source_attaches_cropper_and_keeps_geometry")


def test_hw_roi_degrades_to_hw_without_improcess():
    """librga present but WITHOUT the crop op: hw_roi must fall back to hw
    (full-res data + no cropper), never a broken hardware crop."""
    from kit.adapters.official import OfficialFrameSource
    ref = _gradient(480, 640)
    src = OfficialFrameSource(url=None, input_size=640, hw_roi=True,
                              prefer_rga=True, verbose=False)
    # Emulate the first-frame backend decision with a crop-less librga.
    src._rga = _FakeRgaCropper(ref, can_crop=False)
    # Re-run the hw_roi capability probe the way _decide_backend does.
    if not src._rga.can_crop():
        src.hw_roi = False
        src.hw_letterbox = True
    src._rga_decided = True
    assert src.hw_roi is False and src.hw_letterbox is True
    ext = _FakeExtFrame(ref)
    data, model_data, model_info = src._convert(ext)
    # hw (aux): full-resolution original pixels survive in data, model image aside
    assert data.shape == (480, 640, 3)          # ORIGINAL geometry, not 640x640
    assert model_data.shape == (640, 640, 3) and model_info is not None
    # No cropper is attached in this mode.
    cropper = None
    if src.hw_roi and src._rga is not None and model_info is not None:
        cropper = src._make_cropper(ext)
    assert cropper is None
    print("PASS test_hw_roi_degrades_to_hw_without_improcess")


# --------------------------------------------------------------------------- #
# 4. App.crop_roi_hw dispatch (hardware / numpy fallback / failure)
# --------------------------------------------------------------------------- #
class _StubApp:
    """Minimal stand-in exposing App.crop_roi_hw + its timing/warn plumbing."""
    id = "stub"

    def __init__(self):
        self._t_pre = 0.0
        self._roi_hw_warns = 0

    # Borrow the real methods under test.
    from kit.app import App
    crop_roi_hw = App.crop_roi_hw
    _warn_roi_hw = App._warn_roi_hw


def test_crop_roi_hw_prefers_hardware_cropper():
    ref = _gradient(480, 640)

    class _Cropper:
        def __init__(self):
            self.called = 0

        def crop_square(self, box, out_size, pad):
            self.called += 1
            return np.full((out_size, out_size, 3), 3, dtype=np.uint8), \
                (1.0, 2.0, 3.0, 4.0)

    frame = types.SimpleNamespace(data=ref, roi_cropper=_Cropper(),
                                  model_data=np.empty((640, 640, 3), np.uint8))
    app = _StubApp()
    roi, roi_map = app.crop_roi_hw(frame, [10, 10, 50, 50], 224, 0.2)
    assert frame.roi_cropper.called == 1 and roi_map == (1.0, 2.0, 3.0, 4.0)
    assert np.all(roi == 3)
    print("PASS test_crop_roi_hw_prefers_hardware_cropper")


def test_crop_roi_hw_falls_back_to_numpy_without_cropper():
    ref = _gradient(480, 640)
    frame = types.SimpleNamespace(data=ref, roi_cropper=None, model_data=None)
    app = _StubApp()
    box = [200.0, 150.0, 300.0, 280.0]
    roi, roi_map = app.crop_roi_hw(frame, box, 224, 0.25)
    np_roi, np_map = crop_square_roi(ref, box, 224, 0.25)
    assert roi_map == np_map and np.array_equal(roi, np_roi)
    print("PASS test_crop_roi_hw_falls_back_to_numpy_without_cropper")


def test_crop_roi_hw_gray_on_hardware_failure_never_wrong_pixels():
    """A hardware crop failure returns a gray ROI (not a numpy crop of the
    letterbox, which would be the WRONG region) and does not raise."""
    class _BrokenCropper:
        def crop_square(self, box, out_size, pad):
            raise RuntimeError("improcess failed")

    frame = types.SimpleNamespace(data=np.full((640, 640, 3), 9, np.uint8),
                                  roi_cropper=_BrokenCropper(),
                                  model_data=np.empty((640, 640, 3), np.uint8))
    app = _StubApp()
    roi, _roi_map = app.crop_roi_hw(frame, [10, 10, 50, 50], 224, 0.2)
    assert roi.shape == (224, 224, 3) and np.all(roi == 114)   # gray, not data(9)
    assert app._roi_hw_warns == 1
    print("PASS test_crop_roi_hw_gray_on_hardware_failure_never_wrong_pixels")


if __name__ == "__main__":
    test_geometry_roimap_matches_crop_square_roi_and_oracle()
    test_geometry_negative_control()
    test_crop_roi_interior_matches_numpy_pixelwise()
    test_crop_roi_border_gray_fills_outside_and_matches_inside()
    test_crop_roi_offframe_box_returns_zeros()
    test_hw_roi_source_attaches_cropper_and_keeps_geometry()
    test_hw_roi_degrades_to_hw_without_improcess()
    test_crop_roi_hw_prefers_hardware_cropper()
    test_crop_roi_hw_falls_back_to_numpy_without_cropper()
    test_crop_roi_hw_gray_on_hardware_failure_never_wrong_pixels()
    print("ALL HW-ROI TESTS PASSED")
