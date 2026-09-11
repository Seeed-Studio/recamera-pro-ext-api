"""Run production app loops through deferred input and real ROI ownership APIs.

Only the camera/RGA/RKNN operations and detector output are scripted. App.pre,
model handles, the official ROI scratch/copy path and cascade business loops
are real, so reading x.data early or retaining the scratch canvas is observable.
"""
from __future__ import annotations

import copy
import importlib.util
from pathlib import Path
import sys

import numpy as np
import pytest

from kit.adapters.official import OfficialFrameSource
from kit.app import _ModelHandle


APP_ROOT = Path(__file__).resolve().parents[2] / "apps"


def load_app(monkeypatch, app_id):
    directory = APP_ROOT / app_id
    monkeypatch.syspath_prepend(str(directory))
    # App-local helper names are intentionally generic. Do not accidentally
    # reuse another app's helper imported during pytest collection.
    helpers = {"crayfish-fight": ("logic",),
               "depth-estimation": ("depth_map", "planarity")}
    for name in helpers.get(app_id, ()):
        spec = importlib.util.spec_from_file_location(name, directory / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, name, module)
        spec.loader.exec_module(module)
    name = "_cascade_dma_" + app_id.replace("-", "_")
    spec = importlib.util.spec_from_file_location(name, directory / "app.py")
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


class Camera:
    width, height = 1280, 720
    planes = ((0, 1280, 720), (1280 * 720, 1280, 360))

    def __init__(self, index):
        self.index = index
        self.active = True
        self.pts_us = 1_000_000 + index * 100_000

    @property
    def fd(self):
        assert self.active, "an application used the camera after its frame lease"
        return 700 + self.index


class Rga:
    def __init__(self):
        self.dma = []
        self.crops = []

    def can_letterbox(self, *, dma_output=False):
        return dma_output

    def letterbox_nv12_to_rgb(self, **kwargs):
        assert (kwargs["width"], kwargs["height"]) == (1280, 720)
        assert kwargs["dst_fd"] == 70
        self.dma.append(kwargs)

    def resize_nv12_to_rgb(self, **kwargs):
        pytest.fail("application materialized its detector RGB before DMA inference")

    def convert(self, **kwargs):
        pytest.fail("application requested full-frame RGB instead of the original DMA ROI")

    def crop_nv12_to_rgb(self, **kwargs):
        assert (kwargs["width"], kwargs["height"]) == (1280, 720)
        # Give every frame (and left/right face) distinct source pixels. This
        # writes the real adapter's reusable scratch canvas, not a fresh array.
        marker = (kwargs["fd"] - 700) * 10 + kwargs["src_rect"][0] // 600
        kwargs["out"][:] = marker
        self.crops.append({**kwargs, "marker": marker})
        return kwargs["out"]


def frame_source(app, count, size=640):
    source = OfficialFrameSource(
        input_size=size, direct_preprocess=app.model_frame == "hw-direct",
        hw_roi=app.model_frame == "hw-roi", deferred_preprocess=True, verbose=False)
    rga = Rga()
    source._rga, source._rga_decided = rga, True
    cameras = []

    def frames():
        for index in range(1, count + 1):
            camera = Camera(index)
            cameras.append(camera)
            frame = source._deferred_frame(camera)
            assert frame is not None, "eligible app lost its deferred frame path"
            if app.model_frame == "hw-roi":
                assert frame.roi_cropper is not None
            try:
                yield frame
            finally:
                frame.release()
                camera.active = False

    app.frames = frames
    app._pre_size = size
    return source, rga, cameras


class StopAtInference(Exception):
    pass


class Detector:
    io_transport = "rknn-dma-v1"

    def __init__(self, size=640, *, stop=False):
        self.size, self.stop = size, stop
        self.calls = 0

    def infer_prepared(self, prepare, fallback):
        size = self.size
        assert prepare({"fd": 70, "offset": 0, "size": size * size * 3,
                        "shape": (1, size, size, 3),
                        "strides": (size * size * 3, size * 3, 3, 1),
                        "dtype": "uint8"}) is True
        self.calls += 1
        if self.stop:
            raise StopAtInference
        return [np.array([self.calls], dtype=np.float32)]

    def infer(self, pixels):
        pytest.fail("detector received eager pixels instead of PreparedInput")


class Classifier:
    def __init__(self, size, indices=(0,)):
        self.inputs = []
        self.output = np.full((1, size), -8, dtype=np.float32)
        self.output[0, list(indices)] = 8

    def infer(self, pixels):
        assert pixels.dtype == np.uint8
        self.inputs.append(pixels.copy())
        return [self.output.copy()]


def register(app, name, runtime, aliases=()):
    app.models._register(name, _ModelHandle(name, name + ".rknn", app, runtime), aliases)


def emissions(app):
    results = []
    app.emit = lambda events, pts, **kwargs: results.append(
        copy.deepcopy({"events": events, "pts": pts, **kwargs}))
    return results


