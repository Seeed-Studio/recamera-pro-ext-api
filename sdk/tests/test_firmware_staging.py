from __future__ import annotations

import hashlib
import importlib.util
import stat
from pathlib import Path
from zipfile import ZipFile, ZipInfo

import pytest


REPO = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "recamera_install_site_packages",
    REPO / "tools" / "install_site_packages.py",
)
assert SPEC is not None and SPEC.loader is not None
staging = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(staging)


def _assert_runtime_only(site: Path) -> None:
    for path in site.rglob("*"):
        relative = path.relative_to(site)
        assert "tests" not in relative.parts, relative
        assert "__pycache__" not in relative.parts, relative
        assert ".pytest_cache" not in relative.parts, relative
        assert not path.name.endswith((".pyc", ".pyo")), relative
        assert not (path.name.startswith("test_") and path.suffix == ".py"), relative


def _elf_machine(path: Path) -> int:
    header = path.read_bytes()[:20]
    assert header[:4] == b"\x7fELF"
    assert header[5] == 1  # little endian
    return int.from_bytes(header[18:20], "little")


def _write_elf(path: Path, machine: int = 183) -> None:
    header = bytearray(64)
    header[:4] = b"\x7fELF"
    header[4] = 2  # ELF64
    header[5] = 1  # little endian
    header[6] = 1  # ELF version
    header[16:18] = (3).to_bytes(2, "little")  # ET_DYN
    header[18:20] = machine.to_bytes(2, "little")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(header)


def test_rknn_runtime_link_resolves_in_assembled_rootfs(tmp_path: Path) -> None:
    provider = tmp_path / "media/lib/librknnrt.so"
    _write_elf(provider)
    rootfs = tmp_path / "rootfs"
    runtime = rootfs / "oem/usr/lib/librknnrt.so"
    runtime.parent.mkdir(parents=True)
    runtime.write_bytes(provider.read_bytes())

    link = staging._stage_rknn_runtime_link(rootfs, provider)

    assert link.is_symlink()
    assert link.readlink() == Path("../../oem/usr/lib/librknnrt.so")
    assert link.exists(), "assembled rootfs link must not dangle"
    assert link.resolve(strict=True) == runtime.resolve(strict=True)
    staging._verify_rknn_runtime_link(
        rootfs, provider, require_resolved_target=True
    )


def test_rknn_runtime_overlay_requires_oem_merge_to_resolve(
    tmp_path: Path,
) -> None:
    provider = tmp_path / "media/lib/librknnrt.so"
    _write_elf(provider)
    rootfs = tmp_path / "app-overlay"

    link = staging._stage_rknn_runtime_link(rootfs, provider)

    assert link.is_symlink()
    assert not link.exists(), "isolated app overlay has not received /oem yet"
    staging._verify_rknn_runtime_link(rootfs, provider)
    with pytest.raises(FileNotFoundError, match="link is dangling"):
        staging._verify_rknn_runtime_link(
            rootfs, provider, require_resolved_target=True
        )


def test_rknn_runtime_rejects_non_aarch64_before_staging(
    tmp_path: Path,
) -> None:
    provider = tmp_path / "media/lib/librknnrt.so"
    _write_elf(provider, machine=62)  # EM_X86_64
    rootfs = tmp_path / "rootfs"

    with pytest.raises(ValueError, match="must be AArch64"):
        staging._stage_rknn_runtime_link(rootfs, provider)

    assert not (rootfs / "usr/lib/librknnrt.so").is_symlink()


def test_rknn_runtime_verify_rejects_wrong_or_dangling_links(
    tmp_path: Path,
) -> None:
    provider = tmp_path / "media/lib/librknnrt.so"
    _write_elf(provider)
    rootfs = tmp_path / "rootfs"
    link = rootfs / "usr/lib/librknnrt.so"
    link.parent.mkdir(parents=True)
    link.symlink_to("libwrong.so")

    with pytest.raises(ValueError, match="unexpected librknnrt link target"):
        staging._verify_rknn_runtime_link(rootfs, provider)

    link.unlink()
    link.symlink_to("../../oem/usr/lib/librknnrt.so")
    dangling = rootfs / "oem/usr/lib/librknnrt.so"
    dangling.parent.mkdir(parents=True)
    dangling.symlink_to("missing-librknnrt.so")
    with pytest.raises(FileNotFoundError, match="assembled OEM.*dangling"):
        staging._verify_rknn_runtime_link(rootfs, provider)


def test_rknn_runtime_verify_rejects_different_oem_entity(
    tmp_path: Path,
) -> None:
    provider = tmp_path / "media/lib/librknnrt.so"
    _write_elf(provider)
    provider.write_bytes(provider.read_bytes() + b"media provider")
    rootfs = tmp_path / "rootfs"
    runtime = rootfs / "oem/usr/lib/librknnrt.so"
    _write_elf(runtime)
    runtime.write_bytes(runtime.read_bytes() + b"different OEM entity")
    link = rootfs / "usr/lib/librknnrt.so"
    link.parent.mkdir(parents=True)
    link.symlink_to("../../oem/usr/lib/librknnrt.so")

    with pytest.raises(ValueError, match="differs from its media provider"):
        staging._verify_rknn_runtime_link(
            rootfs, provider, require_resolved_target=True
        )


