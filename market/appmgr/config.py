"""
config.py -- per-app config_schema handling for appmgr (stdlib only).

appmgr must not import the kit package (kit lives at /userdata/local/kit, appmgr
at /userdata/local/appmgr, deployed independently), so this is a self-contained
mirror of the schema logic. Kit's runtime side is kit/config.py.

Responsibilities:
  * read/flatten a manifest config_schema (flat OR grouped) into per-key specs,
  * compute the effective value of each key (config.json overlaid on default),
  * VALIDATE an incoming {key: value} map against the schema (type/enum/range),
  * atomically write the validated overlay to <app_dir>/config.json.
"""
from __future__ import annotations

import base64
import json
import math
import os
import stat
import tempfile
import time
from typing import Any, Dict, List, Tuple

from . import paths


_MAX_TEMPLATE_CHARS = 16 * 1024  # mirrors kit.adapters.output_sink


# --------------------------------------------------------------------------- #
# unified output capability -- injected config_schema group (OUTPUT_SINK_SPEC §3)
# --------------------------------------------------------------------------- #
def _output_schema_items(manifest: dict) -> List[dict]:
    """The output config keys, defaulted from the manifest ``output`` block.

    Complex controls have explicit validators in :func:`_validate_one`.  This
    matters because these values are consumed by network/serial/template code;
    accepting an arbitrary string here would only postpone a confusing failure
    until the app is restarted.
    """
    mout = (manifest or {}).get("output") or {}
    dc = mout.get("default_channel")
    channels = [dc] if isinstance(dc, str) else (list(dc) if dc else ["ws"])
    templates = mout.get("templates") or {}
    return [
        {"key": "output_channels", "type": "channel_multi_select",
         "apply": "restart", "label": "Output channels", "default": channels},
        {"key": "iMode", "type": "enum", "apply": "restart",
         "label": "Output mode", "options": ["ha", "custom", "raw"],
         "default": mout.get("default_mode", "raw")},
        {"key": "template_mode", "type": "enum", "apply": "live",
         "label": "Custom format source", "label_zh": "自定义格式来源",
         "options": [
             {"value": "mapping", "label": "Field mapping",
              "label_zh": "字段映射"},
             {"value": "template", "label": "Message template",
              "label_zh": "消息模板"},
         ],
         # Preserve the historical mapping-first behaviour for existing apps.
         "default": ("mapping" if mout.get("default_mapping") else "template")},
        {"key": "dMqtt", "type": "mqtt", "apply": "restart", "label": "MQTT",
         "default": {"iPort": 1883, "sClientId": "", "sUsername": "",
                     "sPassword": "", "sTopic": "recamera", "sURL": ""}},
        {"key": "dHttp", "type": "http", "apply": "restart", "label": "HTTP",
         "default": {"sUrl": "", "sToken": ""}},
        {"key": "dUart", "type": "uart", "apply": "restart", "label": "UART",
         "default": {"sPort": "", "sPortDev": ""}},
        {"key": "dTemplate", "type": "templates", "apply": "live",
         "label": "Templates",
         "default": {"sDetection": templates.get("detection", ""),
                     "sClassification": templates.get("classification", ""),
                     "sKeypoint": templates.get("keypoint", ""),
                     "sSegmentation": templates.get("segmentation", ""),
                     "sTracking": templates.get("tracking", "")}},
        {"key": "output_mapping", "type": "field_mapping", "apply": "live",
         "label": "Field mapping", "default": mout.get("default_mapping") or []},
        {"key": "output_filters", "type": "output_filters", "apply": "live",
         "label": "Filters",
         "default": {"only_on_detection": False, "classes": [],
                     "rate_limit_hz": 0, "preserve_edge_events": True}},
    ]


_flat_warned: set = set()


