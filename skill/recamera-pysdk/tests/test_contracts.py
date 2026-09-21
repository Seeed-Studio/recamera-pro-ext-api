import ast
import copy
import json
import re
import shutil

import pytest

from .conftest import REPO, SKILL
from sdk_contract import (LOCK, api_signatures, bundled_sdk_root, check_drift,
                          load_sdk_contract, provenance, validate_sdk_builder, ContractError)
from source_contract import inspect_sources
from validate_app import validate_app


def gateway(manifest, mode="brokered"):
    manifest["resources"]["claims"] = [{"name": "result.publish", "mode": mode, "required": True}]
    manifest["permissions"]["sdk"] = ["result.publish"]


def analyze(source, files=None, require_entry=True):
    trees = [(p, ast.parse(s)) for p, s in dict({"app.py": source}, **(files or {})).items()]
    return inspect_sources(trees, "app.py", LOCK["kit_api"], require_entry)


def test_pinned_source_and_api_are_current():
    assert check_drift(REPO)["matches"]
    assert api_signatures((REPO / "kit/app.py").read_text()) == LOCK["kit_api"]
    validate_sdk_builder(bundled_sdk_root())


def test_drift_detection_does_not_update(tmp_path):
    (tmp_path / "kit").mkdir()
    path = tmp_path / "kit/app.py"
    path.write_text("changed")
    report = check_drift(tmp_path)
    assert not report["matches"]
    assert any(c["file"] == "kit/app.py" for c in report["changes"])
    assert path.read_text() == "changed"


def test_bundled_hash_tampering(tmp_path, monkeypatch):
    import sdk_contract
    copy_root = tmp_path / "sdk"
    shutil.copytree(bundled_sdk_root(), copy_root)
    monkeypatch.setattr(sdk_contract, "bundled_sdk_root", lambda: copy_root)
    (copy_root / "market/appmgr/manifest.py").write_text("# stale")
    with pytest.raises(ContractError, match="hash mismatch"):
        validate_sdk_builder(copy_root)


def test_local_provenance():
    report = provenance(REPO)
    assert re.fullmatch(r"[0-9a-f]{40}", report["sdk_source_commit"])
    assert isinstance(report["dirty"], bool)
    assert len(report["files_sha256"]) == 3


@pytest.mark.parametrize("field", [
    {"type": "select", "options": ["a", "b"], "default": "a"},
    {"type": "password", "default": ""},
    {"type": "array", "default": []},
    {"type": "object", "default": {}},
    {"type": "field_mapping", "default": []},
    {"type": "output_filters", "default": {}},
    {"type": "number", "default": 0.25, "apply": "reschedule"},
])
def test_current_config_types(field, manifest, make_app):
    item = dict(key="value", apply="live", **{k: v for k, v in field.items() if k != "apply"})
    item["apply"] = field.get("apply", "live")
    manifest["config_schema"] = {"groups": [{"key": "general", "title": "General", "items": [item]}]}
    report = validate_app(make_app(), "package")
    assert report["valid"], report["errors"]


def test_config_uses_selected_official_validator(tmp_path, manifest, make_app):
    sdk = tmp_path / "sdk"
    shutil.copytree(bundled_sdk_root(), sdk)
    path = sdk / "market/appmgr/manifest.py"
    path.write_text(path.read_text() + '\n_original_config_validator = _validate_config_schema\ndef _validate_config_schema(value):\n    raise ValueError("selected contract sentinel")\n')
    report = validate_app(make_app(), "package", sdk_root=sdk)
    assert any("selected contract sentinel" in e["message"] for e in report["errors"])
    assert report["builder"]["sdk_source_commit"] is None
    assert report["builder"]["dirty"] is None


@pytest.mark.parametrize("app_id", ["builtin", "acousticslab"])
def test_reserved_ids(app_id, manifest, make_app):
    manifest["id"] = app_id
    report = validate_app(make_app(), "package")
    assert any(e["code"] == "firmware_manifest_contract" for e in report["errors"])


def test_official_recording_manifests():
    contract = load_sdk_contract(bundled_sdk_root())
    for path in (REPO / "apps").glob("*/manifest.json"):
        manifest = json.loads(path.read_text())
        if "record_trigger" in manifest:
            assert contract.validate_manifest(manifest, allow_v1=False) == 2


