#!/usr/bin/env python3
"""Offline static validation for reCamera Pro Python SDK applications.

The validator intentionally uses only the Python standard library and never
connects to a device. Hardware and firmware conditions are reported separately
as unverified runtime preconditions.
"""
from __future__ import annotations

import argparse
import ast
import fnmatch
import hashlib
import json
import re
import sys
from pathlib import Path, PurePosixPath
from typing import Any


APP_ID_PATTERN = re.compile(r"[a-z0-9-]{1,64}")
# These types are used by the current official manifests in the SDK checkout.
# They are a Kit/App manifest convention, not a complete recamera_ext schema.
CONFIG_TYPES = frozenset({"number", "integer", "boolean", "string", "enum", "zone", "line"})
CONFIG_APPLY_MODES = frozenset({"live", "restart"})
OUTPUT_MODES = frozenset({"raw", "custom", "ha"})
REMOVED_CALLBACKS = frozenset({"on_results", "process_frame", "run_postproc"})
MODEL_TASK_ALIASES = {
    "detect": "det",
    "pose": "pose",
    "recognize": "rec",
    "landmark": "lmk",
}
BUILTIN_CLASS_NAMES = frozenset({"coco80"})
NPU_RKNN_MODES = frozenset({"scheduled", "brokered", "exclusive"})
NPU_RKNN_SCHEDULED_MODES = frozenset({"scheduled", "brokered"})
RENDER_COORD_SPACES = frozenset({"pixel_xyxy", "normalized_xyxy"})
MANAGED_RESULT_CLAIMS = frozenset({"result.publish", "result.osd", "result.gateway"})
ARTIFACT_KINDS = frozenset({"data", "dictionary", "labels", "onnx", "rknn"})
ARTIFACT_SOURCES = frozenset({"bundled", "catalog"})
ARTIFACT_SHARE_SCOPES = frozenset({"content", "private"})
SHA256_PATTERN = re.compile(r"[0-9a-fA-F]{64}")
RUNTIME_PRECONDITIONS = (
    "The target firmware provides the compatible Python, kit, recamera_ext, and librecamera_ext runtime required by this App.",
    "The required /run/recamera/*.sock endpoints are present, live, and accessible to the App.",
    "Each declared RKNN model can be loaded by the target RV1126B RKNN runtime.",
    "Camera access and the Kit/App output path work with real frame timing and OSD/event publication.",
    "If the App uses GPIO, its board mapping, pinmux, gmgr or documented event route, permissions, and electrical load are verified.",
    "If the App uses audio capture, the documented ALSA topology and access permissions are verified.",
    "The public SDK has no unified speaker/player API. If the App uses a firmware ALSA playback path, verify libasound, the playback card and device string, WAV format, audio-group/root permissions, and audible output on the target hardware before claiming playback works; audible tests require explicit user authorization.",
)


class ValidationFailure(ValueError):
    """Raised when an App cannot be inspected."""


def _issue(severity: str, code: str, location: str, message: str) -> dict[str, str]:
    return {"severity": severity, "code": code, "location": location, "message": message}


def _safe_relative_path(value: Any) -> PurePosixPath | None:
    if not isinstance(value, str) or not value or "\\" in value:
        return None
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts:
        return None
    return path


def _load_json(path: Path, label: str, issues: list[dict[str, str]]) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        issues.append(_issue("error", f"missing_{label}", path.name, f"{path.name} is required"))
        return None
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        issues.append(_issue("error", f"invalid_{label}", path.name, f"cannot read {path.name}: {error}"))
        return None
    if not isinstance(value, dict):
        issues.append(_issue("error", f"invalid_{label}", path.name, f"{path.name} must contain a JSON object"))
        return None
    return value


