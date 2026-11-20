"""Manifest authorization -> native backend choice -> typed UDS transport.

All device/native calls are fakes; AppMgr policy and inference RPC are real.
"""
import hashlib
import json
from pathlib import Path

import numpy as np
import pytest

from kit.errors import CapabilityError
from kit.run import load_app_module
from kit.runtime import ctypes_rknn as native, engine
from market.appmgr import inference_auth, paths
from market.inferenced import server
from market.inferenced.authorization import ModelAuthorization
from market.inferenced.tests.test_backend_selection import Coordinator, FakeRknnLite, GraphLib
from market.inferenced.tests.test_service import RunningService, _TestAuthorizer

APP_DIR = Path(__file__).resolve().parents[1]
OpenVocabularyApp = load_app_module(str(APP_DIR / "app.py")).OpenVocabularyApp

from clip_tokenizer import ClipTokenizer
from model_contracts import model_spec


def installed_policy(tmp_path, monkeypatch, *, legacy_input=False):
    """Run production authorization over a copy of the shipped manifest."""
    manifest = json.loads((APP_DIR / "manifest.json").read_text())
    install_dir = tmp_path / "installed"
    install_dir.mkdir()
    for artifact in manifest["artifacts"]:
        if artifact["kind"] != "rknn":
            continue
        file = install_dir / artifact["file"]
        file.parent.mkdir(exist_ok=True)
        file.write_bytes(artifact["id"].encode())
        artifact.update(sha256=hashlib.sha256(file.read_bytes()).hexdigest(),
                        size=file.stat().st_size)
    if legacy_input:
        manifest["models"][0]["input"] = [1, 640, 640, 3]
        manifest["models"][1]["input"] = [1, 20]
    (install_dir / "manifest.json").write_text(json.dumps(manifest))
    monkeypatch.setattr(paths, "app_dir", lambda _: str(install_dir))
    registry = inference_auth.InferenceAuthorizationRegistry(str(tmp_path / "registry"))
    return registry.prepare(manifest["id"], manifest)


class PolicyAuthorizer(_TestAuthorizer):
    def __init__(self, root, policy):
        super().__init__(root)
        self.models = {item["path"]: ModelAuthorization(**item) for item in policy["models"]}

    def authorize_model(self, authorization, path):
        return self.models[str(Path(path).resolve())]


@pytest.fixture
def fake_native(monkeypatch):
    monkeypatch.delenv("ESK_RKNN_BACKEND", raising=False)
    monkeypatch.delenv("ESK_RKNN_IO_MODE", raising=False)
    monkeypatch.delenv("RECAMERA_NPU_MODE", raising=False)
    monkeypatch.setattr(native, "library_path", lambda: "/fake/librknnrt.so")
    libraries = [GraphLib(inputs=((1, 3, 640, 640), (1, 80, 512))),
                 GraphLib(inputs=((1, 20),), dtype=6)]  # native integer token graph
    next_library = iter(libraries)
    monkeypatch.setattr(native, "_load", lambda: next(next_library))
    lites = []

    class ObservedLite(FakeRknnLite):
        def __init__(self):
            super().__init__()
            self.calls = []

        def inference(self, *, inputs):
            self.calls.append([np.array(x, copy=True) for x in inputs])
            return super().inference(inputs=inputs)

    def factory():
        result = ObservedLite()
        lites.append(result)
        return result

    monkeypatch.setattr(server, "_default_runtime_factory", factory)
    monkeypatch.setattr(engine, "_default_runtime_factory", factory)
    return libraries, lites


def test_old_flat_input_reproduces_reported_two_input_failure(tmp_path, monkeypatch, fake_native):
    policy = installed_policy(tmp_path, monkeypatch, legacy_input=True)
    item = policy["models"][0]
    assert len(item["inputs"]) == 1
    assert item["inputs"][0]["dtype"] == "uint8"
    backend = server.RknnBackend(coordinator=Coordinator())
    with pytest.raises(native.CtypesUnsupportedModel, match="requires one input, got 2"):
        backend.load(item["path"], {"inputs": item["inputs"]})
    libraries, lites = fake_native
    assert libraries[0].destroyed == 1 and lites == []


def test_fixed_package_loads_both_models_and_preserves_float_tensors(
        tmp_path, monkeypatch, fake_native):
    policy = installed_policy(tmp_path, monkeypatch)
    # The single-input uint8 shorthand must never authorize these mixed graphs.
    assert all(item["inputs"] == [] for item in policy["models"])
    backend = server.RknnBackend(coordinator=Coordinator())
    running = RunningService(tmp_path, backend=backend,
                             authorizer=PolicyAuthorizer(tmp_path, policy))
    monkeypatch.setenv("RECAMERA_INFERENCE_SERVICE_SOCK", running.socket)
    app = OpenVocabularyApp()
    sessions = []
    try:
        for item in policy["models"]:
            sessions.append(app._load_model(item["path"]))
        detector, encoder = sessions
        assert detector.strict_inputs and len(detector.spec.inputs) == 2
        assert encoder.strict_inputs and encoder.spec.inputs[0].dtype == "float32"
        tokens = ClipTokenizer(APP_DIR / "tokenizer").encode("cup")
        encoder.infer(tokens)
        image = np.zeros((1, 640, 640, 3), dtype=np.uint8)
        features = np.linspace(-1, 1, 80 * 512, dtype=np.float32).reshape(1, 80, 512)
        detector.infer([image, features])
        assert {item["backend"] for item in running.service.status()["models"]} == {"rknnlite"}
        libraries, lites = fake_native
        assert [lib.destroyed for lib in libraries] == [1, 1]
        np.testing.assert_array_equal(lites[0].calls[0][0], image)
        np.testing.assert_array_equal(lites[0].calls[0][1], features)
        np.testing.assert_array_equal(lites[1].calls[0][0], tokens)
        assert lites[0].calls[0][1].dtype == np.float32
        assert lites[1].calls[0][0].max() == 49407
    finally:
        for session in sessions:
            session.release()
        running.close()


def test_missing_service_does_not_create_local_npu_session(monkeypatch):
    monkeypatch.delenv("RECAMERA_INFERENCE_SERVICE_SOCK", raising=False)
    monkeypatch.delenv("RECAMERA_INFERENCE_SERVICE", raising=False)
    with pytest.raises(CapabilityError) as caught:
        OpenVocabularyApp()._load_model("models/yolo_world_v2s.rknn")
    assert caught.value.code == "scheduled_service_required"


def test_forced_service_ctypes_is_reported_without_an_app_side_bypass(
        tmp_path, monkeypatch, fake_native):
    policy = installed_policy(tmp_path, monkeypatch)
    monkeypatch.setenv("ESK_RKNN_BACKEND", "ctypes")
    running = RunningService(tmp_path, backend=server.RknnBackend(coordinator=Coordinator()),
                             authorizer=PolicyAuthorizer(tmp_path, policy))
    monkeypatch.setenv("RECAMERA_INFERENCE_SERVICE_SOCK", running.socket)
    try:
        with pytest.raises(CapabilityError) as caught:
            OpenVocabularyApp()._load_model(policy["models"][0]["path"])
        assert caught.value.code == "open_vocabulary_backend_unsupported"
        assert "ESK_RKNN_BACKEND" in str(caught.value)
        assert fake_native[1] == []
    finally:
        running.close()


def test_model_spec_rejects_unknown_model_and_uses_explicit_image_geometry():
    with pytest.raises(ValueError, match="unexpected"):
        model_spec("other.rknn")
    assert OpenVocabularyApp.input_size == 640
