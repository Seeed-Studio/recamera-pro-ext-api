#!/usr/bin/env bash
# crayfish-fight -- ONNX -> RKNN for the three device models.
#
# ⚠ RUNS ONLY INSIDE AN x86 DOCKER CONTAINER WITH rknn-toolkit2 2.3.x.
#   rknn-toolkit2 is x86-only: it does not run on the Mac and does not run on
#   the device. The toolkit MINOR version must match the device runtime
#   (librknnrt 2.3.2) or `load_rknn`/`init_runtime` fails on the board --
#   docs/guide/model-onboarding.md §0 / §9.
#
# Inputs  : the four ONNX files produced on the Mac by tools/export_onnx.py
# Outputs : <OUT_DIR>/{det_v1_n_rawhead_int8,det_v1_s_rawhead_int8,
#                      sex_cls_int8,behavior_cls_int8}.rknn
#           (+ the matching .dataset.txt convert.py writes next to each)
# Then    : copy the three you ship into apps/crayfish-fight/models/ under the
#           names manifest.json declares, and run models/convert/device_verify.py
#           ON THE DEVICE before wiring the app up.
#
# ---------------------------------------------------------------------------
# 0. Calibration images -- INT8 accuracy lives or dies here
# ---------------------------------------------------------------------------
# Every model is quantized against images from ITS OWN input distribution.
# Feeding the detector's full frames to the 128px sex classifier (or COCO to
# either) is the classic way to lose 10+ points -- model-onboarding.md §4.
#
#   detector      -> 300 SITE full frames (1080p, side view, blue tank, glare)
#   sex_cls       -> 300 single-animal crops, the same geometry app.py feeds
#                    (detector box, pad=+0.10, squared, 128px)
#   behavior_cls  -> 300 pair-union ROIs (pad=+0.15, squared, 224px)
#
# The site frames live on spark (the training box), not on the Mac:
#
#   # on spark: 300 evenly-spaced frames out of the 1080p site set
#   SRC=~/crayfish-fight/data/site/site_1080
#   DST=~/crayfish-fight/calib/det_site_300
#   mkdir -p "$DST"
#   ls "$SRC"/*.jpg | awk -v n=$(ls "$SRC"/*.jpg | wc -l) \
#       'NR % int((n/300)+1) == 1' | head -300 | xargs -I{} cp {} "$DST"/
#   # (stride-sample, not `head -300`: the site set is 1 fps from four
#   #  contiguous sessions, so the first 300 files are one lighting condition.)
#
#   # crops for the two classifiers: reuse the classifier TRAINING sets, which
#   # already have exactly the app's crop geometry, flattened into one dir
#   # (convert.py's dataset builder does a NON-recursive iterdir):
#   flatten() { mkdir -p "$2"; find "$1" -name '*.jpg' | shuf -n "$3" \
#                 --random-source=<(yes) | xargs -I{} cp {} "$2"/; }
#   flatten ~/crayfish-fight/data/sex/sex/train      ~/crayfish-fight/calib/sex_300 300
#   flatten ~/crayfish-fight/data/behavior/behavior/train \
#           ~/crayfish-fight/calib/behavior_300 300
#
# then move calib/ + export/ into the x86 container's workspace and point the
# variables below at them.
#
# ---------------------------------------------------------------------------
# 1. Usage
# ---------------------------------------------------------------------------
#   docker exec -it <rknn-2.3.x container> bash
#   cd /workspace/recamera_pro
#   ONNX_DIR=/workspace/export OUT_DIR=/workspace/rknn \
#   CALIB_DET=/workspace/calib/det_site_300 \
#   CALIB_SEX=/workspace/calib/sex_300 \
#   CALIB_BEHAVIOR=/workspace/calib/behavior_300 \
#     bash apps/crayfish-fight/tools/convert_rknn.sh
#
#   QUANT=fp16 bash ... convert_rknn.sh      # link-up pass first (§3 of the guide:
#                                            # prove FP16 loads on the board before
#                                            # debugging quantization)
#   MODELS="sex_cls behavior_cls" bash ...   # subset
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
CONVERT_PY="${CONVERT_PY:-$REPO_ROOT/models/convert/convert.py}"
PYTHON_BIN="${PYTHON_BIN:-python3}"

ONNX_DIR="${ONNX_DIR:-/workspace/export}"
OUT_DIR="${OUT_DIR:-/workspace/rknn}"
QUANT="${QUANT:-int8}"                     # int8 | fp16
DATASET_COUNT="${DATASET_COUNT:-300}"
MODELS="${MODELS:-det_v1_n det_v1_s sex_cls behavior_cls}"

CALIB_DET="${CALIB_DET:-/workspace/calib/det_site_300}"
CALIB_SEX="${CALIB_SEX:-/workspace/calib/sex_300}"
CALIB_BEHAVIOR="${CALIB_BEHAVIOR:-/workspace/calib/behavior_300}"

