"""运行时配置加载。形状见 contracts/config.schema.json。

本项目是分类任务：没有 Modbus 段（spec §1 删掉），执行机构走 `actuator` 段 +
`core_waste.gpio` 的回调；`rules` 从「OK/NG 判定」变成「置信度下限 + 兜底类别」。

schema 是唯一事实来源：这里只做「读文件 + 补默认值 + 校验」，不额外发明字段。
jsonschema 装了就用它严格校验；设备上没装时退化成必填字段检查，
这样容器里不必为了启动而装 jsonschema。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

SCHEMA_RELATIVE = "contracts/config.schema.json"

MODEL_DEFAULTS = {"top_k": 3, "track": "baseline"}
RULES_DEFAULTS = {"min_confidence": 0.5, "fallback_category": "residual",
                  "consecutive_frames": 1}
TRIGGER_DEFAULTS = {"mode": "on_demand", "sources": ["http"],
                    "debounce_ms": 800, "merge_pending": True, "max_fps": 5.0}
CAPTURE_STORE_DEFAULTS = {"kind": "none", "retain_days": 7}
ACTUATOR_DEFAULTS = {"enabled": False, "min_confidence": 0.5}
MQTT_DEFAULTS = {"port": 1883, "qos": 0, "retain": False, "tls": False}
WEB_DEFAULTS = {"enabled": True, "bind": "0.0.0.0", "port": 8080,
                "preview_fps": 5.0}
#: VLM 兜底旁路。默认 enabled=false：不配 `vlm` 段的现场，整条链路不存在。
VLM_DEFAULTS = {"enabled": False, "base_url": "http://127.0.0.1:8100",
                "timeout_ms": 4000, "language": "zh", "max_tokens": 320,
                "queue_size": 2, "breaker_failures": 5,
                "breaker_cooloff_s": 30.0, "jpeg_quality": 75,
                "topic": "waste/{stream_id}/fallback",
                "explain_on_fallback": False, "explain_max_sentences": 3,
                "apply_fallback_to_gpio": False}
VLM_TRIGGER_DEFAULTS = {"min_confidence": 0.6, "margin": 0.25,
                        "min_interval_s": 5.0}


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
    filled["trigger"] = _fill(config.get("trigger", {}), TRIGGER_DEFAULTS)
    filled["capture_store"] = _fill(config.get("capture_store", {}),
                                    CAPTURE_STORE_DEFAULTS)
    filled["actuator"] = _fill(config.get("actuator", {}), ACTUATOR_DEFAULTS)
    filled["web"] = _fill(config.get("web", {}), WEB_DEFAULTS)
    vlm = _fill(config.get("vlm", {}), VLM_DEFAULTS)
    vlm["trigger"] = _fill(vlm.get("trigger", {}), VLM_TRIGGER_DEFAULTS)
    filled["vlm"] = vlm
    sources = []
    for source in config.get("sources", []):
        entry = dict(source)
        entry.setdefault("kind", infer_kind(str(entry.get("uri", ""))))
        sources.append(entry)
    filled["sources"] = sources
    return filled


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
    if len(config["classes"]) < 2:
        raise ConfigError("classes must list at least two class names")
    if len(set(config["classes"])) != len(config["classes"]):
        raise ConfigError("classes must not repeat a name")


def validate(config: dict[str, Any], schema_path: str | Path | None = None) -> None:
    """先跑 schema（若可用），再跑 schema 表达不了的跨字段约束。"""
    path = find_schema(schema_path)
    try:
        import jsonschema
    except ImportError:
        path = None                       # 没有 jsonschema，只跑必填字段检查
    if path is not None:
        schema = json.loads(Path(path).read_text(encoding="utf-8"))
        errors = sorted(
            jsonschema.Draft202012Validator(schema).iter_errors(config),
            key=lambda err: list(err.path))
        if errors:
            raise ConfigError("; ".join(
                f"{'/'.join(str(p) for p in err.path) or '<root>'}: {err.message}"
                for err in errors))
    _minimal_validate(config)

    classes = config["classes"]
    fallback = config["rules"].get("fallback_category",
                                   RULES_DEFAULTS["fallback_category"])
    if fallback not in classes:
        raise ConfigError(
            f"rules.fallback_category not in classes: {fallback!r}")
    trigger = config.get("trigger", {})
    if (trigger.get("mode", TRIGGER_DEFAULTS["mode"]) == "on_demand"
            and trigger.get("sources") == []):
        raise ConfigError("trigger.mode=on_demand needs at least one "
                          "trigger.sources entry")
    vlm = config.get("vlm", {})
    vlm_enabled = bool(vlm.get("enabled"))
    if vlm_enabled and not vlm.get("base_url", VLM_DEFAULTS["base_url"]):
        raise ConfigError("vlm.enabled=true requires vlm.base_url")
    vlm_trigger = vlm.get("trigger", {})
    gate = vlm_trigger.get("min_confidence", VLM_TRIGGER_DEFAULTS["min_confidence"])
    margin = vlm_trigger.get("margin", VLM_TRIGGER_DEFAULTS["margin"])
    rules_gate = config["rules"].get("min_confidence",
                                     RULES_DEFAULTS["min_confidence"])
    # 1e-9 是浮点余量：2*0.6-1 在 IEEE754 下是 0.19999999999999996，
    # 不加余量的话 margin=0.2 这个正好不可达的配置会被放过去。
    if vlm_enabled and margin > 0 and margin <= 2 * gate - 1 + 1e-9:
        # softmax 下 top-2 ≤ 1 − top-1，所以在 top-1 ≥ gate 的一侧两者的差至少是
        # 2·gate − 1。margin 不大于这个值时 `ambiguous` 那条依据一次都不会触发。
        # 显式关掉（margin = 0）是合法配置；配一个够不着的正数不是。
        raise ConfigError(
            f"vlm.trigger.margin ({margin}) is unreachable: with "
            f"min_confidence={gate}, top-1 minus top-2 is always >= "
            f"{2 * gate - 1:.4g}. Raise margin, lower min_confidence, "
            f"or set margin to 0 to disable the ambiguous trigger")
    if vlm_enabled and gate < rules_gate:
        # 兜底门限低于「改判成 fallback_category」的门限，等于兜底永远轮不到那些
        # 已经被判成 residual 的帧——那正是最该问 VLM 的一批。启动时拒掉。
        raise ConfigError(
            f"vlm.trigger.min_confidence ({gate}) must be >= "
            f"rules.min_confidence ({rules_gate})")

    store = config.get("capture_store", {})
    kind = store.get("kind", "none")
    if kind == "local" and not store.get("dir"):
        raise ConfigError("capture_store.kind=local requires capture_store.dir")
    if kind == "object_store" and not store.get("base_uri"):
        raise ConfigError("capture_store.kind=object_store requires "
                          "capture_store.base_uri")

    # 开放词汇 track：视觉塔 ONNX 单独不构成一个分类器，原型库与 prompt 集
    # 必须一起给。少给一个就是「模型在跑但比的是上一次校准的原型」，
    # 这类错误不会报异常，只会让精度悄悄掉——所以在启动时拒掉。
    model = config["model"]
    track = model.get("track", MODEL_DEFAULTS["track"])
    if track == "open_vocab":
        for key in ("prototypes", "prompt_set"):
            if not model.get(key):
                raise ConfigError(f"model.track=open_vocab requires model.{key}")
    else:
        extra = [key for key in ("prototypes", "prompt_set", "temperature")
                 if key in model]
        if extra:
            raise ConfigError(
                f"model.track={track} must not carry open-vocabulary fields: "
                f"{extra}")


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
