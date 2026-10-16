import importlib.util
import json
import sys
from pathlib import Path


APP_DIR = Path(__file__).parents[1]
_old_rtsp = sys.modules.pop("rtsp_source", None)
sys.path.insert(0, str(APP_DIR))
try:
    spec = importlib.util.spec_from_file_location("waste_sorting_app_under_test",
                                                  APP_DIR / "app.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
finally:
    sys.path.remove(str(APP_DIR))
    sys.modules.pop("rtsp_source", None)
    if _old_rtsp is not None:
        sys.modules["rtsp_source"] = _old_rtsp


def test_default_is_bundled_path_and_manifest_has_no_legacy_default():
    app = module.WasteSortingApp()
    app._manifest = json.loads((APP_DIR / "manifest.json").read_text(encoding="utf-8"))
    app._bind_params({})
    assert app.model_path == module.DEFAULT_MODEL_PATH
    assert "/userdata/local/models/waste-sorting/" not in json.dumps(app._manifest)


def test_explicit_config_path_overrides_bundled_default(tmp_path):
    app = module.WasteSortingApp()
    app._manifest = json.loads((APP_DIR / "manifest.json").read_text(encoding="utf-8"))
    custom = str(tmp_path / "custom.rknn")
    app._bind_params({"model_path": custom})
    assert app.model_path == custom


def test_cleared_config_restores_bundled_path():
    assert module.resolve_model_path("") == module.DEFAULT_MODEL_PATH
    assert module.resolve_model_path("   ") == module.DEFAULT_MODEL_PATH
    assert module.resolve_model_path(None) == module.DEFAULT_MODEL_PATH


def test_exact_legacy_default_migrates_when_missing(tmp_path):
    bundled = tmp_path / "bundled.rknn"
    bundled.write_bytes(b"model")
    legacy = str(tmp_path / "missing-legacy.rknn")
    assert module.resolve_model_path(legacy, bundled_path=str(bundled),
                                    legacy_path=legacy) == str(bundled)


def test_custom_missing_path_is_not_silently_replaced(tmp_path):
    bundled = tmp_path / "bundled.rknn"
    bundled.write_bytes(b"model")
    custom = str(tmp_path / "custom.rknn")
    assert module.resolve_model_path(custom, bundled_path=str(bundled),
                                    legacy_path=str(tmp_path / "legacy.rknn")) == custom


def test_existing_legacy_path_is_not_migrated(tmp_path):
    bundled = tmp_path / "bundled.rknn"
    bundled.write_bytes(b"model")
    legacy = tmp_path / "legacy.rknn"
    legacy.write_bytes(b"old")
    assert module.resolve_model_path(str(legacy), bundled_path=str(bundled),
                                    legacy_path=str(legacy)) == str(legacy)


def test_missing_bundled_model_does_not_fallback(tmp_path):
    legacy = str(tmp_path / "missing-legacy.rknn")
    bundled = str(tmp_path / "missing-bundled.rknn")
    assert module.resolve_model_path(legacy, bundled_path=bundled,
                                    legacy_path=legacy) == legacy