def test_locked_device_runtime_stages_aarch64_cp311_payload(tmp_path: Path) -> None:
    site = tmp_path / "site-packages"
    site.mkdir()

    staging._install_locked_wheels(
        REPO / "runtime" / "requirements.lock",
        REPO / "release" / "pkg" / "wheels",
        site,
    )

    native = (
        site
        / "rknnlite"
        / "api"
        / "rknn_runtime.cpython-311-aarch64-linux-gnu.so"
    )
    assert native.is_file()
    assert (site / "psutil" / "_psutil_linux.abi3.so").is_file()
    assert (site / "ruamel" / "yaml" / "__init__.py").is_file()
    assert (site / "_ruamel_yaml.cpython-311-aarch64-linux-gnu.so").is_file()
    assert _elf_machine(native) == 183  # EM_AARCH64
    # psutil's upstream wheel carries psutil/tests/*.py; firmware staging must
    # keep the runtime extension/modules without shipping that test suite.
    assert not (site / "psutil" / "tests").exists()
    _assert_runtime_only(site)


def test_runtime_wheel_filters_tests_caches_and_test_modules(tmp_path: Path) -> None:
    wheels = tmp_path / "wheels"
    wheels.mkdir()
    wheel = wheels / "demo_runtime-1.0-py3-none-any.whl"
    with ZipFile(wheel, "w") as archive:
        archive.writestr("demo_runtime/__init__.py", "VALUE = 1\n")
        archive.writestr("demo_runtime/runtime.py", "def run(): return 1\n")
        archive.writestr("demo_runtime/data/test_fixture.json", "{}\n")
        archive.writestr("demo_runtime/tests/__init__.py", "")
        archive.writestr("demo_runtime/tests/test_nested.py", "raise AssertionError\n")
        archive.writestr("demo_runtime/__pycache__/runtime.cpython-311.pyc", b"bad")
        archive.writestr("demo_runtime/test_inline.py", "raise AssertionError\n")
        archive.writestr("demo_runtime/legacy.pyo", b"bad")
        archive.writestr("tests/test_top_level.py", "raise AssertionError\n")
        archive.writestr(
            "demo_runtime-1.0.dist-info/METADATA",
            "Metadata-Version: 2.1\nName: demo-runtime\nVersion: 1.0\n",
        )
        archive.writestr("demo_runtime-1.0.dist-info/RECORD", "")
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    lock = tmp_path / "requirements.lock"
    lock.write_text(f"{digest}  {wheel.name}\n", encoding="utf-8")
    site = tmp_path / "site"
    (site / "tests").mkdir(parents=True)
    (site / "tests" / "stale.py").write_text("stale\n", encoding="utf-8")

    staging._install_locked_wheels(lock, wheels, site)

    assert (site / "demo_runtime" / "runtime.py").is_file()
    assert (site / "demo_runtime" / "data" / "test_fixture.json").is_file()
    assert (site / "demo_runtime-1.0.dist-info" / "METADATA").is_file()
    assert not (site / "tests").exists()
    assert not (site / "demo_runtime" / "tests").exists()
    assert not (site / "demo_runtime" / "__pycache__").exists()
    assert not (site / "demo_runtime" / "test_inline.py").exists()
    assert not (site / "demo_runtime" / "legacy.pyo").exists()
    _assert_runtime_only(site)


def test_runtime_hash_mismatch_fails_before_extraction(tmp_path: Path) -> None:
    wheels = tmp_path / "wheels"
    wheels.mkdir()
    source = next((REPO / "release" / "pkg" / "wheels").glob("psutil-*.whl"))
    wheel = wheels / source.name
    wheel.write_bytes(source.read_bytes())
    lock = tmp_path / "requirements.lock"
    lock.write_text("0" * 64 + "  " + wheel.name + "\n", encoding="utf-8")
    site = tmp_path / "site"
    site.mkdir()

    with pytest.raises(ValueError, match="hash mismatch"):
        staging._install_locked_wheels(lock, wheels, site)

    assert list(site.iterdir()) == []


def test_runtime_wheel_rejects_path_traversal(tmp_path: Path) -> None:
    wheels = tmp_path / "wheels"
    wheels.mkdir()
    wheel = wheels / "bad-1.0-py3-none-any.whl"
    with ZipFile(wheel, "w") as archive:
        archive.writestr("../escape.py", "raise RuntimeError('escaped')\n")
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    lock = tmp_path / "requirements.lock"
    lock.write_text(f"{digest}  {wheel.name}\n", encoding="utf-8")
    site = tmp_path / "site"
    site.mkdir()

    with pytest.raises(ValueError, match="unsafe member"):
        staging._install_locked_wheels(lock, wheels, site)

    assert not (tmp_path / "escape.py").exists()


def test_runtime_wheel_rejects_symlink_inside_filtered_tests(tmp_path: Path) -> None:
    wheels = tmp_path / "wheels"
    wheels.mkdir()
    wheel = wheels / "bad-link-1.0-py3-none-any.whl"
    link = ZipInfo("demo_runtime/tests/test_link.py")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    with ZipFile(wheel, "w") as archive:
        archive.writestr("demo_runtime/runtime.py", "VALUE = 1\n")
        archive.writestr(link, "../../outside.py")
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    lock = tmp_path / "requirements.lock"
    lock.write_text(f"{digest}  {wheel.name}\n", encoding="utf-8")
    site = tmp_path / "site"
    site.mkdir()

    with pytest.raises(ValueError, match="wheel symlink is not allowed"):
        staging._install_locked_wheels(lock, wheels, site)

    # Filtering happens only after every member passes safety validation.
    assert list(site.iterdir()) == []
