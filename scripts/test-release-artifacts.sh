#!/usr/bin/env bash
set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$repo_root"

# These checks intentionally consume artifacts produced outside the ordinary
# host unit-test environment: signed/staged app packages, model payloads and
# the adjacent shared training repository. Missing inputs are release blockers,
# not Python unit-test failures.
export RECAMERA_RELEASE_ARTIFACT_TESTS=1

exec uv run --exact --frozen --python 3.11 \
  pytest -p no:cacheprovider \
    apps/fall-detection/test_temporal_training_tools.py \
    market/catalog \
    "$@"
