"""Reload the exact archived App without SQLite, then run a controlled frame.

The official smoke helper imports the App before invoking its external hook.
Reloading the extracted entry and its local helpers here is intentional: only
that second real Kit load proves startup works when firmware omits SQLite.
"""
import builtins
from contextlib import contextmanager
import importlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace

import cv2
import numpy as np


@contextmanager
def without_sqlite():
    forbidden = {"sqlite3", "_sqlite3"}
    attempts = []
    existing = {name: value for name, value in sys.modules.copy().items()
                if name.split(".", 1)[0] in forbidden}
    for name in existing:
        sys.modules.pop(name)
    original_import = builtins.__import__

    def reject(name):
        if name.split(".", 1)[0] in forbidden:
            attempts.append(name)
            raise ModuleNotFoundError("controlled firmware omits " + name, name=name)

    def guarded_import(name, *args, **kwargs):
        reject(name)
        return original_import(name, *args, **kwargs)

    class Finder:
        def find_spec(self, fullname, path=None, target=None):
            reject(fullname)
            return None

    finder = Finder()
    builtins.__import__ = guarded_import
    sys.meta_path.insert(0, finder)
    try:
        # Verify both import mechanisms before exercising the actual archive.
        for name in sorted(forbidden):
            for load in (builtins.__import__, importlib.import_module):
                try:
                    load(name)
                except ModuleNotFoundError as exc:
                    assert exc.name == name
                else:
                    raise AssertionError("missing-module guard is not active")
        yield attempts
    finally:
        builtins.__import__ = original_import
        sys.meta_path.remove(finder)
        sys.modules.update(existing)


def _exercise(app):
    module = sys.modules[type(app).__module__]

    class Client:
        server_url = "https://controlled.example"

        def __init__(self, *args):
            self.receipts = []

        def configuration(self):
            return {"device_id": "smoke-camera", "project_id": 1,
                    "capture_enabled": True, "capture_interval_seconds": 1}

        def heartbeat(self, values):
            return {"received": True}

        def upload(self, entry):
            self.receipts.append(entry)
            return {"received": True, "capture_id": entry["capture_id"], "asset_id": 1}

    old_client = module.EdgeClient
    previous_root = os.environ.get("APPMGR_APPDATA_DIR")
    try:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["APPMGR_APPDATA_DIR"] = tmp
            module.EdgeClient = Client
            app.setup({"server_url": "https://controlled.example", "device_token": "cvsprout_device_smoke_only"})
            rgb = np.zeros((40, 80, 3), dtype=np.uint8)
            rgb[..., 0] = 255

            def frames():
                deadline = time.monotonic() + 3
                while not app.uploader.capture_state()["enabled"]:
                    assert time.monotonic() < deadline
                    time.sleep(0.01)
                yield SimpleNamespace(data=rgb, w=80, h=40, fmt="RGB", pts=1)

            app.frames = frames
            seen = []
            app.emit = lambda events, pts, **kwargs: seen.append(kwargs)
            app.run()
            assert seen[0]["extra"]["capture_state"] == "capturing"
            assert app.capture_count == 1
            app.uploader.stop.clear()
            app.uploader.step()
            receipt = app.uploader.client.receipts[0]
            pixels = cv2.imdecode(np.frombuffer(receipt["jpeg"], np.uint8), cv2.IMREAD_COLOR)
            assert pixels.shape == (40, 80, 3) and pixels[..., 2].mean() > 250
            assert app.uploader.spool.stats()["pending_images"] == 0
            assert app.upload_thread is None
    finally:
        module.EdgeClient = old_client
        if previous_root is None:
            os.environ.pop("APPMGR_APPDATA_DIR", None)
        else:
            os.environ["APPMGR_APPDATA_DIR"] = previous_root
    return {"frames": 1, "delivered": 1, "original_rgb_verified": True, "queue_acknowledged": True}


def smoke(app):
    from kit.run import find_app, load_app_module

    entry = Path(sys.modules[type(app).__module__].__file__).resolve()
    # The path is supplied by the official verified-archive loader. Never load
    # from a developer checkout to make a broken archive's test pass.
    local_modules = {name: module for name, module in sys.modules.copy().items()
                     if getattr(module, "__file__", None)
                     and Path(module.__file__).resolve().parent == entry.parent}
    for name in local_modules:
        sys.modules.pop(name)
    try:
        with without_sqlite() as attempts:
            reloaded = find_app(load_app_module(str(entry)))
            result = _exercise(reloaded)
            assert "sqlite3" not in sys.modules and "_sqlite3" not in sys.modules
            result.update(sqlite3_unavailable=True, _sqlite3_unavailable=True,
                          exact_archive_reloaded_with_real_kit=True,
                          import_guard_verified=attempts[:4] == ["_sqlite3", "_sqlite3", "sqlite3", "sqlite3"])
            assert result["import_guard_verified"]
            # The official helper ignores hook return values; retain explicit
            # compatibility evidence in its bounded log_tail as well.
            print("capture_portability: " + json.dumps(result, sort_keys=True))
            return result
    finally:
        for name, module in list(sys.modules.items()):
            if (getattr(module, "__file__", None)
                    and Path(module.__file__).resolve().parent == entry.parent):
                sys.modules.pop(name)
        sys.modules.update(local_modules)
