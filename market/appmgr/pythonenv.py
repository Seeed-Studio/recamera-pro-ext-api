"""Per-release, offline Python environment staging for manifest v2 apps.

The module intentionally stops at the installer/runtime boundary:

* it builds an immutable generation under ``VENVS_DIR/<app>/releases/<release>``;
* every wheel is admitted to a content-addressed, root-owned wheelhouse only
  after its expected digest and size match;
* wheels are expanded without pip, indexes, build hooks, or network access;
* a single ``current`` symlink is atomically switched and a one-generation
  rollback record is kept for ``installer.restore_prev``.

The supervisor integration that chooses ``current/bin/python`` belongs to the
runtime layer and is deliberately not implemented here.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass
from email.parser import BytesParser
from pathlib import PurePosixPath
from typing import Any, Mapping

from . import manifest as manifest_contract, paths


ENV_FORMAT_VERSION = 1
ENV_METADATA = "release-env.json"
CURRENT_LINK = "current"
ROLLBACK_RECORD = "rollback.json"
DEFAULT_BASE_PYTHON = "/usr/bin/python3"

MAX_WHEEL_MEMBERS = int(os.environ.get("APPMGR_MAX_WHEEL_MEMBERS", "8192"))
MAX_ENV_UNPACKED_BYTES = int(os.environ.get(
    "APPMGR_MAX_ENV_UNPACKED_BYTES", str(512 * 1024 * 1024)))
ENV_BUILD_TIMEOUT = int(os.environ.get("APPMGR_ENV_BUILD_TIMEOUT", "300"))
ENV_PROBE_TIMEOUT = int(os.environ.get("APPMGR_ENV_PROBE_TIMEOUT", "60"))

_RELEASE_ID_RE = re.compile(r"[0-9A-Za-z][0-9A-Za-z.+-]{0,127}")
_PLATFORM_PROJECTS = frozenset((
    "cv2", "jinja2", "kit", "markupsafe", "numpy", "recamera-ext",
    "recamera-pro-kit", "rknn-toolkit-lite2", "rknnlite",
))


class PythonEnvError(RuntimeError):
    pass


@dataclass(frozen=True)
class StagedEnvironment:
    app_id: str
    release_id: str
    staging_dir: str | None
    final_dir: str
    release_lock_sha256: str
    reused: bool = False


@dataclass(frozen=True)
class EnvironmentActivation:
    app_id: str
    release_id: str
    previous_release_id: str | None
    current_link: str


def _fsync_dir(path: str) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _atomic_json(path: str, value: Mapping[str, Any]) -> None:
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".json.", dir=directory)
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(manifest_contract.canonical_json(value))
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, path)
        temporary = None
        _fsync_dir(directory)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def _app_root(app_id: str) -> str:
    if not paths.valid_app_id(app_id):
        raise PythonEnvError(f"invalid app id {app_id!r}")
    return paths.venv_dir(app_id)


def _release_id(release_lock: Mapping[str, Any]) -> str:
    value = release_lock.get("release_id")
    if not isinstance(value, str) or not _RELEASE_ID_RE.fullmatch(value):
        raise PythonEnvError(f"unsafe release id {value!r}")
    return value


def _wheelhouse_root(root: str | None = None) -> str:
    return root or os.environ.get(
        "APPMGR_WHEELHOUSE_DIR",
        os.path.join(os.path.dirname(paths.VENVS_DIR.rstrip("/")),
                     "wheelhouse", "sha256"),
    )


def _hash_open_file(fileobj) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    for chunk in iter(lambda: fileobj.read(1 << 20), b""):
        digest.update(chunk)
        size += len(chunk)
    return digest.hexdigest(), size


def _verify_regular_file(path: str, expected_sha: str, expected_size: int) -> None:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise PythonEnvError(f"cannot open wheel {path}: {exc}") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise PythonEnvError(f"wheel is not a regular file: {path}")
        if info.st_size != expected_size:
            raise PythonEnvError(
                f"wheel size mismatch for {os.path.basename(path)}: "
                f"got {info.st_size}, want {expected_size}")
        with os.fdopen(fd, "rb", closefd=False) as source:
            digest, size = _hash_open_file(source)
        if size != expected_size or digest != expected_sha:
            raise PythonEnvError(
                f"wheel digest mismatch for {os.path.basename(path)}: "
                f"got {digest}, want {expected_sha}")
    finally:
        os.close(fd)


def wheelhouse_path(wheel: Mapping[str, Any], *, wheelhouse_root: str | None = None) -> str:
    digest = wheel["sha256"]
    filename = wheel["filename"]
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise PythonEnvError("wheel descriptor has invalid sha256")
    if not isinstance(filename, str) or os.path.basename(filename) != filename \
            or not filename.endswith(".whl"):
        raise PythonEnvError("wheel descriptor has unsafe filename")
    root = os.path.realpath(_wheelhouse_root(wheelhouse_root))
    destination = os.path.realpath(os.path.join(root, digest, filename))
    if not paths.is_within(destination, root):
        raise PythonEnvError("wheelhouse digest path escapes configured root")
    return destination


def admit_wheel(source_path: str, wheel: Mapping[str, Any], *,
                wheelhouse_root: str | None = None) -> str:
    """Copy one verified wheel into its immutable content-addressed location."""
    destination = wheelhouse_path(wheel, wheelhouse_root=wheelhouse_root)
    expected_sha = wheel["sha256"]
    expected_size = wheel["size"]
    if os.path.realpath(source_path) == os.path.realpath(destination) and \
            os.path.isfile(destination):
        _verify_regular_file(destination, expected_sha, expected_size)
        return destination
    # A pre-existing content-addressed blob must not let a corrupt bundled
    # source bypass admission.  Verify both independently before reusing it.
    _verify_regular_file(source_path, expected_sha, expected_size)
    if os.path.isfile(destination):
        _verify_regular_file(destination, expected_sha, expected_size)
        return destination

    directory = os.path.dirname(destination)
    os.makedirs(directory, mode=0o755, exist_ok=True)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        source_fd = os.open(source_path, flags)
    except OSError as exc:
        raise PythonEnvError(f"cannot open wheel source {source_path}: {exc}") from exc
    temporary = None
    try:
        source_stat = os.fstat(source_fd)
        if not stat.S_ISREG(source_stat.st_mode):
            raise PythonEnvError(f"wheel source is not a regular file: {source_path}")
        fd, temporary = tempfile.mkstemp(prefix=".wheel.", dir=directory)
        digest = hashlib.sha256()
        size = 0
        with os.fdopen(source_fd, "rb", closefd=False) as source, \
                os.fdopen(fd, "wb") as output:
            for chunk in iter(lambda: source.read(1 << 20), b""):
                output.write(chunk)
                digest.update(chunk)
                size += len(chunk)
            output.flush()
            os.fsync(output.fileno())
        actual_sha = digest.hexdigest()
        if size != expected_size or actual_sha != expected_sha:
            raise PythonEnvError(
                f"wheel admission mismatch for {wheel['filename']}: "
                f"got {actual_sha}/{size}, want {expected_sha}/{expected_size}")
        os.chmod(temporary, 0o444)
        # Concurrent admission of the same digest is harmless: both candidates
        # contain identical bytes. os.replace publishes one complete file.
        os.replace(temporary, destination)
        temporary = None
        _fsync_dir(directory)
        return destination
    finally:
        os.close(source_fd)
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def _bundled_wheel(app_dir: str, wheel: Mapping[str, Any]) -> str:
    relative = wheel["file"]
    root = os.path.realpath(app_dir)
    candidate = os.path.join(app_dir, relative)
    resolved = os.path.realpath(candidate)
    if not paths.is_within(resolved, root):
        raise PythonEnvError(f"bundled wheel escapes app dir: {relative!r}")
    if os.path.islink(candidate):
        raise PythonEnvError(f"bundled wheel may not be a symlink: {relative!r}")
    return candidate


def _resolve_wheels(app_dir: str, manifest: Mapping[str, Any], *,
                    wheelhouse_root: str | None = None) -> list[tuple[dict, str]]:
    resolved = []
    for wheel in manifest["python"]["wheels"]:
        project = wheel["name"].lower().replace("_", "-")
        if project in _PLATFORM_PROJECTS:
            raise PythonEnvError(
                f"app wheel {wheel['name']!r} attempts to replace a platform-owned project")
        if wheel["source"] == "bundled":
            source = _bundled_wheel(app_dir, wheel)
        else:
            source = wheelhouse_path(wheel, wheelhouse_root=wheelhouse_root)
        stored = admit_wheel(source, wheel, wheelhouse_root=wheelhouse_root)
        resolved.append((dict(wheel), stored))
    return resolved


def _safe_zip_path(name: str, wheel_name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if (not name or name.startswith(("/", "\\")) or "\\" in name
            or any(part in ("", ".", "..") for part in path.parts)):
        raise PythonEnvError(f"unsafe wheel member {wheel_name}:{name!r}")
    if any(part.endswith(".data") for part in path.parts):
        raise PythonEnvError(
            f"wheel .data layout is unsupported for offline expansion: {wheel_name}:{name}")
    return path


def _normalise_project(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _validate_wheel_archive(descriptor: Mapping[str, Any], path: str,
                            owners: dict[str, str], total: list[int]) -> list:
    wheel_name = os.path.basename(path)
    try:
        archive = zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile) as exc:
        raise PythonEnvError(f"invalid wheel archive {wheel_name}: {exc}") from exc
    with archive:
        infos = archive.infolist()
        if len(infos) > MAX_WHEEL_MEMBERS:
            raise PythonEnvError(
                f"wheel {wheel_name} has too many members: {len(infos)} > {MAX_WHEEL_MEMBERS}")
        seen: set[str] = set()
        validated = []
        metadata_names = []
        wheel_metadata_names = []
        record_names = []
        for info in infos:
            member = _safe_zip_path(info.filename.rstrip("/"), wheel_name)
            name = str(member)
            if name in seen:
                raise PythonEnvError(f"duplicate wheel member {wheel_name}:{name}")
            seen.add(name)
            mode = (info.external_attr >> 16) & 0o170000
            if stat.S_ISLNK(mode):
                raise PythonEnvError(f"wheel symlink is forbidden: {wheel_name}:{name}")
            if info.flag_bits & 0x1:
                raise PythonEnvError(f"encrypted wheel member is forbidden: {wheel_name}:{name}")
            if info.is_dir():
                validated.append((info, member, True))
                continue
            total[0] += max(0, info.file_size)
            if total[0] > MAX_ENV_UNPACKED_BYTES:
                raise PythonEnvError(
                    f"Python environment exceeds unpacked cap {MAX_ENV_UNPACKED_BYTES}")
            previous = owners.get(name)
            if previous is not None:
                raise PythonEnvError(
                    f"wheel path collision {name!r}: {previous} and {wheel_name}")
            owners[name] = wheel_name
            if name.endswith(".dist-info/METADATA"):
                metadata_names.append(name)
            elif name.endswith(".dist-info/WHEEL"):
                wheel_metadata_names.append(name)
            elif name.endswith(".dist-info/RECORD"):
                record_names.append(name)
            with archive.open(info, "r") as source:
                header = source.read(20)
            if header.startswith(b"\x7fELF"):
                elf_class = header[4] if len(header) > 4 else 0
                byte_order = header[5] if len(header) > 5 else 0
                machine = int.from_bytes(header[18:20], "little") if len(header) >= 20 else -1
                if elf_class != 2 or byte_order != 1 or machine != 183:
                    raise PythonEnvError(
                        f"wheel contains non-AArch64 ELF payload: {wheel_name}:{name}")
            validated.append((info, member, False))
        if len(metadata_names) != 1 or len(wheel_metadata_names) != 1 or len(record_names) != 1:
            raise PythonEnvError(
                f"wheel {wheel_name} must contain exactly one METADATA, WHEEL and RECORD")
        metadata_name = metadata_names[0]
        if metadata_name.rsplit("/", 1)[0] != wheel_metadata_names[0].rsplit("/", 1)[0] \
                or metadata_name.rsplit("/", 1)[0] != record_names[0].rsplit("/", 1)[0]:
            raise PythonEnvError(f"wheel {wheel_name} has split .dist-info metadata")
        metadata_info = archive.getinfo(metadata_name)
        if metadata_info.file_size > 1024 * 1024:
            raise PythonEnvError(f"wheel METADATA is too large: {wheel_name}")
        try:
            with archive.open(metadata_info, "r") as source:
                metadata = BytesParser().parsebytes(source.read())
        except Exception as exc:
            raise PythonEnvError(f"cannot parse wheel METADATA {wheel_name}: {exc}") from exc
        actual_name = metadata.get("Name")
        actual_version = metadata.get("Version")
        if not actual_name or _normalise_project(actual_name) != \
                _normalise_project(str(descriptor["name"])):
            raise PythonEnvError(
                f"wheel project mismatch for {wheel_name}: {actual_name!r} != "
                f"{descriptor['name']!r}")
        if actual_version != descriptor["version"]:
            raise PythonEnvError(
                f"wheel version mismatch for {wheel_name}: {actual_version!r} != "
                f"{descriptor['version']!r}")
        return validated


def _expand_wheels(wheels: list[tuple[dict, str]], site_packages: str) -> None:
    owners: dict[str, str] = {}
    total = [0]
    prepared = []
    # Validate every wheel before writing the first member into the candidate.
    for descriptor, wheel_path in wheels:
        prepared.append((wheel_path, _validate_wheel_archive(
            descriptor, wheel_path, owners, total)))
    for wheel_path, members in prepared:
        with zipfile.ZipFile(wheel_path) as archive:
            for info, member, is_dir in members:
                destination = os.path.join(site_packages, *member.parts)
                if not paths.is_within(os.path.realpath(destination),
                                       os.path.realpath(site_packages)):
                    raise PythonEnvError(f"wheel member escapes environment: {info.filename!r}")
                if is_dir:
                    os.makedirs(destination, mode=0o755, exist_ok=True)
                    continue
                os.makedirs(os.path.dirname(destination), mode=0o755, exist_ok=True)
                with archive.open(info, "r") as source, open(destination, "wb") as output:
                    shutil.copyfileobj(source, output)
                os.chmod(destination, 0o444)


def _clean_subprocess_env() -> dict[str, str]:
    return {
        "HOME": "/nonexistent",
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": "/usr/bin:/bin",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
    }


def _run_checked(command: list[str], *, timeout: int, operation: str) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(command, capture_output=True, text=True,
                                timeout=timeout, env=_clean_subprocess_env())
    except (OSError, subprocess.SubprocessError) as exc:
        raise PythonEnvError(f"{operation} failed to run: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()[-1200:]
        raise PythonEnvError(f"{operation} returned {result.returncode}: {detail}")
    return result


def _site_packages(venv_dir: str) -> str:
    python = os.path.join(venv_dir, "bin", "python")
    result = _run_checked(
        [python, "-I", "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"],
        timeout=ENV_PROBE_TIMEOUT, operation="venv site-packages probe")
    site = result.stdout.strip()
    if not site or not paths.is_within(os.path.realpath(site), os.path.realpath(venv_dir)):
        raise PythonEnvError(f"venv reported unsafe site-packages path {site!r}")
    os.makedirs(site, mode=0o755, exist_ok=True)
    return site


def _probe_imports(venv_dir: str, modules: list[str]) -> None:
    if not modules:
        return
    source = (
        "import importlib,json\n"
        f"mods=json.loads({json.dumps(json.dumps(modules))})\n"
        "[importlib.import_module(name) for name in mods]\n"
    )
    _run_checked([os.path.join(venv_dir, "bin", "python"), "-I", "-c", source],
                 timeout=ENV_PROBE_TIMEOUT, operation="environment import probe")


def _harden_files(root: str) -> None:
    # Directories stay owner-writable so uninstall/GC works without a chmod walk.
    # Payload files are read-only; runtime also sets PYTHONDONTWRITEBYTECODE.
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        for name in dirnames:
            path = os.path.join(directory, name)
            if not os.path.islink(path):
                os.chmod(path, 0o755)
        for name in filenames:
            path = os.path.join(directory, name)
            if os.path.islink(path):
                continue
            mode = 0o555 if os.path.basename(directory) == "bin" else 0o444
            os.chmod(path, mode)


def _expected_env_metadata(manifest: Mapping[str, Any], release_lock: Mapping[str, Any]) -> dict:
    return {
        "env_format": ENV_FORMAT_VERSION,
        "app_id": manifest["id"],
        "release_id": release_lock["release_id"],
        "runtime_profile": manifest["python"]["runtime_profile"],
        "release_lock_sha256": manifest_contract.release_lock_sha256(release_lock),
        "wheels": [
            {key: wheel[key] for key in ("name", "version", "filename", "sha256", "size")}
            for wheel in manifest["python"]["wheels"]
        ],
    }


def stage_environment(
    app_dir: str, manifest: Mapping[str, Any], release_lock: Mapping[str, Any], *,
    base_python: str | None = None, wheelhouse_root: str | None = None,
) -> StagedEnvironment:
    """Build a complete candidate without changing the active environment."""
    if manifest_contract.validate_manifest(manifest) != manifest_contract.MANIFEST_VERSION:
        raise PythonEnvError("per-release environments require manifest v2")
    if release_lock.get("python") != manifest["python"]:
        raise PythonEnvError("release lock Python section does not match manifest")
    app_id = manifest["id"]
    release_id = _release_id(release_lock)
    lock_sha = manifest_contract.release_lock_sha256(release_lock)
    root = _app_root(app_id)
    releases = os.path.join(root, "releases")
    final = os.path.join(releases, release_id)
    metadata = _expected_env_metadata(manifest, release_lock)

    if os.path.islink(final):
        raise PythonEnvError(f"immutable environment may not be a symlink: {final}")
    if os.path.isdir(final):
        try:
            with open(os.path.join(final, ENV_METADATA), "rb") as f:
                existing = json.load(f)
        except (OSError, ValueError) as exc:
            raise PythonEnvError(f"existing environment {final} has invalid metadata: {exc}")
        if existing != metadata:
            raise PythonEnvError(f"immutable environment collision at {final}")
        return StagedEnvironment(app_id, release_id, None, final, lock_sha, reused=True)

    wheels = _resolve_wheels(app_dir, manifest, wheelhouse_root=wheelhouse_root)
    python = base_python or os.environ.get("APPMGR_PLATFORM_PYTHON", DEFAULT_BASE_PYTHON)
    if not os.path.isabs(python) or not os.path.isfile(python):
        raise PythonEnvError(f"platform Python interpreter missing: {python!r}")
    os.makedirs(releases, mode=0o755, exist_ok=True)
    staging = tempfile.mkdtemp(prefix=f".{release_id}.stage.", dir=releases)
    try:
        _run_checked([python, "-m", "venv", "--without-pip", "--system-site-packages", staging],
                     timeout=ENV_BUILD_TIMEOUT, operation="per-release venv creation")
        site = _site_packages(staging)
        _expand_wheels(wheels, site)
        _atomic_json(os.path.join(staging, ENV_METADATA), metadata)
        _probe_imports(staging, list(manifest["python"]["imports"]))
        _harden_files(staging)
        return StagedEnvironment(app_id, release_id, staging, final, lock_sha)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def discard_staged(candidate: StagedEnvironment) -> None:
    if candidate.staging_dir and os.path.isdir(candidate.staging_dir):
        shutil.rmtree(candidate.staging_dir, ignore_errors=True)


def _read_current(root: str) -> str | None:
    link = os.path.join(root, CURRENT_LINK)
    if not os.path.lexists(link):
        return None
    if not os.path.islink(link):
        raise PythonEnvError(f"environment current pointer is not a symlink: {link}")
    target = os.readlink(link)
    prefix = "releases/"
    if not target.startswith(prefix):
        raise PythonEnvError(f"environment current pointer has unsafe target {target!r}")
    release_id = target[len(prefix):]
    if not _RELEASE_ID_RE.fullmatch(release_id) or "/" in release_id:
        raise PythonEnvError(f"environment current pointer has unsafe release {release_id!r}")
    target_path = os.path.join(root, target)
    if os.path.islink(target_path) or not os.path.isdir(target_path):
        raise PythonEnvError(f"environment current pointer target is missing: {target}")
    return release_id


def _switch_current(root: str, release_id: str | None) -> None:
    link = os.path.join(root, CURRENT_LINK)
    if release_id is None:
        if os.path.lexists(link):
            os.unlink(link)
            _fsync_dir(root)
        return
    target = os.path.join("releases", release_id)
    if not os.path.isdir(os.path.join(root, target)):
        raise PythonEnvError(f"cannot activate missing environment {release_id!r}")
    temporary = os.path.join(root, f".current.{os.getpid()}")
    try:
        if os.path.lexists(temporary):
            os.unlink(temporary)
        os.symlink(target, temporary)
        os.replace(temporary, link)
        _fsync_dir(root)
    finally:
        if os.path.lexists(temporary):
            os.unlink(temporary)


def activate_environment(candidate: StagedEnvironment) -> EnvironmentActivation:
    """Publish a staged generation and atomically switch ``current`` to it."""
    root = _app_root(candidate.app_id)
    releases = os.path.join(root, "releases")
    os.makedirs(releases, mode=0o755, exist_ok=True)
    # Validate the old pointer before publishing a new immutable directory.
    previous = _read_current(root)
    if candidate.staging_dir:
        if os.path.exists(candidate.final_dir):
            raise PythonEnvError(f"environment appeared concurrently: {candidate.final_dir}")
        os.rename(candidate.staging_dir, candidate.final_dir)
        _fsync_dir(releases)
    record = {
        "env_format": ENV_FORMAT_VERSION,
        "new_release_id": candidate.release_id,
        "previous_release_id": previous,
        "release_lock_sha256": candidate.release_lock_sha256,
    }
    record_path = os.path.join(root, ROLLBACK_RECORD)
    old_record = None
    try:
        with open(record_path, "rb") as source:
            old_record = json.load(source)
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as exc:
        raise PythonEnvError(f"existing environment rollback record is invalid: {exc}") from exc
    _atomic_json(record_path, record)
    try:
        _switch_current(root, candidate.release_id)
    except BaseException:
        try:
            if old_record is None:
                os.unlink(record_path)
            else:
                _atomic_json(record_path, old_record)
        except BaseException as rollback_exc:
            raise PythonEnvError(
                f"environment activation failed and rollback record could not be restored: "
                f"{rollback_exc}")
        raise
    return EnvironmentActivation(candidate.app_id, candidate.release_id, previous,
                                 os.path.join(root, CURRENT_LINK))


def restore_previous(app_id: str) -> bool:
    """Restore the generation recorded by the last activation, exactly once."""
    root = _app_root(app_id)
    record_path = os.path.join(root, ROLLBACK_RECORD)
    try:
        with open(record_path) as f:
            record = json.load(f)
    except FileNotFoundError:
        return False
    except (OSError, ValueError) as exc:
        raise PythonEnvError(f"cannot read environment rollback record: {exc}") from exc
    if record.get("env_format") != ENV_FORMAT_VERSION:
        raise PythonEnvError("environment rollback record has unsupported format")
    new_release = record.get("new_release_id")
    previous = record.get("previous_release_id")
    if not isinstance(new_release, str) or not _RELEASE_ID_RE.fullmatch(new_release):
        raise PythonEnvError("environment rollback record has unsafe new release")
    if previous is not None and (not isinstance(previous, str)
                                 or not _RELEASE_ID_RE.fullmatch(previous)):
        raise PythonEnvError("environment rollback record has unsafe previous release")
    current = _read_current(root)
    # A previous attempt may have switched the symlink and then lost power
    # before unlinking the record.  Treat that state as already restored and
    # consume the stale record without switching a second time.
    if current == previous:
        try:
            os.unlink(record_path)
            _fsync_dir(root)
        except OSError:
            pass
        return False
    if current != new_release:
        raise PythonEnvError(
            f"environment rollback refused: current={current!r}, expected {new_release!r}")
    _switch_current(root, previous)
    try:
        os.unlink(record_path)
        _fsync_dir(root)
    except OSError:
        # The pointer transition is authoritative.  A retry observes
        # current==previous above and consumes the record exactly once.
        pass
    return True


def current_release_id(app_id: str) -> str | None:
    """Return the active immutable generation without exposing path parsing."""
    return _read_current(_app_root(app_id))


def current_python(app_id: str) -> str | None:
    root = _app_root(app_id)
    release_id = _read_current(root)
    if release_id is None:
        return None
    python = os.path.join(root, "releases", release_id, "bin", "python")
    if not os.path.isfile(python):
        raise PythonEnvError(f"active environment interpreter missing: {python}")
    return python


def discard_candidate(candidate: StagedEnvironment) -> None:
    """Remove an unpublished candidate, including an orphaned final rename.

    ``activate_environment`` publishes the immutable directory before switching
    ``current``.  If the switch fails, ``staging_dir`` has already vanished;
    this helper removes that unreferenced final directory but never deletes an
    active or reused generation.
    """
    discard_staged(candidate)
    if candidate.reused or not os.path.isdir(candidate.final_dir):
        return
    try:
        active = current_release_id(candidate.app_id)
    except PythonEnvError:
        return
    if active != candidate.release_id:
        shutil.rmtree(candidate.final_dir, ignore_errors=True)
        _fsync_dir(os.path.dirname(candidate.final_dir))


def rollback_candidate_activation(candidate: StagedEnvironment) -> bool:
    """Undo a candidate that may have switched ``current`` before failing.

    This is intentionally idempotent for the installer's BaseException path:
    pre-switch failures only discard the orphan, while post-switch failures
    consume the durable rollback record before discarding it.
    """
    active = current_release_id(candidate.app_id)
    restored = False
    if active == candidate.release_id:
        restored = restore_previous(candidate.app_id)
        if current_release_id(candidate.app_id) == candidate.release_id:
            raise PythonEnvError("candidate environment remained active after rollback")
    discard_candidate(candidate)
    return restored


def reconcile_staging(app_id: str) -> list[str]:
    """Remove orphaned candidate directories; never touches complete releases."""
    releases = os.path.join(_app_root(app_id), "releases")
    removed = []
    try:
        names = os.listdir(releases)
    except FileNotFoundError:
        return removed
    for name in names:
        if not name.startswith(".") or ".stage." not in name:
            continue
        path = os.path.join(releases, name)
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
            removed.append(name)
    return removed
