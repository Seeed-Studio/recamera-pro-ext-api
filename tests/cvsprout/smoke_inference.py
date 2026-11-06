"""Used by the official helper against the final extracted archive only."""
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
from kit.config import app_dir_of
from kit.runtime.preprocess import LetterboxInfo


def smoke(app):
    root = Path(app_dir_of(app))
    contract = json.loads((root / "model_contract.json").read_text())
    size, labels = contract["input_size"], contract["classes"]
    app._pre_size, app.class_names = size, labels
    app.setup({"conf": 0.35, "iou": 0.45})
    output = np.zeros((1, 4 + len(labels), 8400), dtype=np.float32)
    output[0, :4, 0] = [size / 2, size / 2, size / 4, size / 8]
    output[0, 4, 0] = 0.9
    prepared = SimpleNamespace(info=LetterboxInfo(size / 1280, 0, size * 0.21875, 1280, 720))
    app.frames = lambda: iter([SimpleNamespace(pts=1.25)])
    app.pre = lambda frame: prepared
    app.models = SimpleNamespace(det=SimpleNamespace(infer=lambda value: [output]))
    seen = []
    app.emit = lambda events, pts, **kwargs: seen.append((events, pts, kwargs))
    app.run()
    assert len(seen) == 1 and seen[0][1] == 1.25
    result = seen[0][2]["results"]
    assert result == [{"box": [480.0, 280.0, 800.0, 440.0], "cls": 0,
                       "cls_name": labels[0], "score": 0.9}], result
    assert seen[0][2]["extra"]["model_release"] == contract["release_id"]
    return {"frames": 1, "detections": 1, "custom_labels": labels, "original_box_verified": True}
