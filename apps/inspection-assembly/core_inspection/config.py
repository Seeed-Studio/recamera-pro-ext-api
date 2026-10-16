"""运行时配置加载。形状见 contracts/config.schema.json。

schema 是唯一事实来源：这里只做「读文件 + 补默认值 + 校验」，不额外发明字段。
jsonschema 装了就用它严格校验；设备上没装时退化成必填字段检查，
这样容器里不必为了启动而装 jsonschema。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

SCHEMA_RELATIVE = "contracts/config.schema.json"

MODEL_DEFAULTS = {"score_threshold": 0.25, "nms_threshold": 0.45}
RULES_DEFAULTS = {"min_score": 0.35, "min_defects": 1, "min_area": 0.0,
                  "consecutive_frames": 1, "ng_on_defect": True,
                  "ng_on_missing": True, "ng_on_extra": True,
                  "ng_on_dimension": True}
ASSEMBLY_DEFAULTS = {"match_distance": 0.15, "min_score": 0.25,
                     "report_extra": True}
MQTT_DEFAULTS = {"port": 1883, "qos": 0, "retain": False, "tls": False}
MODBUS_DEFAULTS = {"enabled": False, "bind": "0.0.0.0", "port": 502,
                   "unit_id": 1, "heartbeat_interval_s": 1.0}
VLM_DEFAULTS = {"enabled": False, "base_url": "http://127.0.0.1:8100",
                "timeout_ms": 4000, "language": "zh", "max_sentences": 4,
                "max_tokens": 320, "queue_size": 2, "breaker_failures": 5,
                "breaker_cooloff_s": 30.0, "jpeg_quality": 75,
                "topic": "inspection/{stream_id}/explanations"}
VLM_TRIGGER_DEFAULTS = {"min_confidence": 0.5, "on_missing_parts": True,
                        "min_interval_s": 5.0}
WEB_DEFAULTS = {"enabled": True, "bind": "0.0.0.0", "port": 8080,
                "preview_fps": 5.0}


class ConfigError(ValueError):
    """配置文件缺字段、类型不对或与 schema 不符。"""


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def find_schema(explicit: str | Path | None = None) -> Path | None:
    """定位 config schema。容器里 contracts/ 与 core-py/ 同级复制。"""
    if explicit is not None:
        path = Path(explicit)
        return path if path.exists() else None
    candidate = _repo_root() / SCHEMA_RELATIVE
    return candidate if candidate.exists() else None


def infer_kind(uri: str) -> str:
    """未显式给 kind 时按 uri 推断采集方式。"""
    lowered = uri.lower()
    if lowered.startswith(("rtsp://", "rtsps://")):
        return "rtsp"
    if lowered.startswith("/dev/video") or uri.isdigit():
        return "usb"
    return "file"


def _fill(section: dict[str, Any], defaults: dict[str, Any]) -> dict[str, Any]:
    merged = dict(defaults)
    merged.update(section)
    return merged


def apply_defaults(config: dict[str, Any]) -> dict[str, Any]:
    """把 schema 里声明的 default 落到实处，运行时代码不再写 `.get(..., x)`。"""
    filled = dict(config)
    filled["model"] = _fill(config.get("model", {}), MODEL_DEFAULTS)
    filled["rules"] = _fill(config.get("rules", {}), RULES_DEFAULTS)
    if "mqtt" in config:
        filled["mqtt"] = _fill(config["mqtt"], MQTT_DEFAULTS)
    filled["modbus"] = _fill(config.get("modbus", {}), MODBUS_DEFAULTS)
    filled["web"] = _fill(config.get("web", {}), WEB_DEFAULTS)
    if "assembly" in config:
        filled["assembly"] = _fill(config["assembly"], ASSEMBLY_DEFAULTS)
    if "vlm" in config:
        vlm = _fill(config["vlm"], VLM_DEFAULTS)
        vlm["trigger"] = _fill(vlm.get("trigger", {}), VLM_TRIGGER_DEFAULTS)
        filled["vlm"] = vlm
    sources = []
    for source in config.get("sources", []):
        entry = dict(source)
        entry.setdefault("kind", infer_kind(str(entry.get("uri", ""))))
        if "assembly" in entry:
            entry["assembly"] = _fill(entry["assembly"], ASSEMBLY_DEFAULTS)
        sources.append(entry)
    filled["sources"] = sources
    return filled


def section_for(config: dict[str, Any], source: dict[str, Any], name: str
                ) -> dict[str, Any] | None:
    """取一路源的 `assembly` / `dimension` 段：源上写了就用源上的，否则用全局。

    两个业务模块的 ROI 是画面坐标，换一路画面就换一套 ROI；共享一份全局配置
    等于假设所有源拍的是同一个工位。源级覆盖是可选的，不写就退回全局。
    """
    if name in source:
        return source[name]
    return config.get(name)


def _minimal_validate(config: dict[str, Any]) -> None:
    """没有 jsonschema 时的兜底：只查 schema 的 required 与致命类型。"""
    for key in ("sources", "model", "classes", "rules"):
        if key not in config:
            raise ConfigError(f"missing required field: {key}")
    if not isinstance(config["sources"], list) or not config["sources"]:
        raise ConfigError("sources must be a non-empty array")
    seen: set[str] = set()
    for index, source in enumerate(config["sources"]):
        for key in ("stream_id", "uri"):
            if not source.get(key):
                raise ConfigError(f"sources[{index}] missing {key}")
        stream_id = source["stream_id"]
        if stream_id in seen:
            raise ConfigError(f"duplicate stream_id: {stream_id}")
        seen.add(stream_id)
    model = config["model"]
    for key in ("path", "accelerator", "input_size"):
        if key not in model:
            raise ConfigError(f"model missing {key}")
    if len(model["input_size"]) != 2:
        raise ConfigError("model.input_size must be [width, height]")
    if not config["classes"]:
        raise ConfigError("classes must be a non-empty array")
    if "ng_classes" not in config["rules"]:
        raise ConfigError("rules missing ng_classes")


def validate(config: dict[str, Any], schema_path: str | Path | None = None) -> None:
    """先跑 schema（若可用），再跑 schema 表达不了的跨字段约束。"""
    path = find_schema(schema_path)
    try:
        import jsonschema
    except ImportError:
        _minimal_validate(config)
    else:
        if path is None:
            _minimal_validate(config)
        else:
            schema = json.loads(Path(path).read_text(encoding="utf-8"))
            errors = sorted(
                jsonschema.Draft202012Validator(schema).iter_errors(config),
                key=lambda err: list(err.path))
            if errors:
                joined = "; ".join(
                    f"{'/'.join(str(p) for p in err.path) or '<root>'}: {err.message}"
                    for err in errors)
                raise ConfigError(joined)
            _minimal_validate(config)

    classes = config["classes"]
    unknown = [name for name in config["rules"].get("ng_classes", [])
               if name not in classes]
    if unknown:
        raise ConfigError(f"rules.ng_classes not in classes: {unknown}")

    # schema 表达不了：期望件的类别必须是 classes 的成员，否则永远匹配不上，
    # 现场看到的是"这个位置一直缺件"，而不是"配置写错了"。
    scopes: list[tuple[str, dict[str, Any]]] = [("assembly", config)]
    for index, source in enumerate(config.get("sources", [])):
        scopes.append((f"sources[{index}].assembly", source))
    for label, holder in scopes:
        section = holder.get("assembly") if isinstance(holder, dict) else None
        if not section:
            continue
        bad = [str(item.get("class")) for item in section.get("expected", [])
               if item.get("class") not in classes]
        if bad:
            raise ConfigError(f"{label}.expected[].class not in classes: {bad}")


def load_config(path: str | Path, schema_path: str | Path | None = None
                ) -> dict[str, Any]:
    """读 JSON 或 YAML 配置，校验后返回补完默认值的 dict。"""
    config_path = Path(path)
    if not config_path.exists():
        raise ConfigError(f"config not found: {config_path}")
    text = config_path.read_text(encoding="utf-8")
    if config_path.suffix in (".yaml", ".yml"):
        import yaml

        config = yaml.safe_load(text)
    else:
        config = json.loads(text)
    if not isinstance(config, dict):
        raise ConfigError("config root must be an object")
    validate(config, schema_path)
    return apply_defaults(config)