def test_recording_event_authorization(manifest, make_app):
    fragment = json.loads(re.search(r"```json\n(.*?)\n```", (SKILL / "references/recording.md").read_text(), re.S)[1])
    manifest.update(fragment)
    gateway(manifest)
    source = 'from kit.app import App\nclass Demo(App):\n    owns_loop = True\n    def run(self):\n        self.request_recording("alarm", ts=1.0)\n'
    app = make_app(source)
    assert validate_app(app, "package")["valid"]
    (app / "app.py").write_text(source.replace('"alarm"', '"undeclared"'))
    report = validate_app(app, "package")
    assert any(e["code"] == "undeclared_recording_event" for e in report["errors"])
    bad = copy.deepcopy(manifest)
    bad["record_trigger"]["signals"][0]["event_kind"] = "not-declared"
    with pytest.raises(ValueError):
        load_sdk_contract(bundled_sdk_root()).validate_manifest(bad)


def test_plain_sdk_demo_is_not_installable(make_app):
    app = make_app('from recamera_ext import FrameSource\nwith FrameSource() as src:\n    for frame in src:\n        pass\n')
    assert validate_app(app, "demo")["valid"]
    report = validate_app(app, "package")
    assert not report["valid"]
    assert report["source_contract"]["state"] == "invalid"


@pytest.mark.parametrize("entry,files", [
    ('from kit.app import App as Base\nclass Demo(Base):\n owns_loop=True\n def run(self): pass\n', {}),
    ('import kit.app as k\nclass Demo(k.App):\n owns_loop=True\n def run(self): pass\nAPP=Demo()\n', {}),
    ('from kit.app import App\nclass Base(App):\n owns_loop=True\n def run(self): pass\nclass Demo(Base): pass\n', {}),
    ('from implementation import Demo\n', {"implementation.py": 'from kit.app import App\nclass Demo(App):\n owns_loop=True\n def run(self): pass\n'}),
    ('from package import Demo\n', {"package/__init__.py": 'from .impl import Demo\n', "package/impl.py": 'from kit.app import App\nclass Demo(App):\n owns_loop=True\n def run(self): pass\n'}),
    ('from kit.app import App\nclass A(App):\n owns_loop=True\n def run(self): pass\nclass B(App):\n owns_loop=True\n def run(self): pass\nAPP=B\n', {}),
])
def test_valid_loader_patterns(entry, files):
    report = analyze(entry, files)
    assert report["state"] == "resolved", report
    assert not report["issues"], report


@pytest.mark.parametrize("source,expected", [
    ("APP = 1", "invalid"),
    ("from factory import make_app\nAPP = make_app()", "unverified"),
    ("from external_base import Base\nclass Demo(Base): pass", "unverified"),
    ("from kit.app import App\nclass A(App): pass\nclass B(App): pass", "invalid"),
    ("if True:\n from kit.app import App\n class Demo(App): pass", "unverified"),
])
def test_unknown_and_ambiguous_entries(source, expected):
    assert analyze(source)["state"] == expected


@pytest.mark.parametrize("call,valid", [
    ("self.emit(events=[], frame=1, results=[])", False),
    ("self.emit(events=[], ts=1, results=[], geometry=[], extra={})", True),
    ("self.emit([], 1, [])", False),
    ("self.request_recording()", False),
    ('self.request_recording("alarm", ts=1)', True),
    ("self.pre(frame=frame)", True),
    ("self.crop_roi_hw(frame, box, out_size=128, pad=0.2)", True),
    ("self.frames(1)", False),
])
def test_api_call_signature(call, valid):
    report = analyze("from kit.app import App\nclass Demo(App):\n owns_loop=True\n def run(self):\n  " + call)
    assert (not any(i["code"] == "invalid_kit_api_call" for i in report["issues"])) == valid


@pytest.mark.parametrize("name", ["OsdSink", "RecordSink"])
def test_appmgr_only_exports(name):
    report = analyze(f"from recamera_ext import {name} as Private\nPrivate()", require_entry=False)
    assert any(e["code"] == "appmgr_only_api" for e in report["issues"])


