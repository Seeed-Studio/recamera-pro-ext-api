#!/bin/sh
set -eu

PYTHON_BIN=${PYTHON_BIN:-python3}

for endpoint in frame.sock result-in.sock probe.sock inference-control.sock; do
  if [ ! -S "/run/recamera/$endpoint" ]; then
    echo "missing extension socket: /run/recamera/$endpoint" >&2
    exit 1
  fi
done

"$PYTHON_BIN" - <<'PY'
import ctypes
from importlib.metadata import version

import numpy

import kit
import recamera_ext
from recamera_ext import InferenceLease, InferenceState, InferenceStatus
from rknnlite.api import RKNNLite

assert numpy.__version__.startswith("1.23."), numpy.__version__
native = ctypes.CDLL("librecamera_ext.so.1")
for symbol in (
    "rc_ext_inference_lease_open",
    "rc_ext_inference_lease_ready",
    "rc_ext_inference_lease_status",
    "rc_ext_inference_lease_alive",
    "rc_ext_inference_lease_close",
    "rc_ext_inference_lease_abandon_after_fork",
):
    getattr(native, symbol)

# The firmware build stages this wheel offline.  Construction exercises all
# compiled rknnlite imports but does not load a model or call init_runtime(), so
# this smoke check never creates an NPU context or competes with rkipc.
assert version("rknn-toolkit-lite2") == "2.3.2"
rknn = RKNNLite()
assert rknn.rknn_runtime is None
rknn.release()

print(
    "device smoke OK:",
    numpy.__version__,
    "rknnlite", version("rknn-toolkit-lite2"),
    recamera_ext.__file__,
    kit.__file__,
)
PY
