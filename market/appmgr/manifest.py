"""Strict manifest-v2 validation and deterministic release metadata.

The module is deliberately stdlib-only and has no appmgr/runtime imports.  It is
the single validator used by both the host-side packager and the device-side
installer.  Legacy manifests (missing ``manifest_version``) remain accepted as
v1, while v2 opts into a strict, closed schema for every security- or
lifecycle-relevant section.

``release.lock.json`` and ``files.sha256`` are package metadata, not source
files.  The packager generates both without mutating the app tree; the installer
recomputes them from the authenticated tar before extraction.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import platform
import re
import sys
from typing import Any, Mapping


MANIFEST_VERSION = 2
RELEASE_LOCK_VERSION = 1
RELEASE_LOCK_PATH = "release.lock.json"
BOM_PATH = "files.sha256"
RESERVED_PACKAGE_PATHS = frozenset((RELEASE_LOCK_PATH, BOM_PATH))
RESERVED_APP_IDS = frozenset(("builtin",))
MAX_ICON_BYTES = 1024 * 1024
ICON_MEDIA_EXTENSIONS = {
    "image/png": (".png",),
    "image/webp": (".webp",),
    "image/jpeg": (".jpg", ".jpeg"),
}
_FORBIDDEN_KEY_DIRS = frozenset((".ssh", "keys"))
_FORBIDDEN_KEY_BASENAMES = frozenset(("authorized_keys", "known_hosts"))
_FORBIDDEN_KEY_SUFFIXES = (".jks", ".key", ".p12", ".pfx", ".pem")

DEFAULT_PLATFORM_PROFILE = "rv1126b-linux-gnu-cp311-rknn232-v1"

_APP_ID_RE = re.compile(r"[a-z0-9-]{1,64}")
_SEMVER_RE = re.compile(
    r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
)
_SAFE_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+:-]{0,127}")
_VERSION_CONSTRAINT_RE = re.compile(
    r"[A-Za-z0-9<>=!~^*][A-Za-z0-9._+,:<>=!~^*-]{0,127}"
)
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_IMPORT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*")
_CONFIG_KEY_RE = re.compile(r"[a-z][a-z0-9_]{0,63}")
_ENDPOINT_RE = re.compile(r"[a-z][a-z0-9_.-]{0,63}")

_V2_REQUIRED = frozenset((
    "manifest_version", "id", "name", "version", "type", "entry",
    "release", "compatibility", "python", "artifacts", "config_schema",
    "resources", "permissions", "health", "instances", "capabilities",
))
_V2_ALLOWED = _V2_REQUIRED | frozenset((
    "name_zh", "description", "description_zh", "author", "image", "icon", "scene",
    "scene_zh", "tags", "models", "needs_model", "postproc", "render",
    "output", "record_trigger", "ha_entities", "package",
))

_CONFIG_TYPES = frozenset((
    "array", "boolean", "enum", "field_mapping", "integer", "line",
    "number", "object", "output_filters", "password", "select", "string",
    "zone",
))
_CONFIG_APPLY = frozenset(("live", "restart", "reschedule"))
_SDK_PERMISSIONS = frozenset((
    "audio.read", "codec.decode", "frame.read", "gpio.read", "gpio.write",
    "npu.infer", "probe.read", "result.publish", "rga.use",
))
_RESOURCE_NAMES = frozenset((
    "audio.capture", "camera.frames", "codec.decode", "npu.rknn",
    "probe.read", "result.publish", "rga",
))
_RESOURCE_MODES = frozenset(("brokered", "exclusive", "scheduled", "shared"))
_OUTPUT_CHANNELS = frozenset(("ws", "mqtt", "http", "uart"))
_OUTPUT_MODES = frozenset(("raw", "custom", "ha"))
_COORD_SPACES = frozenset((
    "pixel_xyxy", "pixel_quad", "pixel_points",
    "normalized_xyxy", "normalized_quad", "normalized_points",
))
_RECORD_TRIGGER_TYPES = frozenset(("detection", "classification", "event"))
_MAX_RECORD_TRIGGER_SIGNALS = 32
_MAX_RECORD_TRIGGER_CLASSES = 256


class ManifestValidationError(ValueError):
    """A manifest or its release metadata violates the package contract."""


def _fail(path: str, message: str) -> None:
    raise ManifestValidationError(f"{path}: {message}")


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _expect_object(value: Any, path: str) -> dict:
    if not isinstance(value, dict):
        _fail(path, "must be an object")
    return value


def _expect_list(value: Any, path: str) -> list:
    if not isinstance(value, list):
        _fail(path, "must be an array")
    return value


def _closed(obj: Mapping[str, Any], allowed: set[str] | frozenset[str], path: str) -> None:
    unknown = sorted(k for k in obj if k not in allowed and not k.startswith("x-"))
    if unknown:
        _fail(path, "unknown field(s): " + ", ".join(unknown))


def _required(obj: Mapping[str, Any], required: set[str] | frozenset[str], path: str) -> None:
    missing = sorted(required - set(obj))
    if missing:
        _fail(path, "missing required field(s): " + ", ".join(missing))


def _string(value: Any, path: str, *, min_len: int = 1, max_len: int = 4096) -> str:
    if not isinstance(value, str) or not (min_len <= len(value) <= max_len):
        _fail(path, f"must be a string of length {min_len}..{max_len}")
    if "\x00" in value:
        _fail(path, "must not contain NUL")
    return value


def _safe_relpath(value: Any, path: str) -> str:
    text = _string(value, path, max_len=255)
    if text.startswith(("/", "\\")) or "\\" in text:
        _fail(path, "must be a POSIX relative path")
    parts = text.split("/")
    if any(part in ("", ".", "..") for part in parts):
        _fail(path, "must be normalized and must not contain empty, '.' or '..' segments")
    if any(any(ord(ch) < 0x20 for ch in part) for part in parts):
        _fail(path, "must not contain control characters")
    return text


def validate_package_member_path(value: Any, path: str = "package member") -> str:
    """Validate one payload path and reject embedded signing/trust material.

    App packages execute as privileged managed workloads but never provision
    appmgr trust. Detached ``.sig`` files stay outside the archive. Rejecting
    conventional private/public-key containers and trust directories at the
    shared build/install boundary prevents a package from presenting bundled
    key material as if it were device-owner or vendor configuration.
    """
    text = _safe_relpath(value, path)
    lowered = [component.casefold() for component in text.split("/")]
    basename = lowered[-1]
    if any(component in _FORBIDDEN_KEY_DIRS for component in lowered) or \
            basename in _FORBIDDEN_KEY_BASENAMES or \
            basename.endswith(_FORBIDDEN_KEY_SUFFIXES):
        _fail(path, "package must not contain signing keys or trust-store material")
    return text


def validate_icon_declaration(value: Any) -> dict[str, str]:
    """Validate and canonicalise manifest-v2's package-bundled icon."""
    obj = _expect_object(value, "icon")
    _closed(obj, frozenset(("path", "media_type")), "icon")
    _required(obj, frozenset(("path", "media_type")), "icon")
    icon_path = validate_package_member_path(obj["path"], "icon.path")
    media_type = _string(obj["media_type"], "icon.media_type", max_len=64)
    extensions = ICON_MEDIA_EXTENSIONS.get(media_type)
    if extensions is None:
        _fail("icon.media_type", "must be image/png, image/webp, or image/jpeg")
    if not icon_path.endswith(extensions):
        _fail(
            "icon.path",
            "extension must match icon.media_type %r (%s)" % (
                media_type, ", ".join(extensions)))
    return {"path": icon_path, "media_type": media_type}


def icon_bytes_match_media_type(data: bytes, media_type: str) -> bool:
    """Return whether a bounded prefix has the declared raster signature."""
    if not isinstance(data, (bytes, bytearray, memoryview)):
        return False
    prefix = bytes(data)
    if media_type == "image/png":
        return prefix.startswith(b"\x89PNG\r\n\x1a\n")
    if media_type == "image/webp":
        return (len(prefix) >= 12 and prefix.startswith(b"RIFF")
                and prefix[8:12] == b"WEBP")
    if media_type == "image/jpeg":
        return prefix.startswith(b"\xff\xd8\xff")
    return False


def _safe_token(value: Any, path: str) -> str:
    text = _string(value, path, max_len=128)
    if not _SAFE_TOKEN_RE.fullmatch(text):
        _fail(path, "contains unsupported characters")
    return text


def _version_constraint(value: Any, path: str) -> str:
    """Validate a compact version/ABI constraint such as ``>=0.2,<0.3``."""
    text = _string(value, path, max_len=128)
    if not _VERSION_CONSTRAINT_RE.fullmatch(text):
        _fail(path, "must be a compact version/ABI constraint")
    return text