def test_gateway_and_direct_are_distinct(manifest, make_app):
    gateway(manifest, "shared")
    direct = 'from kit.app import App\nfrom recamera_ext import ResultSink\nclass Demo(App):\n owns_loop=True\n def run(self):\n  with ResultSink() as sink: pass\n'
    app = make_app(direct)
    assert validate_app(app, "package")["valid"]
    (app / "app.py").write_text(direct.replace('with ResultSink() as sink: pass', 'self.emit(results=[])'))
    report = validate_app(app, "package")
    assert any(e["code"] == "managed_result_claim_not_brokered" for e in report["errors"])


def test_data_only_detection_needs_no_renderer(manifest, make_app):
    gateway(manifest)
    manifest["output"] = {"contract_version": 2, "sink": "ws", "schema": "Detection data",
                          "default_channel": ["ws"], "default_mode": "raw", "default_mapping": [], "fields": [
        {"name": "box", "from": "results[].box", "type": "array", "description": "Box"}]}
    app = make_app('from kit.app import App\nclass Demo(App):\n owns_loop=True\n def run(self): self.emit(results=[])\n')
    report = validate_app(app, "package")
    assert report["valid"], report["errors"]
    assert not any("render" in e["code"] or "osd" in e["code"] for e in report["warnings"])


@pytest.mark.parametrize("key", ["RECAMERA_APP_ID", "RECAMERA_APP_INSTANCE", "RECAMERA_APP_GENERATION", "RECAMERA_RESULT_GATEWAY_REQUIRED"])
def test_managed_identity_not_app_owned(key, make_app):
    app = make_app(f'import os\nos.environ["{key}"] = "custom"\n')
    report = validate_app(app, "demo")
    assert any(e["code"] == "manual_managed_gateway_override" for e in report["errors"])


def test_documented_python_snippets_parse_and_match_kit_api():
    for path in (SKILL / "references").glob("*.md"):
        for index, code in enumerate(re.findall(r"```python\n(.*?)\n```", path.read_text(), re.S)):
            tree = ast.parse(code, filename=f"{path.name}:{index}")
            report = inspect_sources([("app.py", tree)], "app.py", LOCK["kit_api"], False)
            assert not [e for e in report["issues"] if e["code"] == "invalid_kit_api_call"], (path, report)


def test_documented_detector_preserves_deferred_input():
    from types import SimpleNamespace
    text = (SKILL / "references/kit-app-patterns.md").read_text()
    code = re.search(r"```python\n(.*?)\n```", text, re.S)[1]
    namespace = {"__name__": "skill_example"}
    exec(compile(code, "documented_detector", "exec"), namespace)
    app_class = namespace["MyApp"]
    assert app_class.model_dma_input is True
    class Deferred:
        info = object()
        @property
        def data(self):
            pytest.fail("Template materialized deferred input before model inference")
    prepared = Deferred()
    inputs, emissions = [], []
    app = app_class.__new__(app_class)
    app.frames = lambda: iter([SimpleNamespace(pts=1.25)])
    app.pre = lambda frame: prepared
    app.models = SimpleNamespace(det=SimpleNamespace(infer=lambda value: inputs.append(value) or []))
    app.conf, app.iou = 0.5, 0.5
    app.emit = lambda *args, **kwargs: emissions.append((args, kwargs))
    namespace["postprocess"] = lambda outs, info, **kw: []
    app.run()
    assert inputs == [prepared]
    assert emissions == [(([], 1.25), {"results": []})]


def test_known_non_app_explicit_entry_is_invalid():
    report = analyze("class Ordinary: pass\nAPP = Ordinary()")
    assert report["state"] == "invalid"


def test_static_validation_does_not_execute_entry(make_app, tmp_path):
    sentinel = tmp_path / "must-not-exist"
    app = make_app(f'open({str(sentinel)!r}, "w").write("side effect")\nfrom kit.app import App\nclass Demo(App):\n owns_loop=True\n def run(self): pass\n')
    assert validate_app(app, "package")["source_contract"]["state"] == "resolved"
    assert not sentinel.exists()


def test_mixin_mro_is_unverified_instead_of_incorrectly_rejected():
    source = 'from kit.app import App\nclass LoopMixin:\n owns_loop=True\n def run(self): pass\nclass Demo(LoopMixin, App): pass\n'
    report = analyze(source)
    assert report["state"] == "unverified"
    assert not [e for e in report["issues"] if e["severity"] == "error"]
