#!/usr/bin/env python3
"""Build a reCamera Pro App Center v2 package with the SDK's official packager.

Archive layout, deny-lists, the release lock and the BOM are produced by the
upstream ``market/packaging/build.py`` that also ships inside the App Center on
the device, so a package built here is byte-compatible with the installer that
will consume it.  This helper adds the offline pre-flight checks that the
installer would otherwise only report at install time:

* the manifest platform contract (arch / Python / platform profile);
* wheel filename tags and archive admission, matching ``appmgr/pythonenv.py``;
* the dependency closure of a target wheelhouse, with platform-owned projects
  resolved by the device instead of being bundled;
* declared ``python.imports`` actually being importable from that closure;
* package size, member-count and icon caps from ``appmgr/paths.py``;
* a scheduled ``npu.rknn`` app really carrying its bundled ``.rknn`` artifacts.

It never runs pip, never touches the network, never signs an archive and never
connects to a device.
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import importlib.util
import json
import os
import posixpath
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from email.parser import BytesParser
from email.policy import default
from pathlib import Path, PurePosixPath


# ---------------------------------------------------------------------------
# Platform contract.  These values mirror market/appmgr/manifest.py on the
# device and are NOT configurable: the App Center compares them against
# APPMGR_PLATFORM_PROFILE and refuses a package that disagrees.
# ---------------------------------------------------------------------------
PLATFORM_PROFILE = "rv1126b-linux-gnu-cp311-rknn232-v1"
TARGET_ARCH = "aarch64"
TARGET_PYTHON = "3.11"
TARGET_PYTHON_CONSTRAINT = "==3.11.*"
TARGET_PYTHON_TAG = "cp311"
TARGET_ABI_TAG = "cp311"
TARGET_SYS_PLATFORM = "linux"
TARGET_MACHINE = "aarch64"
# Value used by every first-party app in apps/.  manifest.py only requires a
# safe token here, so this label is a declaration, not a device-side switch.
DEFAULT_RUNTIME_PROFILE = "recamera-ai-cp311-v1"

# appmgr/pythonenv.py:_PLATFORM_PROJECTS.  The per-release venv is created with
# --system-site-packages, so these are already importable; an app wheel that
# shadows one is rejected at install time.
PLATFORM_OWNED_PROJECTS = frozenset({
    "cv2", "jinja2", "kit", "markupsafe", "numpy", "recamera-ext",
    "recamera-pro-kit", "rknn-toolkit-lite2", "rknnlite",
})

# appmgr/paths.py and appmgr/pythonenv.py caps.
MAX_PACKAGE_BYTES = 200 * 1024 * 1024
MAX_UNPACKED_BYTES = 400 * 1024 * 1024
MAX_MEMBERS = 4096
MAX_ICON_BYTES = 1024 * 1024
MAX_WHEEL_BYTES = 512 * 1024 * 1024
MAX_WHEEL_MEMBERS = 8192
MAX_ENV_UNPACKED_BYTES = 512 * 1024 * 1024

# Deny-lists mirror market/packaging/build.py so staging and packing agree.
EXCLUDED_DIRS = frozenset({
    "__pycache__", ".git", ".hg", ".svn", "kit", "build", "dist", "target",
    "node_modules", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox",
    ".venv", "venv", ".idea", ".vscode", ".DS_Store", "wheels", "wheelhouse",
})
EXCLUDED_SUFFIXES = (".pyc", ".pyo", ".pyd", ".log", ".swp", ".swo", ".orig")
EXCLUDED_FILES = frozenset({
    "run.pid", ".DS_Store", "release.lock.json", "files.sha256",
    "requirements.lock.json",
})
# Dependency inputs are build-time only: manifest.python.wheels[] is the single
# authoritative declaration the device reads, so a stale requirements file must
# not travel inside the payload.
EXCLUDED_REQUIREMENT_FILES = frozenset({
    "requirements.txt", "requirements.lock", "requirements-py311.txt",
})

SKILL_SDK_SOURCE_REPO = "https://github.com/Seeed-Studio/recamera-pro-ext-api"
SKILL_SDK_SOURCE_COMMIT = "525addec801680f6aabbbb7605d6de3bb390348a"
SKILL_SDK_BUILDER_SHA256 = {
    Path("market/packaging/build.py"):
        "9562dd29f84f7725ab256e17e07e5912591d3603f36bf30292d17bd7fc99cb3f",
    Path("market/appmgr/manifest.py"):
        "fc7bcd975c61e1a8eb04a3c17dfc07b382c84a467d93dca734d43528926894d9",
    Path("market/appmgr/__init__.py"):
        "56cfe9c2fea0f108b3a0490da54ddcb209cac28ed9931cd90b6e0a3c3f46b648",
}
SDK_BUILDER_REQUIRED_FILES = frozenset(SKILL_SDK_BUILDER_SHA256)

# manifest.py:_SEMVER_RE equivalent; the official validator remains the judge.
_SEMVER_RE = re.compile(
    r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-((?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)"
    r"(?:\.(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*))*))?"
    r"(?:\+([0-9a-zA-Z-]+(?:\.[0-9a-zA-Z-]+)*))?"
)
_WHEEL_FILENAME_RE = re.compile(
    r"^(?P<distribution>[^-]+)-(?P<version>[^-]+)-(?P<python>[^-]+)-"
    r"(?P<abi>[^-]+)-(?P<platform>[^-]+)\.whl$"
)
_IMPORT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*")
_REQUIREMENT_RE = re.compile(
    r"^\s*(?P<name>[A-Za-z0-9][A-Za-z0-9_.-]*)"
    r"(?:\[[A-Za-z0-9_.-]+(?:\s*,\s*[A-Za-z0-9_.-]+)*\])?"
    r"\s*(?P<specifier>(?:\([^)]+\)|(?:===|==|!=|<=|>=|<|>|~=)\s*[^,\s;]+"
    r"(?:\s*,\s*(?:===|==|!=|<=|>=|<|>|~=)\s*[^,\s;]+)*)?)"
    r"\s*(?:;\s*(?P<marker>.+))?$"
)
_SPECIFIER_RE = re.compile(r"(===|==|!=|<=|>=|<|>|~=)\s*([^,\s]+)")
_MARKER_RE = re.compile(
    r"^\s*(python_version|platform_machine|sys_platform|implementation_name|extra)\s*"
    r"(==|!=|<=|>=|<|>)\s*(['\"])([^'\"]*)\3\s*$"
)


class PackagingError(ValueError):
    """Raised for invalid or non-reproducible App package inputs."""


def platform_contract() -> dict:
    """Return the fixed device platform contract this packager targets."""
    return {
        "platform_profile": PLATFORM_PROFILE,
        "arch": TARGET_ARCH,
        "python": TARGET_PYTHON_CONSTRAINT,
        "python_version": TARGET_PYTHON,
        "python_tag": TARGET_PYTHON_TAG,
        "abi_tag": TARGET_ABI_TAG,
        "machine": TARGET_MACHINE,
        "sys_platform": TARGET_SYS_PLATFORM,
        "runtime_profile": DEFAULT_RUNTIME_PROFILE,
        "platform_owned_projects": sorted(PLATFORM_OWNED_PROJECTS),
    }


def normalized_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path: Path, label: str) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise PackagingError(f"cannot read {label} {path}: {error}") from error
    if not isinstance(value, dict):
        raise PackagingError(f"{label} must be a JSON object: {path}")
    return value


# ---------------------------------------------------------------------------
# Official SDK builder resolution
# ---------------------------------------------------------------------------
def bundled_sdk_root() -> Path:
    """Return the pinned official SDK builder copy distributed with this Skill."""
    return (Path(__file__).resolve().parent / "sdk-builder").resolve()


def validate_sdk_builder(root: Path, source_label: str) -> Path:
    """Ensure a candidate SDK builder is complete and, when bundled, unmodified."""
    root = root.expanduser().resolve()
    missing = [
        path.as_posix() for path in sorted(SDK_BUILDER_REQUIRED_FILES)
        if not (root / path).is_file()
    ]
    if missing:
        raise PackagingError(
            f"{source_label} SDK builder is incomplete; missing: {', '.join(missing)}"
        )
    if root == bundled_sdk_root():
        mismatches = []
        for relative, expected in SKILL_SDK_BUILDER_SHA256.items():
            actual = sha256_file(root / relative)
            if actual != expected:
                mismatches.append(f"{relative.as_posix()} sha256 {actual}, expected {expected}")
        if mismatches:
            raise PackagingError(
                "bundled SDK builder does not match the pinned official source "
                f"({SKILL_SDK_SOURCE_REPO} @ {SKILL_SDK_SOURCE_COMMIT[:12]}): "
                + "; ".join(mismatches)
            )
    return root


def find_sdk_root(explicit: Path | None) -> Path:
    """Resolve only explicit overrides or the Skill's pinned official builder.

    Implicit checkout discovery from an App parent, cwd or home directory is
    deliberately not performed: it makes builds depend on unrelated local state
    and can silently swap in a builder whose contract differs from the device.
    """
    if explicit is not None:
        return validate_sdk_builder(explicit, "explicit --sdk-root")
    for variable in ("RECAMERA_SDK_ROOT", "RECAMERA_EXT_API_ROOT"):
        value = os.environ.get(variable)
        if value:
            return validate_sdk_builder(Path(value), f"{variable} override")
    return validate_sdk_builder(bundled_sdk_root(), "bundled")


def load_sdk_contract(sdk_root: Path):
    """Load the exact manifest helper paired with the selected official builder."""
    path = sdk_root / "market" / "appmgr" / "manifest.py"
    spec = importlib.util.spec_from_file_location("recamera_skill_manifest_contract", path)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Wheels
# ---------------------------------------------------------------------------
def prepare_wheelhouse(path: Path | None, out_dir: Path) -> tuple[Path, bool]:
    """Use an explicit wheelhouse or create an empty local one."""
    if path is not None:
        resolved = path.expanduser().resolve()
        if not resolved.is_dir():
            raise PackagingError(f"wheelhouse does not exist: {resolved}")
        return resolved, False
    generated = out_dir / ".recamera-empty-wheelhouse"
    generated.mkdir(parents=True, exist_ok=True)
    return generated.resolve(), True


def validate_wheel_filename_tag(wheel: dict) -> None:
    """Apply manifest.py:_validate_wheel's tag rule exactly.

    Portable wheels must be ``py3-none-any`` or ``cp311-none-any``.  AArch64
    wheels must carry a platform tag ending in ``aarch64`` combined with either
    ``py3-none`` or ``cp311-(cp311|abi3)``.  Note that ``py3-none-*aarch64`` is
    valid: the ABI is none, only the platform is architecture specific.
    """
    tag = wheel["filename_tag"]
    parts = tag.split("-")
    if len(parts) < 3:
        raise PackagingError(f"{wheel['path'].name}: invalid wheel tag {tag!r}")
    python_tag, abi_tag = parts[0], parts[1]
    platform_tag = "-".join(parts[2:])
    if platform_tag == "any":
        if python_tag not in ("py3", TARGET_PYTHON_TAG) or abi_tag != "none":
            raise PackagingError(
                f"{wheel['path'].name}: portable wheels must use py3-none-any "
                f"or {TARGET_PYTHON_TAG}-none-any, got {tag!r}"
            )
        return
    if not platform_tag.endswith(TARGET_ARCH) or not (
            (python_tag == "py3" and abi_tag == "none")
            or (python_tag == TARGET_PYTHON_TAG and abi_tag in ("abi3", TARGET_ABI_TAG))):
        raise PackagingError(
            f"{wheel['path'].name}: tag {tag!r} is not installable on the device; "
            f"AArch64 wheels must use py3-none or "
            f"{TARGET_PYTHON_TAG}-({TARGET_ABI_TAG}|abi3) with a platform tag "
            f"ending in {TARGET_ARCH}"
        )


def _wheel_top_level_modules(archive: zipfile.ZipFile) -> set[str]:
    """Collect import roots a wheel provides, ignoring dist-info metadata."""
    modules: set[str] = set()
    for name in archive.namelist():
        parts = PurePosixPath(name).parts
        if not parts or parts[0].endswith((".dist-info", ".data")):
            continue
        if len(parts) == 1:
            stem = parts[0]
            if stem.endswith(".py"):
                modules.add(stem[:-3])
            elif "." in stem:
                modules.add(stem.split(".", 1)[0])
        elif parts[1] == "__init__.py":
            modules.add(parts[0])
    return {module for module in modules if module.isidentifier()}


def check_wheel_archive(wheel: dict, running_total: list[int]) -> None:
    """Mirror appmgr/pythonenv.py wheel admission so install cannot fail later.

    ``running_total`` accumulates unpacked bytes across the whole closure and is
    compared against the device's per-release environment cap.
    """
    path = wheel["path"]
    if wheel["size"] > MAX_WHEEL_BYTES:
        raise PackagingError(
            f"{path.name}: wheel is {wheel['size']} bytes, over the {MAX_WHEEL_BYTES} cap")
    try:
        archive = zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile) as error:
        raise PackagingError(f"invalid wheel archive {path.name}: {error}") from error
    with archive:
        infos = archive.infolist()
        if len(infos) > MAX_WHEEL_MEMBERS:
            raise PackagingError(
                f"{path.name}: too many members: {len(infos)} > {MAX_WHEEL_MEMBERS}")
        seen: set[str] = set()
        metadata_names: list[str] = []
        wheel_names: list[str] = []
        record_names: list[str] = []
        for info in infos:
            name = info.filename.rstrip("/")
            member = PurePosixPath(name)
            if (not name or name.startswith(("/", "\\")) or "\\" in name
                    or any(part in ("", ".", "..") for part in member.parts)):
                raise PackagingError(f"{path.name}: unsafe wheel member {info.filename!r}")
            if any(part.endswith(".data") for part in member.parts):
                raise PackagingError(
                    f"{path.name}: wheel .data layout is unsupported for offline "
                    f"expansion: {info.filename}")
            if name in seen:
                raise PackagingError(f"{path.name}: duplicate wheel member {name}")
            seen.add(name)
            if stat.S_ISLNK((info.external_attr >> 16) & 0o170000):
                raise PackagingError(f"{path.name}: wheel symlink is forbidden: {name}")
            if info.flag_bits & 0x1:
                raise PackagingError(f"{path.name}: encrypted wheel member is forbidden: {name}")
            if info.is_dir():
                continue
            running_total[0] += max(0, info.file_size)
            if running_total[0] > MAX_ENV_UNPACKED_BYTES:
                raise PackagingError(
                    f"bundled wheels exceed the device per-release environment "
                    f"unpacked cap of {MAX_ENV_UNPACKED_BYTES} bytes")
            if name.endswith(".dist-info/METADATA"):
                metadata_names.append(name)
            elif name.endswith(".dist-info/WHEEL"):
                wheel_names.append(name)
            elif name.endswith(".dist-info/RECORD"):
                record_names.append(name)
            with archive.open(info, "r") as source:
                header = source.read(20)
            if header.startswith(b"\x7fELF"):
                elf_class = header[4] if len(header) > 4 else 0
                byte_order = header[5] if len(header) > 5 else 0
                machine = int.from_bytes(header[18:20], "little") if len(header) >= 20 else -1
                if elf_class != 2 or byte_order != 1 or machine != 183:
                    raise PackagingError(
                        f"{path.name}: contains non-AArch64 ELF payload: {name}. "
                        f"Rebuild the wheel for linux_aarch64 / cp311.")
        if len(metadata_names) != 1 or len(wheel_names) != 1 or len(record_names) != 1:
            raise PackagingError(
                f"{path.name}: must contain exactly one METADATA, WHEEL and RECORD")
        dist_infos = {name.rsplit("/", 1)[0] for name in metadata_names + wheel_names + record_names}
        if len(dist_infos) != 1:
            raise PackagingError(f"{path.name}: has split .dist-info metadata")
        wheel["top_level"] = _wheel_top_level_modules(archive)


def wheel_metadata(path: Path) -> dict:
    """Read one wheel's identity, tags and dependency edges."""
    match = _WHEEL_FILENAME_RE.fullmatch(path.name)
    if match is None:
        raise PackagingError(
            f"{path.name}: must use distribution-version-python-abi-platform.whl")
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            metadata_names = [name for name in names if name.endswith(".dist-info/METADATA")]
            wheel_names = [name for name in names if name.endswith(".dist-info/WHEEL")]
            if len(metadata_names) != 1 or len(wheel_names) != 1:
                raise PackagingError(
                    f"{path.name}: exactly one dist-info METADATA and WHEEL are required")
            metadata = BytesParser(policy=default).parsebytes(archive.read(metadata_names[0]))
            name, version = metadata.get("Name"), metadata.get("Version")
            if not name or not version:
                raise PackagingError(f"{path.name}: METADATA must contain Name and Version")
            tags = [
                line[4:].strip()
                for line in archive.read(wheel_names[0]).decode("utf-8").splitlines()
                if line.startswith("Tag:")
            ]
            requires = metadata.get_all("Requires-Dist", []) or []
    except zipfile.BadZipFile as error:
        raise PackagingError(f"invalid wheel archive {path}: {error}") from error
    if not tags:
        raise PackagingError(f"{path.name}: WHEEL has no Tag entries")
    filename_tag = "-".join(match.group(key) for key in ("python", "abi", "platform"))
    distribution = normalized_name(name)
    if normalized_name(match.group("distribution")) != distribution:
        raise PackagingError(
            f"{path.name}: filename distribution does not match METADATA Name {name!r}")
    if match.group("version") != version:
        raise PackagingError(
            f"{path.name}: filename version does not match METADATA Version {version!r}")
    if tags != [filename_tag]:
        raise PackagingError(
            f"{path.name}: WHEEL tags {tags} must exactly declare the filename tag "
            f"{filename_tag!r}")
    wheel = {
        "distribution": distribution,
        "version": version,
        "tags": tags,
        "filename_tag": filename_tag,
        "requires_dist": list(requires),
        "path": path,
        "sha256": sha256_file(path),
        "size": path.stat().st_size,
    }
    validate_wheel_filename_tag(wheel)
    return wheel


