"""Capture startup and durable delivery on Python builds without SQLite."""
from pathlib import Path
import subprocess
import sys


SDK_ROOT = Path(__file__).resolve().parents[2]


def test_real_kit_setup_and_spool_roundtrip_without_sqlite(tmp_path):
    # Use a clean process: test discovery or a desktop plugin may have loaded
    # SQLite already. The guard is active even before Kit or the App imports.
    script = r'''
import builtins
import importlib
import json
import os
from pathlib import Path
import sys

root, data = map(Path, sys.argv[1:])
sys.path[:0] = [str(root), str(root / "sdk/python")]
os.environ["APPMGR_APPDATA_DIR"] = str(data)
for name in list(sys.modules):
    if name.split(".", 1)[0] in {"sqlite3", "_sqlite3"}:
        del sys.modules[name]
original_import = builtins.__import__
attempts = []

def reject(name):
    if name.split(".", 1)[0] in {"sqlite3", "_sqlite3"}:
        attempts.append(name)
        raise ModuleNotFoundError("controlled firmware omits " + name, name=name)

def guarded_import(name, *args, **kwargs):
    reject(name)
    return original_import(name, *args, **kwargs)

class Finder:
    def find_spec(self, fullname, path=None, target=None):
        reject(fullname)
        return None

builtins.__import__ = guarded_import
sys.meta_path.insert(0, Finder())
for name in ("sqlite3", "_sqlite3"):
    for load in (builtins.__import__, importlib.import_module):
        try:
            load(name)
        except ModuleNotFoundError as exc:
            assert exc.name == name
        else:
            raise AssertionError("SQLite import unexpectedly succeeded")
assert len(attempts) == 4

from kit.run import find_app, load_app_module
module = load_app_module(str(root / "apps/cvsprout-capture/app.py"))
app = find_app(module)
app.setup({"server_url": "https://controlled.example", "device_token": "cvsprout_device_test_token"})
spool = app.uploader.spool
assert spool.path == data / "cvsprout-capture/capture-spool"
assert spool.path.is_dir()
capture_id = spool.enqueue(b"controlled JPEG bytes", "owner-a", "2026-11-02T12:00:00+00:00", "batch-1")
assert capture_id and spool.stats()["pending_images"] == 1
assert spool.oldest("owner-b") is None
app.finish()

restarted = find_app(module)
restarted.setup({"server_url": "https://controlled.example", "device_token": "cvsprout_device_test_token"})
spool = restarted.uploader.spool
entry = spool.oldest("owner-a")
assert entry["capture_id"] == capture_id
assert entry["jpeg"] == b"controlled JPEG bytes"
try:
    spool.acknowledge(entry, {"received": True, "capture_id": "wrong", "asset_id": 1})
except RuntimeError:
    pass
else:
    raise AssertionError("mismatched receipt removed a capture")
assert spool.stats()["pending_images"] == 1
spool.acknowledge(entry, {"received": True, "capture_id": capture_id, "asset_id": 1})
assert spool.stats()["pending_images"] == 0
restarted.finish()
assert "sqlite3" not in sys.modules and "_sqlite3" not in sys.modules
print(json.dumps({"blocked_modules": ["sqlite3", "_sqlite3"], "kit_setup": True,
                  "spool_restart_and_ack": True, "import_guard_verified": len(attempts) >= 4}))
'''
    completed = subprocess.run([sys.executable, "-c", script, str(SDK_ROOT), str(tmp_path)],
                               cwd=tmp_path, capture_output=True, text=True, timeout=20)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert '"spool_restart_and_ack": true' in completed.stdout
    assert '"import_guard_verified": true' in completed.stdout
