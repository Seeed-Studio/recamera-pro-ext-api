# Kit Application Patterns

Read this reference for a model-backed application or when using the reusable
reCamera Pro `kit` package. The current kit deliberately supports one
application shape: the application owns the ordinary Python loop, while kit
owns common setup, frame access, preprocessing, model loading, output,
configuration reload, and teardown.

`kit` is part of the extension application runtime, not a standard component
of the device's base Python environment. Before classifying a kit app as
directly runnable, verify the kit runtime, its extension dependencies, the
manifest-declared model, the compatible RKNN runtime, and the required device
permissions. Otherwise classify the request as **Supported with
preconditions**.

The rules in this reference come from the current Kit implementation and
official-App conventions, not from a complete `recamera_ext` schema. In
particular, `owns_loop`, `run(self)`, and calling `super().setup(config)` are
derived from the current `kit.app.App`; the nearest current App remains the
authority for business-specific manifest fields and postprocessing.

## Required current application shape

The official `apps/yolo-detector/app.py` is the reference detector loop. A
model-backed detection App is four readable lines inside `run()`:

```python
from kit.app import App, run_app
from kit.runtime.postprocess.detect import postprocess
from kit import events as E


class MyApp(App):
    owns_loop = True
    # Only boxes are consumed, never frame.data pixels, so the source may
    # letterbox on RGA into frame.data itself. See "Frame cost and throughput".
    model_frame = "hw-direct"

    def run(self):
        for frame in self.frames():
            x = self.pre(frame)
            outs = self.models.det.infer(x.data)          # raw RKNN head tensors
            dets = postprocess(outs, x.info,
                               conf_thres=self.conf, iou_thres=self.iou)
            self.emit([E.detection(d) for d in dets], frame.pts, results=dets)


if __name__ == "__main__":
    run_app(MyApp())
```

`self.models` is populated from `models[]` in the manifest and addressed by its
manifest ID or an unambiguous task alias such as `self.models.det`. `self.conf`
and `self.iou` are auto-bound from the manifest `config_schema` and re-bound on
SIGHUP for `apply: "live"` items, so a plain detector needs no `setup` override.

**`infer()` returns raw RKNN output tensors, not detections.** A YOLO head
exports either one concatenated decoded tensor `[1, 84, 8400]` or several raw
per-stride branch tensors; neither is a list of result rows. You MUST pass the
outputs through the official `kit.runtime.postprocess.detect.postprocess`, which
handles both layouts, runs the DFL decode and NMS, and un-letterboxes back to
original frame pixels. Its signature is
`postprocess(outputs, info, conf_thres=0.25, iou_thres=0.45, input_size=640,
class_names=COCO80)` and it returns a list of
`{"box": [x1, y1, x2, y2], "cls": int, "cls_name": str, "score": float}` sorted
by score descending, with `box` in original-frame pixel `xyxy`.

Two recurring bugs that produce "inference runs but no box ever appears":

- Treating the raw `infer()` tensor as detection rows instead of calling
  `postprocess`. The frontend then receives no `results[].box`.
- Reading class names from a non-existent `prepared.classes`. There is no such
  attribute; classes come from the manifest (`models[].classes`) and are applied
  inside `postprocess` via `class_names`. Pass the geometry as `x.info` (the
  `PreparedInput.info` `LetterboxInfo`), never a hand-built dict.

For non-YOLO heads (pose, OCR, face mesh, QR) the postprocess module and the
result shape differ; follow the closest official app under `apps/` rather than
reusing `detect.postprocess`.

Do not generate legacy shapes. `kit.app` rejects a kit App that lacks
`owns_loop = True`, has a positional argument on `run`, or defines removed
callback hooks including `on_results`, `process_frame`, or `run_postproc`.

## Manifest resource root

Use the official package-root layout for model-backed Apps:

```text
app.py
models/helmet.rknn
labels.txt
manifest.json
```

Kit derives the application root from the directory of the manifest entry
module. If `entry` is `src/app/app.py` while `models[].file` is
`assets/model/helmet.rknn`, Kit looks under `src/app/` while AppMgr authorizes
the package-root path; the result is the device error `model is not an
authorized bundled artifact`. Keep the entry and all manifest-declared
resources relative to the same package root.

Do not compensate with `Path(__file__).parents[...]`. Kit loads
manifest-declared models itself and exposes them as `self.models`; generated
application code should not open or reconstruct model paths.

## Which example to adapt

| Need | Preferred application |
| --- | --- |
| Basic object detector | `apps/yolo-detector/` |
| Detection, tracking, occupancy, zones | `apps/retail-vision/` |
| Pose plus temporal state | `apps/fall-detection/` or `apps/fitness-trainer/` |
| Detection plus face analysis / hardware ROI | `apps/face-analysis/` |
| OCR cascade | `apps/ppocr-reader/` |
| QR code workflow | `apps/qrcode-reader/` |
| Mesh/landmark output | `apps/facemesh-reader/` |
| Speech path | `apps/voice-transcribe/` |

Adapt the narrowest example matching the requested data flow. Retain only the
parts required by the user; do not copy unrelated UI, models, configuration, or
business logic.

## Geometry and outputs

`self.pre(frame)` provides a model input and letterbox mapping. Model results
in model space must be mapped back to original frame geometry before external
results are emitted. For the current Kit detector/output contract, the
official browser-rendered `results[].box` values are original-frame pixel
`xyxy` coordinates and the manifest declares `coord: "pixel_xyxy"`. Do not
normalize those values unless the App deliberately emits normalized values and
declares `coord: "normalized_xyxy"`. This differs from direct
`recamera_ext.ResultSink`, whose boxes are normalized `[0, 1]` values.

