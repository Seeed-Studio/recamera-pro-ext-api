"""Offline checks: the demo's per-frame geometry passes strict builders and Hub sanitizing."""
import importlib.util
import json
import os
import sys
from types import SimpleNamespace

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)

from kit.geometry import GeometryBuilder, sanitize_geometry  # noqa: E402


def _load():
    spec = importlib.util.spec_from_file_location("ogd_app", os.path.join(HERE, "app.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _frame_geometry(app, frames, w=1280, h=720):
    items = None
    for _ in range(frames):
        app._frame_no += 1
        phase = app._frame_no / 15.0
        import math
        app._wave.append(math.sin(phase) * 0.7 + 0.3 * math.sin(phase * 3.1))
        g = GeometryBuilder()
        app._draw_skeleton(g, w, h, phase)
        app._draw_panel(g, w, h)
        app._draw_chart(g, w, h)
        items = g.build()
    return items


def test_geometry_survives_hub_sanitize():
    module = _load()
    manifest = json.load(open(os.path.join(HERE, "manifest.json")))
    policy = manifest["render"]["geometry"]
    app = module.OverlayGeometryDemoApp()
    app.setup({})
    for w, h in ((1280, 720), (640, 360)):
        items = _frame_geometry(app, 200, w, h)
        # strict=True path: every item re-validated by the strict builder
        GeometryBuilder().extend(items)
        clean = sanitize_geometry(items, space="pixel_points",
                                  allowed_types=policy["types"],
                                  max_items=policy["max_items"],
                                  max_points=policy["max_points"],
                                  default_style=policy["style"],
                                  frame_size=(w, h))
        assert len(clean) == len(items) == 44
        assert all(item["space"] == "pixel_points" for item in clean)
        labels = [item for item in clean if "label" in item]
        assert 6 <= len(labels) < 48
        assert any(item["label"].startswith("frame #") for item in labels)


def test_manifest_validates():
    sys.path.insert(0, os.path.join(ROOT, "market"))
    from appmgr import manifest as contract
    data = json.load(open(os.path.join(HERE, "manifest.json")))
    assert contract.validate_manifest(data) == 2
