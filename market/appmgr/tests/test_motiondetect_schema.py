"""appmgr 与 daemon 共用 config_schema.json 的接线测试（防双份漂移回归）。"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
from appmgr import motiondetect

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
SCHEMA_FILE = os.path.join(REPO_ROOT, "services", "motion-detect",
                           "config_schema.json")


def test_appmgr_schema_is_the_shared_file():
    """appmgr 内存中的 CONFIG_SCHEMA 必须就是仓里那份 JSON。"""
    with open(SCHEMA_FILE, encoding="utf-8") as stream:
        shared = json.load(stream)
    assert shared["groups"], "schema 不应为空（检查加载路径回退）"
    assert motiondetect.CONFIG_SCHEMA == shared


def test_validate_uses_shared_bounds():
    clean, errors, modes = motiondetect._validate(
        {"sq_diff_threshold": 600, "min_changed_ratio": 0.01, "bogus": 1})
    assert errors == {"bogus": "unknown key"}
    assert clean == {"sq_diff_threshold": 600.0, "min_changed_ratio": 0.01}
    assert modes == {"live"}

    _, _, modes = motiondetect._validate({"sample_fps": 10})
    assert modes == {"restart"}

    _, errors, _ = motiondetect._validate({"min_changed_ratio": 0.9})
    assert "min_changed_ratio" in errors
