"""config_schema.json 单一事实来源的结构与派生测试（纯离线，无设备）。

覆盖三条不变量：
  1. schema 自身结构合法（类型已知、默认值在范围内、键不重复）；
  2. daemon 派生表（CONFIG_SPEC/DEFAULTS/RESTART_KEYS）符合预期；
  3. 出厂模板 config.json 与 schema 派生默认值一致——防止第三份拷贝漂移。
"""
import json
import os

import config_schema
from config_schema import SCHEMA_PATH, coerce_value, derive_tables, load_schema

SERVICE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_schema_structure():
    schema = load_schema()
    assert schema["groups"], "至少一个 UI 分组"
    keys = []
    for item in config_schema.iter_items(schema):
        keys.append(item["key"])
        assert item["type"] in ("number", "integer", "string", "password"), \
            f"{item['key']}: 未知类型 {item['type']}"
        assert "default" in item, f"{item['key']}: 缺少 default"
        if item["type"] in ("number", "integer"):
            assert item["min"] <= item["default"] <= item["max"], \
                f"{item['key']}: 默认值越界"
            if "step" in item:
                assert item["step"] > 0
        assert item.get("apply", "live") in ("live", "restart")
    assert len(keys) == len(set(keys)), "参数键重复"


def test_derive_tables():
    spec, defaults, restart = derive_tables(load_schema())
    # 类型映射与范围
    assert spec["sq_diff_threshold"] == ("float", 1, 65025)
    assert spec["motion_hold_ms"] == ("int", 0, 600000)
    assert spec["mqtt_pass"][0] == "str"
    # 默认值经类型转换
    assert isinstance(defaults["sq_diff_threshold"], float)
    assert isinstance(defaults["sample_fps"], float)
    assert isinstance(defaults["analysis_width"], int)
    assert defaults["http_port"] == 8099
    # 重启键由 apply=restart 派生
    assert restart == {"sample_fps"}
    # internal 参数不进 UI groups
    ui_keys = {item["key"] for group in load_schema()["groups"]
               for item in group["items"]}
    assert not ({"http_host", "http_port", "http_token"} & ui_keys)


def test_derive_tables_rejects_bad_schema():
    bad = {"groups": [{"items": [
        {"key": "x", "type": "integer", "min": 0, "max": 10, "default": 99}]}]}
    try:
        derive_tables(bad)
    except ValueError:
        return
    raise AssertionError("默认值越界应抛 ValueError")


def test_factory_config_matches_defaults():
    _, defaults, _ = derive_tables(load_schema())
    with open(os.path.join(SERVICE_ROOT, "config.json"),
              encoding="utf-8") as stream:
        factory = json.load(stream)
    assert factory == defaults, \
        "config.json 是出厂模板，必须与 schema 默认值一致"


def test_coerce_value_boundaries():
    assert coerce_value(("float", 0.0, 1.0), "0.05") == 0.05
    assert coerce_value(("int", 0, 10), 11) is None
    assert coerce_value(("int", 0, 10), "abc") is None
    assert coerce_value(("str", None, None), 123) == "123"
