"""Host-only fixtures for the ctypes SDK tests.

The SDK package is loaded under a private package name.  This keeps the tests
independent of adapter tests that intentionally install a fake ``recamera_ext``
module in ``sys.modules``, while still exercising normal relative imports.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


@pytest.fixture(scope="session")
def sdk_module():
    """Return a host-loadable instance of the real Python SDK package."""

    package_dir = Path(__file__).resolve().parents[1] / "python" / "recamera_ext"
    module_name = "_recamera_ext_sdk_tests"
    cached = sys.modules.get(module_name)
    if cached is not None:
        return cached

    spec = importlib.util.spec_from_file_location(
        module_name,
        package_dir / "__init__.py",
        submodule_search_locations=[str(package_dir)],
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("could not construct a module spec for recamera_ext")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(module_name, None)
        raise
    return module