def _flat_to_grouped(cs: dict, app_name: str = "") -> dict:
    """DEPRECATED input form: flat {key: spec} -> {"groups":[{items:[...]}]}.

    ★Canonical `config_schema` is GROUPED★ (`groups[].items[]`) -- every in-repo
    app publishes it and the frontend SchemaForm renders by group. A flat schema
    from a third-party package built before the unification is normalised here,
    once, at the manifest boundary, so every consumer below sees only groups.
    """
    items = [{"key": k, **v} for k, v in cs.items() if isinstance(v, dict)]
    if items and app_name not in _flat_warned:
        _flat_warned.add(app_name)
        print(f"[appmgr.config] {app_name or '<app>'}: flat `config_schema` is "
              f"deprecated; publish the grouped form "
              f"(config_schema.groups[].items[])", flush=True)
    return {"groups": [{"key": "general", "title": "General", "items": items}]}


def _normalized_schema(manifest: dict) -> dict:
    """The manifest's `config_schema` in canonical grouped form."""
    cs = (manifest or {}).get("config_schema") or {}
    if not isinstance(cs, dict):
        return {"groups": []}
    if "groups" in cs:
        return dict(cs)
    return _flat_to_grouped(cs, (manifest or {}).get("id")
                            or (manifest or {}).get("name") or "")


def _has_output_group(cs: dict) -> bool:
    for g in cs.get("groups") or []:
        for it in g.get("items") or []:
            if it.get("key") == "output_channels":
                return True
    return False


def effective_manifest(manifest: dict) -> dict:
    """Return the manifest with its `config_schema` in canonical grouped form,
    plus the `output` group injected when the app declares
    `capabilities:["output"]`.

    Pure + idempotent. This is the single source `schema_specs`, `get_config`,
    and `do_set_config`/`validate_config` share so GET validation, POST
    validation and apply-mode classification all agree on the output keys."""
    if not (manifest or {}).get("config_schema") and \
            "output" not in ((manifest or {}).get("capabilities") or []):
        return manifest                    # nothing to normalise, nothing to add
    cs = _normalized_schema(manifest)
    caps = (manifest or {}).get("capabilities") or []
    if "output" in caps and not _has_output_group(cs):
        cs["groups"] = list(cs.get("groups") or []) + [
            {"title": "Output", "key": "output",
             "items": _output_schema_items(manifest)}]
    m = dict(manifest)
    m["config_schema"] = cs
    return m


# --------------------------------------------------------------------------- #
# schema flatten
# --------------------------------------------------------------------------- #
def schema_specs(manifest: dict) -> Dict[str, dict]:
    """Return {key: item_spec} from the canonical grouped config_schema.

    The `output` capability group (OUTPUT_SINK_SPEC §3) is injected here so every
    consumer -- GET, POST validation, apply-mode -- sees the output keys."""
    cs = _normalized_schema(effective_manifest(manifest))
    out: Dict[str, dict] = {}
    for g in cs.get("groups") or []:
        for it in g.get("items") or []:
            if "key" in it:
                out[it["key"]] = it
    return out


def schema_defaults(manifest: dict) -> Dict[str, Any]:
    return {k: v["default"] for k, v in schema_specs(manifest).items()
            if "default" in v}


# --------------------------------------------------------------------------- #
# config.json read / write
# --------------------------------------------------------------------------- #
def config_path(app_id: str) -> str:
    """Canonical user-config path: /userdata/local/appdata/<id>/config.json.

    ★Deliberately OUTSIDE the install dir★ -- installer.install() swaps the whole
    /userdata/local/apps/<id>/ directory, so a config living in there was deleted
    by every upgrade (silent loss of thresholds / ROI / output mapping)."""
    return os.path.join(paths.appdata_dir(app_id), "config.json")


def legacy_config_path(app_id: str) -> str:
    """Pre-migration location: inside the install dir (wiped by upgrades)."""
    return os.path.join(paths.app_dir(app_id), "config.json")


_UPGRADE_CONFIG_FILES = (
    ("appdata", "config.json"),
    ("appdata", "config.quarantine.json"),
    ("appdata", "config.json.corrupt"),
    ("legacy", "config.json"),
    ("legacy", "config.json.migrated"),
)
_MAX_UPGRADE_CONFIG_FILE_BYTES = 1024 * 1024
_MAX_UPGRADE_CONFIG_TOTAL_BYTES = 4 * 1024 * 1024


def _upgrade_config_path(app_id: str, scope: str, name: str) -> str:
    root = (paths.appdata_dir(app_id) if scope == "appdata"
            else paths.app_dir(app_id))
    return os.path.join(root, name)


