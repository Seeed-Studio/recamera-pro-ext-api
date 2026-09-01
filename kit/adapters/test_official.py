"""
Offline sanity tests for the official extension-API adapters (official.py).

No device, no librecamera_ext.so, no librga: we inject a fake `recamera_ext`
module into sys.modules that mimics the shipped SDK's FrameSource/ResultSink
surface, then assert that:

  * OfficialFrameSource yields our `Frame` contract (uint8 RGB HWC, fmt="RGB",
    pts == pts_us/1e6) using the OpenCV-fallback path (no librga present);
  * OfficialResultSink maps detect dicts -> send_detections(pts_us, boxes) with
    the exact pts round-trip, source_id, and box/label/class_id mapping, and
    skips boxless (classification-only) results;
  * the registry selects the Official* implementations when the sockets probe
    present, and the base-loop Frame is directly consumable by preprocess.letterbox.

Run:  python3 -m kit.adapters.test_official      (from repo root)
"""
import os
import sys
import tempfile
import types

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import numpy as np


# --- fake `recamera_ext` SDK module ----------------------------------------- #
class _FakeExtFrame:
    """Mimics recamera_ext.Frame: full NV12->BGR done here (no cv2 needed)."""

    def __init__(self, seq, pts_us, w, h):
        self.seq = seq
        self.pts_us = pts_us
        self.width = w
        self.height = h
        self.fourcc = 0x3231564E
        self.planes = [(0, w, h), (w * h, w, h // 2)]  # (offset, stride, vstride)
        self.fd = 7
        # Private C-buf shim: OfficialFrameSource reads _c.fd for the RGA path;
        # with no librga present that path is never taken, but keep it realistic.
        self._c = types.SimpleNamespace(fd=7)

    def to_bgr(self):
        # Deterministic BGR image so we can assert the channel flip.
        img = np.zeros((self.height, self.width, 3), dtype=np.uint8)
        img[..., 0] = 10   # B
        img[..., 1] = 20   # G
        img[..., 2] = 30   # R
        return img


class _FakeExtFrameSource:
    def __init__(self, config=None, timeout_ms=1000, lib_path=None):
        self.width, self.height = 1280, 720
        self.fourcc, self.pool_depth, self.max_outstanding = 0x3231564E, 4, 2
        self._n = 3

    def __iter__(self):
        for i in range(self._n):
            yield _FakeExtFrame(seq=i, pts_us=1_000_000 + i * 33_000,
                                w=self.width, h=self.height)

    def close(self):
        pass


class _FakeExtResultSink:
    """Captures every send_* call as (method, source_id, pts_us, items)."""

    calls = []  # class-level capture

    def __init__(self, source_id, lib_path=None):
        self.source_id = source_id

    def _rec(self, method, pts_us, items):
        _FakeExtResultSink.calls.append((method, self.source_id, pts_us, list(items)))
        return 0

    def send_detections(self, pts_us, boxes):
        return self._rec("send_detections", pts_us, boxes)

    def send_classification(self, pts_us, items):
        return self._rec("send_classification", pts_us, items)

    def send_segmentation(self, pts_us, items):
        return self._rec("send_segmentation", pts_us, items)

    def send_tracking(self, pts_us, items):
        return self._rec("send_tracking", pts_us, items)

    def send_keypoints(self, pts_us, instances):
        return self._rec("send_keypoints", pts_us, instances)

    def close(self):
        pass


def _install_fake_ext():
    mod = types.ModuleType("recamera_ext")
    mod.FrameSource = _FakeExtFrameSource
    mod.ResultSink = _FakeExtResultSink
    mod.FrameConfig = lambda **kw: types.SimpleNamespace(**kw)
    sys.modules["recamera_ext"] = mod


def test_frame_source_yields_rgb():
    _install_fake_ext()
    from kit.adapters.official import OfficialFrameSource
    from kit.runtime.preprocess import letterbox

    src = OfficialFrameSource(url=None, prefer_rga=False, verbose=False)
    frames = list(src.frames())
    assert len(frames) == 3, len(frames)
    f0 = frames[0]
    assert f0.data.dtype == np.uint8 and f0.data.shape == (720, 1280, 3), f0.data.shape
    assert f0.fmt == "RGB", f0.fmt
    assert (f0.w, f0.h) == (1280, 720)
    # BGR (10,20,30) -> RGB flip => (30,20,10)
    assert tuple(f0.data[0, 0]) == (30, 20, 10), tuple(f0.data[0, 0])
    # pts_us -> seconds round-trips exactly back to us.
    assert abs(f0.pts - 1.0) < 1e-9, f0.pts
    assert int(round(f0.pts * 1e6)) == 1_000_000
    # The base loop feeds frame.data straight into letterbox -> proves contract.
    padded, info = letterbox(f0.data, 640)
    assert padded.shape == (640, 640, 3) and padded.dtype == np.uint8
    assert (info.orig_w, info.orig_h) == (1280, 720)
    print("PASS test_frame_source_yields_rgb (RGB HWC, pts round-trip, letterbox-ready)")


def test_direct_model_preprocess_geometry_and_fallback_contract():
    """The RGA fast path returns model pixels but preserves camera geometry."""
    _install_fake_ext()
    from kit.adapters.official import OfficialFrameSource

    class _FakeRga:
        def __init__(self):
            self.calls = []

        def resize_nv12_to_rgb(self, **kwargs):
            self.calls.append(kwargs)
            return np.full((kwargs["dst_height"], kwargs["dst_width"], 3),
                           7, dtype=np.uint8)

    src = OfficialFrameSource(url=None, input_size=640,
                              direct_preprocess=True, prefer_rga=False,
                              verbose=False)
    # Simulate the successful ABI probe without loading a host librga.
    src._rga = _FakeRga()
    src._rga_decided = True
    src.prefer_rga = True
    src.direct_preprocess = True
    frame = _FakeExtFrame(seq=0, pts_us=123, w=1280, h=720)
    data, padded, info = src._convert(frame)
    # direct mode: the letterbox IS the frame data (no full-res convert at all).
    assert data is padded
    assert padded.shape == (640, 640, 3)
    assert info.orig_w == 1280 and info.orig_h == 720
    assert info.scale == 0.5 and info.pad_w == 0 and info.pad_h == 140
    assert np.all(padded[:140] == 114) and np.all(padded[140:500] == 7)
    assert np.all(padded[500:] == 114)
    call = src._rga.calls[0]
    assert (call["dst_width"], call["dst_height"]) == (640, 360)
    # A direct-path failure must latch to the established full-RGB fallback.
    class _BrokenRga(_FakeRga):
        def resize_nv12_to_rgb(self, **kwargs):
            raise RuntimeError("ABI mismatch")

        def convert(self, **kwargs):
            return np.zeros((kwargs["height"], kwargs["width"], 3), dtype=np.uint8)

    src._rga = _BrokenRga()
    src.direct_preprocess = True
    rgb, no_model, no_info = src._convert(frame)
    assert rgb.shape == (720, 1280, 3) and no_info is None and no_model is None
    assert src.direct_preprocess is False
    print("PASS test_direct_model_preprocess_geometry_and_fallback_contract")


def test_hw_letterbox_keeps_original_pixels_alongside_model_image():
    """"hw" mode: RGA letterbox in model_data, ORIGINAL pixels still in data.

    This is what lets ROI/perspective-cropping apps (face/facemesh/ppocr) skip
    the Python letterbox without losing source-resolution pixels.
    """
    _install_fake_ext()
    from kit.adapters.official import OfficialFrameSource

    class _FakeRga:
        def resize_nv12_to_rgb(self, **kwargs):
            return np.full((kwargs["dst_height"], kwargs["dst_width"], 3),
                           7, dtype=np.uint8)

        def convert(self, **kwargs):
            return np.full((kwargs["height"], kwargs["width"], 3), 9, dtype=np.uint8)

    src = OfficialFrameSource(url=None, input_size=640, hw_letterbox=True,
                              prefer_rga=False, verbose=False)
    assert src.hw_letterbox is True and src.direct_preprocess is False
    src._rga = _FakeRga()
    src._rga_decided = True
    frame = _FakeExtFrame(seq=0, pts_us=123, w=1280, h=720)
    data, model_data, info = src._convert(frame)
    # Full-resolution originals survive for cropping ...
    assert data.shape == (720, 1280, 3) and np.all(data == 9)
    # ... and the model input is the hardware letterbox with matching geometry.
    assert model_data.shape == (640, 640, 3)
    assert info.scale == 0.5 and info.pad_w == 0 and info.pad_h == 140
    assert np.all(model_data[:140] == 114) and np.all(model_data[140:500] == 7)

    # direct_preprocess wins if both are somehow requested.
    both = OfficialFrameSource(url=None, input_size=640, direct_preprocess=True,
                               hw_letterbox=True, prefer_rga=False, verbose=False)
    assert both.direct_preprocess is True and both.hw_letterbox is False
    print("PASS test_hw_letterbox_keeps_original_pixels_alongside_model_image")


# Test frame geometry: width != height so we prove x/width vs y/height, chosen
# so the sample pixel coords normalize to round [0,1] values.
_FW, _FH = 200, 400


def test_result_sink_maps_detections():
    _install_fake_ext()
    _FakeExtResultSink.calls = []
    from kit.adapters.official import OfficialResultSink, OsdInjectResultSink
    assert OsdInjectResultSink is OfficialResultSink  # back-compat alias

    sink = OfficialResultSink(app_id="yolo-detector", verbose=False)
    sink.set_frame_size(_FW, _FH)          # base loop does this every frame
    payload = {"results": [
        {"box": [10.0, 20.0, 110.0, 220.0], "cls": 0, "cls_name": "person", "score": 0.9},
        {"box": [1, 2, 3, 4], "cls": 2, "cls_name": "car", "score": 0.7},
    ]}
    sink.emit(payload, pts=1.0)            # 1.0 s -> 1_000_000 us
    sink.emit({"results": []}, pts=2.033)  # empty -> clears OSD via empty detections
    sink.close()

    assert len(_FakeExtResultSink.calls) == 2, _FakeExtResultSink.calls
    method, sid, pts_us, boxes = _FakeExtResultSink.calls[0]
    assert method == "send_detections" and sid == "yolo-detector"
    assert pts_us == 1_000_000, pts_us            # exact s->us round-trip
    assert len(boxes) == 2, boxes
    x1, y1, x2, y2, score, label, class_id = boxes[0]
    # NORMALIZED: x/200, y/400 -> 10/200=0.05, 20/400=0.05, 110/200=0.55, 220/400=0.55
    assert (x1, y1, x2, y2) == (0.05, 0.05, 0.55, 0.55), (x1, y1, x2, y2)
    assert label == "person" and class_id == 0 and abs(score - 0.9) < 1e-6
    # empty frame -> empty send_detections still fires (clears the OSD).
    m2, _s2, pts_us2, boxes2 = _FakeExtResultSink.calls[1]
    assert m2 == "send_detections" and pts_us2 == 2_033_000 and boxes2 == []
    # qrcode-style quad (no box) derives a bbox, then normalizes.
    _FakeExtResultSink.calls = []
    sink = OfficialResultSink(app_id="qrcode", verbose=False)
    sink.set_frame_size(_FW, _FH)
    sink.emit({"results": [{"text": "HELLO",
                            "quad": [[10, 10], [30, 12], [28, 40], [8, 38]]}]}, pts=0.5)
    m, _s, _p, boxes = _FakeExtResultSink.calls[0]
    # bbox=(8,10,30,40) -> (8/200,10/400,30/200,40/400)=(0.04,0.025,0.15,0.10)
    assert m == "send_detections" and boxes[0][:4] == (0.04, 0.025, 0.15, 0.10), boxes
    assert boxes[0][5] == "HELLO"          # text -> label
    print("PASS test_result_sink_maps_detections (normalized [0,1] + quad->bbox + empty-clears)")


def test_result_sink_normalizes_and_clamps():
    _install_fake_ext()
    _FakeExtResultSink.calls = []
    from kit.adapters.official import OfficialResultSink

    # Guard: emit WITHOUT set_frame_size -> skipped, no send (avoids 1px boxes).
    sink = OfficialResultSink(app_id="x", verbose=False)
    sink.emit({"results": [{"box": [1, 2, 3, 4], "score": 0.5}]}, pts=1.0)
    assert _FakeExtResultSink.calls == [], "must not send without frame size"

    # Out-of-frame pixels clamp to [0,1]: x1<0 -> 0, y1>H -> 1, x2>W -> 1, y2>>H -> 1.
    sink.set_frame_size(_FW, _FH)          # 200 x 400
    sink.emit({"results": [{"box": [-10, 500, 250, 800], "score": 0.5,
                            "cls_name": "person"}]}, pts=1.0)
    _m, _s, _p, boxes = _FakeExtResultSink.calls[0]
    assert boxes[0][:4] == (0.0, 1.0, 1.0, 1.0), boxes[0][:4]
    print("PASS test_result_sink_normalizes_and_clamps (no-size guard + [0,1] clamp)")


def test_result_sink_maps_keypoints():
    _install_fake_ext()
    _FakeExtResultSink.calls = []
    from kit.adapters.official import OfficialResultSink

    # pose result: box + 17 [x,y,conf] keypoints (fall/fitness schema).
    kps = [[float(i), float(i + 1), 0.8] for i in range(17)]
    sink = OfficialResultSink(app_id="fitness-trainer", verbose=False)
    sink.set_frame_size(_FW, _FH)          # 200 x 400
    sink.emit({"results": [{"box": [5, 6, 105, 206], "score": 0.88,
                            "cls": 0, "cls_name": "person", "keypoints": kps}],
               "events": [{"kind": "workout", "reps": 3}]}, pts=1.5)
    sink.close()

    assert len(_FakeExtResultSink.calls) == 1, _FakeExtResultSink.calls
    method, _sid, pts_us, instances = _FakeExtResultSink.calls[0]
    assert method == "send_keypoints", method
    assert pts_us == 1_500_000
    inst = instances[0]
    # NORMALIZED object box: (5/200,6/400,105/200,206/400)
    assert inst["box"] == (0.025, 0.015, 0.525, 0.515), inst["box"]
    assert inst["label"] == "person" and abs(inst["score"] - 0.88) < 1e-6
    assert len(inst["points"]) == 17
    # point schema: (x/W, y/H, score, keypoint_id) with id == index (COCO order)
    assert inst["points"][0] == (0.0, 0.0025, 0.8, 0), inst["points"][0]
    assert inst["points"][16] == (0.08, 0.0425, 0.8, 16), inst["points"][16]
    # facemesh landmark schema: [x,y] pairs (no conf) -> conf defaults to 1.0
    _FakeExtResultSink.calls = []
    sink = OfficialResultSink(app_id="facemesh", verbose=False)
    sink.set_frame_size(_FW, _FH)
    sink.emit({"results": [{"box": [0, 0, 50, 50], "kind": "face",
                            "keypoints": [[1.0, 2.0], [3.0, 4.0]]}]}, pts=0.1)
    _m, _s, _p, instances = _FakeExtResultSink.calls[0]
    # (1/200, 2/400, 1.0, 0)
    assert instances[0]["points"][0] == (0.005, 0.005, 1.0, 0), instances[0]["points"]
    print("PASS test_result_sink_maps_keypoints (normalized pose + facemesh, id=index, conf default)")


def test_result_sink_maps_classification():
    _install_fake_ext()
    _FakeExtResultSink.calls = []
    from kit.adapters.official import OfficialResultSink

    # face-analysis result: box + attributes -> send_classification WITH ROI box.
    sink = OfficialResultSink(app_id="face-analysis", verbose=False)
    sink.set_frame_size(_FW, _FH)          # 200 x 400
    sink.emit({"results": [{
        "box": [10, 10, 60, 70], "cls_name": "face", "score": 0.95,
        "gender": "Male", "gender_conf": 0.9,
        "age": "30-39", "age_conf": 0.6,
        "emotion": "Happiness", "emotion_conf": 0.7,
    }]}, pts=2.0)
    sink.close()

    assert len(_FakeExtResultSink.calls) == 1, _FakeExtResultSink.calls
    method, _sid, pts_us, items = _FakeExtResultSink.calls[0]
    assert method == "send_classification", method   # NOT send_detections
    assert pts_us == 2_000_000
    # (score, class_id, label, normalized_box)
    score, class_id, label, box = items[0]
    assert label == "Male,30-39,Happiness", label     # composite attribute label
    assert abs(score - 0.6) < 1e-6                     # min of the attr confidences
    # ROI box normalized: (10/200,10/400,60/200,70/400)
    assert box == (0.05, 0.025, 0.30, 0.175), box
    # generic boxless label classifier -> 3-tuple, no box.
    _FakeExtResultSink.calls = []
    sink = OfficialResultSink(app_id="clf", verbose=False)
    sink.set_frame_size(_FW, _FH)
    sink.emit({"results": [{"label": "cat", "score": 0.8, "cls": 3}]}, pts=0.2)
    m, _s, _p, items = _FakeExtResultSink.calls[0]
    assert m == "send_classification" and items[0] == (0.8, 3, "cat"), items
    print("PASS test_result_sink_maps_classification (attrs->classification + normalized ROI box + boxless)")


def test_result_sink_maps_tracking():
    _install_fake_ext()
    _FakeExtResultSink.calls = []
    from kit.adapters.official import OfficialResultSink

    # retail-vision: raw person detections in results, tracked boxes in events.
    sink = OfficialResultSink(app_id="retail-vision", verbose=False)
    sink.set_frame_size(_FW, _FH)          # 200 x 400
    sink.emit({
        "results": [{"box": [0, 0, 10, 10], "cls": 0, "cls_name": "person", "score": 0.9}],
        "events": [
            {"kind": "track", "track_id": 7, "state": "ENGAGED", "score": 0.9,
             "box": [20.0, 40.0, 100.0, 200.0]},
            {"kind": "metrics", "occupancy": 1},   # non-track event ignored
        ],
    }, pts=3.0)
    sink.close()

    assert len(_FakeExtResultSink.calls) == 1, _FakeExtResultSink.calls
    method, _sid, pts_us, items = _FakeExtResultSink.calls[0]
    assert method == "send_tracking", method   # supersedes the raw detections
    assert pts_us == 3_000_000
    x1, y1, x2, y2, score, class_id, label, track_id = items[0]
    # NORMALIZED: (20/200,40/400,100/200,200/400)
    assert (x1, y1, x2, y2) == (0.10, 0.10, 0.50, 0.50), (x1, y1, x2, y2)
    assert track_id == 7 and label == "ENGAGED", (track_id, label)
    print("PASS test_result_sink_maps_tracking (normalized track box, supersedes detections)")


def test_registry_selects_official():
    _install_fake_ext()
    from kit.adapters import registry
    from kit.adapters.official import OfficialFrameSource, OfficialResultSink
    from kit.adapters.result_sink import WsResultSink

    for k in ("RECAMERA_ADAPTER_PREFER", "RECAMERA_RESULT_INGRESS",
              "RECAMERA_RESULT_OSD"):
        os.environ.pop(k, None)
    with tempfile.NamedTemporaryFile(prefix="frame-", suffix=".sock") as ftf, \
         tempfile.NamedTemporaryFile(prefix="result-in-", suffix=".sock") as rtf:
        os.environ["RECAMERA_FRAME_SOCK"] = ftf.name
        os.environ["RECAMERA_RESULT_SOCK"] = rtf.name
        caps = registry.capabilities(refresh=True)
        assert not caps.frame_broker and not caps.result_ingress, caps

        # Regular files and UNKNOWN endpoints never auto-switch the data path.
        src = registry.select_frame_source(url="rtsp://x", prefer="ffmpeg")
        from kit.adapters.frame_source import FfmpegRtspSource
        assert isinstance(src, FfmpegRtspSource), type(src)
        src.close()

        # ★S1★ Result sink does NOT auto-switch to OSD burn-in on socket
        # presence -- the default is the SOFTWARE overlay (WsResultSink).
        sink = registry.select_result_sink("ws", host="127.0.0.1", port=0,
                                           app_id="demo")
        assert isinstance(sink, WsResultSink), type(sink)
        sink.close()

        # OSD burn-in is opt-in: RECAMERA_RESULT_OSD=1 or kind="osd".
        os.environ["RECAMERA_RESULT_OSD"] = "1"
        from kit.errors import CapabilityError
        try:
            registry.select_result_sink("ws", host="0.0.0.0", port=8124,
                                        app_id="demo")
            raise AssertionError("custom native endpoint must fail closed")
        except CapabilityError:
            pass
        os.environ.pop("RECAMERA_RESULT_OSD", None)
    for k in ("RECAMERA_FRAME_SOCK", "RECAMERA_RESULT_SOCK"):
        os.environ.pop(k, None)
    os.environ["RECAMERA_ADAPTER_PREFER"] = "official"
    src = registry.select_frame_source(url="rtsp://x", prefer="ffmpeg")
    assert isinstance(src, OfficialFrameSource), type(src)
    osd2 = registry.select_result_sink("osd", app_id="demo")
    assert isinstance(osd2, OfficialResultSink), type(osd2)
    os.environ.pop("RECAMERA_ADAPTER_PREFER", None)
    registry.capabilities(refresh=True)
    print("PASS test_registry_selects_official (filesystem fails closed; "
          "explicit matching-firmware policy selects official)")


if __name__ == "__main__":
    test_frame_source_yields_rgb()
    test_direct_model_preprocess_geometry_and_fallback_contract()
    test_result_sink_maps_detections()
    test_result_sink_normalizes_and_clamps()
    test_result_sink_maps_keypoints()
    test_result_sink_maps_classification()
    test_result_sink_maps_tracking()
    test_registry_selects_official()
    print("ALL OFFICIAL ADAPTER TESTS PASSED")


def test_official_result_sink_stats_surface():
    """OfficialResultSink.stats() exposes local delivery tallies (frames handed
    in, SDK send attempts) so an app can read result-delivery health. With the
    fake SDK sink (no stats()) there is no `wire` sub-dict; with a real SDK sink
    it would carry sent/oversize_rejected/send_error."""
    _install_fake_ext()
    _FakeExtResultSink.calls = []
    from kit.adapters.official import OfficialResultSink

    sink = OfficialResultSink(app_id="yolo-detector", verbose=False)
    sink.set_frame_size(_FW, _FH)
    sink.emit({"results": [{"box": [10.0, 20.0, 110.0, 220.0], "cls": 0,
                            "cls_name": "person", "score": 0.9}]}, pts=1.0)
    sink.emit({"results": []}, pts=2.0)   # empty -> still one send_detections
    st = sink.stats()
    assert st["frames"] == 2, st
    assert st["send_calls"] == 2, st      # one send per emit
    assert st["send_fail"] == 0 and st["oversize_rejected"] == 0, st
    assert "wire" not in st               # fake SDK sink has no stats()
    sink.close()
