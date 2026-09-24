# ONNX → RKNN → reCamera Pro App

Read this when the requested app needs a new model, ONNX conversion, calibration,
or model-output verification. A supplied, already compatible `.rknn` does not
need reconversion. This workflow builds on the host; it does not contact a device.

## 1. Select a model-specific reference

Source baseline: [Rockchip Model Zoo at
bad6c7334531becaf90a561988519b7bec34d0ab](https://github.com/airockchip/rknn_model_zoo/tree/bad6c7334531becaf90a561988519b7bec34d0ab).
This is independent of the skill's SDK/builder pin. Record a new revision when
using newer examples. Read the selected model's README, export instructions,
conversion script, and inference/postprocessing implementation together.

| Model family | Official example at that revision | What must be checked |
| --- | --- | --- |
| YOLOv8 detection | `examples/yolov8/python/convert.py`, `yolov8.py` | Example mean `[0,0,0]`, std `[255,255,255]`; exact raw-head order, DFL/decode/NMS and labels must match the app |
| MobileNet / ResNet | `examples/mobilenet/python/mobilenet.py`, `examples/resnet/python/resnet.py` | Conversion is in the inference script; ImageNet mean `[123.675,116.28,103.53]`, std `[58.395,57.12,57.375]`; confirm crop/resize and output softmax |
| PP-OCR detection | `examples/PPOCR/PPOCR-Det/python/convert.py` | Its own normalization, input geometry and postprocessing; OCR recognition is a separate model/contract |
| YOLOv8 pose | `examples/yolov8_pose/python/convert.py` | Some platform paths use `hybrid_quantization_step1/step2` with export-specific tensor names; ordinary INT8 is not a replacement for that recipe |
| YOLOv8 segmentation | `examples/yolov8_seg/python/convert.py` | Segmentation heads, prototypes, masks and postprocessing differ from detection |
| Audio, multiple inputs, dynamic shapes, other architectures | Closest supported model's example and Toolkit API reference | Use a separate reviewed conversion script when the helper below does not cover the contract |

Do not identify a model by filename alone or copy internal tensor names across
different exports. A stock framework YOLO export is not necessarily the same
graph as the Model Zoo example or Kit's raw-head detector. Do not automatically
remove NMS, cut the graph, downgrade opset/IR, or change input dimensions to make
compilation pass. Re-export intentionally and verify the changed outputs.

## 2. Isolate the host toolchain

The documented helper baseline is **Linux x86_64, Python 3.11, RKNN-Toolkit2
2.3.2**, targeting **`rv1126b`**. `rv1126` is a different, legacy target.
Other host/toolkit combinations require separate verification; this baseline
does not assert that Rockchip only supports x86 hosts. The firmware's current
package profile is `rv1126b-linux-gnu-cp311-rknn232-v1`. A compiler/runtime
version difference needs a compatibility check; it is not automatically proof
that every model will fail to load.

From the skill directory, create the environment outside the app's source tree:

```bash
uv venv --python 3.11 /tmp/recamera-rknn-env
uv pip install --python /tmp/recamera-rknn-env/bin/python \
  'torch==2.4.0' --index-url https://download.pytorch.org/whl/cpu
uv pip install --python /tmp/recamera-rknn-env/bin/python \
  -r references/model-conversion-requirements.txt
uv run --no-project --python /tmp/recamera-rknn-env/bin/python \
  python scripts/convert_model.py check-env
```

The requirements use the official Toolkit2 wheel pinned to source revision
`59a913d172e7f5ff03c9076e2ec7b1b1288ffd08` and its SHA-256. Remaining host
transitive dependencies are resolved by uv; save `uv pip freeze --python
/tmp/recamera-rknn-env/bin/python` with release build records if exact environment
reproduction is needed. No downloads happen in `convert_model.py` itself.
Do not install Toolkit2, ONNX, Torch, or this host environment in the app's
device wheelhouse. Existing platform runtime packages remain firmware-owned.

## 3. Inspect ONNX and write its preprocessing contract

```bash
uv run --no-project --python /tmp/recamera-rknn-env/bin/python \
  python scripts/convert_model.py inspect /path/to/model.onnx
```

This runs the ONNX checker and lists IR/opsets, tensor names/shapes/types,
operators and SHA-256 values, including external weights in the same directory
tree. It cannot prove that every operator compiles on the NPU. For the generic
helper, export one **float32, static, batch-1, NCHW three-channel image input**.
Multiple outputs are supported. Dynamic/multiple/non-image inputs and special
quantization use the selected official recipe instead of guessed defaults.
For multiple inputs, also read [runtime backend and managed integration prerequisites](kit-app-patterns.md#multi-input-models-and-backend-selection):
successful conversion does not extend the App Center loader/authorization
contract, and the single-input ctypes/shared DMA optimization does not apply.

Write `recipe.json` **outside the app tree**. This example applies only to a
model trained/exported for RGB pixels normalized by 255, with this exact input:

```json
{
  "input_name": "images",
  "input_layout": "NCHW",
  "input_shape": [1, 3, 640, 640],
  "color": "RGB",
  "mean": [0, 0, 0],
  "std": [255, 255, 255],
  "resize": {"mode": "letterbox", "pad_value": 114}
}
```

The helper supports `RGB`/`BGR`, ONNX `NCHW` and `letterbox`/`stretch` only.
Toolkit2 2.3.2 interprets the ONNX image input as NCHW in this conversion path;
NHWC graphs need an explicit re-export/adaptation and model-specific validation.
The runtime image buffer and Kit manifest are still NHWC, as described below.
Crop, affine, audio normalization and custom preprocessing also need a dedicated
recipe. Mean/std are in **model channel order**, with `(pixel - mean) / std`
baked into RKNN. The app then supplies raw uint8 pixels; it must not normalize
again. If normalization is already in the ONNX graph, use mean zero/std one
when that matches the graph's input contract.

Calibration and verification share the same resize/padding (Pillow bilinear).
Toolkit's image loader otherwise may resize differently from the app.
`quant_img_RGB2BGR` affects calibration image reading only: it does **not** put
a runtime channel swap in the exported model. The app must supply BGR itself
for BGR models. Kit/RGA's standard model input is RGB. RGA and Pillow rounding
can differ; compare real device input/output as part of hardware validation.

## 4. Build a non-quantized baseline, then INT8

```bash
uv run --no-project --python /tmp/recamera-rknn-env/bin/python \
  python scripts/convert_model.py convert \
  --onnx /path/to/model.onnx --recipe /path/to/recipe.json \
  --out /path/to/build/model-fp --quant fp --model-id det --task detect
```

The helper executes `config → load_onnx → build → export_rknn`, checks every
return code and releases Toolkit resources even on failure. `fp` sets
`do_quantization=False`; the usual `fp16` manifest label does not guarantee
every internal operation's precision. Each output directory must be new;
existing output is never overwritten or reported as a successful new build.
Toolkit scratch files go into a temporary directory, separate from the app.

For INT8, supply `calibration.txt` with one image path per line, relative to the
list file (absolute paths also work). The helper prepares resized lossless RGB
PNGs, records original/prepared image hashes and passes that dataset to Toolkit.
It does not silently use the Model Zoo's demonstration COCO subset.

```text
calibration/daylight-01.jpg
calibration/night-01.jpg
calibration/backlight-01.png
```

```bash
uv run --no-project --python /tmp/recamera-rknn-env/bin/python \
  python scripts/convert_model.py convert \
  --onnx /path/to/model.onnx --recipe /path/to/recipe.json \
  --out /path/to/build/model-i8 --quant i8 \
  --dataset /path/to/calibration.txt --model-id det --task detect
```

Use a representative dataset covering expected lighting, distances, backgrounds,
object sizes and negative scenes. Its size and selection depend on the model;
a fixed image count does not establish accuracy. Preserve the FP baseline and
evaluate INT8 on held-out data. For hybrid quantization, retain the official
step1/step2 configuration and selected tensor names in the build record; the
generic helper intentionally does not guess these settings.

## 5. Validate numbers separately from device behavior

To compare against ONNX Runtime, add held-out image(s) and explicit tolerances
to **either build command**, using another new output directory:

```text
--sample /path/to/held-out.jpg --sample /path/to/held-out-2.jpg --atol 0.01 --rtol 0.01
```

Those tolerance values illustrate syntax only: select bounds for the actual
model/task. The helper runs ONNX on normalized float32 input and the just-built
RKNN graph on raw uint8 NHWC input. It calls `init_runtime()` with **no target**
for the host simulator. Per the [Toolkit2 API reference](https://github.com/airockchip/rknn-toolkit2/blob/59a913d172e7f5ff03c9076e2ec7b1b1288ffd08/doc/03_Rockchip_RKNPU_API_Reference_RKNN_Toolkit2_V2.3.2_EN.pdf),
loading an exported `.rknn` with `load_rknn()` is insufficient for simulation;
build first in the same Toolkit instance.

Outputs are matched by ONNX graph order, with exact shapes and floating-point
values required. The tool does not guess transposes or dequantize unknown
integer outputs. An export whose output order/layout changes needs an explicit
model-specific mapping. Saved NPZ files and the report include max/mean
absolute error, cosine similarity and `allclose` results. Exit codes:

- `0`: compilation succeeded; inspect whether comparison ran.
- `1`: environment, contract, compiler or comparison-execution error.
- `2`: compiled, but at least one requested numerical comparison failed.

The report records **conversion**, **numerical_validation** and
**device_validation** independently. Device validation remains `not_run`.
A generated `.rknn` or high cosine similarity alone does not establish task
accuracy. Run the same decode/postprocessing/labels on ONNX, FP and INT8 and
measure task metrics (for example detection mAP, classification accuracy or OCR
accuracy), not just visually plausible results. Failed checks need investigation,
not relaxed tolerances merely to obtain a green report.

## 6. Add the artifact to an App Center package

A successful build produces `model.rknn`, `conversion-report.json` and
`manifest.fragment.json`; a requested comparison also produces output NPZs.
If that comparison fails, the diagnostic artifact/report remain but the manifest
fragment is withheld. Inspect `app_integration` for custom preprocessing needs.
Only the RKNN and required labels belong in the app. Keep the ONNX, toolchain,
dataset, logs and conversion reports in host build records.

After reviewing validation results, copy `model.rknn` to the fragment's
`models[].file` (for `--model-id det`, `app/models/det.rknn`). **Append** the
fragment's `models[]` and `artifacts[]` entries to the full v2 manifest; preserve
existing entries/configuration and resolve duplicate IDs/paths deliberately.
The fragment is not a complete manifest and must not replace one.

- The artifact has `kind: rknn`, `source: bundled`, matching `file`/`mount`,
  SHA-256 and positive size. Recompute these if the actual model changes.
- Declare `npu.rknn` with `mode: scheduled` and SDK permission `npu.infer`.
  Keep existing brokered/exclusive choices only after checking their runtime
  lane; do not silently change an existing app's resource ownership.
- Set an explicit `resources.limits.memory_mb` with room for all resident
  models, shared input/output buffers and the App's own working memory. Without
  it, the current authorization defaults to the sum of model reservations;
  shared buffers allocated for the first model can leave insufficient budget
  for a second model. For example, two 33 MiB reservations plus a few KiB of
  shared buffers exceed an implicit 66 MiB limit. Start from the closest App's
  measured budget and verify all models load together; do not use RKNN file
  size alone as peak memory or prescribe one fixed limit for every App.
- Keep the entry (such as `app.py`) at the app root. Add the model's actual
  `classes` list or bundled label file; never silently inherit COCO labels for
  a custom class set. `task` identifies the model/aliases, not its decoder.
- **Kit manifest `input` is `[1,H,W,C]`**, even when ONNX is NCHW. The helper
  generates that metadata. Standard primary `self.pre(frame)` currently uses
  one square size from `models[0].input[1]`; rectangular, BGR, stretch/crop,
  unusual padding or secondary-model inputs require matching custom app
  preprocessing. The generated fragment does not configure it automatically.
- For a compatible standard RGB square-letterbox detector, keep
  `model_frame = "hw-direct"`, `model_dma_input = True`,
  `prepared = self.pre(frame)` and `self.models.det.infer(prepared)` inside the
  normal `owns_loop = True` / `run(self)` lifecycle. Use ndarray/custom routes
  where the model contract needs them. Do not add removed `on_frame` callbacks
  or direct unleased `rknnlite` initialization.

Read [app-packaging.md](app-packaging.md) for the full v2 manifest, permissions
and platform dependencies, then run (ordinary skill/SDK environment is enough):

```bash
python scripts/validate_app.py --app-dir /path/to/app --mode package
python scripts/package_app.py --app-dir /path/to/app --out /path/to/dist
```

Offline package success verifies structure and hashes, not NPU execution. Use
[runtime-validation.md](runtime-validation.md) for archive smoke testing and
optional SSH upload/installation. When the user requests device testing,
install/launch through App Center/AppMgr and
its declared scheduled lane. Check model initialization, real-frame colors,
boxes/classes/keypoints, FP versus INT8 accuracy, loop/pre/post/model latency,
RSS over time, stop/restart and resource release. Hardware test scripts from
Model Zoo often create their own RKNN context; do not run those alongside
managed applications or bypass the firmware's NPU lease/scheduling protocol.

For startup failures use `scripts/diagnose_managed_app.py`. Match the installed
artifact hash and runtime version to the conversion report before attributing a
failure to quantization. A simulator pass cannot establish hardware latency,
memory stability or driver compatibility.