def snapshot_upgrade_config(app_id: str) -> dict:
    """Capture every config file an upgrade may mutate, byte for byte.

    The snapshot is JSON-serialisable so the install phase journal can recover
    the same bytes after a daemon crash or power loss.  Refuse links and special
    files: silently following one while preparing root code would turn rollback
    into an arbitrary-file writer.
    """
    if not paths.valid_app_id(app_id):
        raise ValueError("invalid app id %r" % app_id)
    records = []
    total_bytes = 0
    appdata_root = paths.appdata_dir(app_id)
    try:
        appdata_info = os.lstat(appdata_root)
    except FileNotFoundError:
        appdata_existed = False
    else:
        if not stat.S_ISDIR(appdata_info.st_mode):
            raise OSError("upgrade config root is not a directory: %s" % appdata_root)
        appdata_existed = True
    for scope, name in _UPGRADE_CONFIG_FILES:
        pathname = _upgrade_config_path(app_id, scope, name)
        try:
            info = os.lstat(pathname)
        except FileNotFoundError:
            records.append({"scope": scope, "name": name, "present": False})
            continue
        if not stat.S_ISREG(info.st_mode):
            raise OSError("upgrade config path is not a regular file: %s" % pathname)
        if info.st_size > _MAX_UPGRADE_CONFIG_FILE_BYTES:
            raise OSError("upgrade config file is too large: %s" % pathname)
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(pathname, flags)
        try:
            opened = os.fstat(fd)
            if (not stat.S_ISREG(opened.st_mode)
                    or opened.st_dev != info.st_dev or opened.st_ino != info.st_ino
                    or opened.st_size != info.st_size):
                raise OSError("upgrade config changed while being snapshotted: %s" % pathname)
            if opened.st_size > _MAX_UPGRADE_CONFIG_FILE_BYTES:
                raise OSError("upgrade config file is too large: %s" % pathname)
            with os.fdopen(fd, "rb", closefd=False) as source:
                data = source.read(_MAX_UPGRADE_CONFIG_FILE_BYTES + 1)
        finally:
            os.close(fd)
        if len(data) != opened.st_size:
            raise OSError("upgrade config changed while being snapshotted: %s" % pathname)
        total_bytes += len(data)
        if total_bytes > _MAX_UPGRADE_CONFIG_TOTAL_BYTES:
            raise OSError("upgrade config snapshot exceeds size limit")
        records.append({
            "scope": scope,
            "name": name,
            "present": True,
            "mode": stat.S_IMODE(info.st_mode),
            "data_b64": base64.b64encode(data).decode("ascii"),
        })
    return {
        "schema_version": 1,
        "app_id": app_id,
        "appdata_existed": appdata_existed,
        "files": records,
    }