# ---------------------------------------------------------------------------
# 2. Preflight -- fail loudly here, not 20 minutes into a build
# ---------------------------------------------------------------------------
die() { echo "ERROR: $*" >&2; exit 1; }

"$PYTHON_BIN" - <<'PY' || die "rknn-toolkit2 not importable -- wrong container?"
import sys
try:
    from rknn.api import RKNN            # noqa: F401
except Exception as e:                   # pragma: no cover
    print(f"rknn.api import failed: {e}", file=sys.stderr)
    sys.exit(1)
try:
    import rknn
    print(f"[preflight] rknn-toolkit2 {getattr(rknn, '__version__', '?')}")
except Exception:
    pass
PY

[ -f "$CONVERT_PY" ] || die "converter not found: $CONVERT_PY"
[ -d "$ONNX_DIR" ] || die "ONNX_DIR not found: $ONNX_DIR (run tools/export_onnx.py on the Mac first)"
mkdir -p "$OUT_DIR"

need_calib() { [ "$QUANT" = "int8" ]; }
check_calib() {
  need_calib || return 0
  local d="$1" who="$2"
  [ -d "$d" ] || die "$who: INT8 needs --dataset-dir, missing: $d (see §0 above)"
  local n; n=$(find "$d" -maxdepth 1 -type f \( -name '*.jpg' -o -name '*.jpeg' \
      -o -name '*.png' -o -name '*.bmp' \) | wc -l | tr -d ' ')
  [ "$n" -ge 50 ] || die "$who: only $n calibration images in $d (want >= $DATASET_COUNT)"
  echo "[preflight] $who calibration: $n images in $d"
}

# ---------------------------------------------------------------------------
# 3. Convert
# ---------------------------------------------------------------------------
# --yolo-head detect: convert.py re-cuts the graph at the 6 head leaf-Convs.
#   Harmless on an ONNX export_onnx.py already cut (the same _LEAF_RE selects
#   the same six tensors), and it is what keeps the two paths in agreement.
# mean/std: convert.py's defaults (0 / 255) BAKE the /255 into the rknn, so the
#   device feeds RAW uint8 -- which is exactly what kit's letterbox and
#   crop_roi_hw hand over. Do not add a /255 in app.py.
convert_one() {
  local id="$1" onnx="$2" out="$3" calib="$4"; shift 4
  echo
  echo "=== [$id] $QUANT  $onnx -> $out"
  [ -f "$onnx" ] || die "$id: missing $onnx"
  local args=(--onnx "$onnx" --out "$out" --platform rv1126b --quant "$QUANT" "$@")
  if need_calib; then
    args+=(--dataset-dir "$calib" --dataset-count "$DATASET_COUNT")
  fi
  "$PYTHON_BIN" "$CONVERT_PY" "${args[@]}"
  ls -l "$out"
}

for m in $MODELS; do
  case "$m" in
    det_v1_n)
      check_calib "$CALIB_DET" det_v1_n
      convert_one det_v1_n "$ONNX_DIR/det_v1_n.onnx" \
        "$OUT_DIR/det_v1_n_rawhead_${QUANT}.rknn" "$CALIB_DET" \
        --yolo-head detect
      ;;
    det_v1_s)
      check_calib "$CALIB_DET" det_v1_s
      convert_one det_v1_s "$ONNX_DIR/det_v1_s.onnx" \
        "$OUT_DIR/det_v1_s_rawhead_${QUANT}.rknn" "$CALIB_DET" \
        --yolo-head detect
      ;;
    sex_cls)
      check_calib "$CALIB_SEX" sex_cls
      convert_one sex_cls "$ONNX_DIR/sex_cls.onnx" \
        "$OUT_DIR/sex_cls_${QUANT}.rknn" "$CALIB_SEX"
      ;;
    behavior_cls)
      check_calib "$CALIB_BEHAVIOR" behavior_cls
      convert_one behavior_cls "$ONNX_DIR/behavior_cls.onnx" \
        "$OUT_DIR/behavior_cls_${QUANT}.rknn" "$CALIB_BEHAVIOR"
      ;;
    *) die "unknown model id: $m" ;;
  esac
done

cat <<EOF

=== done. artefacts in $OUT_DIR
Next (docs/guide/model-onboarding.md §5-§8):
  1. copy to the device and prove each one LOADS there:
       python3 models/convert/device_verify.py /path/to/<model>.rknn
     (a non-zero return here is a toolkit/runtime version mismatch, not an app bug)
  2. numeric check against the host ONNX:
       python3 models/convert/device_infer_verify.py ...
  3. drop the three shipped models into apps/crayfish-fight/models/ under the
     names manifest.json declares:
       det_v1_n_rawhead_int8.rknn  sex_cls_int8.rknn  behavior_cls_int8.rknn
     (shipping det_v1_s instead: rename it AND edit models[0].file)
  4. package + install + activate:
       python3 market/packaging/build.py apps/crayfish-fight
EOF