def _sha256(value: Any, path: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        _fail(path, "must be a lowercase 64-character SHA-256")
    return value


def _positive_int(value: Any, path: str, *, maximum: int | None = None) -> int:
    if not _is_int(value) or value <= 0:
        _fail(path, "must be a positive integer")
    if maximum is not None and value > maximum:
        _fail(path, f"must be <= {maximum}")
    return value


def manifest_version(manifest: Mapping[str, Any]) -> int:
    value = manifest.get("manifest_version", 1)
    if not _is_int(value):
        _fail("manifest_version", "must be an integer")
    return value


def _validate_common(manifest: dict) -> None:
    app_id = manifest.get("id")
    if not isinstance(app_id, str) or not _APP_ID_RE.fullmatch(app_id):
        _fail("id", "must match [a-z0-9-]{1,64}")
    if app_id in RESERVED_APP_IDS:
        _fail("id", "is reserved for a firmware system application")

    version = _string(manifest.get("version"), "version", max_len=64)
    if any(ch in version for ch in ("/", "\\")) or any(ord(ch) < 0x20 for ch in version):
        _fail("version", "must be a path-safe value")

    entry = _safe_relpath(manifest.get("entry", "app.py"), "entry")
    if not entry.endswith(".py"):
        _fail("entry", "must name a Python source file")

    models = manifest.get("models", [])
    if not isinstance(models, list):
        _fail("models", "must be an array")
    for index, model in enumerate(models):
        p = f"models[{index}]"
        obj = _expect_object(model, p)
        if "file" in obj:
            _safe_relpath(obj["file"], p + ".file")


def _finite_number(value: Any, *, integer: bool = False) -> bool:
    if integer:
        return _is_int(value)
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(float(value))
    except (OverflowError, TypeError, ValueError):
        return False


def _enum_option_values(options: Any, path: str) -> list:
    """Validate dropdown options and return their typed values.

    Scalar arrays are the original contract.  Labelled option objects add a
    convenient self-contained form without removing the parallel
    ``option_labels``/``option_labels_zh`` arrays used by existing packages.
    """
    values = []
    for index, raw in enumerate(_expect_list(options, path)):
        option_path = f"{path}[{index}]"
        value = raw
        if isinstance(raw, dict):
            _closed(raw, frozenset(("value", "label", "label_zh")), option_path)
            _required(raw, frozenset(("value",)), option_path)
            for label_key in ("label", "label_zh"):
                if label_key in raw:
                    _string(raw[label_key], option_path + "." + label_key,
                            min_len=0, max_len=128)
            value = raw["value"]
        if (value is None or isinstance(value, (dict, list))
                or not isinstance(value, (str, int, float, bool))
                or isinstance(value, float) and not math.isfinite(value)):
            _fail(option_path + (".value" if isinstance(raw, dict) else ""),
                  "must be a finite string/number/integer/boolean scalar")
        if any(_enum_value_equal(value, old) for old in values):
            _fail(option_path, "duplicates an earlier option value")
        values.append(value)
    if not values:
        _fail(path, "must be a non-empty array")
    return values


def _enum_value_equal(left: Any, right: Any) -> bool:
    """Match the JSON/JavaScript wire model without conflating bool and int."""
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return left == right
    return type(left) is type(right) and left == right


def _validate_config_complex_default(spec: dict, path: str) -> None:
    """Validate structured manifest defaults rather than accepting opaque JSON."""
    if "default" not in spec:
        return
    value = spec["default"]
    config_type = spec["type"]
    if config_type == "zone":
        if value in (None, []):
            return
        points = _expect_list(value, path + ".default")
        if len(points) < 3:
            _fail(path + ".default", "zone polygon needs at least 3 points")
        if "maxPoints" in spec and len(points) > spec["maxPoints"]:
            _fail(path + ".default", "zone exceeds maxPoints")
        for index, point in enumerate(points):
            pp = f"{path}.default[{index}]"
            coords = _expect_list(point, pp)
            if (len(coords) != 2 or any(not _finite_number(c)
                                        or not 0 <= float(c) <= 1 for c in coords)):
                _fail(pp, "must be [x,y] with finite coordinates in [0,1]")
    elif config_type == "line":
        if value in (None, {}):
            return
        line = _expect_object(value, path + ".default")
        _closed(line, frozenset(("a", "b", "in")), path + ".default")
        _required(line, frozenset(("a", "b")), path + ".default")
        for endpoint in ("a", "b"):
            coords = _expect_list(line[endpoint], path + f".default.{endpoint}")
            if (len(coords) != 2 or any(not _finite_number(c)
                                        or not 0 <= float(c) <= 1 for c in coords)):
                _fail(path + f".default.{endpoint}",
                      "must be [x,y] with finite coordinates in [0,1]")
        if "in" in line and line["in"] not in ("left", "right"):
            _fail(path + ".default.in", "must be left or right")
    elif config_type == "field_mapping":
        rows = _expect_list(value, path + ".default")
        for index, raw in enumerate(rows):
            rp = f"{path}.default[{index}]"
            row = _expect_object(raw, rp)
            _closed(row, frozenset(("source", "target", "topic", "task",
                                    "omit_if_none")), rp)
            _required(row, frozenset(("source", "target", "topic")), rp)
            for field in ("source", "target", "topic"):
                _string(row[field], rp + "." + field, max_len=512)
            if "task" in row:
                _string(row["task"], rp + ".task", max_len=64)
            if "omit_if_none" in row and not isinstance(
                    row["omit_if_none"], bool):
                _fail(rp + ".omit_if_none", "must be boolean")
    elif config_type == "output_filters":
        filters = _expect_object(value, path + ".default")
        _closed(filters, frozenset(("only_on_detection", "classes",
                                    "rate_limit_hz", "preserve_edge_events")),
                path + ".default")
        for field in ("only_on_detection", "preserve_edge_events"):
            if field in filters and not isinstance(filters[field], bool):
                _fail(path + f".default.{field}", "must be boolean")
        if "rate_limit_hz" in filters and (not _finite_number(
                filters["rate_limit_hz"]) or not 0 <= filters["rate_limit_hz"] <= 1000):
            _fail(path + ".default.rate_limit_hz", "must be in [0,1000]")
        if "classes" in filters:
            classes = _expect_list(filters["classes"], path + ".default.classes")
            if any(v is None or isinstance(v, (dict, list, bool))
                   or not isinstance(v, (str, int, float))
                   or isinstance(v, (int, float)) and not _finite_number(v)
                   for v in classes):
                _fail(path + ".default.classes", "must contain scalar class ids/names")


def _validate_config_schema(value: Any) -> dict[str, str]:
    schema = _expect_object(value, "config_schema")
    _closed(schema, frozenset(("groups", "revision")), "config_schema")
    groups = _expect_list(schema.get("groups"), "config_schema.groups")
    if "revision" in schema:
        _positive_int(schema["revision"], "config_schema.revision")

    seen_groups: set[str] = set()
    seen_items: set[str] = set()
    apply_modes: dict[str, str] = {}
    group_allowed = frozenset(("key", "title", "title_zh", "description",
                               "description_zh", "items"))
    item_allowed = frozenset((
        "apply", "default", "directional", "help", "help_zh", "key", "max",
        "maxPoints", "min", "option_labels", "option_labels_zh", "options",
        "step", "title", "title_zh", "type",
    ))
    for gi, group in enumerate(groups):
        gp = f"config_schema.groups[{gi}]"
        obj = _expect_object(group, gp)
        _closed(obj, group_allowed, gp)
        _required(obj, frozenset(("key", "title", "items")), gp)
        key = _string(obj["key"], gp + ".key", max_len=64)
        if not _CONFIG_KEY_RE.fullmatch(key):
            _fail(gp + ".key", "must match [a-z][a-z0-9_]{0,63}")
        if key in seen_groups:
            _fail(gp + ".key", f"duplicate group key {key!r}")
        seen_groups.add(key)
        _string(obj["title"], gp + ".title", max_len=128)
        items = _expect_list(obj["items"], gp + ".items")
        for ii, item in enumerate(items):
            ip = f"{gp}.items[{ii}]"
            spec = _expect_object(item, ip)
            _closed(spec, item_allowed, ip)
            _required(spec, frozenset(("key", "type", "apply")), ip)
            ikey = _string(spec["key"], ip + ".key", max_len=64)
            if not _CONFIG_KEY_RE.fullmatch(ikey):
                _fail(ip + ".key", "must match [a-z][a-z0-9_]{0,63}")
            if ikey in seen_items:
                _fail(ip + ".key", f"duplicate config key {ikey!r}")
            seen_items.add(ikey)
            if spec["type"] not in _CONFIG_TYPES:
                _fail(ip + ".type", "unsupported config type")
            if spec["apply"] not in _CONFIG_APPLY:
                _fail(ip + ".apply", "must be live, restart or reschedule")
            apply_modes[ikey] = spec["apply"]
            if spec["type"] in ("enum", "select"):
                options = _expect_list(spec.get("options"), ip + ".options")
                option_values = _enum_option_values(options, ip + ".options")
                if "default" in spec and not any(
                        _enum_value_equal(spec["default"], option)
                        for option in option_values):
                    _fail(ip + ".default", "must be one of the typed option values")
                for labels_key in ("option_labels", "option_labels_zh"):
                    if labels_key in spec:
                        labels = _expect_list(spec[labels_key], ip + "." + labels_key)
                        if len(labels) != len(options) or any(
                                not isinstance(label, str) for label in labels):
                            _fail(ip + "." + labels_key,
                                  "must contain one string for each enum option")
            if spec["type"] == "boolean" and "default" in spec \
                    and not isinstance(spec["default"], bool):
                _fail(ip + ".default", "must match type boolean")
            if spec["type"] in ("string", "password") and "default" in spec \
                    and not isinstance(spec["default"], str):
                _fail(ip + ".default", f"must match type {spec['type']}")
            if spec["type"] == "array" and "default" in spec \
                    and not isinstance(spec["default"], list):
                _fail(ip + ".default", "must match type array")
            if spec["type"] == "object" and "default" in spec \
                    and not isinstance(spec["default"], dict):
                _fail(ip + ".default", "must match type object")
            for bound in ("min", "max", "step"):
                if bound not in spec:
                    continue
                number = spec[bound]
                valid_number = _finite_number(
                    number, integer=spec["type"] == "integer")
                if spec["type"] not in ("integer", "number") or not valid_number:
                    _fail(ip + "." + bound, "is only valid as a matching numeric value")
            if "min" in spec and "max" in spec and spec["min"] > spec["max"]:
                _fail(ip, "min must be <= max")
            if "step" in spec and spec["step"] <= 0:
                _fail(ip + ".step", "must be positive")
            if spec["type"] in ("integer", "number") and "default" in spec:
                default = spec["default"]
                valid_number = _finite_number(
                    default, integer=spec["type"] == "integer")
                if not valid_number:
                    _fail(ip + ".default", f"must match type {spec['type']}")
                if "min" in spec and default < spec["min"]:
                    _fail(ip + ".default", "must be >= min")
                if "max" in spec and default > spec["max"]:
                    _fail(ip + ".default", "must be <= max")
            if "directional" in spec and not isinstance(spec["directional"], bool):
                _fail(ip + ".directional", "must be a boolean")
            if "maxPoints" in spec:
                _positive_int(spec["maxPoints"], ip + ".maxPoints")
            if spec["type"] in ("zone", "line", "field_mapping",
                                "output_filters"):
                _validate_config_complex_default(spec, ip)
    return apply_modes


def _validate_output_contract(value: Any) -> None:
    """Validate the optional, typed result/template metadata contract.

    Older manifest-v2 packages shipped an intentionally loose ``output``
    object.  They remain installable.  A publisher opts into the closed v2
    contract with ``contract_version: 2``; the browser Result Center and the
    Result Hub may then rely on these fields without guessing.
    """
    output = _expect_object(value, "output")
    if "port" in output:
        _fail("output.port",
              "fixed ports are forbidden; endpoint_mode=allocated owns binding")
    if "contract_version" not in output:
        return
    if output.get("contract_version") != 2:
        _fail("output.contract_version", "must equal 2")
    allowed = frozenset((
        "contract_version", "sink", "schema", "topic", "default_channel",
        "default_mode", "persistent_summary", "fields", "default_mapping",
    ))
    _closed(output, allowed, "output")
    _required(output, frozenset((
        "contract_version", "sink", "schema", "default_channel",
        "default_mode", "fields", "default_mapping",
    )), "output")
    if output["sink"] != "ws":
        _fail("output.sink", "must be 'ws'; appmgr owns the public result port")
    _string(output["schema"], "output.schema", max_len=4096)
    if "topic" in output:
        _string(output["topic"], "output.topic", max_len=512)
    channels = _expect_list(output["default_channel"], "output.default_channel")
    if not channels or len(channels) != len(set(channels)):
        _fail("output.default_channel", "must contain unique channels")
    for index, channel in enumerate(channels):
        if channel not in _OUTPUT_CHANNELS:
            _fail(f"output.default_channel[{index}]",
                  "must be ws, mqtt, http or uart")
    if output["default_mode"] not in _OUTPUT_MODES:
        _fail("output.default_mode", "must be raw, custom or ha")
    if "persistent_summary" in output and \
            not isinstance(output["persistent_summary"], bool):
        _fail("output.persistent_summary", "must be boolean")

    fields = _expect_list(output["fields"], "output.fields")
    names: set[str] = set()
    field_allowed = frozenset((
        "name", "from", "type", "coord", "description", "event_kind",
        "optional", "derived",
    ))
    for index, value in enumerate(fields):
        path = f"output.fields[{index}]"
        field = _expect_object(value, path)
        _closed(field, field_allowed, path)
        _required(field, frozenset(("name", "from", "type", "description")), path)
        name = _safe_token(field["name"], path + ".name")
        if name in names:
            _fail(path + ".name", "must be unique")
        names.add(name)
        _string(field["from"], path + ".from", max_len=512)
        field_type = _string(field["type"], path + ".type", max_len=128)
        _string(field["description"], path + ".description", max_len=1024)
        if "coord" in field and field["coord"] not in _COORD_SPACES:
            _fail(path + ".coord", "unsupported coordinate space")
        if field["from"] == "geometry[]":
            if field.get("derived") is True:
                _fail(path + ".derived",
                      "geometry[] is runtime data and cannot be derived")
            if field.get("coord") not in ("pixel_points", "normalized_points"):
                _fail(path + ".coord",
                      "geometry[] requires pixel_points or normalized_points")
            if field_type != "geometry[]":
                _fail(path + ".type", "geometry[] requires type geometry[]")
        if any(shape in field_type.lower() for shape in
               ("bbox", "quad", "keypoint")) and "coord" not in field:
            _fail(path + ".coord",
                  "is required for bbox, quad and keypoint fields")
        if "event_kind" in field:
            _safe_token(field["event_kind"], path + ".event_kind")
        for flag in ("optional", "derived"):
            if flag in field and not isinstance(field[flag], bool):
                _fail(path + "." + flag, "must be boolean")

    mapping = _expect_list(output["default_mapping"], "output.default_mapping")
    mapping_allowed = frozenset(("source", "target", "topic", "task"))
    for index, value in enumerate(mapping):
        path = f"output.default_mapping[{index}]"
        row = _expect_object(value, path)
        _closed(row, mapping_allowed, path)
        _required(row, frozenset(("source", "target", "topic")), path)
        _string(row["source"], path + ".source", max_len=512)
        _safe_token(row["target"], path + ".target")
        _string(row["topic"], path + ".topic", max_len=512)
        if "task" in row:
            _safe_token(row["task"], path + ".task")


def _validate_record_trigger(value: Any, output_value: Any) -> None:
    """Validate the signed contract used by the managed-app recording bridge.

    This declaration is an authorization boundary, not presentation metadata:
    only fields explicitly listed here may be projected into Vigil.  Runtime
    payloads cannot add signals or widen the advertised class set.
    """
    root = _expect_object(value, "record_trigger")
    _closed(root, frozenset(("version", "signals")), "record_trigger")
    _required(root, frozenset(("version", "signals")), "record_trigger")
    if isinstance(root["version"], bool) or root["version"] != 1:
        _fail("record_trigger.version", "must equal 1")
    signals = _expect_list(root["signals"], "record_trigger.signals")
    if not signals or len(signals) > _MAX_RECORD_TRIGGER_SIGNALS:
        _fail("record_trigger.signals", "must contain 1..%d signals" %
              _MAX_RECORD_TRIGGER_SIGNALS)

    output = output_value if isinstance(output_value, dict) else {}
    if output.get("contract_version") != 2:
        _fail("record_trigger", "requires output.contract_version=2")
    fields = output.get("fields") if isinstance(output.get("fields"), list) else []
    direct_result_fields = {}
    for field in fields:
        if (not isinstance(field, dict) or field.get("derived") is True
                or not isinstance(field.get("from"), str)):
            continue
        match = re.fullmatch(
            r"results\[\]\.([A-Za-z_][A-Za-z0-9_]*)", field["from"])
        if match is None:
            continue
        direct_name = match.group(1)
        if direct_name in direct_result_fields:
            _fail("record_trigger",
                  f"requires an unambiguous direct {direct_name!r} output field")
        direct_result_fields[direct_name] = field
    declared_event_kinds = {
        str(match.group(1) or field.get("event_kind") or "")
        for field in fields
        if isinstance(field, dict) and field.get("derived") is not True
        and isinstance(field.get("from"), str)
        and (match := re.fullmatch(
            r"events\[(?:kind=([A-Za-z0-9_.-]+))?\]\."
            r"[A-Za-z_][A-Za-z0-9_]*", field["from"]))
    }
    has_label = any(name in direct_result_fields
                    for name in ("cls_name", "label", "kind"))
    has_score = any(name in direct_result_fields
                    for name in ("score", "confidence"))
    box = direct_result_fields.get("box")
    has_box = (isinstance(box, dict)
               and box.get("coord") in ("pixel_xyxy", "normalized_xyxy"))

    ids: set[str] = set()
    event_kinds: set[str] = set()
    authorized_labels: set[str] = set()
    frame_kinds: set[str] = set()
    allowed = frozenset((
        "id", "type", "classes", "supports_roi", "event_kind",
    ))
    for index, raw in enumerate(signals):
        path = f"record_trigger.signals[{index}]"
        signal = _expect_object(raw, path)
        _closed(signal, allowed, path)
        _required(signal, frozenset(("id", "type", "supports_roi")), path)
        signal_id = _safe_token(signal["id"], path + ".id")
        if signal_id in ids:
            _fail(path + ".id", "must be unique")
        ids.add(signal_id)
        kind = signal["type"]
        if kind not in _RECORD_TRIGGER_TYPES:
            _fail(path + ".type", "must be detection, classification or event")
        if not isinstance(signal["supports_roi"], bool):
            _fail(path + ".supports_roi", "must be boolean")

        if kind in ("detection", "classification"):
            frame_kinds.add(kind)
            if len(frame_kinds) > 1:
                _fail("record_trigger.signals",
                      "cannot mix detection and classification signals")
            _required(signal, frozenset(("classes",)), path)
            if "event_kind" in signal:
                _fail(path + ".event_kind", "is only valid for event signals")
            classes = _expect_list(signal["classes"], path + ".classes")
            if not classes or len(classes) > _MAX_RECORD_TRIGGER_CLASSES:
                _fail(path + ".classes", "must contain 1..%d labels" %
                      _MAX_RECORD_TRIGGER_CLASSES)
            seen: set[str] = set()
            for class_index, label_value in enumerate(classes):
                label_path = f"{path}.classes[{class_index}]"
                label = _string(label_value, label_path, max_len=128)
                if label != label.strip() or any(
                        ord(character) < 0x20 or ord(character) == 0x7f
                        for character in label):
                    _fail(label_path,
                          "must not have surrounding whitespace or control characters")
                if label in seen:
                    _fail(label_path, "must be unique")
                if label in authorized_labels:
                    _fail(label_path,
                          "must be unique across all recording signals")
                seen.add(label)
                authorized_labels.add(label)
            if not has_label or not has_score:
                _fail(path, "requires direct result label and score fields")
            if kind == "detection" and not has_box:
                _fail(path, "detection requires a direct normalized/pixel box field")
            if kind == "classification" and signal["supports_roi"]:
                _fail(path + ".supports_roi",
                      "classification signals cannot support ROI")
            continue

        _required(signal, frozenset(("event_kind",)), path)
        if "classes" in signal:
            _fail(path + ".classes", "is not valid for event signals")
        if signal["supports_roi"]:
            _fail(path + ".supports_roi", "event signals cannot support ROI")
        event_kind = _safe_token(signal["event_kind"], path + ".event_kind")
        if len(event_kind) > 96:
            _fail(path + ".event_kind",
                  "must not exceed the canonical Result Hub 96-character limit")
        if event_kind != event_kind.lower():
            _fail(path + ".event_kind",
                  "must be lowercase to match canonical Result Hub events")
        if event_kind in event_kinds:
            _fail(path + ".event_kind", "must be unique")
        if event_kind in authorized_labels:
            _fail(path + ".event_kind",
                  "must be unique across all recording signals")
        event_kinds.add(event_kind)
        authorized_labels.add(event_kind)
        if event_kind not in declared_event_kinds:
            _fail(path + ".event_kind",
                  "must reference a declared output event kind")


def effective_record_trigger(manifest: Any) -> dict:
    """Return a defensive copy of one validated recording capability."""
    if not isinstance(manifest, dict) or manifest.get("manifest_version") != 2:
        return {}
    value = manifest.get("record_trigger")
    if value is None:
        return {}
    try:
        _validate_record_trigger(value, manifest.get("output"))
    except ManifestValidationError:
        return {}
    return json.loads(json.dumps(value))


_GEOMETRY_COLOR_RE = re.compile(r"#[0-9A-Fa-f]{6}(?:[0-9A-Fa-f]{2})?\Z")
_GEOMETRY_TYPES = frozenset(("point", "line", "polyline", "polygon"))
_GEOMETRY_STYLE_FIELDS = frozenset((
    "color", "line_width", "point_radius", "fill", "fill_color", "opacity",
))


def _validate_geometry_style(value: Any, path: str) -> None:
    style = _expect_object(value, path)
    _closed(style, _GEOMETRY_STYLE_FIELDS, path)
    for key in ("color", "fill_color"):
        if key in style and (not isinstance(style[key], str)
                             or not _GEOMETRY_COLOR_RE.fullmatch(style[key])):
            _fail(path + "." + key, "must be #RRGGBB or #RRGGBBAA")
    if "fill" in style and not isinstance(style["fill"], bool):
        _fail(path + ".fill", "must be boolean")
    bounds = {
        "line_width": (0.25, 16.0),
        "point_radius": (0.5, 32.0),
        "opacity": (0.0, 1.0),
    }
    for key, (minimum, maximum) in bounds.items():
        if key in style and (not _finite_number(style[key])
                             or not minimum <= float(style[key]) <= maximum):
            _fail(path + "." + key,
                  f"must be a finite number in [{minimum},{maximum}]")


def _validate_render_contract(value: Any) -> None:
    render = _expect_object(value, "render")
    if "schema_version" not in render:
        return                         # legacy loose render remains compatible
    schema_version = render.get("schema_version")
    # JSON booleans are distinct from numbers.  Python's ``True == 1`` must
    # not make runtime admission disagree with Draft 2020-12 ``const: 1``.
    if isinstance(schema_version, bool) or schema_version != 1:
        _fail("render.schema_version", "must equal 1")
    _closed(render, frozenset((
        "schema_version", "boxes", "quads", "keypoints", "geometry", "events",
        "stream_osd",
    )), "render")
    if "boxes" in render:
        boxes = _expect_object(render["boxes"], "render.boxes")
        _closed(boxes, frozenset(("color_by", "label", "line_width")),
                "render.boxes")
        for key in ("color_by", "label"):
            if key in boxes:
                _safe_token(boxes[key], "render.boxes." + key)
        if "line_width" in boxes:
            _positive_int(boxes["line_width"], "render.boxes.line_width",
                          maximum=16)
    if "quads" in render:
        quads = _expect_object(render["quads"], "render.quads")
        _closed(quads, frozenset(("points", "label", "line_width")),
                "render.quads")
        _required(quads, frozenset(("points",)), "render.quads")
        _safe_token(quads["points"], "render.quads.points")
        if "label" in quads:
            _safe_token(quads["label"], "render.quads.label")
        if "line_width" in quads:
            _positive_int(quads["line_width"], "render.quads.line_width",
                          maximum=16)
    if "keypoints" in render:
        keypoints = _expect_object(render["keypoints"], "render.keypoints")
        _closed(keypoints, frozenset((
            "layout", "point_radius", "line_width", "conf_min", "skeleton",
        )), "render.keypoints")
        _required(keypoints, frozenset(("layout",)), "render.keypoints")
        _safe_token(keypoints["layout"], "render.keypoints.layout")
        for key in ("point_radius", "line_width"):
            if key in keypoints:
                _positive_int(keypoints[key], "render.keypoints." + key,
                              maximum=16)
        if "conf_min" in keypoints:
            conf = keypoints["conf_min"]
            if isinstance(conf, bool) or not isinstance(conf, (int, float)) or \
                    not 0 <= conf <= 1:
                _fail("render.keypoints.conf_min", "must be a number in 0..1")
        if "skeleton" in keypoints:
            _expect_list(keypoints["skeleton"], "render.keypoints.skeleton")
    if "geometry" in render:
        geometry = _expect_object(render["geometry"], "render.geometry")
        _closed(geometry, frozenset((
            "types", "max_items", "max_points", "style",
        )), "render.geometry")
        _required(geometry, frozenset(("types",)), "render.geometry")
        types = _expect_list(geometry["types"], "render.geometry.types")
        if (not types or len(types) != len(set(types))
                or any(kind not in _GEOMETRY_TYPES for kind in types)):
            _fail("render.geometry.types",
                  "must contain unique point, line, polyline or polygon values")
        if "max_items" in geometry:
            _positive_int(geometry["max_items"], "render.geometry.max_items",
                          maximum=256)
        if "max_points" in geometry:
            max_points = _positive_int(
                geometry["max_points"], "render.geometry.max_points",
                maximum=256)
            minimum_by_type = {
                "point": 1, "line": 2, "polyline": 2, "polygon": 3,
            }
            required = max(minimum_by_type[kind] for kind in types)
            if max_points < required:
                _fail(
                    "render.geometry.max_points",
                    "must be at least %d for declared geometry types" % required,
                )
        if "style" in geometry:
            _validate_geometry_style(geometry["style"], "render.geometry.style")
    if "events" in render:
        events = _expect_object(render["events"], "render.events")
        allowed = frozenset((
            "as", "position", "duration_sec", "max_lines", "text", "fields",
        ))
        for kind, value in events.items():
            _safe_token(kind, "render.events kind")
            path = "render.events." + kind
            spec = _expect_object(value, path)
            _closed(spec, allowed, path)
            if "as" in spec and spec["as"] not in (
                    "none", "badge", "toast", "panel", "subtitle"):
                _fail(path + ".as", "unsupported renderer")
            for key in ("position", "text"):
                if key in spec:
                    _string(spec[key], path + "." + key, max_len=1024)
            for key in ("duration_sec", "max_lines"):
                if key in spec and (isinstance(spec[key], bool) or
                                     not isinstance(spec[key], (int, float)) or
                                     spec[key] < 0):
                    _fail(path + "." + key, "must be a non-negative number")
            if "fields" in spec:
                values = _expect_list(spec["fields"], path + ".fields")
                for index, field in enumerate(values):
                    _safe_token(field, f"{path}.fields[{index}]")
    if "stream_osd" in render:
        osd = _expect_object(render["stream_osd"], "render.stream_osd")
        _closed(osd, frozenset(("supported", "default")), "render.stream_osd")
        _required(osd, frozenset(("supported", "default")), "render.stream_osd")
        supported = _expect_list(osd["supported"], "render.stream_osd.supported")
        if not supported or len(supported) != len(set(supported)) or \
                any(value != "boxes" for value in supported):
            _fail("render.stream_osd.supported",
                  "first release supports the unique value 'boxes' only")
        if not isinstance(osd["default"], bool) or osd["default"]:
            _fail("render.stream_osd.default",
                  "external stream OSD must default to false")


def _validate_render_references(output_value: Any, render_value: Any) -> None:
    """Bind strict renderer references to declared runtime result fields."""
    strict_output = (isinstance(output_value, dict)
                     and output_value.get("contract_version") == 2)
    render_version = (render_value.get("schema_version")
                      if isinstance(render_value, dict) else None)
    strict_render = (isinstance(render_value, dict)
                     and not isinstance(render_version, bool)
                     and render_version == 1)
    fields = output_value.get("fields") or [] if strict_output else []
    geometry_fields = [
        field for field in fields
        if (isinstance(field, dict)
            and field.get("from") == "geometry[]"
            and field.get("derived") is not True)]
    render_geometry = bool(strict_render and "geometry" in render_value)
    if render_geometry and not strict_output:
        _fail("render.geometry",
              "requires output.contract_version=2 and exactly one geometry[] field")
    if geometry_fields and not render_geometry:
        _fail("output.fields",
              "geometry[] requires render.schema_version=1 with render.geometry")
    if render_geometry and len(geometry_fields) != 1:
        _fail("render.geometry",
              "requires exactly one declared output.fields geometry[] field")
    if strict_render and "stream_osd" in render_value:
        if not strict_output:
            _fail("render.stream_osd", "requires output.contract_version=2")
        box_fields = [
            field for field in fields
            if (isinstance(field, dict)
                and field.get("from") == "results[].box"
                and field.get("derived") is not True)
        ]
        coordinates = [field.get("coord") for field in box_fields]
        if (not any(field.get("name") == "box" for field in box_fields)
                or not coordinates
                or not all(isinstance(coord, str) for coord in coordinates)
                or len(set(coordinates)) != 1
                or coordinates[0] not in (
                    "pixel_xyxy", "normalized_xyxy")):
            _fail("render.stream_osd",
                  "requires a direct field named box from results[].box and "
                  "one consistent pixel_xyxy or normalized_xyxy space")
    if not strict_output or not strict_render:
        return
    result_fields = {
        field.get("name"): field
        for field in fields
        if isinstance(field, dict)
        and isinstance(field.get("from"), str)
        and field["from"].startswith("results[].")
    }
    event_kinds = {
        field.get("event_kind") for field in fields
        if isinstance(field, dict) and field.get("event_kind")
    }
    boxes = render_value.get("boxes")
    if isinstance(boxes, dict):
        if "box" not in result_fields:
            _fail("render.boxes", "requires a declared results[].box field")
        for key in ("label", "color_by"):
            reference = boxes.get(key)
            if reference is not None and reference not in result_fields:
                _fail("render.boxes." + key,
                      "must reference a declared results[] field")
    quads = render_value.get("quads")
    if isinstance(quads, dict):
        for key in ("points", "label"):
            reference = quads.get(key)
            if reference is not None and reference not in result_fields:
                _fail("render.quads." + key,
                      "must reference a declared results[] field")
    if "keypoints" in render_value and "keypoints" not in result_fields:
        _fail("render.keypoints",
              "requires a declared results[].keypoints field")
    for kind in (render_value.get("events") or {}):
        if kind not in event_kinds:
            _fail("render.events." + kind,
                  "requires an output field with the same event_kind")


def _validate_release(value: Any) -> None:
    obj = _expect_object(value, "release")
    _closed(obj, frozenset(("sequence", "channel")), "release")
    _required(obj, frozenset(("sequence", "channel")), "release")
    _positive_int(obj["sequence"], "release.sequence")
    if obj["channel"] not in ("stable", "beta", "dev"):
        _fail("release.channel", "must be stable, beta or dev")


def _validate_compatibility(value: Any) -> None:
    obj = _expect_object(value, "compatibility")
    allowed = frozenset((
        "platform_profile", "arch", "python", "firmware_api", "kit_api",
        "sdk_abi", "rknn_runtime",
    ))
    _closed(obj, allowed, "compatibility")
    _required(obj, frozenset(("platform_profile", "arch", "python")), "compatibility")
    _safe_token(obj["platform_profile"], "compatibility.platform_profile")
    if obj["arch"] != "aarch64":
        _fail("compatibility.arch", "RV1126B packages must declare aarch64")
    if obj["python"] != "==3.11.*":
        _fail("compatibility.python", "must be exactly ==3.11.*")
    for key in ("firmware_api", "kit_api", "sdk_abi", "rknn_runtime"):
        if key in obj:
            _version_constraint(obj[key], "compatibility." + key)


def _validate_wheel(value: Any, index: int) -> None:
    path = f"python.wheels[{index}]"
    obj = _expect_object(value, path)
    allowed = frozenset((
        "name", "version", "filename", "file", "sha256", "size", "tags",
        "source",
    ))
    _closed(obj, allowed, path)
    _required(obj, frozenset((
        "name", "version", "filename", "sha256", "size", "tags", "source",
    )), path)
    _safe_token(obj["name"], path + ".name")
    _safe_token(obj["version"], path + ".version")
    filename = _string(obj["filename"], path + ".filename", max_len=255)
    if os.path.basename(filename) != filename or not filename.endswith(".whl"):
        _fail(path + ".filename", "must be a bare .whl filename")
    wheel_parts = filename[:-4].split("-")
    if len(wheel_parts) != 5:
        _fail(path + ".filename", "must use distribution-version-python-abi-platform.whl")
    wheel_project, wheel_version, python_tag, abi_tag, platform_tag = wheel_parts
    normalise_project = lambda text: re.sub(r"[-_.]+", "-", text).lower()
    if normalise_project(wheel_project) != normalise_project(obj["name"]):
        _fail(path + ".filename", "distribution does not match wheel name")
    if wheel_version != obj["version"]:
        _fail(path + ".filename", "embedded version does not match wheel version")
    if platform_tag == "any":
        if python_tag not in ("py3", "cp311") or abi_tag != "none":
            _fail(path + ".filename", "portable wheels must use py3-none-any")
    elif not platform_tag.endswith("aarch64") or not (
            (python_tag == "py3" and abi_tag == "none")
            or (python_tag == "cp311" and abi_tag in ("abi3", "cp311"))):
        _fail(path + ".filename",
              "AArch64 wheels must use py3-none or cp311-(cp311|abi3)")
    _sha256(obj["sha256"], path + ".sha256")
    _positive_int(obj["size"], path + ".size", maximum=512 * 1024 * 1024)
    tags = _expect_list(obj["tags"], path + ".tags")
    if not tags:
        _fail(path + ".tags", "must not be empty")
    for ti, tag in enumerate(tags):
        _safe_token(tag, f"{path}.tags[{ti}]")
    declared_tag = f"{python_tag}-{abi_tag}-{platform_tag}"
    if tags != [declared_tag]:
        _fail(path + ".tags", f"must exactly declare filename tag {declared_tag!r}")
    if obj["source"] not in ("bundled", "catalog"):
        _fail(path + ".source", "must be bundled or catalog")
    if obj["source"] == "bundled":
        if "file" not in obj:
            _fail(path, "bundled wheel requires file")
        wheel_path = _safe_relpath(obj["file"], path + ".file")
        if not wheel_path.startswith("wheels/") or os.path.basename(wheel_path) != filename:
            _fail(path + ".file", "must be wheels/<filename>")
    elif "file" in obj:
        _fail(path + ".file", "catalog wheel must be resolved by digest, not a package path")


def _validate_python(value: Any) -> None:
    obj = _expect_object(value, "python")
    _closed(obj, frozenset(("runtime_profile", "isolation", "wheels", "imports")), "python")
    _required(obj, frozenset(("runtime_profile", "isolation", "wheels", "imports")), "python")
    _safe_token(obj["runtime_profile"], "python.runtime_profile")
    if obj["isolation"] != "per-release":
        _fail("python.isolation", "must be per-release")
    wheels = _expect_list(obj["wheels"], "python.wheels")
    seen_names: set[str] = set()
    seen_files: set[str] = set()
    for index, wheel in enumerate(wheels):
        _validate_wheel(wheel, index)
        name = wheel["name"].lower().replace("_", "-")
        if name in seen_names:
            _fail(f"python.wheels[{index}].name", "duplicate project")
        seen_names.add(name)
        if wheel["filename"] in seen_files:
            _fail(f"python.wheels[{index}].filename", "duplicate filename")
        seen_files.add(wheel["filename"])
    imports = _expect_list(obj["imports"], "python.imports")
    if len(set(imports)) != len(imports):
        _fail("python.imports", "must not contain duplicates")
    for index, module in enumerate(imports):
        if not isinstance(module, str) or not _IMPORT_RE.fullmatch(module):
            _fail(f"python.imports[{index}]", "must be a Python module name")


def _validate_artifacts(value: Any) -> None:
    artifacts = _expect_list(value, "artifacts")
    seen_ids: set[str] = set()
    seen_mounts: set[str] = set()
    allowed = frozenset((
        "id", "kind", "source", "file", "filename", "sha256", "size", "mount",
        "required", "share_scope",
    ))
    for index, artifact in enumerate(artifacts):
        path = f"artifacts[{index}]"
        obj = _expect_object(artifact, path)
        _closed(obj, allowed, path)
        _required(obj, frozenset((
            "id", "kind", "source", "sha256", "size", "mount", "required",
            "share_scope",
        )), path)
        artifact_id = _safe_token(obj["id"], path + ".id")
        if artifact_id in seen_ids:
            _fail(path + ".id", "duplicate artifact id")
        seen_ids.add(artifact_id)
        if obj["kind"] not in ("data", "dictionary", "labels", "onnx", "rknn"):
            _fail(path + ".kind", "unsupported artifact kind")
        if obj["source"] not in ("bundled", "catalog"):
            _fail(path + ".source", "must be bundled or catalog")
        _sha256(obj["sha256"], path + ".sha256")
        _positive_int(obj["size"], path + ".size", maximum=1024 * 1024 * 1024)
        mount = _safe_relpath(obj["mount"], path + ".mount")
        if mount in seen_mounts:
            _fail(path + ".mount", "duplicate artifact mount")
        seen_mounts.add(mount)
        if not isinstance(obj["required"], bool):
            _fail(path + ".required", "must be a boolean")
        if obj["share_scope"] not in ("content", "private"):
            _fail(path + ".share_scope", "must be content or private")
        if obj["source"] == "bundled":
            if "file" not in obj:
                _fail(path, "bundled artifact requires file")
            file_path = _safe_relpath(obj["file"], path + ".file")
            if file_path != mount:
                _fail(path + ".file", "bundled artifact file must equal mount")
        else:
            if "file" in obj:
                _fail(path + ".file", "catalog artifact must be resolved by digest")
            filename = obj.get("filename")
            if not isinstance(filename, str) or os.path.basename(filename) != filename:
                _fail(path + ".filename", "catalog artifact requires a bare filename")


def _validate_claims(value: Any, path: str) -> set[str]:
    claims = _expect_list(value, path)
    seen: set[str] = set()
    for index, claim in enumerate(claims):
        cp = f"{path}[{index}]"
        obj = _expect_object(claim, cp)
        _closed(obj, frozenset(("name", "mode", "required", "quantity", "timeout_sec")), cp)
        _required(obj, frozenset(("name", "mode", "required")), cp)
        if obj["name"] not in _RESOURCE_NAMES:
            _fail(cp + ".name", "unknown resource")
        if obj["name"] in seen:
            _fail(cp + ".name", "duplicate resource claim")
        seen.add(obj["name"])
        if obj["mode"] not in _RESOURCE_MODES:
            _fail(cp + ".mode", "must be shared, exclusive, brokered or scheduled")
        if obj["mode"] == "scheduled" and obj["name"] != "npu.rknn":
            _fail(cp + ".mode", "scheduled mode is currently reserved for npu.rknn")
        if not isinstance(obj["required"], bool):
            _fail(cp + ".required", "must be a boolean")
        if "quantity" in obj:
            _positive_int(obj["quantity"], cp + ".quantity", maximum=64)
        if "timeout_sec" in obj:
            if not isinstance(obj["timeout_sec"], (int, float)) or isinstance(obj["timeout_sec"], bool) or obj["timeout_sec"] < 0:
                _fail(cp + ".timeout_sec", "must be a non-negative number")
    return seen


def _validate_resources(value: Any, config_keys: dict[str, str]) -> set[str]:
    obj = _expect_object(value, "resources")
    _closed(obj, frozenset(("claims", "profiles", "limits")), "resources")
    if "claims" not in obj and "profiles" not in obj:
        _fail("resources", "requires claims or profiles")
    resource_names: set[str] = set()
    if "claims" in obj:
        resource_names.update(_validate_claims(obj["claims"], "resources.claims"))
    if "profiles" in obj:
        profiles = _expect_list(obj["profiles"], "resources.profiles")
        if not profiles:
            _fail("resources.profiles", "must not be empty")
        seen_when: set[str] = set()
        for index, profile_spec in enumerate(profiles):
            path = f"resources.profiles[{index}]"
            profile_obj = _expect_object(profile_spec, path)
            _closed(profile_obj, frozenset(("when", "claims")), path)
            _required(profile_obj, frozenset(("when", "claims")), path)
            when = _expect_object(profile_obj["when"], path + ".when")
            if len(when) != 1:
                _fail(path + ".when", "must contain exactly one config equality")
            key = next(iter(when), "")
            if key not in config_keys:
                _fail(path + ".when", f"references unknown config key {key!r}")
            if config_keys[key] == "live":
                _fail(path + ".when",
                      "profile selector config must restart or reschedule")
            fingerprint = json.dumps(when, sort_keys=True, separators=(",", ":"))
            if fingerprint in seen_when:
                _fail(path + ".when", "duplicate resource profile condition")
            seen_when.add(fingerprint)
            resource_names.update(_validate_claims(profile_obj["claims"], path + ".claims"))
    limits = _expect_object(obj.get("limits", {}), "resources.limits")
    _closed(limits, frozenset(("memory_mb", "cpu_percent", "storage_mb",
                               "shutdown_grace_sec")), "resources.limits")
    for key in ("memory_mb", "storage_mb", "shutdown_grace_sec"):
        if key in limits:
            _positive_int(limits[key], "resources.limits." + key)
    if "cpu_percent" in limits:
        _positive_int(limits["cpu_percent"], "resources.limits.cpu_percent", maximum=400)
    return resource_names


def _validate_permissions(value: Any) -> set[str]:
    obj = _expect_object(value, "permissions")
    _closed(obj, frozenset(("sdk", "filesystem", "network", "devices")), "permissions")
    _required(obj, frozenset(("sdk", "filesystem", "network")), "permissions")
    sdk = _expect_list(obj["sdk"], "permissions.sdk")
    if len(set(sdk)) != len(sdk):
        _fail("permissions.sdk", "must not contain duplicates")
    for index, permission in enumerate(sdk):
        if permission not in _SDK_PERMISSIONS:
            _fail(f"permissions.sdk[{index}]", "unknown SDK permission")
    fs = _expect_object(obj["filesystem"], "permissions.filesystem")
    _closed(fs, frozenset(("read", "write")), "permissions.filesystem")
    _required(fs, frozenset(("read", "write")), "permissions.filesystem")
    for mode in ("read", "write"):
        scopes = _expect_list(fs[mode], f"permissions.filesystem.{mode}")
        if len(set(scopes)) != len(scopes):
            _fail(f"permissions.filesystem.{mode}", "must not contain duplicates")
        allowed = {"app", "appdata", "artifacts", "tmp"}
        if any(scope not in allowed for scope in scopes):
            _fail(f"permissions.filesystem.{mode}", "contains an unknown logical scope")
    network = _expect_object(obj["network"], "permissions.network")
    _closed(network, frozenset(("listen", "outbound")), "permissions.network")
    _required(network, frozenset(("listen", "outbound")), "permissions.network")
    for mode in ("listen", "outbound"):
        endpoints = _expect_list(network[mode], f"permissions.network.{mode}")
        if len(set(endpoints)) != len(endpoints):
            _fail(f"permissions.network.{mode}", "must not contain duplicates")
        for index, endpoint in enumerate(endpoints):
            if not isinstance(endpoint, str) or not _ENDPOINT_RE.fullmatch(endpoint):
                _fail(f"permissions.network.{mode}[{index}]", "must be a logical endpoint name")
    devices = _expect_list(obj.get("devices", []), "permissions.devices")
    if devices:
        _fail("permissions.devices", "direct device access is not supported in manifest v2")
    return set(sdk)


def _validate_health(value: Any) -> None:
    obj = _expect_object(value, "health")
    _closed(obj, frozenset((
        "protocol", "startup_timeout_sec", "stabilization_sec",
        "liveness_interval_sec", "liveness_failures", "restart",
    )), "health")
    _required(obj, frozenset((
        "protocol", "startup_timeout_sec", "stabilization_sec",
        "liveness_interval_sec", "liveness_failures", "restart",
    )), "health")
    if obj["protocol"] != "kit-health-v1":
        _fail("health.protocol", "must be kit-health-v1")
    for key in ("startup_timeout_sec", "liveness_interval_sec", "liveness_failures"):
        _positive_int(obj[key], "health." + key)
    if not _is_int(obj["stabilization_sec"]) or obj["stabilization_sec"] < 0:
        _fail("health.stabilization_sec", "must be a non-negative integer")
    restart = _expect_object(obj["restart"], "health.restart")
    _closed(restart, frozenset(("policy", "max_attempts", "window_sec", "backoff_sec")),
            "health.restart")
    _required(restart, frozenset(("policy", "max_attempts", "window_sec", "backoff_sec")),
              "health.restart")
    if restart["policy"] not in ("never", "on-failure"):
        _fail("health.restart.policy", "must be never or on-failure")
    if not _is_int(restart["max_attempts"]) or restart["max_attempts"] < 0:
        _fail("health.restart.max_attempts", "must be a non-negative integer")
    _positive_int(restart["window_sec"], "health.restart.window_sec")
    backoff = _expect_list(restart["backoff_sec"], "health.restart.backoff_sec")
    if any(not _is_int(item) or item < 0 for item in backoff):
        _fail("health.restart.backoff_sec", "must contain non-negative integers")


def _validate_instances(value: Any) -> None:
    obj = _expect_object(value, "instances")
    _closed(obj, frozenset(("max", "config_scope", "data_scope", "endpoint_mode")),
            "instances")
    _required(obj, frozenset(("max", "config_scope", "data_scope", "endpoint_mode")),
              "instances")
    _positive_int(obj["max"], "instances.max", maximum=1)
    if obj["max"] != 1:
        _fail("instances.max", "this platform supports exactly one instance per app")
    if obj["config_scope"] not in ("app", "instance"):
        _fail("instances.config_scope", "must be app or instance")
    if obj["data_scope"] not in ("app", "instance"):
        _fail("instances.data_scope", "must be app or instance")
    if obj["endpoint_mode"] != "allocated":
        _fail("instances.endpoint_mode", "must be allocated")


def _validate_package(value: Any) -> None:
    obj = _expect_object(value, "package")
    _closed(obj, frozenset(("exclude",)), "package")
    patterns = _expect_list(obj.get("exclude", []), "package.exclude")
    if len(set(patterns)) != len(patterns):
        _fail("package.exclude", "must not contain duplicate globs")
    for index, value in enumerate(patterns):
        path = f"package.exclude[{index}]"
        pattern = _string(value, path, max_len=255)
        if pattern.startswith(("/", "\\")) or "\\" in pattern:
            _fail(path, "must be a POSIX app-relative glob")
        if ".." in pattern.split("/"):
            _fail(path, "must not contain '..' path segments")


def validate_manifest(manifest: Any, *, allow_v1: bool = True) -> int:
    """Validate one manifest and return its schema version (1 or 2).

    v1 deliberately retains its historical, permissive extension surface.  The
    common identity/path checks still run so build and install agree on the
    minimum safe contract.  v2 is closed except for explicitly namespaced
    ``x-*`` extension fields.
    """
    obj = _expect_object(manifest, "manifest")
    version = manifest_version(obj)
    if version == 1:
        if not allow_v1:
            _fail("manifest_version", "legacy v1 manifests are not allowed here")
        _validate_common(obj)
        return 1
    if version != MANIFEST_VERSION:
        _fail("manifest_version", f"unsupported version {version}; expected 1 or 2")

    _closed(obj, _V2_ALLOWED, "manifest")
    _required(obj, _V2_REQUIRED, "manifest")
    _validate_common(obj)
    if not _SEMVER_RE.fullmatch(obj["version"]):
        _fail("version", "manifest v2 requires SemVer")
    _string(obj["name"], "name", max_len=128)
    if obj["type"] != "self-hosted":
        _fail("type", "manifest v2 currently supports only self-hosted")
    caps = _expect_list(obj["capabilities"], "capabilities")
    if len(set(caps)) != len(caps) or any(not isinstance(cap, str) for cap in caps):
        _fail("capabilities", "must contain unique strings")
    _validate_release(obj["release"])
    _validate_compatibility(obj["compatibility"])
    _validate_python(obj["python"])
    _validate_artifacts(obj["artifacts"])
    config_keys = _validate_config_schema(obj["config_schema"])
    resource_names = _validate_resources(obj["resources"], config_keys)
    sdk_permissions = _validate_permissions(obj["permissions"])
    claim_permissions = {
        "audio.capture": "audio.read",
        "camera.frames": "frame.read",
        "codec.decode": "codec.decode",
        "npu.rknn": "npu.infer",
        "probe.read": "probe.read",
        "result.publish": "result.publish",
        "rga": "rga.use",
    }
    for resource_name in sorted(resource_names):
        permission = claim_permissions[resource_name]
        if permission not in sdk_permissions:
            _fail("permissions.sdk",
                  f"resource {resource_name!r} requires permission {permission!r}")
    _validate_health(obj["health"])
    _validate_instances(obj["instances"])
    if "icon" in obj:
        validate_icon_declaration(obj["icon"])
    if "package" in obj:
        _validate_package(obj["package"])
    if "output" in obj:
        _validate_output_contract(obj["output"])
    if "record_trigger" in obj:
        _validate_record_trigger(obj["record_trigger"], obj.get("output"))
    if "render" in obj:
        _validate_render_contract(obj["render"])
    # Invoke even when either section is absent/legacy: strict geometry is a
    # bidirectional contract and must never install successfully only to be
    # discarded silently by Result Hub.
    _validate_render_references(obj.get("output"), obj.get("render"))
    return MANIFEST_VERSION


def check_platform_compatibility(
    manifest: Mapping[str, Any], *, profile_name: str | None = None,
    arch: str | None = None, python_version: tuple[int, int] | None = None,
) -> None:
    """Fail closed when a v2 package targets a different device profile."""
    if manifest_version(manifest) != MANIFEST_VERSION:
        return
    compat = manifest["compatibility"]
    actual_profile = profile_name or os.environ.get(
        "APPMGR_PLATFORM_PROFILE", DEFAULT_PLATFORM_PROFILE)
    actual_arch = arch or os.environ.get("APPMGR_PLATFORM_ARCH", platform.machine())
    if actual_arch == "arm64":
        actual_arch = "aarch64"
    actual_python = python_version or (sys.version_info.major, sys.version_info.minor)
    if compat["platform_profile"] != actual_profile:
        _fail("compatibility.platform_profile",
              f"requires {compat['platform_profile']!r}, device provides {actual_profile!r}")
    if compat["arch"] != actual_arch:
        _fail("compatibility.arch",
              f"requires {compat['arch']!r}, device provides {actual_arch!r}")
    if actual_python != (3, 11):
        _fail("compatibility.python",
              f"requires CPython 3.11, device appmgr runs {actual_python[0]}.{actual_python[1]}")


def _normalise_records(files: Mapping[str, Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for raw_path, raw_record in files.items():
        path = validate_package_member_path(raw_path, f"files[{raw_path!r}]")
        if path in RESERVED_PACKAGE_PATHS:
            _fail(path, "is reserved package metadata")
        record = _expect_object(raw_record, f"files[{path!r}]")
        digest = _sha256(record.get("sha256"), f"files[{path!r}].sha256")
        size = record.get("size")
        if not _is_int(size) or size < 0:
            _fail(f"files[{path!r}].size", "must be a non-negative integer")
        out[path] = {"sha256": digest, "size": size}
    return out


def validate_package_files(manifest: Mapping[str, Any], files: Mapping[str, Mapping[str, Any]]) -> None:
    """Validate entry/dependency/artifact paths against actual payload records."""
    version = validate_manifest(manifest)
    records = _normalise_records(files)
    for required in ("manifest.json", manifest.get("entry", "app.py")):
        if required not in records:
            _fail(required, "required package file is missing")
    if version == 1:
        return

    if "icon" in manifest:
        icon = validate_icon_declaration(manifest["icon"])
        record = records.get(icon["path"])
        if record is None:
            _fail("icon.path", f"package is missing {icon['path']!r}")
        if record["size"] > MAX_ICON_BYTES:
            _fail(
                "icon.path",
                f"icon exceeds {MAX_ICON_BYTES} byte limit: {record['size']}")

    for index, wheel in enumerate(manifest["python"]["wheels"]):
        if wheel["source"] != "bundled":
            continue
        path = wheel["file"]
        record = records.get(path)
        if record is None:
            _fail(f"python.wheels[{index}].file", f"package is missing {path!r}")
        if record != {"sha256": wheel["sha256"], "size": wheel["size"]}:
            _fail(f"python.wheels[{index}]", f"digest/size does not match {path!r}")

    artifact_mounts = {item["mount"] for item in manifest["artifacts"]}
    for index, artifact in enumerate(manifest["artifacts"]):
        if artifact["source"] != "bundled":
            continue
        path = artifact["file"]
        record = records.get(path)
        if record is None:
            _fail(f"artifacts[{index}].file", f"package is missing {path!r}")
        if record != {"sha256": artifact["sha256"], "size": artifact["size"]}:
            _fail(f"artifacts[{index}]", f"digest/size does not match {path!r}")

    for index, model in enumerate(manifest.get("models", [])):
        model_file = model.get("file")
        if model_file and model_file not in records and model_file not in artifact_mounts:
            _fail(f"models[{index}].file", "is neither bundled nor a declared artifact mount")


def bundled_payload_paths(manifest: Mapping[str, Any]) -> tuple[str, ...]:
    """Return v2 wheel/artifact paths that may be supplied by an asset root."""
    if validate_manifest(manifest) != MANIFEST_VERSION:
        return ()
    declared = []
    for wheel in manifest["python"]["wheels"]:
        if wheel["source"] == "bundled":
            declared.append(wheel["file"])
    for artifact in manifest["artifacts"]:
        if artifact["source"] == "bundled":
            declared.append(artifact["file"])
    if len(set(declared)) != len(declared):
        _fail("manifest", "bundled wheel/artifact paths must be globally unique")
    return tuple(sorted(declared))


def canonical_json(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"),
                       ensure_ascii=False) + "\n").encode("utf-8")


def _bom_bytes(records: Mapping[str, Mapping[str, Any]]) -> bytes:
    lines = [f"{record['sha256']}  {record['size']}  {path}\n"
             for path, record in sorted(records.items())]
    return "".join(lines).encode("utf-8")


def parse_bom(data: bytes) -> dict[str, dict[str, Any]]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        _fail(BOM_PATH, f"must be UTF-8: {exc}")
    records: dict[str, dict[str, Any]] = {}
    for lineno, line in enumerate(text.splitlines(), 1):
        parts = line.split("  ", 2)
        if len(parts) != 3:
            _fail(f"{BOM_PATH}:{lineno}", "expected '<sha256>  <size>  <path>'")
        digest, size_text, path = parts
        _sha256(digest, f"{BOM_PATH}:{lineno}.sha256")
        try:
            size = int(size_text, 10)
        except ValueError:
            _fail(f"{BOM_PATH}:{lineno}.size", "must be an integer")
        if size < 0:
            _fail(f"{BOM_PATH}:{lineno}.size", "must be non-negative")
        path = _safe_relpath(path, f"{BOM_PATH}:{lineno}.path")
        if path in records:
            _fail(f"{BOM_PATH}:{lineno}.path", "duplicate path")
        if path in RESERVED_PACKAGE_PATHS:
            _fail(f"{BOM_PATH}:{lineno}.path", "metadata cannot list itself")
        records[path] = {"sha256": digest, "size": size}
    if not records:
        _fail(BOM_PATH, "must not be empty")
    return records


def make_release_metadata(
    manifest: Mapping[str, Any], files: Mapping[str, Mapping[str, Any]],
) -> tuple[dict[str, Any], bytes]:
    """Return deterministic ``(release_lock, files.sha256 bytes)`` for v2."""
    if validate_manifest(manifest) != MANIFEST_VERSION:
        _fail("manifest_version", "release metadata is only defined for v2")
    records = _normalise_records(files)
    validate_package_files(manifest, records)
    bom = _bom_bytes(records)
    bom_sha = hashlib.sha256(bom).hexdigest()
    manifest_sha = records["manifest.json"]["sha256"]
    identity = hashlib.sha256((manifest_sha + bom_sha).encode("ascii")).hexdigest()[:16]
    release_id = f"{manifest['version']}-{identity}"
    lock = {
        "lock_version": RELEASE_LOCK_VERSION,
        "manifest_version": MANIFEST_VERSION,
        "app": {"id": manifest["id"], "version": manifest["version"]},
        "release": dict(manifest["release"]),
        "release_id": release_id,
        "manifest_sha256": manifest_sha,
        "bom": {
            "path": BOM_PATH,
            "sha256": bom_sha,
            "entries": len(records),
            "payload_bytes": sum(record["size"] for record in records.values()),
        },
        "compatibility": dict(manifest["compatibility"]),
        "python": json.loads(json.dumps(manifest["python"])),
        "artifacts": json.loads(json.dumps(manifest["artifacts"])),
    }
    return lock, bom


def verify_release_metadata(
    manifest: Mapping[str, Any], release_lock: Any, bom_data: bytes,
    files: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """Recompute and exactly match v2 release metadata against package bytes."""
    lock = _expect_object(release_lock, RELEASE_LOCK_PATH)
    actual = _normalise_records(files)
    declared = parse_bom(bom_data)
    if declared != actual:
        missing = sorted(set(actual) - set(declared))
        extra = sorted(set(declared) - set(actual))
        changed = sorted(path for path in set(actual) & set(declared)
                         if actual[path] != declared[path])
        _fail(BOM_PATH, f"does not match payload; missing={missing}, extra={extra}, changed={changed}")
    expected_lock, expected_bom = make_release_metadata(manifest, actual)
    if expected_bom != bom_data:
        _fail(BOM_PATH, "is not in canonical sorted form")
    if lock != expected_lock:
        _fail(RELEASE_LOCK_PATH, "does not match manifest and payload BOM")
    return expected_lock


def release_lock_sha256(release_lock: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(release_lock)).hexdigest()