def _atomic_write_bytes(pathname: str, data: bytes, mode: int) -> None:
    directory = os.path.dirname(pathname)
    os.makedirs(directory, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".config-restore.", dir=directory)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, int(mode) & 0o777)
        os.replace(temporary, pathname)
        temporary = None
        try:
            directory_fd = os.open(
                directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def _fsync_parent(pathname: str) -> None:
    directory = os.path.dirname(pathname)
    try:
        fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def restore_upgrade_config(snapshot: dict) -> None:
    """Restore :func:`snapshot_upgrade_config` exactly and idempotently."""
    if not isinstance(snapshot, dict) or snapshot.get("schema_version") != 1:
        raise ValueError("invalid upgrade config snapshot")
    app_id = snapshot.get("app_id")
    if not isinstance(app_id, str) or not paths.valid_app_id(app_id):
        raise ValueError("invalid upgrade config snapshot app id")
    for root in (paths.appdata_dir(app_id), paths.app_dir(app_id)):
        try:
            root_info = os.lstat(root)
        except FileNotFoundError:
            continue
        if not stat.S_ISDIR(root_info.st_mode):
            raise OSError("upgrade config root is not a directory: %s" % root)
    expected = {(scope, name) for scope, name in _UPGRADE_CONFIG_FILES}
    records = snapshot.get("files")
    if not isinstance(records, list) or len(records) != len(expected):
        raise ValueError("invalid upgrade config snapshot files")
    seen = set()
    total_bytes = 0
    for record in records:
        if not isinstance(record, dict):
            raise ValueError("invalid upgrade config snapshot record")
        key = (record.get("scope"), record.get("name"))
        if key not in expected or key in seen:
            raise ValueError("invalid upgrade config snapshot path")
        seen.add(key)
        pathname = _upgrade_config_path(app_id, *key)
        try:
            current = os.lstat(pathname)
        except FileNotFoundError:
            current = None
        if current is not None and not stat.S_ISREG(current.st_mode):
            raise OSError("upgrade config path is not a regular file: %s" % pathname)
        if record.get("present") is True:
            try:
                data = base64.b64decode(record.get("data_b64", ""), validate=True)
                mode = int(record.get("mode", 0o600))
            except (ValueError, TypeError) as exc:
                raise ValueError("invalid upgrade config snapshot payload") from exc
            if len(data) > _MAX_UPGRADE_CONFIG_FILE_BYTES:
                raise ValueError("upgrade config snapshot file exceeds size limit")
            total_bytes += len(data)
            if total_bytes > _MAX_UPGRADE_CONFIG_TOTAL_BYTES:
                raise ValueError("upgrade config snapshot exceeds size limit")
            _atomic_write_bytes(pathname, data, mode)
        elif record.get("present") is False:
            try:
                if current is not None:
                    os.unlink(pathname)
                    _fsync_parent(pathname)
            except FileNotFoundError:
                pass
        else:
            raise ValueError("invalid upgrade config snapshot presence")
    if seen != expected:
        raise ValueError("incomplete upgrade config snapshot")
    if not snapshot.get("appdata_existed"):
        try:
            os.rmdir(paths.appdata_dir(app_id))
        except OSError:
            pass


def _atomic_write_json(path: str, data: Dict[str, Any]) -> None:
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".config.", dir=d)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def _canonical_ok(path: str) -> bool:
    """True iff the canonical config at `path` exists AND parses as a dict."""
    try:
        with open(path) as f:
            return isinstance(json.load(f), dict)
    except (OSError, ValueError):
        return False


def migrate_legacy_config(app_id: str) -> bool:
    """One-shot, idempotent move of <app_dir>/config.json -> appdata.

    Devices upgraded from an older appmgr still carry the user's settings inside
    the install dir. Copy them to the new location (only if nothing valid is
    there yet -- the new location always wins), then rename the old file to
    `config.json.migrated` so it is not re-read and the trace stays on disk.

    ★Only RETIRE the legacy file once the canonical copy exists AND parses★
    (健壮#20). The old code renamed the legacy file even when the canonical write
    had failed or the canonical file was corrupt -- leaving BOTH gone and the app
    silently back on manifest defaults. Now a canonical that will not parse keeps
    the legacy file as the source of truth.

    Returns True when a legacy file was consumed/retired. Best-effort: an
    unreadable/corrupt legacy file is left untouched and reported as False.
    """
    old = legacy_config_path(app_id)
    if not os.path.isfile(old):
        return False
    try:
        with open(old) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return False                       # corrupt: leave it, don't destroy
    if not isinstance(data, dict):
        return False
    new = config_path(app_id)
    if os.path.isfile(new) and not _canonical_ok(new):
        # A canonical file EXISTS but does not parse (half-written / corrupted).
        # Do NOT blindly overwrite it and do NOT retire the legacy file: quarantine
        # the corrupt copy (it may hold a newer, partially-written value worth
        # inspecting) and leave the legacy file as the working source of truth
        # (load_user_config falls back to it). A later clean state migrates it.
        try:
            os.replace(new, new + ".corrupt")
        except OSError:
            pass
        return False
    if not os.path.isfile(new):            # nothing there yet -> write it
        try:
            _atomic_write_json(new, data)
        except OSError:
            return False                   # cannot persist -> KEEP the legacy file
    if not _canonical_ok(new):
        return False                       # write did not take -> keep legacy
    os.replace(old, old + ".migrated")     # keep a trace, stop re-reading it
    return True