# Platform contract mirrored from market/appmgr/manifest.py on the device.  The
# App Center compares these against APPMGR_PLATFORM_PROFILE at install time, so
# a package that disagrees is refused before any code runs.
PLATFORM_PROFILE = "rv1126b-linux-gnu-cp311-rknn232-v1"
TARGET_ARCH = "aarch64"
TARGET_PYTHON_CONSTRAINT = "==3.11.*"
TARGET_PYTHON_TAG = "cp311"
# appmgr/pythonenv.py:_PLATFORM_PROJECTS.  The per-release venv uses
# --system-site-packages, so an app wheel shadowing one is refused at install.
PLATFORM_OWNED_PROJECTS = frozenset({
    "cv2", "jinja2", "kit", "markupsafe", "numpy", "recamera-ext",
    "recamera-pro-kit", "rknn-toolkit-lite2", "rknnlite",
})
MAX_ICON_BYTES = 1024 * 1024
SEMVER_PATTERN = re.compile(
    r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-((?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)"
    r"(?:\.(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*))*))?"
    r"(?:\+([0-9a-zA-Z-]+(?:\.[0-9a-zA-Z-]+)*))?"
)
WHEEL_FILENAME_PATTERN = re.compile(
    r"^(?P<distribution>[^-]+)-(?P<version>[^-]+)-(?P<python>[^-]+)-"
    r"(?P<abi>[^-]+)-(?P<platform>[^-]+)\.whl$"
)
# appmgr/visualization.py:supports_detection_stream_osd
STREAM_OSD_COORDS = ("pixel_xyxy", "normalized_xyxy")
# Payload files that are authoring or build inputs, never runtime payload.
PUBLISH_JUNK_PATTERNS = (
    "requirements*.txt", "requirements.lock", "*.zip", "*.tar.gz", "*.whl",
    "test_*.py", "*_test.py", "conftest.py", "Makefile", "*.ipynb",
    "SOP.md", "*.log", ".recamera-*",
)


def _normalise_project(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _validate_platform_contract(manifest: dict[str, Any], issues: list[dict[str, str]]) -> None:
    """Check the fixed device platform contract and the wheel descriptors."""
    compatibility = manifest.get("compatibility")
    if isinstance(compatibility, dict):
        expected = {
            "platform_profile": PLATFORM_PROFILE,
            "arch": TARGET_ARCH,
            "python": TARGET_PYTHON_CONSTRAINT,
        }
        for key, value in expected.items():
            actual = compatibility.get(key)
            if actual is None:
                issues.append(_issue(
                    "error", "missing_platform_contract", f"compatibility.{key}",
                    f"compatibility.{key} is required and must be {value!r}"))
            elif actual != value:
                issues.append(_issue(
                    "error", "platform_contract_mismatch", f"compatibility.{key}",
                    f"must be {value!r} for this device, got {actual!r}; the App Center "
                    f"compares it against APPMGR_PLATFORM_PROFILE and refuses a mismatch"))
    else:
        issues.append(_issue(
            "error", "missing_platform_contract", "compatibility",
            "manifest.compatibility is required and must declare platform_profile, arch "
            "and python"))

    python = manifest.get("python")
    if not isinstance(python, dict):
        return
    runtime_profile = python.get("runtime_profile")
    if not isinstance(runtime_profile, str) or not runtime_profile:
        issues.append(_issue(
            "error", "invalid_runtime_profile", "python.runtime_profile",
            "must be a non-empty token"))
    elif runtime_profile == PLATFORM_PROFILE:
        issues.append(_issue(
            "error", "runtime_profile_reuses_platform_profile", "python.runtime_profile",
            "must not reuse compatibility.platform_profile; first-party apps declare a "
            "runtime label such as 'recamera-ai-cp311-v1'"))
    if python.get("isolation") != "per-release":
        issues.append(_issue(
            "error", "invalid_isolation", "python.isolation",
            "must be 'per-release'; the device builds one immutable venv per release"))

    wheels = python.get("wheels")
    if wheels is None:
        return
    if not isinstance(wheels, list):
        issues.append(_issue("error", "invalid_wheels", "python.wheels", "must be an array"))
        return
    seen_names: set[str] = set()
    seen_files: set[str] = set()
    for index, wheel in enumerate(wheels):
        location = f"python.wheels[{index}]"
        if not isinstance(wheel, dict):
            issues.append(_issue("error", "invalid_wheel", location, "must be an object"))
            continue
        name = wheel.get("name")
        filename = wheel.get("filename")
        if not isinstance(name, str) or not name:
            issues.append(_issue("error", "invalid_wheel_name", f"{location}.name", "must be a non-empty string"))
            continue
        normalised = _normalise_project(name)
        if normalised in PLATFORM_OWNED_PROJECTS:
            issues.append(_issue(
                "error", "platform_owned_wheel", f"{location}.name",
                f"{name!r} is provided by the device runtime; bundling it makes the "
                f"installer refuse the app. Remove the wheel and import it directly."))
        if normalised in seen_names:
            issues.append(_issue("error", "duplicate_wheel_project", f"{location}.name", "duplicate project"))
        seen_names.add(normalised)
        if not isinstance(filename, str) or not filename.endswith(".whl"):
            issues.append(_issue("error", "invalid_wheel_filename", f"{location}.filename", "must be a bare .whl filename"))
            continue
        if filename in seen_files:
            issues.append(_issue("error", "duplicate_wheel_filename", f"{location}.filename", "duplicate filename"))
        seen_files.add(filename)
        match = WHEEL_FILENAME_PATTERN.fullmatch(filename)
        if match is None:
            issues.append(_issue(
                "error", "invalid_wheel_filename", f"{location}.filename",
                "must use distribution-version-python-abi-platform.whl"))
            continue
        if _normalise_project(match.group("distribution")) != normalised:
            issues.append(_issue("error", "wheel_name_mismatch", f"{location}.filename", "distribution does not match wheel name"))
        if match.group("version") != wheel.get("version"):
            issues.append(_issue("error", "wheel_version_mismatch", f"{location}.filename", "embedded version does not match wheel version"))
        python_tag, abi_tag = match.group("python"), match.group("abi")
        platform_tag = match.group("platform")
        if platform_tag == "any":
            if python_tag not in ("py3", TARGET_PYTHON_TAG) or abi_tag != "none":
                issues.append(_issue(
                    "error", "unsupported_wheel_tag", f"{location}.filename",
                    f"portable wheels must use py3-none-any or {TARGET_PYTHON_TAG}-none-any"))
        elif not platform_tag.endswith(TARGET_ARCH) or not (
                (python_tag == "py3" and abi_tag == "none")
                or (python_tag == TARGET_PYTHON_TAG and abi_tag in ("abi3", TARGET_PYTHON_TAG))):
            issues.append(_issue(
                "error", "unsupported_wheel_tag", f"{location}.filename",
                f"AArch64 wheels must use py3-none or {TARGET_PYTHON_TAG}-({TARGET_PYTHON_TAG}|abi3) "
                f"with a platform tag ending in {TARGET_ARCH}"))
        declared_tag = f"{python_tag}-{abi_tag}-{platform_tag}"
        if wheel.get("tags") != [declared_tag]:
            issues.append(_issue(
                "error", "invalid_wheel_tags", f"{location}.tags",
                f"must exactly declare the filename tag {declared_tag!r}"))
        size = wheel.get("size")
        if isinstance(size, bool) or not isinstance(size, int) or size <= 0 or size > 512 * 1024 * 1024:
            issues.append(_issue("error", "invalid_wheel_size", f"{location}.size", "must be a positive byte count <= 512 MiB"))
        source = wheel.get("source")
        if source not in ("bundled", "catalog"):
            issues.append(_issue("error", "invalid_wheel_source", f"{location}.source", "must be bundled or catalog"))
        elif source == "bundled":
            declared_file = wheel.get("file")
            if declared_file != f"wheels/{filename}":
                issues.append(_issue(
                    "error", "invalid_wheel_file", f"{location}.file",
                    f"must be wheels/{filename}"))
        elif "file" in wheel:
            issues.append(_issue(
                "error", "invalid_wheel_file", f"{location}.file",
                "catalog wheel must be resolved by digest, not a package path"))

    imports = python.get("imports")
    if imports is None:
        return
    if not isinstance(imports, list) or not all(isinstance(item, str) for item in imports):
        issues.append(_issue("error", "invalid_imports", "python.imports", "must be an array of module names"))
        return
    if len(set(imports)) != len(imports):
        issues.append(_issue("error", "duplicate_imports", "python.imports", "must not contain duplicates"))
    for index, module in enumerate(imports):
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*", module):
            issues.append(_issue("error", "invalid_import_name", f"python.imports[{index}]", "must be a Python module name"))


def _box_fields(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    output = manifest.get("output")
    fields = output.get("fields") if isinstance(output, dict) else None
    if not isinstance(fields, list):
        return []
    return [
        field for field in fields
        if isinstance(field, dict)
        and field.get("from") == "results[].box"
        and field.get("derived") is not True
    ]


def _validate_stream_osd(manifest: dict[str, Any], issues: list[dict[str, str]]) -> None:
    """Report whether detection boxes can reach the video preview.

    Two independent paths exist.  The browser overlay is drawn from Result Hub
    envelopes and needs a declared coordinate space.  Burn-in OSD on the stream
    itself additionally needs render.stream_osd plus a per-app opt-in that the
    user enables through the App Center visualization API; the manifest is only
    allowed to advertise the capability, never to switch it on.
    """
    boxes = _box_fields(manifest)
    render = manifest.get("render")
    render = render if isinstance(render, dict) else {}
    if not boxes:
        if "stream_osd" in render:
            issues.append(_issue(
                "error", "stream_osd_without_box_field", "render.stream_osd",
                "requires output.fields[] to declare a non-derived results[].box field"))
        return
    coords = [field.get("coord") for field in boxes]
    if not all(isinstance(coord, str) and coord in STREAM_OSD_COORDS for coord in coords) \
            or len(set(coords)) != 1:
        issues.append(_issue(
            "error", "ambiguous_box_coord", "output.fields[].coord",
            f"every non-derived results[].box field must declare one consistent coord from "
            f"{list(STREAM_OSD_COORDS)}; anything else is projected as 'unknown' and the "
            f"frontend draws no box"))
        return
    if not any(field.get("name") == "box" for field in boxes):
        issues.append(_issue(
            "warning", "box_field_not_named_box", "output.fields[].name",
            "stream OSD additionally requires a box field literally named 'box'"))
    if "stream_osd" in render:
        osd = render.get("stream_osd")
        if not isinstance(osd, dict):
            issues.append(_issue("error", "invalid_stream_osd", "render.stream_osd", "must be an object"))
            return
        if osd.get("supported") != ["boxes"]:
            issues.append(_issue(
                "error", "invalid_stream_osd_supported", "render.stream_osd.supported",
                "must be exactly ['boxes']"))
        if osd.get("default") is not False:
            issues.append(_issue(
                "error", "invalid_stream_osd_default", "render.stream_osd.default",
                "must be false; burn-in is always a user opt-in"))
        if render.get("schema_version") != 1:
            issues.append(_issue(
                "error", "stream_osd_requires_render_schema", "render.schema_version",
                "render.stream_osd requires render.schema_version == 1"))
        output = manifest.get("output")
        if not isinstance(output, dict) or output.get("contract_version") != 2:
            issues.append(_issue(
                "error", "stream_osd_requires_output_v2", "output.contract_version",
                "render.stream_osd requires output.contract_version == 2"))
    else:
        issues.append(_issue(
            "warning", "missing_stream_osd", "render.stream_osd",
            "boxes will only reach the browser overlay. To also allow burn-in on the video "
            "stream, declare render.schema_version=1 and "
            "render.stream_osd={'supported':['boxes'],'default':false}; the user then "
            "enables it per app through the App Center visualization setting."))


def _looks_like_count_key(key: str) -> bool:
    return (
        key.endswith(("_frames", "_count"))
        or key.startswith("count_")
        or key in {"interval_frames", "target_reps", "target_sets"}
    )


def _validate_config_schema(manifest: dict[str, Any], issues: list[dict[str, str]]) -> None:
    schema = manifest.get("config_schema")
    if schema is None:
        return
    if not isinstance(schema, dict):
        issues.append(_issue("error", "invalid_config_schema", "manifest.config_schema", "config_schema must be an object"))
        return
    groups = schema.get("groups")
    if not isinstance(groups, list):
        issues.append(_issue("error", "invalid_config_groups", "manifest.config_schema.groups", "config_schema must use groups[].items[]"))
        return
    seen_keys: set[str] = set()
    for group_index, group in enumerate(groups):
        group_location = f"manifest.config_schema.groups[{group_index}]"
        if not isinstance(group, dict) or not isinstance(group.get("items"), list):
            issues.append(_issue("error", "invalid_config_group", group_location, "each group must be an object with an items array"))
            continue
        for item_index, item in enumerate(group["items"]):
            location = f"{group_location}.items[{item_index}]"
            if not isinstance(item, dict):
                issues.append(_issue("error", "invalid_config_item", location, "config item must be an object"))
                continue
            key = item.get("key")
            if not isinstance(key, str) or not key:
                issues.append(_issue("error", "missing_config_key", f"{location}.key", "config item key must be a non-empty string"))
            elif key in seen_keys:
                issues.append(_issue("error", "duplicate_config_key", f"{location}.key", f"duplicate config key: {key}"))
            else:
                seen_keys.add(key)
            item_type = item.get("type")
            if item_type not in CONFIG_TYPES:
                issues.append(_issue("error", "invalid_config_type", f"{location}.type", f"Skill manifest validation accepts the current Kit types: {sorted(CONFIG_TYPES)}"))
            apply_mode = item.get("apply")
            if apply_mode is not None and apply_mode not in CONFIG_APPLY_MODES:
                issues.append(_issue("error", "invalid_config_apply", f"{location}.apply", f"apply must be one of {sorted(CONFIG_APPLY_MODES)}"))
            default = item.get("default")
            if item_type == "integer" and (not isinstance(default, int) or isinstance(default, bool)):
                issues.append(_issue("error", "invalid_integer_default", f"{location}.default", "integer default must be an integer and not a boolean"))
            if isinstance(key, str) and _looks_like_count_key(key) and item_type != "integer":
                issues.append(_issue("error", "count_requires_integer", f"{location}.type", f"count-like config key {key!r} must use integer type"))
            elif isinstance(key, str) and key.startswith(("max_", "min_")) and item_type != "integer":
                issues.append(_issue("warning", "review_count_type", f"{location}.type", f"review whether {key!r} is a count; continuous thresholds may remain number"))


def _validate_artifacts(
    app_dir: Path,
    manifest: dict[str, Any],
    entry_is_root: bool,
    issues: list[dict[str, str]],
) -> dict[str, dict[str, Any]]:
    artifacts = manifest.get("artifacts", [])
    if not isinstance(artifacts, list):
        issues.append(_issue("error", "invalid_artifacts", "manifest.artifacts", "artifacts must be an array"))
        return {}
    bundled_by_path: dict[str, dict[str, Any]] = {}
    for index, artifact in enumerate(artifacts):
        location = f"manifest.artifacts[{index}]"
        if not isinstance(artifact, dict):
            issues.append(_issue("error", "invalid_artifact", location, "artifact entry must be an object"))
            continue
        for field in ("id", "kind", "source", "sha256", "size", "mount", "required", "share_scope"):
            if field not in artifact:
                issues.append(_issue("error", f"missing_artifact_{field}", f"{location}.{field}", f"artifact field is required: {field}"))
        artifact_id, kind, source = artifact.get("id"), artifact.get("kind"), artifact.get("source")
        if not isinstance(artifact_id, str) or not artifact_id:
            issues.append(_issue("error", "invalid_artifact_id", f"{location}.id", "artifact id must be a non-empty string"))
        if kind not in ARTIFACT_KINDS:
            issues.append(_issue("error", "invalid_artifact_kind", f"{location}.kind", f"artifact kind must be one of {sorted(ARTIFACT_KINDS)}"))
        if source not in ARTIFACT_SOURCES:
            issues.append(_issue("error", "invalid_artifact_source", f"{location}.source", f"artifact source must be one of {sorted(ARTIFACT_SOURCES)}"))
        if artifact.get("share_scope") not in ARTIFACT_SHARE_SCOPES:
            issues.append(_issue("error", "invalid_artifact_share_scope", f"{location}.share_scope", f"artifact share_scope must be one of {sorted(ARTIFACT_SHARE_SCOPES)}"))
        if not isinstance(artifact.get("required"), bool):
            issues.append(_issue("error", "invalid_artifact_required", f"{location}.required", "artifact required must be a boolean"))
        size = artifact.get("size")
        if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
            issues.append(_issue("error", "invalid_artifact_size", f"{location}.size", "artifact size must be a positive integer"))
        sha256 = artifact.get("sha256")
        if not isinstance(sha256, str) or SHA256_PATTERN.fullmatch(sha256) is None:
            issues.append(_issue("error", "invalid_artifact_sha256", f"{location}.sha256", "artifact sha256 must be 64 hexadecimal characters"))

        file_path = _safe_relative_path(artifact.get("file"))
        mount_path = _safe_relative_path(artifact.get("mount"))
        if source == "bundled" and artifact.get("file") is None:
            issues.append(_issue("error", "missing_artifact_file", f"{location}.file", "bundled artifact file is required"))
        if artifact.get("file") is not None and file_path is None:
            issues.append(_issue("error", "unsafe_artifact_path", f"{location}.file", "artifact file must be a safe relative path"))
        if artifact.get("mount") is not None and mount_path is None:
            issues.append(_issue("error", "unsafe_artifact_path", f"{location}.mount", "artifact mount must be a safe relative path"))
        if source == "bundled" and file_path is not None and mount_path is not None and file_path != mount_path:
            issues.append(_issue(
                "error", "artifact_file_mount_mismatch", f"{location}.file",
                f"bundled artifact file must equal mount: {artifact.get('file')!r} != {artifact.get('mount')!r}",
            ))
        if source == "bundled" and file_path is not None and mount_path is not None and file_path == mount_path:
            target = app_dir.joinpath(*file_path.parts)
            if not target.is_file():
                issues.append(_issue("error", "missing_artifact_file_on_disk", f"{location}.file", f"artifact file does not exist: {artifact.get('file')}"))
            else:
                actual_size = target.stat().st_size
                if isinstance(size, int) and not isinstance(size, bool) and size != actual_size:
                    issues.append(_issue("error", "artifact_size_mismatch", f"{location}.size", f"artifact size does not match the file: declared {size}, actual {actual_size}"))
                if isinstance(sha256, str) and SHA256_PATTERN.fullmatch(sha256) is not None:
                    digest = hashlib.sha256()
                    with target.open("rb") as artifact_file:
                        for chunk in iter(lambda: artifact_file.read(1024 * 1024), b""):
                            digest.update(chunk)
                    actual_sha256 = digest.hexdigest()
                    if sha256.casefold() != actual_sha256:
                        issues.append(_issue("error", "artifact_sha256_mismatch", f"{location}.sha256", f"artifact sha256 does not match the file: declared {sha256}, actual {actual_sha256}"))
            normalized_path = mount_path.as_posix()
            if normalized_path in bundled_by_path:
                issues.append(_issue("error", "duplicate_artifact_mount", f"{location}.mount", f"duplicate bundled artifact path: {normalized_path}"))
            else:
                bundled_by_path[normalized_path] = artifact
        if not entry_is_root and (file_path is not None or mount_path is not None):
            issues.append(_issue(
                "error", "kit_resource_path_mismatch", location,
                "Kit resolves model/artifact paths from the entry file's directory; "
                "put the entry and all bundled resources at the package root",
            ))
    return bundled_by_path


def _validate_models(
    app_dir: Path,
    manifest: dict[str, Any],
    entry_is_root: bool,
    issues: list[dict[str, str]],
) -> tuple[set[str], dict[str, list[str]]]:
    models = manifest.get("models", [])
    if not isinstance(models, list):
        issues.append(_issue("error", "invalid_models", "manifest.models", "models must be an array"))
        return set(), {}
    ids: set[str] = set()
    tasks: dict[str, list[str]] = {}
    for index, model in enumerate(models):
        location = f"manifest.models[{index}]"
        if not isinstance(model, dict):
            issues.append(_issue("error", "invalid_model", location, "model entry must be an object"))
            continue
        model_id, file_name, task = model.get("id"), model.get("file"), model.get("task")
        for field, value in (("id", model_id), ("file", file_name), ("task", task)):
            if not isinstance(value, str) or not value:
                issues.append(_issue("error", f"missing_model_{field}", f"{location}.{field}", f"model {field} must be a non-empty string"))
        if isinstance(model_id, str) and model_id:
            if model_id in ids:
                issues.append(_issue("error", "duplicate_model_id", f"{location}.id", f"duplicate model id: {model_id}"))
            ids.add(model_id)
        if isinstance(task, str) and task:
            tasks.setdefault(task, []).append(model_id if isinstance(model_id, str) else "")
        path = _safe_relative_path(file_name)
        if file_name is not None and path is None:
            issues.append(_issue("error", "unsafe_model_path", f"{location}.file", "model file must be a safe relative path"))
        elif path is not None and not app_dir.joinpath(*path.parts).is_file():
            issues.append(_issue("error", "missing_model_file", f"{location}.file", f"model file does not exist: {file_name}"))
        if not entry_is_root and path is not None:
            issues.append(_issue(
                "error", "kit_resource_path_mismatch", f"{location}.file",
                "Kit resolves model paths from the entry file's directory; use a package-root entry such as app.py",
            ))
        classes = model.get("classes")
        if isinstance(classes, str) and classes not in BUILTIN_CLASS_NAMES:
            classes_path = _safe_relative_path(classes)
            if classes_path is None:
                issues.append(_issue("error", "unsafe_classes_path", f"{location}.classes", "classes must be a built-in name or safe relative path"))
            else:
                if not entry_is_root:
                    issues.append(_issue(
                        "error", "kit_resource_path_mismatch", f"{location}.classes",
                        "Kit resolves classes files from the entry file's directory; use a package-root entry such as app.py",
                    ))
                target = app_dir.joinpath(*classes_path.parts)
                if not target.is_file() or not target.read_text(encoding="utf-8", errors="ignore").strip():
                    issues.append(_issue("error", "missing_classes_file", f"{location}.classes", f"classes file is missing or empty: {classes}"))
        elif classes is not None and not isinstance(classes, (str, list)):
            issues.append(_issue("error", "invalid_model_classes", f"{location}.classes", "classes must be a built-in name, relative path, or array"))
    return ids, tasks


def _validate_resources(manifest: dict[str, Any], issues: list[dict[str, str]]) -> None:
    """Check AppMgr's NPU scheduling contract before package delivery."""
    resources = manifest.get("resources")
    if resources is None:
        return
    if not isinstance(resources, dict):
        issues.append(_issue("error", "invalid_resources", "manifest.resources", "resources must be an object"))
        return
    claims = resources.get("claims")
    if claims is None:
        return
    if not isinstance(claims, list):
        issues.append(_issue("error", "invalid_resource_claims", "manifest.resources.claims", "resource claims must be an array"))
        return
    for index, claim in enumerate(claims):
        location = f"manifest.resources.claims[{index}]"
        if not isinstance(claim, dict):
            issues.append(_issue("error", "invalid_resource_claim", location, "resource claim must be an object"))
            continue
        if claim.get("name") == "npu.rknn" and claim.get("mode") not in NPU_RKNN_MODES:
            issues.append(_issue(
                "error",
                "invalid_npu_resource_mode",
                f"{location}.mode",
                "npu.rknn must use scheduled service (scheduled/brokered) or exclusive legacy mode",
            ))


def _manifest_uses_managed_runtime(manifest: dict[str, Any]) -> bool:
    """Return whether the App is expected to run under AppMgr ownership."""
    models = manifest.get("models")
    if isinstance(models, list) and models:
        return True
    capabilities = manifest.get("capabilities")
    if isinstance(capabilities, list) and "output" in capabilities:
        return True
    resources = manifest.get("resources")
    claims = resources.get("claims") if isinstance(resources, dict) else None
    return isinstance(claims, list) and any(
        isinstance(claim, dict) and claim.get("name") in MANAGED_RESULT_CLAIMS
        for claim in claims
    )


def _validate_managed_runtime_manifest(manifest: dict[str, Any], issues: list[dict[str, str]]) -> None:
    """Enforce the AppMgr-owned result endpoint contract for managed Apps."""
    if not _manifest_uses_managed_runtime(manifest):
        return
    instances = manifest.get("instances")
    if not isinstance(instances, dict):
        issues.append(_issue(
            "error", "missing_managed_instances", "manifest.instances",
            "model/output Apps must declare instances.endpoint_mode=allocated so AppMgr owns runtime endpoints",
        ))
        return
    if instances.get("endpoint_mode") != "allocated":
        issues.append(_issue(
            "error", "invalid_managed_endpoint_mode", "manifest.instances.endpoint_mode",
            "managed model/output Apps must use endpoint_mode=allocated; fixed App-owned endpoints are not supported",
        ))
    resources = manifest.get("resources")
    claims = resources.get("claims") if isinstance(resources, dict) else None
    publish_claims = [
        claim for claim in claims or []
        if isinstance(claim, dict) and claim.get("name") == "result.publish"
    ] if isinstance(claims, list) else []
    if len(publish_claims) != 1:
        issues.append(_issue(
            "error", "missing_managed_result_claim", "manifest.resources.claims",
            "managed model/output Apps must declare exactly one result.publish claim with mode=brokered",
        ))
    elif publish_claims[0].get("mode") != "brokered":
        issues.append(_issue(
            "error", "managed_result_claim_not_brokered", "manifest.resources.claims",
            "managed model/output Apps must use result.publish mode=brokered; shared selects result.ingress and does not inject the authenticated result gateway",
        ))
    permissions = manifest.get("permissions")
    network = permissions.get("network") if isinstance(permissions, dict) else None
    listeners = network.get("listen") if isinstance(network, dict) else None
    if isinstance(listeners, list) and any(
        value == 8124 or value == "8124" or value == "127.0.0.1:8124"
        or value == "0.0.0.0:8124" for value in listeners
    ):
        issues.append(_issue(
            "error", "fixed_result_sink_port", "manifest.permissions.network.listen",
            "AppMgr owns the result gateway; the App must not declare or bind the reserved 8124 port",
        ))


def _validate_scheduled_rknn_artifacts(
    manifest: dict[str, Any],
    bundled_artifacts: dict[str, dict[str, Any]],
    issues: list[dict[str, str]],
) -> None:
    """Mirror AppMgr inference authorization for scheduled NPU apps."""
    resources = manifest.get("resources")
    claims = resources.get("claims") if isinstance(resources, dict) else None
    scheduled_npu = any(
        isinstance(claim, dict)
        and claim.get("name") == "npu.rknn"
        and claim.get("mode") in NPU_RKNN_SCHEDULED_MODES
        for claim in claims
    ) if isinstance(claims, list) else False
    if not scheduled_npu:
        return
    models = manifest.get("models")
    if not isinstance(models, list) or not models:
        issues.append(_issue(
            "error", "missing_scheduled_rknn_models", "manifest.models",
            "scheduled npu.rknn requires at least one models[] entry",
        ))
        return
    for index, model in enumerate(models):
        if not isinstance(model, dict):
            continue
        model_path = _safe_relative_path(model.get("file"))
        if model_path is None:
            continue
        artifact = bundled_artifacts.get(model_path.as_posix())
        if artifact is None or artifact.get("kind") != "rknn":
            issues.append(_issue(
                "error", "missing_scheduled_rknn_artifact", f"manifest.models[{index}].file",
                f"scheduled npu.rknn requires a bundled RKNN artifact for model file: {model.get('file')}",
            ))


def _validate_output(manifest: dict[str, Any], mode: str, issues: list[dict[str, str]]) -> None:
    capabilities = manifest.get("capabilities", [])
    if not isinstance(capabilities, list):
        issues.append(_issue("error", "invalid_capabilities", "manifest.capabilities", "capabilities must be an array"))
        return
    if "output" not in capabilities:
        return
    output = manifest.get("output")
    if not isinstance(output, dict):
        issues.append(_issue("error", "missing_output", "manifest.output", "output capability requires an output object"))
        return
    channels = output.get("default_channel")
    valid_channels = isinstance(channels, str) and bool(channels) or (
        isinstance(channels, list) and bool(channels) and all(isinstance(item, str) and item for item in channels)
    )
    if not valid_channels:
        issues.append(_issue("error", "invalid_default_channel", "manifest.output.default_channel", "default_channel must be a non-empty string or string array"))
    if output.get("default_mode") not in OUTPUT_MODES:
        issues.append(_issue("error", "invalid_default_mode", "manifest.output.default_mode", f"default_mode must be one of {sorted(OUTPUT_MODES)}"))
    fields = output.get("fields")
    if not isinstance(fields, list) or not fields:
        issues.append(_issue("error", "missing_output_fields", "manifest.output.fields", "output.fields must be a non-empty array"))
    else:
        names: set[str] = set()
        sources: set[str] = set()
        for index, field in enumerate(fields):
            location = f"manifest.output.fields[{index}]"
            if not isinstance(field, dict):
                issues.append(_issue("error", "invalid_output_field", location, "output field must be an object"))
                continue
            for key in ("name", "from", "type", "description"):
                if not isinstance(field.get(key), str) or not field[key]:
                    issues.append(_issue("error", f"missing_output_field_{key}", f"{location}.{key}", f"output field {key} must be a non-empty string"))
            name, source = field.get("name"), field.get("from")
            if isinstance(name, str) and name:
                if name in names:
                    issues.append(_issue("error", "duplicate_output_field_name", f"{location}.name", f"duplicate output field name: {name}"))
                names.add(name)
            if isinstance(source, str) and source:
                if source in sources:
                    issues.append(_issue("error", "duplicate_output_field_source", f"{location}.from", f"duplicate output field source: {source}"))
                sources.add(source)
    mappings = output.get("default_mapping")
    missing_severity = "error" if mode == "publish" else "warning"
    if not isinstance(mappings, list) or not mappings:
        issues.append(_issue(missing_severity, "missing_default_mapping", "manifest.output.default_mapping", "public output should declare a non-empty default_mapping"))
    else:
        for index, mapping in enumerate(mappings):
            location = f"manifest.output.default_mapping[{index}]"
            if not isinstance(mapping, dict):
                issues.append(_issue("error", "invalid_output_mapping", location, "mapping must be an object"))
                continue
            for key in ("source", "target", "topic"):
                if not isinstance(mapping.get(key), str) or not mapping[key]:
                    issues.append(_issue("error", f"missing_mapping_{key}", f"{location}.{key}", f"mapping {key} must be a non-empty string"))


def _is_app_base(base: ast.expr, aliases: set[str]) -> bool:
    return isinstance(base, ast.Name) and base.id in aliases or (
        isinstance(base, ast.Attribute) and base.attr == "App"
    )


def _calls_super_setup(function: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    for node in ast.walk(function):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute) or node.func.attr != "setup":
            continue
        owner = node.func.value
        if isinstance(owner, ast.Call) and isinstance(owner.func, ast.Name) and owner.func.id == "super":
            return True
    return False


def _model_attribute_names(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute):
            continue
        owner = node.value
        if isinstance(owner, ast.Attribute) and owner.attr == "models" and isinstance(owner.value, ast.Name) and owner.value.id == "self":
            names.add(node.attr)
    return names


def _kit_app_aliases(tree: ast.Module) -> set[str]:
    aliases: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module == "kit.app":
            for name in node.names:
                if name.name == "App":
                    aliases.add(name.asname or name.name)
    return aliases


def _string_literals(tree: ast.AST) -> list[str]:
    return [node.value for node in ast.walk(tree) if isinstance(node, ast.Constant) and isinstance(node.value, str)]


def _literal_command(value: ast.AST) -> bool:
    """Return whether a subprocess executable is statically fixed."""
    if isinstance(value, ast.Constant) and isinstance(value.value, str) and value.value:
        return True
    if isinstance(value, (ast.List, ast.Tuple)) and value.elts:
        first = value.elts[0]
        return isinstance(first, ast.Constant) and isinstance(first.value, str) and bool(first.value)
    return False


def _validate_command_execution(tree: ast.AST, relative: str, issues: list[dict[str, str]]) -> None:
    """Reject configurable/root command entry points in public Apps."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = node.func.attr if isinstance(node.func, ast.Attribute) else None
        owner = node.func.value.id if isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name) else None
        if owner == "os" and name in {"system", "popen"}:
            issues.append(_issue("error", "forbidden_command_api", f"{relative}:{node.lineno}", "Skill safety policy: os.system/os.popen are not allowed in public Apps"))
            continue
        if owner != "subprocess" or name not in {"run", "Popen", "call", "check_call", "check_output"}:
            continue
        shell = next((keyword.value for keyword in node.keywords if keyword.arg == "shell"), None)
        if isinstance(shell, ast.Constant) and shell.value is True:
            issues.append(_issue("error", "forbidden_shell_execution", f"{relative}:{node.lineno}", "Skill safety policy: subprocess shell execution is not allowed"))
        if node.args and not _literal_command(node.args[0]):
            issues.append(_issue("error", "configurable_command_entry", f"{relative}:{node.lineno}", "Skill safety policy: the executable passed to subprocess must be a fixed literal or literal argv list"))


def _validate_managed_result_channel(tree: ast.AST, relative: str, issues: list[dict[str, str]]) -> None:
    """Reject child-owned result listeners and hand-written AppMgr identity."""
    sink_classes = {"WsResultSink", "GatewayResultSink"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            function_name = node.func.id if isinstance(node.func, ast.Name) else (
                node.func.attr if isinstance(node.func, ast.Attribute) else None
            )
            if function_name in sink_classes:
                issues.append(_issue(
                    "error", "direct_result_sink_construction", f"{relative}:{node.lineno}",
                    "AppMgr-managed Apps must publish through kit.App.emit(); do not construct WsResultSink or GatewayResultSink",
                    ))
            port_keyword = next((keyword.value for keyword in node.keywords if keyword.arg == "port"), None)
            if isinstance(port_keyword, ast.Constant) and port_keyword.value == 8124:
                issues.append(_issue(
                    "error", "fixed_result_sink_port", f"{relative}:{node.lineno}",
                    "AppMgr-managed Apps must not hard-code the reserved result port 8124",
                ))
            if function_name in {"open_result_sink", "select_result_sink"}:
                kind = node.args[0] if node.args else None
                if isinstance(kind, ast.Constant) and kind.value in {"ws", "osd"}:
                    issues.append(_issue(
                        "error", "direct_result_sink_construction", f"{relative}:{node.lineno}",
                        "AppMgr-managed Apps must not select a child-owned WebSocket/OSD sink; use kit.App.emit()",
                    ))
            if isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
                if node.func.value.id == "os" and node.func.attr in {"putenv"}:
                    first = node.args[0] if node.args else None
                    if isinstance(first, ast.Constant) and first.value == "RECAMERA_RESULT_GATEWAY_SOCK":
                        issues.append(_issue(
                            "error", "manual_managed_gateway_override", f"{relative}:{node.lineno}",
                            "RECAMERA_RESULT_GATEWAY_SOCK is minted by AppMgr and must not be set by the App",
                        ))
        target = None
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            target = node.targets[0] if isinstance(node, ast.Assign) and node.targets else node.target
        if isinstance(target, ast.Subscript) and isinstance(target.value, ast.Attribute):
            if isinstance(target.value.value, ast.Name) and target.value.value.id == "os" and target.value.attr == "environ":
                key = target.slice
                if isinstance(key, ast.Constant) and key.value == "RECAMERA_RESULT_GATEWAY_SOCK":
                    issues.append(_issue(
                        "error", "manual_managed_gateway_override", f"{relative}:{node.lineno}",
                        "RECAMERA_RESULT_GATEWAY_SOCK is minted by AppMgr and must not be assigned by the App",
                    ))
        # Plain documentation strings and diagnostic messages are harmless. The
        # validator only rejects 8124 when it is used as a socket/CLI argument,
        # which is handled by the call-site checks above.


def _emit_paths(tree: ast.AST) -> tuple[set[str], bool, bool]:
    """Collect envelope roots and top-level keys produced by kit.App.emit()."""
    roots: set[str] = set()
    dynamic_results = False
    found_emit = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute) or node.func.attr != "emit":
            continue
        if not isinstance(node.func.value, ast.Name) or node.func.value.id != "self":
            continue
        found_emit = True
        roots.update({"results", "events", "inference_time_ms", "pipeline_ms", "stream_id"})
        for keyword in node.keywords:
            if keyword.arg == "extra" and isinstance(keyword.value, ast.Dict):
                for key in keyword.value.keys:
                    if isinstance(key, ast.Constant) and isinstance(key.value, str):
                        roots.add(key.value)
            if keyword.arg == "results" and not isinstance(keyword.value, (ast.List, ast.Tuple, ast.Dict)):
                dynamic_results = True
        # render is an envelope member only when the manifest declares it.
        # It is handled as a known root after a real emit() is found.
    return roots, dynamic_results, found_emit


def _output_source_root(source: str) -> str | None:
    match = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)(?:\[|\.|$)", source)
    return match.group(1) if match else None


def _validate_output_paths(manifest: dict[str, Any], trees: list[tuple[str, ast.AST]], issues: list[dict[str, str]]) -> None:
    output = manifest.get("output")
    if not isinstance(output, dict) or not isinstance(output.get("fields"), list):
        return
    roots: set[str] = set()
    dynamic_results = False
    kit_emit_found = False
    direct_sink_found = False
    for _, tree in trees:
        found, dynamic, has_emit = _emit_paths(tree)
        roots.update(found)
        dynamic_results = dynamic_results or dynamic
        kit_emit_found = kit_emit_found or has_emit
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in {"send_detections", "send_classifications", "send_tracks", "send_keypoints"}:
                if isinstance(node.func.value, ast.Name) and node.func.value.id in {"ResultSink", "sink"}:
                    direct_sink_found = True
    if not kit_emit_found:
        if direct_sink_found:
            issues.append(_issue("warning", "direct_resultsink_contract", "manifest.output", "direct recamera_ext.ResultSink output is not described by kit manifest output.fields; validate its normalized coordinates and microsecond PTS separately"))
        return
    for index, field in enumerate(output["fields"]):
        if not isinstance(field, dict) or not isinstance(field.get("from"), str):
            continue
        source = field["from"]
        root = _output_source_root(source)
        if root == "extra":
            issues.append(_issue("error", "invalid_emit_output_path", f"manifest.output.fields[{index}].from", "kit.App.emit(extra=...) merges keys at the envelope root; do not use the literal extra.* path"))
        elif root and root not in roots:
            issues.append(_issue("error", "unemitted_output_path", f"manifest.output.fields[{index}].from", f"output path root {root!r} is not produced by any kit.App.emit() call"))
        elif root == "results" and not dynamic_results and "[]" in source:
            issues.append(_issue("warning", "unresolved_output_path", f"manifest.output.fields[{index}].from", "results field is emitted, but its nested keys are computed dynamically and could not be proven statically"))


def _kit_emit_result_usage(trees: list[tuple[str, ast.AST]]) -> tuple[bool, bool]:
    """Return (kit results emission found, nested result shape is dynamic)."""
    found = False
    dynamic = False
    for _, tree in trees:
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "emit"
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "self"):
                continue
            results_keyword = next(
                (keyword for keyword in node.keywords if keyword.arg == "results"),
                None,
            )
            if results_keyword is None:
                continue
            found = True
            if not isinstance(results_keyword.value, (ast.List, ast.Tuple, ast.Dict)):
                dynamic = True
    return found, dynamic


def _validate_detection_render_contract(
    manifest: dict[str, Any],
    model_tasks: dict[str, list[str]],
    trees: list[tuple[str, ast.AST]],
    issues: list[dict[str, str]],
) -> None:
    """Require the browser-renderable contract for Kit detection Apps.

    Publishing an envelope is not enough to make the Result Center draw boxes.
    This gate applies only to Kit Apps that declare a detector model and/or
    emit Kit results, leaving event-only and direct ResultSink Apps separate.
    """
    kit_results_found, dynamic_results = _kit_emit_result_usage(trees)
    detector_declared = any(
        bool(model_tasks.get(task))
        for task in ("detect", "det", "detection")
    )
    output = manifest.get("output")
    output_fields = output.get("fields") if isinstance(output, dict) else None
    has_box_field = isinstance(output_fields, list) and any(
        isinstance(field, dict) and field.get("from") == "results[].box"
        for field in output_fields
    )
    if not (detector_declared and kit_results_found) and not has_box_field:
        return

    location = "manifest.output"
    if not isinstance(output, dict):
        issues.append(_issue(
            "error", "missing_detection_output_contract", location,
            "Kit detection Apps must declare output.contract_version=2, sink=ws, results[].box, and render.boxes so the frontend can draw boxes",
        ))
        return
    if output.get("contract_version") != 2:
        issues.append(_issue(
            "error", "missing_detection_output_contract", f"{location}.contract_version",
            "Kit detection Apps must set output.contract_version to 2 for browser-renderable results",
        ))
    if output.get("sink") != "ws":
        issues.append(_issue(
            "error", "invalid_detection_output_sink", f"{location}.sink",
            "browser-renderable Kit detection output must use sink=ws; AppMgr owns the result endpoint",
        ))
    if not isinstance(output_fields, list):
        issues.append(_issue(
            "error", "missing_detection_box_field", f"{location}.fields",
            "Kit detection output must declare a results[].box field",
        ))
        output_fields = []
    box_fields = [
        field for field in output_fields
        if isinstance(field, dict) and field.get("from") == "results[].box"
    ]
    direct_box_fields = [field for field in box_fields if field.get("derived") is not True]
    if not any(field.get("name") == "box" for field in direct_box_fields):
        issues.append(_issue(
            "error", "missing_detection_box_field", f"{location}.fields",
            "Kit detection output must declare one direct field named box from results[].box",
        ))
    coordinates = [field.get("coord") for field in direct_box_fields]
    if not coordinates or any(coord not in RENDER_COORD_SPACES for coord in coordinates):
        issues.append(_issue(
            "error", "invalid_detection_box_coord", f"{location}.fields",
            "results[].box must declare coord=pixel_xyxy or coord=normalized_xyxy",
        ))
    elif len(set(coordinates)) != 1:
        issues.append(_issue(
            "error", "inconsistent_detection_box_coord", f"{location}.fields",
            "all direct results[].box fields must use one coordinate space",
        ))

    render = manifest.get("render")
    if not isinstance(render, dict) or render.get("schema_version") != 1:
        issues.append(_issue(
            "error", "missing_detection_render_contract", "manifest.render",
            "Kit detection Apps must declare render.schema_version=1 with render.boxes for frontend box rendering",
        ))
        return
    boxes = render.get("boxes")
    if not isinstance(boxes, dict):
        issues.append(_issue(
            "error", "missing_detection_render_boxes", "manifest.render.boxes",
            "render.boxes is required for frontend detection-box rendering",
        ))
        return
    field_names = {
        field.get("name") for field in output_fields
        if isinstance(field, dict) and isinstance(field.get("name"), str)
    }
    for key in ("label", "color_by"):
        reference = boxes.get(key)
        if reference is not None and reference not in field_names:
            issues.append(_issue(
                "error", "invalid_detection_render_reference", f"manifest.render.boxes.{key}",
                f"render.boxes.{key} must reference a declared output field",
            ))
    if dynamic_results:
        issues.append(_issue(
            "warning", "dynamic_detection_results", "manifest.output.fields",
            "results are emitted from a dynamically computed value; static validation cannot prove every item contains box, cls/label, and score fields",
        ))


def _validate_python(app_dir: Path, model_ids: set[str], model_tasks: dict[str, list[str]], issues: list[dict[str, str]]) -> list[tuple[str, ast.AST]]:
    parsed_trees: list[tuple[str, ast.AST]] = []
    for path in sorted(app_dir.rglob("*.py")):
        relative = path.relative_to(app_dir).as_posix()
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=relative)
        except (OSError, UnicodeError, SyntaxError) as error:
            issues.append(_issue("error", "invalid_python", relative, f"cannot parse Python source: {error}"))
            continue
        parsed_trees.append((relative, tree))
        literals = _string_literals(tree)
        if any("/dev/video" in value or "/var/tmp/rkipc" in value for value in literals):
            issues.append(_issue("error", "forbidden_media_path", relative, "App must not bypass or compete with RKIPC media paths"))
        _validate_entry_file_path_usage(tree, relative, issues)
        _validate_command_execution(tree, relative, issues)
        _validate_managed_result_channel(tree, relative, issues)
        aliases = _kit_app_aliases(tree)
        for node in tree.body:
            if not isinstance(node, ast.ClassDef) or not any(_is_app_base(base, aliases) for base in node.bases):
                continue
            owns_loop = False
            methods: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    methods[child.name] = child
                elif isinstance(child, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "owns_loop" for target in child.targets):
                    owns_loop = isinstance(child.value, ast.Constant) and child.value.value is True
                elif isinstance(child, ast.AnnAssign) and isinstance(child.target, ast.Name) and child.target.id == "owns_loop":
                    owns_loop = isinstance(child.value, ast.Constant) and child.value.value is True
            class_location = f"{relative}:{node.lineno}"
            if not owns_loop:
                issues.append(_issue("error", "missing_owns_loop", class_location, "current Kit implementation: App subclass must declare owns_loop = True"))
            run = methods.get("run")
            if run is None:
                issues.append(_issue("error", "missing_run", class_location, "current Kit implementation: App subclass must implement run(self)"))
            else:
                positional = list(run.args.posonlyargs) + list(run.args.args)
                if len(positional) != 1 or positional[0].arg != "self" or run.args.vararg is not None:
                    issues.append(_issue("error", "invalid_run_signature", f"{relative}:{run.lineno}", "current Kit implementation: run must have the signature run(self)"))
            for removed in sorted(REMOVED_CALLBACKS & set(methods)):
                issues.append(_issue("error", "removed_kit_callback", f"{relative}:{methods[removed].lineno}", f"current Kit implementation: {removed} is not part of the current App lifecycle"))
            setup = methods.get("setup")
            if setup is not None and not _calls_super_setup(setup):
                issues.append(_issue("error", "missing_super_setup", f"{relative}:{setup.lineno}", "current Kit implementation: setup(config) override must call super().setup(config)"))
        valid_model_attributes = set(model_ids)
        for task, ids in model_tasks.items():
            if len(ids) == 1 and task in MODEL_TASK_ALIASES:
                valid_model_attributes.add(MODEL_TASK_ALIASES[task])
        for attribute in sorted(_model_attribute_names(tree) - valid_model_attributes):
            issues.append(_issue("warning", "unknown_model_attribute", relative, f"self.models.{attribute} does not match a declared model id or unambiguous task alias"))
    return parsed_trees


def _path_call_with_file(node: ast.AST) -> bool:
    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "Path" and len(node.args) == 1):
        return False
    argument = node.args[0]
    return (
        (isinstance(argument, ast.Constant) and argument.value == "__file__")
        or (isinstance(argument, ast.Name) and argument.id == "__file__")
    )


def _validate_entry_file_path_usage(tree: ast.AST, relative: str, issues: list[dict[str, str]]) -> None:
    """Discourage deriving the package root from the Kit entry module."""
    uses_file_path = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and _path_call_with_file(node.value):
            uses_file_path = True
            if node.attr in {"parent", "parents"}:
                issues.append(_issue(
                    "error", "manual_app_root_derivation", relative,
                    "do not derive the App/package root from __file__; Kit resolves manifest resources itself",
                ))
            continue
        if _path_call_with_file(node):
            uses_file_path = True
    if uses_file_path:
        issues.append(_issue(
            "warning", "entry_file_path_usage", relative,
            "Path(__file__) changes meaning when AppMgr imports the entry module; avoid it for package resources",
        ))


def _validate_publish_hygiene(
    app_dir: Path,
    manifest: dict[str, Any],
    mode: str,
    issues: list[dict[str, str]],
) -> None:
    """Apply release-grade checks that only matter for a published package."""
    version = manifest.get("version")
    if not isinstance(version, str) or SEMVER_PATTERN.fullmatch(version) is None:
        issues.append(_issue(
            "error", "invalid_app_version", "manifest.version",
            "manifest v2 requires SemVer"))
    release = manifest.get("release")
    if isinstance(release, dict):
        channel = release.get("channel")
        if mode == "publish" and channel != "stable":
            issues.append(_issue(
                "error", "unstable_publish_channel", "release.channel",
                f"publish requires channel 'stable', got {channel!r}"))
    icon = manifest.get("icon")
    if isinstance(icon, dict):
        relative = icon.get("path")
        if isinstance(relative, str) and relative:
            source = app_dir / relative
            if not source.is_file():
                issues.append(_issue(
                    "error", "missing_icon", "icon.path",
                    f"declared icon is not present in the App directory: {relative}"))
            else:
                size = source.stat().st_size
                if size > MAX_ICON_BYTES:
                    issues.append(_issue(
                        "error", "icon_too_large", "icon.path",
                        f"icon is {size} bytes, over the {MAX_ICON_BYTES}-byte device cap"))
                prefix = source.read_bytes()[:16]
                raster = (
                    prefix.startswith(b"\x89PNG\r\n\x1a\n")
                    or prefix.startswith(b"\xff\xd8\xff")
                    or (len(prefix) >= 12 and prefix.startswith(b"RIFF") and prefix[8:12] == b"WEBP")
                )
                if not raster:
                    issues.append(_issue(
                        "error", "icon_not_raster", "icon.path",
                        "icon must be PNG, JPEG or WebP; the installer refuses other "
                        "formats including SVG"))
    if mode != "publish":
        return
    # Authoring notes, tests and archives frequently sit next to app.py during
    # development.  The official builder prunes dotfiles and build products but
    # not these, so flag them before they ship inside a published payload.
    junk: list[str] = []
    for path in sorted(app_dir.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(app_dir).as_posix()
        if any(part.startswith(".") for part in relative.split("/")):
            continue
        name = path.name
        if any(fnmatch.fnmatch(name, pattern) or fnmatch.fnmatch(relative, pattern)
               for pattern in PUBLISH_JUNK_PATTERNS):
            junk.append(relative)
    if junk:
        issues.append(_issue(
            "warning", "publish_payload_hygiene", "package",
            "these authoring/build files would ship inside the payload: "
            + ", ".join(junk[:12])
            + (" ..." if len(junk) > 12 else "")
            + "; move them out of the App directory or list them in package.exclude"))
    models = manifest.get("models")
    health = manifest.get("health") if isinstance(manifest.get("health"), dict) else {}
    startup_timeout = health.get("startup_timeout_sec")
    if isinstance(models, list) and models:
        if isinstance(startup_timeout, bool) or not isinstance(startup_timeout, (int, float)):
            issues.append(_issue(
                "warning", "missing_startup_timeout", "health.startup_timeout_sec",
                "an app that loads RKNN models should declare a startup timeout of at "
                "least 10 seconds; model load plus NPU reservation can exceed the default"))
        elif startup_timeout < 10:
            issues.append(_issue(
                "warning", "short_startup_timeout", "health.startup_timeout_sec",
                f"{startup_timeout}s is short for an app that loads RKNN models; consider "
                f"at least 10s so a cold NPU reservation is not killed as unhealthy"))


def validate_app(
    app_dir: Path,
    mode: str = "demo",
    sdk_source_commit: str | None = None,
) -> dict[str, Any]:
    """Validate an App directory without accessing a device or network."""
    if mode not in {"demo", "package", "publish"}:
        raise ValidationFailure(f"unknown validation mode: {mode}")
    app_dir = app_dir.resolve()
    if not app_dir.is_dir():
        raise ValidationFailure(f"App directory does not exist: {app_dir}")
    issues: list[dict[str, str]] = []
    manifest = _load_json(app_dir / "manifest.json", "manifest", issues)
    if manifest is not None:
        app_id = manifest.get("id")
        if not isinstance(app_id, str) or APP_ID_PATTERN.fullmatch(app_id) is None:
            issues.append(_issue("error", "invalid_app_id", "manifest.id", "id must match [a-z0-9-]{1,64}"))
        entry = manifest.get("entry")
        if entry is None and mode == "demo":
            entry = "app.py"
            issues.append(_issue("warning", "implicit_demo_entry", "manifest.entry", "demo mode assumes app.py; package and publish modes require an explicit entry"))
        entry_path = _safe_relative_path(entry)
        if entry_path is None:
            issues.append(_issue("error", "invalid_entry", "manifest.entry", "entry must be a safe relative path"))
        elif not app_dir.joinpath(*entry_path.parts).is_file():
            issues.append(_issue("error", "missing_entry", "manifest.entry", f"entry file does not exist: {entry}"))
        entry_is_root = entry_path is not None and len(entry_path.parts) == 1
        if "pipeline" in manifest:
            issues.append(_issue("error", "unsupported_pipeline_field", "manifest.pipeline", "SDK/Kit evidence: the current public runtime has no consumer for manifest.pipeline"))
        _validate_config_schema(manifest, issues)
        bundled_artifacts = _validate_artifacts(app_dir, manifest, entry_is_root, issues)
        model_ids, model_tasks = _validate_models(app_dir, manifest, entry_is_root, issues)
        _validate_resources(manifest, issues)
        _validate_managed_runtime_manifest(manifest, issues)
        _validate_scheduled_rknn_artifacts(manifest, bundled_artifacts, issues)
        _validate_output(manifest, mode, issues)
        if mode in {"package", "publish"}:
            _validate_platform_contract(manifest, issues)
        _validate_stream_osd(manifest, issues)
        trees = _validate_python(app_dir, model_ids, model_tasks, issues)
        _validate_output_paths(manifest, trees, issues)
        _validate_detection_render_contract(manifest, model_tasks, trees, issues)
        _validate_publish_hygiene(app_dir, manifest, mode, issues)
    errors = [issue for issue in issues if issue["severity"] == "error"]
    warnings = [issue for issue in issues if issue["severity"] == "warning"]
    return {
        "schema": 1,
        "mode": mode,
        "app_dir": str(app_dir),
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "platform": {
            "platform_profile": PLATFORM_PROFILE,
            "arch": TARGET_ARCH,
            "python": TARGET_PYTHON_CONSTRAINT,
        },
        "sdk_source_commit": sdk_source_commit,
        "device_connected": False,
        "runtime_preconditions_unverified": list(RUNTIME_PRECONDITIONS),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate a reCamera Pro Python App without a device")
    parser.add_argument("--app-dir", required=True, type=Path)
    parser.add_argument("--mode", choices=("demo", "package", "publish"), default="demo")
    parser.add_argument("--sdk-source-commit")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    try:
        report = validate_app(args.app_dir, args.mode, args.sdk_source_commit)
    except ValidationFailure as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    payload = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(payload, encoding="utf-8")
    print(payload, end="")
    return 0 if report["valid"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
