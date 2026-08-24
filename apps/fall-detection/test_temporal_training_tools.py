"""Dependency-light checks for the Pro-native temporal training artifacts."""

from __future__ import annotations

import importlib.util
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

import numpy as np


HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_exported_profile_is_production_loader_compatible(tmp_path):
    training = load("pro_temporal_training_tool", HERE / "tools" /
                    "train_freeze_temporal_mlp.py")
    app = load("pro_temporal_app_for_export_test", HERE / "app.py")
    feature_dim = app._TemporalProfile.feature_dim
    hidden = 2
    scaler = SimpleNamespace(
        mean_=np.zeros(feature_dim, dtype=np.float32),
        scale_=np.ones(feature_dim, dtype=np.float32),
    )
    model = SimpleNamespace(
        coefs_=[np.zeros((feature_dim, hidden), dtype=np.float32),
                np.zeros((hidden, 1), dtype=np.float32)],
        intercepts_=[np.zeros(hidden, dtype=np.float32),
                     np.zeros(1, dtype=np.float32)],
    )
    profile_path = tmp_path / "profile.json.gz"
    training.export_profile(
        profile_path, scaler, model,
        {"threshold": 0.75, "consecutive": 2},
        np.ones(app._TemporalProfile.frame_dim, dtype=np.float32),
        {"pose_model_sha256": "test"},
    )
    profile = app._TemporalProfile(str(profile_path))
    assert profile.w1.shape == (feature_dim, hidden)
    assert profile.threshold == 0.75
    assert profile.consecutive == 2


@unittest.skipUnless(
    os.environ.get("RECAMERA_RELEASE_ARTIFACT_TESTS") == "1",
    "requires the adjacent sscma-example-sg200x training repository",
)
def test_default_shared_training_module_exists():
    training = load("pro_temporal_training_path_test", HERE / "tools" /
                    "train_freeze_temporal_mlp.py")
    assert training.default_training_module().is_file()


def test_development_loader_does_not_read_subject4_metadata(tmp_path, monkeypatch):
    training = load("pro_temporal_split_firewall_test", HERE / "tools" /
                    "train_freeze_temporal_mlp.py")
    dataset = tmp_path / "dataset"
    traces = tmp_path / "traces"
    (dataset / "subject-1").mkdir(parents=True)
    (dataset / "subject-4").mkdir(parents=True)
    (dataset / "subject-1" / "Fall.csv").write_text(
        "file,label\n01.mp4,fall [1.0]\n", encoding="utf-8")
    (dataset / "subject-4" / "Fall.csv").write_text(
        "THIS MUST STAY UNREAD\n", encoding="utf-8")
    traces.mkdir()

    original = Path.read_text
    reads = []

    def audited_read(path, *args, **kwargs):
        reads.append(Path(path))
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", audited_read)
    assert training.load_subject_clips(
        SimpleNamespace(), traces, dataset, {1}) == []
    assert dataset / "subject-1" / "Fall.csv" in reads
    assert dataset / "subject-4" / "Fall.csv" not in reads


def test_trace_extractor_probes_with_ffmpeg_when_ffprobe_is_absent(monkeypatch):
    extractor = load("pro_trace_probe_fallback_test", HERE / "tools" /
                     "extract_pose_traces.py")
    monkeypatch.setattr(extractor.shutil, "which", lambda name: None)
    monkeypatch.setattr(
        extractor.subprocess, "run",
        lambda *args, **kwargs: SimpleNamespace(
            stderr="Stream #0:0: Video: h264, yuv420p, 1280x720 [SAR 1:1]"))
    assert extractor.probe_size(Path("clip.mp4")) == (1280, 720)