def load_user_config(app_id: str) -> Dict[str, Any]:
    try:
        migrate_legacy_config(app_id)
    except OSError:
        pass                               # read-only fs etc: fall through
    for p in (config_path(app_id), legacy_config_path(app_id)):
        try:
            with open(p) as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            continue
    return {}


def effective_values_from_overlay(manifest: dict,
                                  overlay: Dict[str, Any]) -> Dict[str, Any]:
    """Build effective values from one already-loaded sparse user overlay.

    ``template_mode`` was introduced after ``output_mapping``/``dTemplate``.
    For a legacy overlay that explicitly cleared mapping, retain the historical
    template fallback even when the package itself ships a default mapping.
    Once a user saves ``template_mode``, that explicit choice always wins.
    """
    eff = schema_defaults(manifest)
    for k, v in (overlay or {}).items():
        eff[k] = v
    caps = (manifest or {}).get("capabilities") or []
    if "output" in caps and "template_mode" not in (overlay or {}):
        eff["template_mode"] = (
            "mapping" if eff.get("output_mapping") else "template")
    return eff


def effective_values(manifest: dict, app_id: str) -> Dict[str, Any]:
    """Manifest defaults overlaid by the user's config.json (config.json wins)."""
    return effective_values_from_overlay(manifest, load_user_config(app_id))


def write_user_config(app_id: str, config: Dict[str, Any]) -> None:
    """MERGE `config` into the existing config.json and write it back atomically.

    ★MERGE semantics (not replace)★: a config POST carries only the keys the user
    changed. Replacing the whole file would reset every OTHER overlaid key back to
    its manifest default. Instead we read the current config.json, overlay the
    posted keys on top (posted values win), and persist the union. Overlaying one
    parameter therefore never clobbers previously-saved overlays.

    A posted key set to ``None`` is REMOVED from the overlay (reverts that single
    key to its manifest default) -- e.g. clearing a `zone`. Write is atomic
    (temp file + fsync + rename), as before.
    """
    merged = load_user_config(app_id)   # {} if missing/corrupt; also migrates
    for k, v in (config or {}).items():
        if v is None:
            merged.pop(k, None)
        else:
            merged[k] = v
    _atomic_write_json(config_path(app_id), merged)


# --------------------------------------------------------------------------- #
# validation
# --------------------------------------------------------------------------- #
def _is_num(x) -> bool:
    if not isinstance(x, (int, float)) or isinstance(x, bool):
        return False
    try:
        return math.isfinite(float(x))
    except (OverflowError, TypeError, ValueError):
        return False


def _valid_point(p) -> bool:
    return (isinstance(p, (list, tuple)) and len(p) == 2
            and all(_is_num(c) and -0.001 <= c <= 1.001 for c in p))


def _enum_values(spec: dict) -> Tuple[List[Any], str]:
    """Return typed enum values from legacy scalars or labelled option objects."""
    options = spec.get("options")
    if not isinstance(options, list) or not options:
        return [], "options must be a non-empty array"
    values: List[Any] = []
    for index, option in enumerate(options):
        value = option
        if isinstance(option, dict):
            if "value" not in option:
                return [], f"options[{index}] needs value"
            unknown = set(option) - {"value", "label", "label_zh"}
            if unknown:
                return [], (f"options[{index}] has unknown field(s): "
                            + ", ".join(sorted(unknown)))
            for label_key in ("label", "label_zh"):
                if label_key in option and not isinstance(option[label_key], str):
                    return [], f"options[{index}].{label_key} must be a string"
            value = option["value"]
        if value is None or isinstance(value, (dict, list)) or not isinstance(
                value, (str, int, float, bool)):
            return [], f"options[{index}] value must be a JSON scalar"
        if isinstance(value, float) and not math.isfinite(value):
            return [], f"options[{index}] value must be finite"
        if any(_enum_value_equal(value, old) for old in values):
            return [], f"options[{index}] duplicates an earlier value"
        values.append(value)
    return values, ""


def _enum_value_equal(left: Any, right: Any) -> bool:
    """JSON-wire equality: numbers share one domain; bool/string stay typed."""
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    if (isinstance(left, (int, float)) and isinstance(right, (int, float))):
        return left == right
    return type(left) is type(right) and left == right