# ---------------------------------------------------------------------------
# Requirements and dependency closure
# ---------------------------------------------------------------------------
def parse_requirements(path: Path | None) -> dict[str, str] | None:
    """Parse an exact-pinned requirement file into {normalised name: specifier}."""
    if path is None:
        return None
    if path.suffix.lower() == ".json":
        data = load_json(path, "requirements")
        items = data.get("packages", data)
        if isinstance(items, dict):
            items = [{"distribution": name, "version": version} for name, version in items.items()]
        if not isinstance(items, list):
            raise PackagingError(
                "requirements JSON must contain a packages list or name/version object")
        parsed = {}
        for item in items:
            if (not isinstance(item, dict) or not isinstance(item.get("distribution"), str)
                    or not isinstance(item.get("version"), str)):
                raise PackagingError(
                    "requirements JSON entries need distribution and version strings")
            parsed[normalized_name(item["distribution"])] = "==" + item["version"]
        return parsed
    parsed = {}
    pattern = re.compile(r"^\s*([A-Za-z0-9_.-]+)\s*==\s*([^\s;#]+)\s*$")
    for line_number, original in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = original.split("#", 1)[0].strip()
        if not line:
            continue
        match = pattern.fullmatch(line)
        if not match:
            raise PackagingError(
                f"{path}:{line_number}: use exact name==version requirements only; the "
                f"device installs offline and cannot resolve ranges")
        parsed[normalized_name(match.group(1))] = "==" + match.group(2)
    return parsed


