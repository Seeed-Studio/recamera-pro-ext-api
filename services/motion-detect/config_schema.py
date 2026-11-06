"""motion-detect 配置 schema 的加载与派生（唯一事实来源 = config_schema.json）。

同一份 JSON 被两侧消费：

  - daemon（motion_daemon.py）：``derive_tables()`` 派生运行时校验表
    ``(CONFIG_SPEC, DEFAULTS, RESTART_KEYS)``；
  - appmgr（market/appmgr/motiondetect.py）：原样读取 ``groups`` 驱动
    App Center 配置页与服务端校验。

改参数只改 config_schema.json，两侧各自在 import 时加载。本模块刻意保持
纯标准库、无重依赖，供离线单测直接 import。
"""
import json
import os

SCHEMA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "config_schema.json")

# UI 类型 -> daemon 校验类型。boolean 暂未支持：新增该类型的参数时
# 需要同步扩展这里与 derive_tables 的默认值校验。
_TYPE_MAP = {"number": "float", "integer": "int", "string": "str",
             "password": "str"}


def load_schema(path=None) -> dict:
    with open(path or SCHEMA_PATH, "r", encoding="utf-8") as stream:
        return json.load(stream)


def iter_items(schema: dict):
    """按声明顺序遍历全部参数项：UI groups 在前，daemon 内部项在后。"""
    for group in schema.get("groups", []):
        yield from group.get("items", [])
    yield from schema.get("internal", [])


def coerce_value(spec, val):
    """按 (kind, lo, hi) 校验并转换单个值；非法返回 None。"""
    kind, lo, hi = spec
    try:
        val = int(val) if kind == "int" else \
              float(val) if kind == "float" else str(val)
    except (TypeError, ValueError):
        return None
    if kind in ("int", "float") and lo is not None and hi is not None \
            and not (lo <= val <= hi):
        return None
    return val


def derive_tables(schema: dict):
    """从 schema 派生 (CONFIG_SPEC, DEFAULTS, RESTART_KEYS)。

    schema 结构非法（未知类型 / 默认值越界）时抛 ValueError——这是打包
    或提交错误，必须响亮失败而不是带病运行。
    """
    spec, defaults, restart = {}, {}, set()
    for item in iter_items(schema):
        key = item["key"]
        kind = _TYPE_MAP.get(item.get("type"))
        if kind is None:
            raise ValueError(
                f"config_schema: {key!r} 未知类型 {item.get('type')!r}")
        spec[key] = (kind, item.get("min"), item.get("max"))
        default = coerce_value(spec[key], item.get("default"))
        if default is None:
            raise ValueError(
                f"config_schema: {key!r} 默认值非法或超范围")
        defaults[key] = default
        if item.get("apply") == "restart":
            restart.add(key)
    return spec, defaults, restart