def _validate_string_map(key: str, value: Any, allowed: set[str], *,
                         integer_fields: set[str] = frozenset()) \
        -> Tuple[bool, Any, str]:
    if not isinstance(value, dict):
        return False, None, f"{key}: expected object"
    unknown = set(value) - allowed
    if unknown:
        return False, None, (f"{key}: unknown field(s): "
                             + ", ".join(sorted(unknown)))
    out = dict(value)
    for field, field_value in value.items():
        if field in integer_fields:
            if (not _is_num(field_value)
                    or float(field_value) != int(float(field_value))):
                return False, None, f"{key}.{field}: expected integer"
            out[field] = int(field_value)
        elif not isinstance(field_value, str):
            return False, None, f"{key}.{field}: expected string"
    return True, out, ""


def _validate_mapping(key: str, value: Any) -> Tuple[bool, Any, str]:
    if not isinstance(value, list):
        return False, None, f"{key}: expected array"
    if len(value) > 256:
        return False, None, f"{key}: at most 256 mapping rows are allowed"
    allowed = {"source", "target", "topic", "task", "omit_if_none"}
    out = []
    for index, row in enumerate(value):
        prefix = f"{key}[{index}]"
        if not isinstance(row, dict):
            return False, None, f"{prefix}: expected object"
        unknown = set(row) - allowed
        if unknown:
            return False, None, (f"{prefix}: unknown field(s): "
                                 + ", ".join(sorted(unknown)))
        for required in ("source", "target", "topic"):
            if not isinstance(row.get(required), str) or not row[required].strip():
                return False, None, f"{prefix}.{required}: expected non-empty string"
        if "task" in row and not isinstance(row["task"], str):
            return False, None, f"{prefix}.task: expected string"
        if "omit_if_none" in row and not isinstance(row["omit_if_none"], bool):
            return False, None, f"{prefix}.omit_if_none: expected boolean"
        out.append(dict(row))
    return True, out, ""


def _validate_output_filters(key: str, value: Any) -> Tuple[bool, Any, str]:
    if not isinstance(value, dict):
        return False, None, f"{key}: expected object"
    allowed = {"only_on_detection", "classes", "rate_limit_hz",
               "preserve_edge_events"}
    unknown = set(value) - allowed
    if unknown:
        return False, None, (f"{key}: unknown field(s): "
                             + ", ".join(sorted(unknown)))
    out = dict(value)
    for field in ("only_on_detection", "preserve_edge_events"):
        if field in value and not isinstance(value[field], bool):
            return False, None, f"{key}.{field}: expected boolean"
    if "rate_limit_hz" in value:
        rate = value["rate_limit_hz"]
        if not _is_num(rate) or not 0 <= float(rate) <= 1000:
            return False, None, f"{key}.rate_limit_hz: expected number in [0,1000]"
        out["rate_limit_hz"] = float(rate)
    if "classes" in value:
        classes = value["classes"]
        if (not isinstance(classes, list) or len(classes) > 1024
                or any(v is None or isinstance(v, (dict, list, bool))
                       or not isinstance(v, (str, int, float))
                       or isinstance(v, (int, float)) and not _is_num(v)
                       for v in classes)):
            return False, None, f"{key}.classes: expected scalar array"
    return True, out, ""