def discover_requirements(app_dir: Path, explicit: Path | None) -> Path | None:
    """Use an exact dependency file from the App when one is present."""
    if explicit is not None:
        return explicit.expanduser().resolve()
    for filename in ("requirements.lock", "requirements.txt", "requirements-py311.txt"):
        candidate = app_dir / filename
        if candidate.is_file():
            return candidate.resolve()
    return None


def _version_key(value: str) -> tuple:
    """Compare the numeric versions accepted by the offline closure checker."""
    if not re.fullmatch(r"\d+(?:\.\d+)*(?:[._-](?:a|b|rc|post|dev)\d+)?", value.lower()):
        raise PackagingError(f"unsupported non-numeric Python package version {value!r}")
    parts = re.findall(r"\d+|[a-z]+", value.lower())
    return tuple((0, int(part)) if part.isdigit() else (1, part) for part in parts)


def _version_satisfies(version: str, specifier: str, location: str) -> bool:
    if not specifier:
        return True
    text = specifier.strip()
    if text.startswith("(") and text.endswith(")"):
        text = text[1:-1].strip()
    constraints = _SPECIFIER_RE.findall(text)
    joined = ",".join(f"{op}{value}" for op, value in constraints).replace(" ", "")
    if not constraints or joined != text.replace(" ", ""):
        raise PackagingError(f"{location}: unsupported version constraint {specifier!r}")
    actual = _version_key(version)
    for operator, required in constraints:
        if operator == "==" and required.endswith(".*"):
            prefix = _version_key(required[:-2])
            if actual[:len(prefix)] != prefix:
                return False
            continue
        expected = _version_key(required)
        if operator in {"==", "==="} and actual != expected:
            return False
        if operator == "!=" and actual == expected:
            return False
        if operator == ">=" and actual < expected:
            return False
        if operator == ">" and actual <= expected:
            return False
        if operator == "<" and actual >= expected:
            return False
        if operator == "<=" and actual > expected:
            return False
        if operator == "~=":
            if actual < expected:
                return False
            numeric = [int(part) for part in re.findall(r"\d+", required)]
            upper = numeric[:1] if len(numeric) == 1 else numeric[:-1]
            upper[-1] += 1
            if actual >= _version_key(".".join(map(str, upper))):
                return False
    return True


