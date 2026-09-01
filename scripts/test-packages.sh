#!/usr/bin/env bash
set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
smoke_root=$(mktemp -d "${TMPDIR:-/tmp}/recamera-package-smoke.XXXXXX")

cleanup() {
  if [ -n "${smoke_root:-}" ] && [ -d "$smoke_root" ]; then
    rm -rf -- "$smoke_root"
  fi
}
trap cleanup EXIT

dist_dir="$smoke_root/dist"
venv_dir="$smoke_root/venv"

cd "$repo_root"
uv build --all-packages --wheel --offline --no-build-logs --out-dir "$dist_dir"

python3 - "$dist_dir" <<'PY'
import sys
import zipfile
from email.parser import BytesParser
from pathlib import Path

dist = Path(sys.argv[1])
sdk_wheels = list(dist.glob("recamera_ext-*.whl"))
kit_wheels = list(dist.glob("recamera_pro_kit-*.whl"))
assert len(sdk_wheels) == 1, sdk_wheels
assert len(kit_wheels) == 1, kit_wheels

with zipfile.ZipFile(sdk_wheels[0]) as wheel:
    names = set(wheel.namelist())
    assert "recamera_ext/__init__.py" in names
    metadata_name = next(name for name in names if name.endswith(".dist-info/METADATA"))
    metadata = BytesParser().parsebytes(wheel.read(metadata_name))
    assert metadata["Name"] == "recamera-ext"
    assert metadata["Version"] == "1.4.0"
    assert set(metadata["Requires-Python"].split(",")) == {">=3.11", "<3.12"}

with zipfile.ZipFile(kit_wheels[0]) as wheel:
    names = set(wheel.namelist())
    assert "kit/__init__.py" in names
    assert "kit/app.py" in names
    assert "kit/geometry.py" in names
    assert "kit/runtime/postprocess/detect.py" in names
    assert "kit/workflow/runtime.py" in names
    assert not any("/__pycache__/" in name or name.endswith(".pyc") for name in names)
    assert not any(name.startswith("kit/tests/") for name in names)
    assert not any(
        Path(name).name.startswith("test_") and name.endswith(".py")
        for name in names
    )
    metadata_name = next(name for name in names if name.endswith(".dist-info/METADATA"))
    metadata = BytesParser().parsebytes(wheel.read(metadata_name))
    requirements = metadata.get_all("Requires-Dist") or []
    assert metadata["Name"] == "recamera-pro-kit"
    assert set(metadata["Requires-Python"].split(",")) == {">=3.11", "<3.12"}
    assert any(requirement.startswith("numpy<1.24,>=1.23") for requirement in requirements)
    assert any(requirement.startswith("recamera-ext<2,>=1.4") for requirement in requirements)
PY

uv venv --python 3.11 --no-project "$venv_dir"

# Dependency resolution is covered by `uv lock` and the host suite. This smoke
# test deliberately installs only the two freshly built artifacts so it also
# works in a network-isolated firmware build environment.
uv pip install --offline --no-deps --python "$venv_dir/bin/python" \
  "$dist_dir"/recamera_ext-*.whl \
  "$dist_dir"/recamera_pro_kit-*.whl

(
  cd "$smoke_root"
  "$venv_dir/bin/python" - <<'PY'
from importlib.metadata import version
from pathlib import Path

import kit
import kit.config
import kit.geometry
import kit.workflow
import recamera_ext
from recamera_ext import (
    FrameSource,
    InferenceLease,
    InferenceState,
    InferenceStatus,
    ProbeSource,
    ResultSink,
)

site_root = Path(recamera_ext.__file__).resolve().parents[1]
assert Path(kit.__file__).resolve().is_relative_to(site_root)
assert version("recamera-ext") == "1.4.0"
assert version("recamera-pro-kit") == "0.1.0"
assert FrameSource and ProbeSource and ResultSink
assert InferenceLease and InferenceState and InferenceStatus
assert kit.GeometryBuilder().point(1, 2).build()[0]["type"] == "point"
print("package smoke OK:", recamera_ext.__file__, kit.__file__)
PY
)