def _validate_one(spec: dict, value) -> Tuple[bool, Any, str]:
    """Return (ok, coerced_value, error). Type/enum/range per spec['type']."""
    t = spec.get("type", "number")
    key = spec.get("key", "?")

    # JSON null is the schema-independent reset operation: remove this key from
    # the user overlay so its manifest default becomes effective again.
    if value is None:
        return True, None, ""

    if t == "integer":
        # ★Integer-semantics control★: counts, frame intervals, list caps. The
        # value MUST come back out as an `int` -- it ends up in slices/indices
        # (`results[:max_faces]`) and in event payloads where `12.0` is wrong.
        if not _is_num(value) or float(value) != int(float(value)):
            return False, None, f"{key}: expected integer, got {value!r}"
        v = int(float(value))
        if "step" in spec and (not _is_num(spec["step"])
                               or float(spec["step"]) <= 0
                               or float(spec["step"]) != int(float(spec["step"]))):
            return False, None, f"{key}: schema step must be a positive integer"
        if "min" in spec and (not _is_num(spec["min"])
                              or v < int(spec["min"])):
            return False, None, f"{key}: {v} < min {spec['min']}"
        if "max" in spec and (not _is_num(spec["max"])
                              or v > int(spec["max"])):
            return False, None, f"{key}: {v} > max {spec['max']}"
        return True, v, ""

    if t == "number":
        if not _is_num(value):
            return False, None, f"{key}: expected number, got {type(value).__name__}"
        v = float(value)
        if "step" in spec and (not _is_num(spec["step"])
                               or float(spec["step"]) <= 0):
            return False, None, f"{key}: schema step must be a positive number"
        if "min" in spec and (not _is_num(spec["min"])
                              or v < float(spec["min"]) - 1e-9):
            return False, None, f"{key}: {v} < min {spec['min']}"
        if "max" in spec and (not _is_num(spec["max"])
                              or v > float(spec["max"]) + 1e-9):
            return False, None, f"{key}: {v} > max {spec['max']}"
        # keep ints int (step==1 and integral) so counts stay clean
        if spec.get("step") == 1 and v == int(v):
            v = int(v)
        return True, v, ""

    if t == "boolean":
        if not isinstance(value, bool):
            return False, None, f"{key}: expected boolean"
        return True, value, ""

    if t in ("enum", "select"):
        opts, option_error = _enum_values(spec)
        if option_error:
            return False, None, f"{key}: invalid schema: {option_error}"
        if not any(_enum_value_equal(value, option) for option in opts):
            return False, None, f"{key}: {value!r} not in {opts}"
        return True, value, ""

    if t in ("string", "password"):
        if not isinstance(value, str):
            return False, None, f"{key}: expected string"
        return True, value, ""

    if t == "array":
        if not isinstance(value, list):
            return False, None, f"{key}: expected array"
        return True, value, ""

    if t == "object":
        if not isinstance(value, dict):
            return False, None, f"{key}: expected object"
        return True, value, ""

    if t == "zone":
        if value in (None, [], {}):
            return True, None, ""
        if not isinstance(value, list) or not all(_valid_point(p) for p in value):
            return False, None, f"{key}: zone must be a list of [x,y] in [0,1]"
        maxp = spec.get("maxPoints")
        if maxp and len(value) > int(maxp):
            return False, None, f"{key}: zone has {len(value)} > maxPoints {maxp}"
        if 0 < len(value) < 3:
            return False, None, f"{key}: zone polygon needs >= 3 points"
        return True, [[float(a), float(b)] for a, b in value], ""

    if t == "line":
        if value in (None, {}, []):
            return True, None, ""
        if (not isinstance(value, dict) or not _valid_point(value.get("a"))
                or not _valid_point(value.get("b"))):
            return False, None, f"{key}: line needs a=[x,y] and b=[x,y] in [0,1]"
        out = {"a": [float(c) for c in value["a"]],
               "b": [float(c) for c in value["b"]]}
        if "in" in value:
            if str(value["in"]).lower() not in ("left", "right"):
                return False, None, f"{key}: line 'in' must be left|right"
            out["in"] = str(value["in"]).lower()
        return True, out, ""

    if t == "channel_multi_select":
        allowed = {"ws", "mqtt", "http", "uart"}
        if (not isinstance(value, list) or len(value) > len(allowed)
                or any(not isinstance(v, str) or v not in allowed for v in value)
                or len(set(value)) != len(value)):
            return False, None, (f"{key}: expected a unique array containing only "
                                 "ws, mqtt, http, uart")
        return True, list(value), ""

    if t == "mqtt":
        ok, out, error = _validate_string_map(
            key, value,
            {"sURL", "sUrl", "iPort", "sClientId", "sUsername", "sPassword",
             "sTopic"}, integer_fields={"iPort"})
        if ok and not 1 <= out.get("iPort", 1883) <= 65535:
            return False, None, f"{key}.iPort: expected integer in [1,65535]"
        return ok, out, error

    if t == "http":
        return _validate_string_map(key, value, {"sUrl", "sURL", "sToken"})

    if t == "uart":
        return _validate_string_map(key, value, {"sPort", "sPortDev"})

    if t == "templates":
        ok, out, error = _validate_string_map(
            key, value,
            {"sDetection", "sClassification", "sKeypoint", "sSegmentation",
             "sTracking", "sOBB"})
        if ok and any(len(v) > _MAX_TEMPLATE_CHARS for v in out.values()):
            return False, None, (f"{key}: each template must be at most "
                                 f"{_MAX_TEMPLATE_CHARS} characters")
        return ok, out, error

    if t == "field_mapping":
        return _validate_mapping(key, value)

    if t == "output_filters":
        return _validate_output_filters(key, value)

    return False, None, f"{key}: unsupported config type {t!r}"