def _marker_atom(value: str, location: str) -> bool:
    value = value.strip()
    while value.startswith("(") and value.endswith(")"):
        value = value[1:-1].strip()
    match = _MARKER_RE.fullmatch(value)
    if match is None:
        raise PackagingError(f"{location}: unsupported environment marker {value!r}")
    key, operator, _, expected = match.groups()
    actual = {
        "python_version": TARGET_PYTHON,
        "platform_machine": TARGET_MACHINE,
        "sys_platform": TARGET_SYS_PLATFORM,
        "implementation_name": "cpython",
        "extra": "",
    }[key]
    return {
        "==": actual == expected, "!=": actual != expected,
        ">=": actual >= expected, ">": actual > expected,
        "<=": actual <= expected, "<": actual < expected,
    }[operator]


def _requirement(requirement: str, owner: str) -> tuple[str, str, bool]:
    match = _REQUIREMENT_RE.fullmatch(requirement)
    if match is None:
        raise PackagingError(f"{owner}: unsupported Requires-Dist entry {requirement!r}")
    name = normalized_name(match.group("name"))
    specifier = match.group("specifier") or ""
    marker = match.group("marker")
    if not marker:
        active = True
    else:
        # Optional extras are not selected by this packager.  Compound markers
        # are evaluated only when every atom is one of the supported forms.
        active = False
        for alternative in [part.strip() for part in re.split(r"\s+or\s+", marker)]:
            atoms = [part.strip() for part in re.split(r"\s+and\s+", alternative)]
            if all(_marker_atom(atom, owner) for atom in atoms):
                active = True
                break
    return name, specifier, active


def select_wheels(
    wheelhouse: Path,
    requirements: dict[str, str] | None,
) -> tuple[list[dict], list[dict], list[dict], list[str]]:
    """Resolve the offline install closure from a flat target wheelhouse.

    Returns ``(bundled, platform_resolved, dependency_edges, warnings)``.  A
    dependency on a platform-owned project is satisfied by the device's system
    site-packages and must not be bundled; the SDK does not publish those
    versions, so no version constraint can be checked for them.
    """
    if not wheelhouse.is_dir():
        raise PackagingError(f"wheelhouse does not exist: {wheelhouse}")
    warnings: list[str] = []
    wheels: list[dict] = []
    running_total = [0]
    for path in sorted(wheelhouse.iterdir(), key=lambda item: item.name):
        if path.is_symlink():
            raise PackagingError(f"wheelhouse contains a symlink: {path.name}")
        if path.is_dir():
            raise PackagingError(
                f"wheelhouse must be a flat directory; nested directory found: {path.name}")
        if not path.is_file():
            raise PackagingError(f"wheelhouse contains unsupported non-file: {path.name}")
        if path.suffix != ".whl":
            raise PackagingError(f"wheelhouse contains unsupported non-wheel file: {path.name}")
        wheel = wheel_metadata(path)
        check_wheel_archive(wheel, running_total)
        wheels.append(wheel)
    by_name: dict[str, dict] = {}
    for wheel in wheels:
        if wheel["distribution"] in by_name:
            raise PackagingError(
                f"wheelhouse has multiple wheels for {wheel['distribution']}; keep exactly "
                f"one version per project so the closure is reproducible")
        by_name[wheel["distribution"]] = wheel
    shadowed = sorted(set(by_name) & PLATFORM_OWNED_PROJECTS)
    if shadowed:
        raise PackagingError(
            "wheelhouse must not contain platform-owned projects already provided by the "
            "device runtime: " + ", ".join(shadowed))

    roots = set(requirements) if requirements is not None else set(by_name)
    if requirements is None and by_name:
        warnings.append(
            "no requirements file was found; every wheel in the wheelhouse is treated as a "
            "root and bundled. Provide requirements.txt with exact name==version pins to "
            "make the closure explicit.")
    selected: set[str] = set()
    constraints: dict[str, list[tuple[str, str]]] = {}
    platform_resolved: list[dict] = []
    dependency_edges: list[dict] = []
    pending = [
        (name, requirements.get(name, "") if requirements else "", "root requirement")
        for name in sorted(roots)
    ]
    while pending:
        name, specifier, owner = pending.pop(0)
        constraints.setdefault(name, []).append((specifier, owner))
        if name in PLATFORM_OWNED_PROJECTS:
            for constraint, source in constraints[name]:
                if constraint:
                    warnings.append(
                        f"{source} requires {name}{constraint}; {name} is provided by the "
                        f"device runtime and its version is not declared, so the "
                        f"constraint could not be verified offline")
            if not any(item["name"] == name for item in platform_resolved):
                platform_resolved.append(
                    {"name": name, "version": None, "source": "platform"})
            selected.add(name)
            continue
        wheel = by_name.get(name)
        if wheel is None:
            raise PackagingError(
                f"missing wheel for dependency {name}; the device installs offline, so add "
                f"a cp311/aarch64 wheel for it to the wheelhouse")
        for constraint, source in constraints[name]:
            if not _version_satisfies(wheel["version"], constraint, f"{source} -> {name}"):
                raise PackagingError(
                    f"{source} requires {name}{constraint}, but wheel is {wheel['version']}")
        if name in selected:
            continue
        selected.add(name)
        for requirement in wheel["requires_dist"]:
            dependency_name, dependency_specifier, active = _requirement(
                requirement, wheel["path"].name)
            if active:
                dependency_edges.append({
                    "from": wheel["distribution"],
                    "to": dependency_name,
                    "requirement": requirement,
                })
                pending.append((dependency_name, dependency_specifier, wheel["path"].name))
    bundled = [by_name[name] for name in sorted(selected) if name in by_name]
    return (
        bundled,
        sorted(platform_resolved, key=lambda item: item["name"]),
        sorted(dependency_edges, key=lambda item: (item["from"], item["to"], item["requirement"])),
        warnings,
    )


