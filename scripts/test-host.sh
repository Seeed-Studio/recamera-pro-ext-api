#!/usr/bin/env bash
set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$repo_root"

# Keep the host suite independent of target-only sockets, RKNN and shared
# libraries. The lock pins the development NumPy to the firmware's 1.23.x ABI.
RECAMERA_RELEASE_ARTIFACT_TESTS=0 exec uv run --exact --frozen --python 3.11 \
  pytest -p no:cacheprovider \
    sdk/tests \
    kit/adapters \
    kit/tests \
    apps \
    market/appmgr/tests \
    market/inferenced/tests \
    market/catalog \
    market/packaging \
    tools/test_install_platform_services.py \
    tools/test_service_launchers.py \
    "$@"