def validate_config(manifest: dict, incoming: dict) -> Tuple[Dict[str, Any], List[str]]:
    """Validate incoming {key: value} against the schema.

    Returns (clean, errors). `clean` holds only schema-known, valid keys (a
    sparse overlay to persist). Unknown keys are rejected as errors. On any
    error `clean` is still returned but the caller should refuse to write.
    """
    specs = schema_specs(manifest)
    clean: Dict[str, Any] = {}
    errors: List[str] = []
    if not isinstance(incoming, dict):
        return {}, ["config must be an object"]
    for key, val in incoming.items():
        spec = specs.get(key)
        if spec is None:
            errors.append(f"{key}: unknown parameter")
            continue
        ok, coerced, err = _validate_one(spec, val)
        if ok:
            # Preserve None: it is an intentional delete/reset marker consumed
            # by write_user_config, not an invalid/missing value.
            clean[key] = coerced
        else:
            errors.append(err)
    return clean, errors


def _quarantine_config(app_id: str, dropped: Dict[str, Any]) -> None:
    """Persist config keys a new schema rejected, so they are inspectable rather
    than silently lost. Best-effort."""
    try:
        d = paths.appdata_dir(app_id)
        os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".quarantine.", suffix=".json", dir=d)
        with os.fdopen(fd, "w") as f:
            json.dump({"ts": int(time.time()), "dropped": dropped},
                      f, indent=2, ensure_ascii=False)
        os.replace(tmp, os.path.join(d, "config.quarantine.json"))
    except OSError:
        pass


def revalidate_user_config(manifest: dict, app_id: str) -> dict:
    """After an upgrade, drop stored config keys the NEW schema rejects (健壮#20).

    A manifest upgrade can REMOVE a key, change its type, or narrow a range. The
    stored config.json is not re-checked anywhere, so a now-invalid value would
    reach the app unchanged. Here every stored key is validated against the new
    schema: unknown or invalid keys are dropped (and quarantined for inspection),
    valid keys are kept as-is. Returns {"dropped": {...}, "kept": n}.

    ★No-op when the manifest declares NO schema★ (specs == {}): a third-party app
    without a config_schema must not have its entire config wiped just because
    there is nothing to validate against."""
    cfg = load_user_config(app_id)
    if not cfg:
        return {"dropped": {}, "kept": 0}
    specs = schema_specs(manifest)
    if not specs:
        return {"dropped": {}, "kept": len(cfg), "skipped": True}
    kept: Dict[str, Any] = {}
    dropped: Dict[str, Any] = {}
    for k, v in cfg.items():
        spec = specs.get(k)
        if spec is None:
            dropped[k] = v
            continue
        ok, _coerced, _err = _validate_one(spec, v)
        if ok:
            kept[k] = v                    # keep the user's original value verbatim
        else:
            dropped[k] = v
    if dropped:
        _quarantine_config(app_id, dropped)
        _atomic_write_json(config_path(app_id), kept)
    return {"dropped": dropped, "kept": len(kept)}


def get_config(manifest: dict, app_id: str) -> dict:
    """Response payload for GET /api/appMgr/config: schema + effective values."""
    return {
        "id": app_id,
        "config_schema": effective_manifest(manifest).get("config_schema") or {},
        "values": effective_values(manifest, app_id),
        "defaults": schema_defaults(manifest),
    }