def validate_imports(
    imports: list,
    bundled: list[dict],
    payload_modules: set[str],
) -> None:
    """Fail when a declared import cannot be satisfied on the device.

    The installer creates the per-release venv and probes every declared import
    with ``python -I``; an unsatisfied import aborts installation.  Checking it
    here turns that into a build-time error with an actionable message.
    """
    provided: set[str] = set(payload_modules)
    for wheel in bundled:
        provided |= wheel.get("top_level") or set()
    provided |= PLATFORM_OWNED_PROJECTS
    stdlib = set(getattr(sys, "stdlib_module_names", frozenset()))
    for module in imports:
        root = str(module).split(".", 1)[0]
        if root in provided or root in stdlib:
            continue
        raise PackagingError(
            f"python.imports declares {module!r}, but no bundled wheel, app module or "
            f"platform-owned project provides {root!r}. Add the matching aarch64/cp311 "
            f"wheel to the wheelhouse, or remove the import declaration.")


# ---------------------------------------------------------------------------
# Payload staging
# ---------------------------------------------------------------------------
def _match_any(name: str, relative: str, patterns: tuple[str, ...]) -> bool:
    return any(
        fnmatch.fnmatch(name, pattern) or fnmatch.fnmatch(relative, pattern)
        for pattern in patterns
    )


def copy_app(app_dir: Path, staging: Path) -> tuple[dict, list[str], set[str]]:
    """Stage the payload and return ``(manifest, payload_files, modules)``."""
    manifest = load_json(app_dir / "manifest.json", "manifest")
    if manifest.get("manifest_version") != 2:
        raise PackagingError(
            "App manifest_version must be 2 for the current App Center contract")
    app_id, version = manifest.get("id"), manifest.get("version")
    if not isinstance(app_id, str) or not re.fullmatch(r"[a-z0-9-]{1,64}", app_id):
        raise PackagingError("manifest.id must match [a-z0-9-]{1,64}")
    if not isinstance(version, str) or not _SEMVER_RE.fullmatch(version):
        raise PackagingError(
            f"manifest.version {version!r} is not SemVer; manifest v2 requires SemVer")
    package = manifest.get("package")
    excludes = tuple(package.get("exclude") or ()) if isinstance(package, dict) else ()

    payload_files: list[str] = []
    modules: set[str] = set()
    for root, dirs, files in os.walk(app_dir):
        root_path = Path(root)
        relative_root = root_path.relative_to(app_dir).as_posix()
        dirs[:] = sorted(
            directory for directory in dirs
            if directory not in EXCLUDED_DIRS
            and not directory.startswith(".")
            and not _match_any(directory, directory if relative_root == "."
                               else f"{relative_root}/{directory}", excludes)
        )
        for filename in sorted(files):
            source = root_path / filename
            relative = source.relative_to(app_dir)
            relative_posix = relative.as_posix()
            if (filename in EXCLUDED_FILES or filename in EXCLUDED_REQUIREMENT_FILES
                    or filename.startswith(".")
                    or filename.endswith(EXCLUDED_SUFFIXES)
                    or _match_any(filename, relative_posix, excludes)):
                continue
            if source.is_symlink():
                raise PackagingError(f"App source symlink is not supported: {relative_posix}")
            destination = staging / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
            payload_files.append(relative_posix)
            if filename.endswith(".py") and relative.parent == Path("."):
                modules.add(filename[:-3])
            elif filename == "__init__.py" and relative.parent != Path("."):
                modules.add(relative.parent.parts[0])
    if "manifest.json" not in payload_files:
        raise PackagingError("staged payload is missing manifest.json")
    return manifest, sorted(payload_files), modules


def validate_icon(app_dir: Path, manifest: dict, contract) -> None:
    """Check a declared icon exists, is raster and is within the device cap."""
    icon = manifest.get("icon")
    if icon is None:
        return
    if not isinstance(icon, dict):
        raise PackagingError("manifest.icon must be an object with path and media_type")
    relative = icon.get("path")
    if not isinstance(relative, str) or not relative:
        raise PackagingError("manifest.icon.path must be a non-empty package-relative path")
    source = app_dir / relative
    if not source.is_file():
        raise PackagingError(
            f"manifest.icon.path {relative!r} is not present in the App directory")
    size = source.stat().st_size
    if size > MAX_ICON_BYTES:
        raise PackagingError(
            f"icon {relative} is {size} bytes, over the {MAX_ICON_BYTES}-byte device cap")
    data = source.read_bytes()[:16]
    matcher = getattr(contract, "icon_bytes_match_media_type", None) if contract else None
    media_type = icon.get("media_type")
    if callable(matcher):
        if not matcher(data, media_type):
            raise PackagingError(
                f"icon {relative} bytes do not match declared media_type {media_type!r}; "
                f"the installer refuses non-raster icons such as SVG")
    elif data.startswith(b"<") or b"<svg" in data:
        raise PackagingError(f"icon {relative} looks like SVG; the installer refuses it")


