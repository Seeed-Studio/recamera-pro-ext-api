# Monocular depth model selection — reCamera Pro (RV1126B)

Goal: replace FastDepth with a more accurate monocular depth head for **plane
detection** in face liveness. A printed photo or a phone-screen replay is one
flat surface; a live face is not. Only the *structure* of relative depth inside
the face box matters — absolute scale is never used — under a latency budget of
about 150 ms per frame on a single-core 3 TOPS NPU with 2 GB of system RAM.

Date: 2026-09-06. Toolkit: rknn-toolkit2 2.3.2 on `wsl2-local`, matching the
device's librknnrt 2.3.2. Device: reCamera Pro at `192.168.3.53` (adb, root).

## Candidates

| | MiDaS v2.1 small | Depth Anything V2 Small | FastDepth |
|---|---|---|---|
| Backbone | EfficientNet-Lite3, pure CNN | ViT-S (DINOv2) + DPT head | MobileNet + NNConv5 |
| ONNX source | `unity/inference-engine-midas` → `models/model-small_opset19.onnx` (hf-mirror) | `onnx-community/depth-anything-v2-small` → `onnx/model.onnx` (hf-mirror) | not obtained |
| Input (static) | 1×3×256×256 | 1×3×252×252 (14 multiple) | — |
| ONNX parameters | 16.68 M | 24.71 M | ~3.9 M (published) |
| Ops | Conv/BN/Clip/Relu/Resize/Pad only | MatMul, Softmax, Erf, ReduceMean, ConvTranspose, Resize | — |
| RKNN FP16 convert | **OK** | **OK** | not attempted |
| .rknn size | 33 736 622 B | 51 530 245 B | — |
| Weight memory | 32 471 KB | 47 999 KB | — |
| Internal memory | 10 272 KB | 9 234 KB | — |
| PC-sim vs onnxruntime Pearson (3 images) | 0.999989 / 0.999990 / 0.999989 | 0.999976 / 0.999999 / 0.999998 | — |
| Device latency, 20 infers | **mean 50.73 ms, max 65.15 ms** (min 41.53, median 48.99) | mean 160.69 ms, max 175.46 ms (min 135.28, median 166.56) | — |
| Model load time | 185.2 ms | 257.1 ms | — |
| Within 150 ms budget | yes, 3x headroom | **no** — mean already over budget | — |

FastDepth was not evaluated: no ready ONNX export was found on the mirrors, and
the published checkpoint is a PyTorch/TVM artifact that would need its own
export pass. It is the SG2002 baseline on a different SoC, so it is a reference
point rather than a candidate here. Both models below beat it on parameter count
and on published KITTI/NYU accuracy, which is the reason for the swap.

## Conclusion

**Ship MiDaS v2.1 small at 256×256, FP16.**

- 50.7 ms mean / 65.2 ms max leaves room for SCRFD + ArcFace + MiniFASNet in the
  same frame budget; Depth Anything V2 Small alone consumes the entire 150 ms.
- Both convert cleanly and both reproduce onnxruntime to Pearson > 0.9999 in the
  PC simulator, so FP16 conversion costs nothing measurable in either case. The
  ViT operator set (MatMul / Softmax / Erf / LayerNorm decomposition) is
  supported by rknn-toolkit2 2.3.2 for `rv1126b` — that question is settled, and
  the decision is purely latency.
- On the planarity metric the two models separate a flat surface from a face by
  the same order of magnitude (table below), so the 3.2x latency of Depth
  Anything buys nothing for this task.

Keep the Depth Anything V2 Small `.rknn` around: if the plane test later needs
finer structure than MiDaS provides, it is a drop-in at a known cost.

## Conversion

Both models were converted with the shared `convert.py` on `wsl2-local`
(`~/face-rknn-convert`), `--quant fp16`, `--platform rv1126b`. Logs are in
`models/convert/`.

Normalisation differs between the two and matters:

- **MiDaS** `model-small_opset19.onnx` has the ImageNet normalisation **inside
  the graph** (`Sub` with `[0.485, 0.456, 0.406]`, `Div` with
  `[0.229, 0.224, 0.225]` as nodes 1 and 3). The graph therefore expects RGB in
  [0, 1], so the RKNN normalisation is `--mean 0,0,0 --std 255,255,255` and the
  app feeds raw uint8 RGB.
- **Depth Anything V2** expects externally normalised input, so the ImageNet
  constants are baked into the RKNN instead:
  `--mean 123.675,116.28,103.53 --std 58.395,57.12,57.375`.

Depth Anything's ONNX ships with dynamic `batch_size` / `height` / `width`;
`make_dim_param_fixed` + `fix_output_shapes` pinned it to 1×3×252×252 before
conversion (252 = 18×14, the ViT patch grid). The exported output shape strings
stay symbolic in the ONNX metadata, which is cosmetic — the built graph is
static and the runtime returns (1, 252, 252).

## Simulator cross-check

