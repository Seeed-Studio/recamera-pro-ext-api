"""Offline checks for the overlay-plugin demo: geometry pipeline, manifest
contract, and the ui.overlay -> web/overlay.html binding (bytes hash, entry
name, self-contained document, port handshake)."""
import hashlib
import importlib.util
import json
import math
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "market"))

from kit.geometry import GeometryBuilder, sanitize_geometry  # noqa: E402


def _load():
    spec = importlib.util.spec_from_file_location("opd_app", os.path.join(HERE, "app.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _frame_geometry(app, frames, w=1280, h=720):
    items = None
    for _ in range(frames):
        app._frame_no += 1
        t = 1000.0 + app._frame_no / 15.0
        g = GeometryBuilder()
        app._draw_zone(g, w, h, t)
        app._draw_anchor(g, w, h, t)
        items = g.build()
    return items


def _manifest():
    with open(os.path.join(HERE, "manifest.json")) as stream:
        return json.load(stream)


def _overlay_html():
    with open(os.path.join(HERE, "web", "overlay.html"), "rb") as stream:
        return stream.read()


def test_geometry_survives_hub_sanitize():
    module = _load()
    manifest = _manifest()
    policy = manifest["render"]["geometry"]
    app = module.OverlayPluginDemoApp()
    app.setup({})
    for w, h in ((1280, 720), (640, 360)):
        items = _frame_geometry(app, 40, w, h)
        # strict=True path: every item re-validated by the strict builder
        GeometryBuilder().extend(items)
        clean = sanitize_geometry(items, space="pixel_points",
                                  allowed_types=policy["types"],
                                  max_items=policy["max_items"],
                                  max_points=policy["max_points"],
                                  default_style=policy["style"],
                                  frame_size=(w, h))
        assert len(clean) == len(items) == 2
        kinds = {item["type"] for item in clean}
        assert kinds == {"polygon", "point"}
        assert all(item["space"] == "pixel_points" for item in clean)
        labels = [item for item in clean if "label" in item]
        assert len(labels) == 1
        assert labels[0]["label"].startswith("frame #")


def test_manifest_and_overlay_declaration_validate():
    from appmgr import manifest as contract
    manifest = _manifest()
    assert contract.validate_manifest(manifest) == 2
    declared = contract.validate_overlay_declaration(manifest["ui"])
    assert declared["entry"] == "web/overlay.html"
    # the declared digest is the digest of the shipped bytes: exactly what the
    # installer enforces and the /overlay route's ?h= must match
    assert declared["sha256"] == hashlib.sha256(_overlay_html()).hexdigest()


def test_overlay_html_is_self_contained():
    html = _overlay_html().decode("utf-8")
    # scan the effective document: comments may mention forbidden APIs
    html = re.sub(r"<!--.*?-->", "", html, flags=re.DOTALL)
    for pattern in (r"<link\b", r"\bsrc\s*=", r"\bhref\s*=", r"url\(",
                    r"@import\b", r"https?://", r"\bfetch\s*\(",
                    r"XMLHttpRequest", r"\bWebSocket\b", r"localStorage",
                    r"sessionStorage", r"document\.cookie",
                    r"parent\.document", r"window\.open"):
        assert re.search(pattern, html, re.IGNORECASE) is None, pattern


def test_overlay_html_reads_injected_port_and_posts_one_ready():
    html = _overlay_html().decode("utf-8")
    assert "window.__overlayPort" in html
    assert '"recamera.overlay.plugin"' in html
    # the ready handshake: protocol 1, sent once, over the port
    assert re.search(r"type:\s*\"ready\"", html)
    assert re.search(r"protocol:\s*PROTOCOL", html)
    # sanity: the two startup chips exist so a mounted plugin is visible
    assert "overlay plugin ready" in html