Maintain this order:

```text
model-space output
  -> undo letterbox / map to source frame
  -> apply app logic such as tracking or zones
  -> emit results[].box in the manifest-declared Kit coordinate space
  -> emit with the source frame PTS
```

Do not normalize by model input dimensions after letterbox; padding makes that
incorrect. Ordinary browser box rendering uses `results[].box` plus the strict
`output`/`render.boxes` manifest contract. `render.stream_osd` is only needed
when the user explicitly needs boxes in RTSP or recording streams. Drawing a
rectangle with OpenCV on an app-owned image is not an official preview overlay.

## Frame cost and throughput

The default `model_frame = "cpu"` letterboxes inside the Python loop. That
Python resize costs roughly 40 ms/frame at 1280x720 -> 640x640, which is often
the dominant CPU cost in a detector that otherwise only consumes boxes. Choose
the mode that matches what the App actually reads:

| Mode | `frame.data` | Original-res pixels | Use when |
| --- | --- | --- | --- |
| `"cpu"` (default) | full-res RGB | yes | always correct; App crops or reads source pixels in Python |
| `"hw"` | full-res RGB | yes (RGA letterbox in `frame.model_data`) | model-backed App that still crops source pixels (ROI / perspective) after inference |
| `"hw-direct"` | the RGA letterbox | no | App consumes only detections/keypoints and never reads `frame.data`; cheapest path, also skips the full-res NV12->RGB conversion |
| `"hw-roi"` | the RGA letterbox | no (NV12 dma-buf kept reachable) | detect -> crop -> second-model cascade; take every ROI through `self.crop_roi_hw(...)` |

Moving the letterbox onto RGA measured about +49% end-to-end throughput in the
official detector. `frame.w`/`frame.h` and post-processed coordinates stay in
original camera geometry in every mode, so `postprocess` output is unchanged.
The hardware modes fall back to the CPU letterbox (identical geometry, never an
error) when `needs_model` is false, the backend exposes no dma-buf fd
(RTSP/snapshot), or RGA/librga is unavailable.

Throughput discipline for a generated detector:

- Prefer `"hw-direct"` when only boxes are consumed; do not keep `"cpu"` and pay
  the per-frame resize for pixels the App never reads.
- Do not run a needless BGR/RGB conversion per frame; `pre()` already returns
  model-ready input.
- Do not push full-resolution frames into a thread queue; copy only the small
  result or ROI data you actually need across threads.
- Rate-limit `emit`; publishing every frame at camera rate can saturate the
  WebSocket sink. Emit on change or on a bounded interval when the contract
  allows it.
- When tuning, measure NPU inference, CPU preprocess, postprocess, and output
  separately (kit already times the `pre`/infer budgets) instead of guessing
  which stage is slow.

## Lifecycle and configuration

Use `setup(config)` for state derived from configuration. An override must call
`super().setup(config)` before using kit-managed state. Keep frame-local work
in `run`. Let kit primitives do their intended jobs:

- `self.frames()` obtains frames through the configured adapter.
- `self.pre(frame)` prepares model input and geometry metadata.
- `self.models.<id>.infer(...)` invokes a manifest-loaded model. This is the
  only model invocation surface generated application code should use; current
  kit selects its own ctypes-backed `librknnrt.so` backend by default and
  keeps `rknnlite` only as a fallback.
- `self.emit(events, frame.pts, results=results)` publishes application events
  and SDK-compatible result data.
- Kit `emit` uses seconds; direct ResultSink uses
  microsecond `pts_us`. `self.emit(extra={"alarm": value})` creates the
  top-level field `alarm`, not `extra.alarm`.

Verify any additional kit helper against the current local `kit/app.py` and the
closest app before using it. Do not infer method names from an older guide or
invent convenience methods in generated code.

When editing an existing kit app, preserve its manifest contract and current
entry pattern unless the user specifically requests a coordinated configuration
change. Packaging is a separate Skill-defined workflow; read
`app-packaging.md` and do not treat its private dependency layout as current
AppMgr behavior. It does not install models, dependencies, or manifests on a
device.

## Code-generation checklist

Before returning a kit-based implementation, verify:

- `owns_loop = True` is present.
- `run(self)` has no positional arguments after `self`.
- No removed callback hook is defined.
- An overridden `setup(config)` calls `super().setup(config)`.
- A YOLO detector calls `kit.runtime.postprocess.detect.postprocess` on the raw
  `infer()` outputs and passes `x.info`; it never treats the raw tensor as
  result rows and never reads a non-existent `prepared.classes`.
- `model_frame` matches what the App reads (`"hw-direct"` when only boxes are
  consumed), and no per-frame BGR conversion or full-frame thread queue is
  introduced.
- The entry and manifest-declared models, labels, and artifacts share the
  package root, with no manual `Path(__file__).parents[...]` root derivation.
- Each requested model access maps to a declared manifest model ID or an
  unambiguous task alias.
- Output geometry returns to original-frame coordinates before normalization.
- Emitted output carries the matching frame PTS when it originated from a frame.
- A detector manifest declares `output.contract_version: 2`, `output.sink: "ws"`,
  a direct `box` field from `results[].box`, and `render.schema_version: 1`
  with `render.boxes` before it is considered browser-renderable.
- Any saved/queued image or probe data was copied before the next frame loop.
- The code makes no deployment, package-installation, direct camera-device, or
  internal-RPC assumption.