def wheel_descriptor(wheel: dict) -> dict:
    filename = wheel["path"].name
    return {
        "name": wheel["distribution"],
        "version": wheel["version"],
        "filename": filename,
        "file": f"wheels/{filename}",
        "sha256": wheel["sha256"],
        "size": wheel["size"],
        "tags": [wheel["filename_tag"]],
        "source": "bundled",
    }


def update_manifest(
    staging: Path,
    manifest: dict,
    wheels: list[dict],
    payload_modules: set[str],
) -> dict:
    """Fill the platform-owned manifest fields and re-validate the result."""
    result = dict(manifest)
    compatibility = dict(result.get("compatibility") or {})
    expected = {
        "platform_profile": PLATFORM_PROFILE,
        "arch": TARGET_ARCH,
        "python": TARGET_PYTHON_CONSTRAINT,
    }
    for key, value in expected.items():
        if key in compatibility and compatibility[key] != value:
            raise PackagingError(
                f"manifest.compatibility.{key}={compatibility[key]!r} does not match the "
                f"device platform contract {value!r}; the App Center compares this against "
                f"APPMGR_PLATFORM_PROFILE and refuses a mismatch")
        compatibility[key] = value
    result["compatibility"] = compatibility

    python = dict(result.get("python") or {})
    declared_profile = python.get("runtime_profile")
    if declared_profile in (None, ""):
        python["runtime_profile"] = DEFAULT_RUNTIME_PROFILE
    elif not isinstance(declared_profile, str):
        raise PackagingError("manifest.python.runtime_profile must be a string token")
    elif declared_profile == PLATFORM_PROFILE:
        raise PackagingError(
            "manifest.python.runtime_profile must not reuse compatibility.platform_profile; "
            f"first-party apps declare a runtime label such as {DEFAULT_RUNTIME_PROFILE!r}")
    python["isolation"] = "per-release"
    python["wheels"] = [wheel_descriptor(wheel) for wheel in wheels]
    imports = python.get("imports")
    if imports is None:
        imports = []
    if not isinstance(imports, list) or not all(isinstance(item, str) for item in imports):
        raise PackagingError("manifest.python.imports must be a list of module names")
    if len(set(imports)) != len(imports):
        raise PackagingError("manifest.python.imports must not contain duplicates")
    for module in imports:
        if not _IMPORT_RE.fullmatch(module):
            raise PackagingError(f"manifest.python.imports entry {module!r} is not a module name")
    validate_imports(imports, wheels, payload_modules)
    if wheels and not imports:
        # Not fatal: an app may import bundled wheels lazily by full path, but
        # the installer's import probe then covers nothing.
        pass
    python["imports"] = imports
    result["python"] = python
    (staging / "manifest.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return result


def run_validator(app_dir: Path, mode: str) -> dict:
    """Delegate the authoritative contract verdict to validate_app.py."""
    validator_path = Path(__file__).with_name("validate_app.py")
    command = [sys.executable, str(validator_path), "--app-dir", str(app_dir), "--mode", mode]
    completed = subprocess.run(command, text=True, capture_output=True, check=False)
    if completed.returncode:
        raise PackagingError(f"App contract validation failed:\n{completed.stdout}{completed.stderr}")
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise PackagingError(f"validator produced no JSON report: {error}") from error


def _verify_managed_result_claim(manifest: dict) -> None:
    """Fail closed if a managed App cannot receive AppMgr's result gateway."""
    models = manifest.get("models")
    capabilities = manifest.get("capabilities")
    managed = isinstance(models, list) and bool(models)
    managed = managed or (isinstance(capabilities, list) and "output" in capabilities)
    if not managed:
        return
    resources = manifest.get("resources")
    claims = resources.get("claims") if isinstance(resources, dict) else None
    publish_claims = [
        claim for claim in claims or []
        if isinstance(claim, dict) and claim.get("name") == "result.publish"
    ] if isinstance(claims, list) else []
    if len(publish_claims) != 1:
        raise PackagingError(
            "managed model/output Apps must declare exactly one result.publish claim "
            "with mode=brokered")
    if publish_claims[0].get("mode") != "brokered":
        raise PackagingError(
            "Kit model/output Apps must use result.publish: brokered; shared selects "
            "result.ingress and does not receive AppMgr's result gateway environment, "
            "causing fallback to a child-owned port 8124")


# ---------------------------------------------------------------------------
# Final archive verification
# ---------------------------------------------------------------------------
def _archive_records(package: tarfile.TarFile) -> dict[str, dict[str, int | str]]:
    """Read regular payload members and calculate the records used by the v2 BOM."""
    records: dict[str, dict[str, int | str]] = {}
    for member in package.getmembers():
        if "\\" in member.name:
            raise PackagingError(
                f"archive member name contains a backslash: {member.name!r}; the official "
                "builder emitted a Windows-style path that will not extract to the intended "
                "location on the device. Build the package on a POSIX environment "
                "(Linux/WSL/macOS), or keep every manifest-declared resource at the package "
                "root so its archive name contains no directory separator.")
        name = member.name
        if not name or name == "." or member.isdir() or name.endswith("/"):
            if member.issym() or member.islnk():
                raise PackagingError(f"archive contains a link member: {name!r}")
            continue
        normalized = posixpath.normpath(name)
        if normalized != name or name.startswith("/") or normalized == ".." or normalized.startswith("../"):
            raise PackagingError(f"archive contains an unsafe member path: {name!r}")
        if name in {"release.lock.json", "files.sha256"}:
            if not member.isfile():
                raise PackagingError(f"archive metadata member is not a regular file: {name}")
            continue
        if not member.isfile():
            raise PackagingError(f"archive member must be a regular file: {name}")
        if name in records:
            raise PackagingError(f"archive contains duplicate member: {name}")
        source = package.extractfile(member)
        if source is None:
            raise PackagingError(f"archive member cannot be read: {name}")
        digest = hashlib.sha256()
        size = 0
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
        records[name] = {"sha256": digest.hexdigest(), "size": size}
    return records


def _parse_archive_bom(data: bytes) -> dict[str, dict[str, int | str]]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise PackagingError(f"files.sha256 is not UTF-8: {error}") from error
    records: dict[str, dict[str, int | str]] = {}
    for line_number, line in enumerate(text.splitlines(), 1):
        parts = line.split("  ", 2)
        if len(parts) != 3:
            raise PackagingError(f"files.sha256:{line_number}: expected '<sha256>  <size>  <path>'")
        digest, size_text, path = parts
        if re.fullmatch(r"[0-9a-fA-F]{64}", digest) is None:
            raise PackagingError(f"files.sha256:{line_number}: invalid sha256")
        try:
            size = int(size_text, 10)
        except ValueError as error:
            raise PackagingError(f"files.sha256:{line_number}: invalid size") from error
        if size < 0 or not path or posixpath.normpath(path) != path or path.startswith("/"):
            raise PackagingError(f"files.sha256:{line_number}: unsafe or invalid path")
        if path in records or path in {"release.lock.json", "files.sha256"}:
            raise PackagingError(f"files.sha256:{line_number}: duplicate or metadata path")
        records[path] = {"sha256": digest.lower(), "size": size}
    if not records:
        raise PackagingError("files.sha256 must contain at least one payload file")
    return records


def _verify_final_archive(archive: Path, expected_manifest: dict, sdk_root: Path) -> dict:
    """Fail closed on the bytes that will actually be installed on the device."""
    package_bytes = archive.stat().st_size
    if package_bytes > MAX_PACKAGE_BYTES:
        raise PackagingError(
            f"package is {package_bytes} bytes, over the device install cap of "
            f"{MAX_PACKAGE_BYTES} bytes; reduce bundled models or wheels")
    try:
        with tarfile.open(archive, mode="r:gz") as package:
            members = package.getmembers()
            if len(members) > MAX_MEMBERS:
                raise PackagingError(
                    f"package has {len(members)} members, over the device cap of {MAX_MEMBERS}")
            names = {member.name.replace("\\", "/") for member in members}
            required = {"manifest.json", "release.lock.json", "files.sha256"}
            if not required.issubset(names):
                raise PackagingError(
                    "archive is missing SDK-generated members: "
                    + ", ".join(sorted(required - names)))
            manifest_data = package.extractfile(package.getmember("manifest.json"))
            lock_data = package.extractfile(package.getmember("release.lock.json"))
            bom_data = package.extractfile(package.getmember("files.sha256"))
            if manifest_data is None or lock_data is None or bom_data is None:
                raise PackagingError("archive metadata cannot be read")
            try:
                manifest = json.loads(manifest_data.read().decode("utf-8"))
                release_lock = json.loads(lock_data.read().decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise PackagingError(f"archive metadata is invalid JSON: {error}") from error
            if not isinstance(manifest, dict) or not isinstance(release_lock, dict):
                raise PackagingError("archive manifest and release lock must be JSON objects")
            if (manifest.get("id") != expected_manifest.get("id")
                    or manifest.get("version") != expected_manifest.get("version")):
                raise PackagingError(
                    "archive manifest identity does not match the requested App: expected "
                    f"{expected_manifest.get('id')}@{expected_manifest.get('version')}, got "
                    f"{manifest.get('id')}@{manifest.get('version')}")
            records = _archive_records(package)
            bom_bytes = bom_data.read()
            declared_records = _parse_archive_bom(bom_bytes)
            if declared_records != records:
                raise PackagingError("archive files.sha256 does not match the archive payload")
            unpacked = sum(int(record["size"]) for record in records.values())
            if unpacked > MAX_UNPACKED_BYTES:
                raise PackagingError(
                    f"package unpacks to {unpacked} bytes, over the device cap of "
                    f"{MAX_UNPACKED_BYTES} bytes")

            contract = load_sdk_contract(sdk_root)
            verify_release_metadata = getattr(contract, "verify_release_metadata", None)
            if callable(verify_release_metadata):
                try:
                    verify_release_metadata(manifest, release_lock, bom_bytes, records)
                except Exception as error:
                    raise PackagingError(
                        f"archive release metadata verification failed: {error}") from error
            else:
                # Test doubles and legacy local checkouts may not expose the
                # helper.  Keep the basic identity/digest checks fail-closed.
                if release_lock.get("app") != {
                    "id": manifest.get("id"), "version": manifest.get("version")
                }:
                    raise PackagingError(
                        "archive release.lock.json app identity does not match manifest")
                if release_lock.get("manifest_sha256") != records["manifest.json"]["sha256"]:
                    raise PackagingError(
                        "archive release.lock.json manifest digest does not match manifest.json")
                if release_lock.get("bom", {}).get("sha256") != hashlib.sha256(bom_bytes).hexdigest():
                    raise PackagingError(
                        "archive release.lock.json BOM digest does not match files.sha256")

            icon = manifest.get("icon")
            if isinstance(icon, dict) and isinstance(icon.get("path"), str):
                icon_record = records.get(icon["path"])
                if icon_record is None:
                    raise PackagingError(
                        f"declared icon is missing from the archive payload: {icon['path']}")
                if int(icon_record["size"]) > MAX_ICON_BYTES:
                    raise PackagingError(
                        f"archived icon {icon['path']} exceeds the {MAX_ICON_BYTES}-byte cap")

            resources = manifest.get("resources")
            claims = resources.get("claims") if isinstance(resources, dict) else None
            scheduled_npu = any(
                isinstance(claim, dict)
                and claim.get("name") == "npu.rknn"
                and claim.get("mode") in {"scheduled", "brokered"}
                for claim in claims
            ) if isinstance(claims, list) else False
            artifact_summary: list[dict] = []
            artifacts = manifest.get("artifacts", [])
            if not isinstance(artifacts, list):
                raise PackagingError("archive manifest.artifacts must be an array")
            bundled_rknn: dict[str, dict] = {}
            for artifact in artifacts:
                if not isinstance(artifact, dict) or artifact.get("source") != "bundled":
                    continue
                path = artifact.get("file")
                if not isinstance(path, str) or path != artifact.get("mount"):
                    raise PackagingError("archive bundled artifact file must equal mount")
                record = records.get(path)
                if record is None:
                    raise PackagingError(f"archive bundled artifact is missing from payload: {path}")
                if (record["sha256"] != str(artifact.get("sha256", "")).lower()
                        or record["size"] != artifact.get("size")):
                    raise PackagingError(f"archive bundled artifact digest/size mismatch: {path}")
                artifact_summary.append({
                    "id": artifact.get("id"), "kind": artifact.get("kind"),
                    "source": "bundled", "file": path,
                    "sha256": record["sha256"], "size": record["size"],
                })
                if artifact.get("kind") == "rknn":
                    bundled_rknn[path] = artifact
            if scheduled_npu:
                models = manifest.get("models")
                if not isinstance(models, list) or not models:
                    raise PackagingError(
                        "final archive declares a scheduled npu.rknn claim but has no models")
                if not bundled_rknn:
                    raise PackagingError(
                        "final archive scheduled application has no bundled RKNN artifact; "
                        "inference authorization will fail on the device")
                for model in models:
                    model_file = model.get("file") if isinstance(model, dict) else None
                    if not isinstance(model_file, str) or model_file not in bundled_rknn:
                        raise PackagingError(
                            "final archive scheduled application is missing a bundled RKNN "
                            f"artifact for model file: {model_file!r}")
            _verify_managed_result_claim(manifest)
            return {
                "manifest": manifest,
                "records": records,
                "artifacts": artifact_summary,
                "package_bytes": package_bytes,
                "unpacked_bytes": unpacked,
                "member_count": len(members),
            }
    except (OSError, tarfile.TarError) as error:
        raise PackagingError(
            f"SDK packager produced an unreadable tar.gz: {archive}: {error}") from error


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------
def build(
    app_dir: Path,
    out_dir: Path,
    wheelhouse: Path | None = None,
    requirements_path: Path | None = None,
    sdk_root: Path | None = None,
    payload_roots: list[Path] | None = None,
    mode: str = "package",
) -> tuple[Path, Path]:
    """Validate, stage, pack with the official builder and verify the archive."""
    if mode not in {"package", "publish"}:
        raise PackagingError(f"unsupported mode {mode!r}; use package or publish")
    app_dir = app_dir.expanduser().resolve()
    out_dir = out_dir.expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    sdk_root = find_sdk_root(sdk_root)
    builder_mode = "local" if sdk_root != bundled_sdk_root() else "bundled"
    builder_info = {
        "mode": builder_mode,
        "source": "skill-bundled-pinned" if builder_mode == "bundled" else "caller-supplied-override",
        "upstream_repository": SKILL_SDK_SOURCE_REPO,
        "sdk_source_commit": SKILL_SDK_SOURCE_COMMIT if builder_mode == "bundled" else None,
    }
    validation = run_validator(app_dir, mode)
    contract = load_sdk_contract(sdk_root)
    manifest = load_json(app_dir / "manifest.json", "manifest")
    validate_icon(app_dir, manifest, contract)

    wheelhouse, generated_wheelhouse = prepare_wheelhouse(wheelhouse, out_dir)
    requirements_path = discover_requirements(app_dir, requirements_path)
    requirements = parse_requirements(requirements_path) if requirements_path else None
    wheels, platform_resolved, dependency_edges, warnings = select_wheels(wheelhouse, requirements)

    builder = sdk_root / "market" / "packaging" / "build.py"
    if not builder.is_file():
        raise PackagingError(f"SDK official packager not found: {builder}")
    with tempfile.TemporaryDirectory(prefix="recamera-app-package-") as temporary:
        staging = Path(temporary) / "app"
        staging.mkdir()
        source_manifest, payload_files, payload_modules = copy_app(app_dir, staging)
        (staging / "wheels").mkdir()
        for wheel in wheels:
            shutil.copyfile(wheel["path"], staging / "wheels" / wheel["path"].name)
        staged_manifest = update_manifest(staging, source_manifest, wheels, payload_modules)
        archive = out_dir / f"{staged_manifest['id']}-{staged_manifest['version']}-arm64.tar.gz"
        # A failed or non-writing builder must never let a matching archive from
        # a prior run masquerade as this build's output.
        if archive.exists():
            archive.unlink()
        command = [sys.executable, str(builder), str(staging), "--out", str(out_dir)]
        for payload_root in payload_roots or []:
            command.extend(["--payload-root", str(payload_root.expanduser().resolve())])
        completed = subprocess.run(command, text=True, capture_output=True, check=False)
        if completed.returncode:
            raise PackagingError(f"SDK packager failed:\n{completed.stdout}{completed.stderr}")

    archive = out_dir / f"{manifest['id']}-{manifest['version']}-arm64.tar.gz"
    if not archive.is_file():
        raise PackagingError(f"SDK packager did not produce expected archive: {archive}")
    final_archive = _verify_final_archive(archive, manifest, sdk_root)
    archive_wheels = sorted(
        name for name in final_archive["records"]
        if name.startswith("wheels/") and name.endswith(".whl"))
    expected_wheels = sorted(f"wheels/{wheel['path'].name}" for wheel in wheels)
    if archive_wheels != expected_wheels:
        raise PackagingError(
            "archive wheel payload does not match the selected wheel closure: expected "
            f"{expected_wheels}, got {archive_wheels}")

    report = out_dir / f"{manifest['id']}-{manifest['version']}-build-report.json"
    report.write_text(json.dumps({
        "archive": archive.name,
        "sha256": sha256_file(archive),
        "mode": mode,
        "platform": platform_contract(),
        "final_archive": {
            "manifest_sha256": final_archive["records"]["manifest.json"]["sha256"],
            "package_bytes": final_archive["package_bytes"],
            "unpacked_bytes": final_archive["unpacked_bytes"],
            "member_count": final_archive["member_count"],
            "bundled_artifacts": final_archive["artifacts"],
        },
        "payload_files": payload_files,
        "bundled_packages": [wheel_descriptor(wheel) for wheel in wheels],
        "platform_packages": platform_resolved,
        "dependency_edges": dependency_edges,
        "requirements_source": str(requirements_path) if requirements_path else None,
        "generated_inputs": {"empty_wheelhouse": generated_wheelhouse},
        "warnings": warnings,
        "builder": builder_info,
        "validation": validation,
        "signature": {
            "required_by_device_policy": True,
            "performed_by_skill": False,
            "scope": "detached signature over exact archive bytes",
        },
    }, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return archive, report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Package a reCamera Pro App with the SDK App Center v2 packager")
    parser.add_argument("--app-dir", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--wheelhouse", type=Path,
                        help="Flat directory of cp311/aarch64 wheels; omitted means none")
    parser.add_argument("--requirements", type=Path,
                        help="Exact name==version pins; auto-discovered from the App dir")
    parser.add_argument("--sdk-root", type=Path,
                        help="SDK checkout override; also read from RECAMERA_SDK_ROOT")
    parser.add_argument("--payload-root", action="append", type=Path,
                        help="Optional SDK builder payload overlay; may be repeated")
    parser.add_argument("--mode", choices=("package", "publish"), default="package",
                        help="package for local install, publish adds release hygiene checks")
    args = parser.parse_args(argv)
    try:
        archive, report = build(
            args.app_dir, args.out, args.wheelhouse, args.requirements,
            args.sdk_root, args.payload_root, args.mode)
    except (OSError, PackagingError, subprocess.SubprocessError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    print(f"built {archive}")
    print(f"report {report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