@pytest.mark.parametrize("app_id,cls,model_id,alias,size", [
    ("face-analysis", "FaceAnalysisApp", "detector", "det", 640),
    ("crayfish-fight", "CrayfishFightApp", "crayfish_det", "det", 640),
    ("yolo-detector", "YoloDetectorApp", "detector", "det", 640),
    ("retail-vision", "RetailVisionApp", "detector", "det", 640),
    ("fitness-trainer", "FitnessTrainerApp", "pose", "pose", 640),
    ("fall-detection", "FallDetectionApp", "pose", "pose", 640),
    ("depth-estimation", "DepthEstimationApp", "depth", "depth", 256),
])
def test_actual_app_loop_passes_prepared_input_without_rgb_materialization(
        monkeypatch, app_id, cls, model_id, alias, size):
    module = load_app(monkeypatch, app_id)
    app = getattr(module, cls)()
    model = Detector(size, stop=True)
    register(app, model_id, model, (alias,))
    _, rga, cameras = frame_source(app, 1, size)
    # Stop after the real loop has reached the real model handle. No app-specific
    # business setup/decoder is needed to test this boundary for all seven apps.
    with pytest.raises(StopAtInference):
        app.run()
    assert model.calls == 1 and len(rga.dma) == 1
    assert all(not camera.active for camera in cameras)


def test_face_loop_keeps_original_roi_source_and_per_track_evidence(monkeypatch):
    module = load_app(monkeypatch, "face-analysis")
    app = module.FaceAnalysisApp()
    detector = Detector()
    fairface = Classifier(18, indices=(0, 7, 9))
    emotion = Classifier(8)
    register(app, "detector", detector, ("det",))
    register(app, module.FF_ID, fairface)
    register(app, module.EMO_ID, emotion)
    app.setup({})
    detections = [{"box": [100, 100, 260, 260], "score": 0.95, "cls": 0},
                  {"box": [830, 100, 990, 260], "score": 0.90, "cls": 0}]
    monkeypatch.setattr(module.face_post, "postprocess", lambda *a, **kw: copy.deepcopy(detections))
    _, rga, cameras = frame_source(app, 4)
    emitted = emissions(app)
    app.run()
    assert detector.calls == 4 and len(rga.dma) == 4
    assert len(fairface.inputs) == len(emotion.inputs) == len(rga.crops) == 8
    assert all(pixels.shape == (224, 224, 3) for pixels in fairface.inputs)
    assert [int(pixels[0, 0, 0]) for pixels in fairface.inputs] == [10, 11, 20, 21, 30, 31, 40, 41]
    # The right-hand face is beyond the detector's 640-wide letterbox; reaching
    # the original camera cropper is required to obtain these source pixels.
    assert any(crop["src_rect"][0] > 640 for crop in rga.crops)
    assert all(not camera.active for camera in cameras)
    assert [r["track_id"] for r in emitted[0]["results"]] == [r["track_id"] for r in emitted[-1]["results"]]
    assert all(r["evidence_frames"] == 4 and r["stable"] for r in emitted[-1]["results"])
    assert all(r["blur"] for packet in emitted for r in packet["results"])


def test_crayfish_temporal_sequence_owns_crops_after_scratch_reuse(monkeypatch):
    module = load_app(monkeypatch, "crayfish-fight")
    app = module.CrayfishFightApp()
    app.capture_enabled = False
    app.proximity_window = app.proximity_hits = 1
    app.sex_vote_frames = 1
    app.temporal_stride = 1
    detector = Detector()
    sex = Classifier(len(module.SEX_LABELS))
    behavior = Classifier(len(module.BEHAVIOR_LABELS), indices=(2,))
    temporal = Classifier(len(module.TEMPORAL_LABELS), indices=(2,))
    for name, runtime in [(module.DET_ID, detector), (module.SEX_ID, sex),
                          (module.BEHAVIOR_ID, behavior), (module.TEMPORAL_ID, temporal)]:
        register(app, name, runtime)
    app.setup({})
    detections = [{"box": [100, 100, 260, 260], "score": 0.95, "cls": 0},
                  {"box": [280, 100, 440, 260], "score": 0.90, "cls": 0}]
    monkeypatch.setattr(module, "detect_post", lambda *a, **kw: copy.deepcopy(detections))
    source, rga, cameras = frame_source(app, 8)
    emitted = emissions(app)
    app.run()
    assert detector.calls == len(rga.dma) == len(emitted) == 8
    assert len(sex.inputs) == 2  # settled votes are retained across frames
    assert len(behavior.inputs) == 8
    assert len(temporal.inputs) == 1
    assert all(not camera.active for camera in cameras)
    keys = app._tbuf.ready_keys()
    assert len(keys) == 1
    crops = app._tbuf.sequence(keys[0])
    assert [int(crop[0, 0, 0]) for crop in crops] == list(range(10, 81, 10))
    scratch = source._roi_scratch[module.BEHAVIOR_INPUT]
    assert all(not np.shares_memory(crop, scratch) for crop in crops)
    scratch[:] = 255
    # The temporal collage must still contain each frame in its trained slot,
    # even after the source's scratch allocation has been overwritten again.
    collage = temporal.inputs[0]
    assert collage.shape == (448, 448, 3)
    assert [int(collage[row * 224, col * 112, 0])
            for row in range(2) for col in range(4)] == list(range(10, 81, 10))
    assert [int(crop[0, 0, 0]) for crop in crops] == list(range(10, 81, 10))