PC simulator (`load_onnx` + `build` + `init_runtime()` with no `target`) against
onnxruntime CPU, same uint8 NHWC input path, on three real images: the probe
frame `probe_003301.jpg`, its face inset rescaled to fill the frame, and an
unrelated scene photo. Pearson correlation of the full depth map, threshold 0.98:

| image | MiDaS | Depth Anything V2 S |
|---|---|---|
| probe (2D photo on grey mount) | 0.999989 | 0.999976 |
| face filling the frame | 0.999990 | 0.999999 |
| scene | 0.999989 | 0.999998 |

## Planarity metric

`apps/face-recognition/depth_liveness.py` (pure numpy) fits a plane
`d ~ a*x + b*y + c` over the face box by least squares and reports:

- `residual_ratio` — RMS residual divided by the depth map's p95−p5 range.
- `planarity` — `clip(1 - residual_ratio / 0.03, 0, 1)`; 1.0 = flat.
- `relief` — peak-to-peak residual over the same range; catches a nose that RMS
  averages away.
- `score` — `1 - planarity`, the liveness-facing value (higher = more live).

Fitting a plane rather than looking at raw depth range is what makes a photo
held at an angle read as planar: the tilt is absorbed by the `a*x + b*y` terms.

**The denominator is the frame's depth range, not the in-box spread.** In-box
normalisation is degenerate exactly where it matters: over a flat textureless
surface the residual *is* the spread. Measured on the grey photo mount in the
probe frame, MiDaS gives residual/in-box-std = 0.383 — indistinguishable from
the 0.983 of a real face — while residual/frame-range gives 0.0048 against
0.0524, a factor of 11.

### Demonstration

`evaluation/depth_planarity_demo.py`, run over the simulator depth maps in
`models/depth_out/`:

```
=== midas_v21_small_256 ===
case                                planarity   relief   score resid/range       n
face inside pasted 2D photo            0.0000    0.935  1.0000      0.0524    2688
grey mount (true plane control)        0.8411    0.025  0.1589      0.0048    1600
same face filling the frame            0.0177    0.441  0.9823      0.0295   11900

=== dav2_small_252 ===
case                                planarity   relief   score resid/range       n
face inside pasted 2D photo            0.0000    0.363  1.0000      0.0539    2632
grey mount (true plane control)        0.8374    0.031  0.1626      0.0049    1521
same face filling the frame            0.0000    0.713  1.0000      0.0533   11466
```

**Read this with the caveat that it does not yet demonstrate spoof rejection.**
`probe_003301.jpg` is a face photo *digitally* pasted onto a grey field, not a
photograph of a printed photo. The depth network sees an ordinary face and
predicts ordinary face relief for it — which is why the "face inside pasted 2D
photo" row scores as live. What the run does establish:

1. The metric is not fooled by texture: the grey mount, a genuine flat surface
   with no gradient to fit, scores 0.84 planarity against 0.00–0.02 for a face.
   The separation is a factor of 10 in `residual_ratio` and 20–40 in `relief`.
2. Both models produce the same ordering with the same margins, so MiDaS loses
   nothing structurally against the 3.2x more expensive model.

The `RESIDUAL_FULL_SCALE = 0.03` constant is set from these three measurements
and **must be recalibrated on real device captures** — a physical printed photo
and a phone screen held in front of the camera, at the working distance — before
`score` is used as a hard gate. The end-to-end spoof numbers are the next work
item, not this one.

Unit tests: `tests/test_depth_liveness.py`, 14 cases, synthetic plane vs
synthetic sphere cap plus scale/sign/tilt invariance, NaN holes, bbox
image-space mapping, and the `depth_flatness` model wrapper. All pass.

## Artifacts

Not committed (`models/*.rknn` is gitignored):

- `models/depth_midas_v21_small_256_fp16.rknn` — md5 `8f479e945f8879b93010141274d6464a`
- `models/depth_dav2_small_252_fp16.rknn` — md5 `3dd93681e15192267c7a442a9bca6a5a`
- `models/convert/depth_midas_v21_small_256_fp16.convert.log`
- `models/convert/depth_dav2_small_252_fp16.convert.log`
- `models/depth_out/*.npy` — simulator depth maps used by the demo

On device both `.rknn` were pushed to `/userdata/local/bench/` with matching
md5, benchmarked, and left there. The face-recognition app was stopped for the
benchmark and restarted afterwards.

### Device benchmark note

`RknnModel` is constructed with **no `core_mask`** — RV1126B has a single NPU
core, and passing a mask is meaningless there. The face-recognition app holds
the NPU while it runs, so it must be stopped before benchmarking or the numbers
include contention.

Restarting it afterwards must **not** go through `adb shell`: adbd reaps the
detached child, so `restart.sh` reports "launched" and the process is gone
seconds later (measured twice, including through `setsid`). Run it over SSH
instead — `ssh root@<device> 'sh /userdata/local/apps/face-recognition/restart.sh'`
— where the process survives session exit.
