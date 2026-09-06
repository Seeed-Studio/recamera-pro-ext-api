# Liveness v2 — offline calibration on public anti-spoofing datasets

Date: 2026-09-06. Host: macOS arm64, onnxruntime fp32 CPU. No device, no human
subject sampling. Produced by `evaluation/calibrate_offline.py` +
`tools/fit_liveness.py`.

The point of this run is to put numbers on the three `liveness_*` knobs that
were previously set from a single probe image: the fusion thresholds
(`liveness_t_live` / `liveness_t_spoof`), the motion noise floor
(`liveness_motion_noise_floor`), and whether depth is ready to carry weight.
Everything is measured with the SAME preprocessing code the device runs —
`scrfd.decode`, `liveness.crop_minifas`, `depth_liveness.planarity_from_depth`
— so the only differences from a device capture are the camera and the RV1126B
fp16 quantisation.

## Data

All three sets were pulled through `hf-mirror.com` (`HF_ENDPOINT`,
`huggingface_hub`); nothing was fetched from `huggingface.co` directly.

| Dataset | HF repo | License | What it is | Frames used | Tracks |
|---|---|---|---|---|---|
| NUAA Photograph Imposter DB (`raw`) | `akahana/anti-spoofing-nuaaaa` | not stated on the repo; NUAA's own terms are research-only | 640×480 webcam **full frames**. `ClientRaw` = genuine, `ImposterRaw` = a printed photo re-shot by the same webcam | 500 real + 500 print | 143 |
| CASIA-FASD (frames) | `akahana/anti-spoofing-casiafasd` | not stated on the repo; CASIA-FASD is research-only | 256×256 **loose face crops** decoded from the original clips. Attack type recovered from the filename (`<subj>_<clip>.avi_<frame>_<tag>.jpg`): clips 1/2/HR_1 real, 3–6/HR_2/HR_3 printed photo, 7/8/HR_4 screen replay | 205 real + 197 print + 189 screen | 252 |
| Axon Labs Replay Display sample | `AxonData/Display_replay_attacks` | **CC-BY-4.0** | modern phone/monitor replay, **full frames** decoded at 2 fps from 5 clips, plus 5 genuine stills | 5 real + 127 screen | 10 |

Totals after detection: **710 real / 697 print / 316 screen = 1723 rows**,
283 + 122 tracks. Sampling is round-robin over tracks inside each label, so no
single subject dominates.

Detection loss: NUAA 0/1000, CASIA-FASD 759/1350 (56%), Axon 25/157 (16%). The
CASIA-FASD loss is a property of the mirror, not of the dataset: its images are
256×256 crops in which the face fills most of the frame, and SCRFD-500M scores
those at 0.15–0.45 rather than the >0.9 it gives a normally-framed face. That
run used `--conf 0.20`; NUAA and Axon used the default 0.4.

### Why three sets and not one

CASIA-FASD is the only one of the three that has print AND screen attacks from
the same subjects and the same cameras as its genuine clips, so it is the only
place where the print-vs-screen comparison is not confounded by capture
conditions. But its images are crops, which costs the 4.0× crop and makes depth
meaningless. NUAA supplies full frames for real-vs-print, Axon supplies full
frames for screen. Every cross-dataset comparison below is marked as such.

## Feature results

`tools/fit_liveness.py --max-far 0.01 --holdout 0.3 --seed 7`, tracks split
whole. AUCs from the pooled three-dataset fit; the NUAA column is the
same-camera real-vs-print case.

| Feature | AUC (pooled fit) | AUC (pooled val) | AUC (NUAA only, val) | Threshold @ FAR ≤ 1% (pooled fit) |
|---|---|---|---|---|
| `P_tex_v2` (MiniFASNet 2.7×) | 0.9970 | 0.9899 | 1.0000 | 0.358 → TPR 0.958 |
| `P_tex_v1se` (MiniFASNet V1SE 4.0×) | 0.9956 | 0.9901 | 0.9994 | 0.441 → TPR 0.932 |
| **texture ensemble** (mean of the two) | **0.9975** | **0.9933** | **0.9997** | **0.318 → TPR 0.963** |
| `motion_residual` (static jitter) | 0.4918 | 0.6122 | 0.5000 | — |
| `correlation` (static jitter) | 0.4963 | 0.5000 | 0.4944 | — |
| `motion_score` (static jitter) | 0.5026 | 0.3855 | 0.4331 | — |
| `depth_score` (MiDaS v2.1 small planarity) | 0.4263 | 0.4357 | 0.6492 | — |
| fused (texture + motion) | 0.9972 | 0.9903 | 0.9997 | 0.287 → TPR 0.967 |
| fused + depth | 0.9928 | 0.9816 | 0.9954 | 0.407 → TPR 0.961 |

Motion and depth sitting at chance is the expected result, not a failure — see
the two sections below.

### Texture is the whole discriminator, and screen is the hard case

Per-frame ensemble score, all 1723 rows pooled:

| ensemble ≥ t | FRR (real) | FAR (print) | FAR (screen) | FAR (all spoof) |
|---|---|---|---|---|
| 0.30 | 0.031 | 0.0029 | 0.0949 | 0.0316 |
| 0.40 | 0.044 | 0.0014 | 0.0728 | 0.0237 |
| 0.50 | 0.059 | 0.0000 | 0.0380 | 0.0118 |
| 0.60 | 0.089 | 0.0000 | 0.0222 | 0.0069 |
| **0.65** (current `t_live`) | 0.111 | 0.0000 | 0.0063 | 0.0020 |
| 0.80 | 0.176 | 0.0000 | 0.0032 | 0.0010 |

Printed photos are destroyed: zero false accepts across 697 print frames at any
threshold from 0.50 up. Screen replay is 10–30× harder at every threshold, and
it is what sets the operating point. Median ensemble score by class and set:

| set | real | print | screen |
|---|---|---|---|
| NUAA (full frame) | 0.985 | 0.000 | — |
| CASIA-FASD (crops) | 0.972 | 0.001 | 0.006 |
| Axon (full frame) | 0.994 | — | 0.025 (p95 **0.602**) |

The Axon p95 of 0.602 is the number to worry about: on full-frame, well-lit,
modern-phone replays a non-trivial tail of frames reads as live to the texture
head alone.

### The device does not decide per frame — track-level numbers

`liveness_temporal` folds texture through an EMA (`alpha=0.4`, `min_samples=3`)
before thresholding. Replaying that EMA over each track's frames (237 tracks
with ≥3 frames: 68 real, 127 print, 42 screen):

| `t_live` | FRR (real tracks) | FAR (print) | FAR (screen) | FAR (all spoof) |
|---|---|---|---|---|
| 0.35 | 0.000 | 0.000 | 0.024 | 0.006 |
| 0.45 | 0.029 | 0.000 | 0.000 | 0.000 |
| **0.55** | **0.044** | **0.000** | **0.000** | **0.000** |
| 0.60 | 0.088 | 0.000 | 0.000 | 0.000 |
| 0.65 (current) | 0.118 | 0.000 | 0.000 | 0.000 |

0.45–0.55 holds zero false accepts over 169 spoof tracks while cutting the
false-reject rate from 11.8% to 3–4%. 0.55 is the recommendation: it keeps a
0.10 margin over the last threshold that admitted a spoof track.

### The 2.7×/4.0× ensemble collapses whenever the face is large

`liveness.get_expanded_box` clamps the requested scale so the expansion fits
the image: `scale = min((H-1)/box_h, (W-1)/box_w, scale)`. A 4.0× crop of a
face of height `h` needs `h ≤ (H-1)/4` — 120 px in a 480-row frame. Measured
fraction of rows where V2 and V1SE clamped to the *same* effective scale:

| set | median face px | frame height | rows with identical crops |
|---|---|---|---|
| NUAA | 158 | 480 | 68% |
| CASIA-FASD | 140 | 256 | 100% |
| Axon | 890 | 1080–1920 | 100% |

On the majority of this data the "ensemble" is two different networks looking at
one identical crop, not the wide-context/narrow-context pair the design intends.
It still helps (ensemble AUC beats both members), but the V1SE context argument
is not being tested here. On device this is a *framing* constraint: the 4.0×
view only exists when the face occupies less than a quarter of the frame height.

### Motion: this run measures the noise floor, nothing else

Every image here is a still, so `motion_residual` cannot contain live micro-
motion. What it does contain is the detector's own noise: each image was
re-detected 5× under a random ±2 px translation and ±5% brightness change, and
`liveness_temporal.update_motion` was run over the resulting five-point series.
That is exactly the signal a printed photo or a static screen produces, which
makes it the right basis for `liveness_motion_noise_floor` and for nothing else.

Pooled over all 1718 rows with a residual:

```
motion_residual   p50=0.00202  p90=0.00388  p95=0.00451  p99=0.00609  max=0.04788
correlation       p50=-0.241   p90=-0.086   p95=-0.027   p99=0.104    max=0.836
```

**The current floor of 0.003 is below the p78 of pure detector jitter**: 21.3%
of these still images produce a residual above it, so on 1 frame in 5 a photo
gets a non-None `motion_score` and is scored as if it had moved.

| floor | fraction of STILL images that still register motion |
|---|---|
| 0.003 (current) | 0.213 |
| 0.004 | 0.091 |
| 0.005 | 0.029 |
| **0.006** | **0.011** |
| 0.007 | 0.006 |

`correlation_low = 0.15` is confirmed by the same data from the other side: the
p99 of the static correlation is 0.104, so the `m_corr` term is already pinned
at 0 for a still subject. Leave it, and leave `correlation_high = 0.65`.

### Depth is not ready to carry weight — and on screens it is inverted

`depth_score = 1 - planarity` should be LOW for a flat attack. Measured median
by class, MiDaS v2.1 small at 256×256:

| class | median `depth_score` | implied `residual_ratio` (= 0.03 × score) |
|---|---|---|
| real | 0.525 | 0.0158 |
| print | 0.423 | 0.0127 |
| **screen** | **1.000** | **≥ 0.0300 (saturated)** |

Screen replays come out as the *least* planar class in the whole set — the
opposite of the premise. Pooled AUC is 0.426, i.e. worse than chance, and adding
depth to the fusion lowers every AUC it touches (0.9972 → 0.9928 fit,
0.9903 → 0.9816 validation). Two causes, both about framing rather than about
the metric:

1. On the Axon replay clips the face fills the frame (median 890 px). MiDaS
   predicts ordinary face relief for a large face whatever surface it is on, and
   the p95−p5 denominator is small because there is no background left, so the
   ratio saturates.
2. On CASIA-FASD there is no scene at all — the image *is* the face box.

`depth_liveness.py` already says `RESIDUAL_FULL_SCALE = 0.03` must be
recalibrated on device captures. This run does not supply that recalibration; it
supplies the evidence that 0.03 is *saturating* (p95 of `residual_ratio` is
0.0300 for real and screen alike, i.e. clipped) and that the constant is
probably too small by roughly 2×. `liveness_depth_enabled` must stay false.

## Suggested configuration

```json
{
  "liveness_t_live": 0.55,
  "liveness_t_spoof": 0.35,
  "liveness_motion_noise_floor": 0.006,
  "liveness_motion_high": 0.020,
  "liveness_correlation_low": 0.15,
  "liveness_correlation_high": 0.65,
  "liveness_w_texture": 0.70,
  "liveness_w_motion": 0.30,
  "liveness_w_depth": 0.20,
  "liveness_depth_enabled": false
}
```

This is **not** what `--output-thresholds` wrote
(`data/thresholds-offline.json` says `t_live = 0.2873`). That file reports the
lowest per-FRAME fused threshold whose false-accept rate is ≤ 1%, and the device
does not decide per frame — it thresholds a 3-sample EMA. The track-level sweep
above is the matching measurement, and it says a much higher threshold is free:
0.55 costs nothing in false accepts and buys back 7 points of FRR against the
current 0.65.

Changes against the current defaults in `liveness_temporal.LivenessConfig`:

| key | current | suggested | why |
|---|---|---|---|
| `liveness_t_live` | 0.65 | **0.55** | zero spoof-track false accepts over 169 spoof tracks at both values; 0.55 cuts real-track FRR from 11.8% to 4.4% |
| `liveness_t_spoof` | 0.45 | **0.35** | keeps the 0.20 hysteresis band width the tool assumes |
| `liveness_motion_noise_floor` | 0.003 | **0.006** | 21.3% of still images clear 0.003; 1.1% clear 0.006 |
| `liveness_w_depth` | 0.20 | 0.20, **disabled** | depth AUC 0.43 here; do not enable until recalibrated on device |
| `depth_liveness.RESIDUAL_FULL_SCALE` | 0.03 | **leave at 0.03, recalibrate on device** | saturated on this data; the right value needs frames with real scene context |

`liveness_motion_high = 0.020` is unchanged and **untested**: the upper end of
the motion ramp is defined by live micro-motion, which no still-image dataset
contains.

## Limitations

Read every number above with these attached.

1. **No live micro-motion, so no motion discrimination was measured.** The
   motion AUC of ~0.5 means "these are all stills", not "motion does not work".
   `liveness_motion_high` and `w_motion` cannot be fitted offline at all.
2. **No blink.** `EAR` is null and `blink` false on every row. The blink path in
   `fuse_liveness` — the strongest positive the system has — is untested here.
3. **Different cameras.** None of these frames came from the reCamera Pro's
   sensor, its ISP, or its exposure behaviour. NUAA is a 2010-era 640×480
   webcam; CASIA-FASD is 2012-era; only the Axon clips are modern phone capture.
   MiniFASNet is sensitive to sensor noise and moiré, which are exactly the cues
   that differ most between cameras.
4. **fp32 CPU, not fp16 NPU.** The RV1126B quantisation error is not included.
   The existing fp16-vs-onnxruntime cross-checks in
   `evaluation/depth-model-selection.md` are for the depth head only.
5. **The 4.0× crop was mostly not exercised** (63–100% of rows clamped to the
   same box as 2.7×), so V1SE's context contribution is not measured.
6. **CASIA-FASD detection loss is 56%**, and the surviving 44% are the frames
   SCRFD found easiest — a selection effect that plausibly favours cleaner,
   better-lit frames in every class.
7. **Only 5 genuine full-frame samples share a capture setup with the screen
   attacks** (Axon). The pooled real-vs-screen comparison is mostly
   cross-dataset and therefore partly a camera comparison.
8. **Licenses.** Only the Axon sample is openly licensed (CC-BY-4.0). NUAA and
   CASIA-FASD are research-use datasets redistributed by third parties on the
   Hub with no license field; they are used here for internal threshold fitting
   and nothing derived from them is redistributed.

**A device recheck is required before shipping these numbers**: a printed photo
and a phone screen held at the working distance, plus a real face, captured
through `liveness_capture` and re-fitted with the same `tools/fit_liveness.py`
invocation. This run narrows the search — it does not replace that capture.

## Reproducing

```bash
export HF_ENDPOINT=https://hf-mirror.com
cd apps/face-recognition/evaluation

# data (into evaluation/data/, gitignored)
uv run --with huggingface_hub python - <<'PY'
from huggingface_hub import hf_hub_download, snapshot_download
hf_hub_download('akahana/anti-spoofing-nuaaaa','nuaaaa.tar.gz',repo_type='dataset',local_dir='data/nuaa_meta')
hf_hub_download('akahana/anti-spoofing-casiafasd','casiafasd.tar.gz',repo_type='dataset',local_dir='data/casiafasd_meta')
hf_hub_download('unity/inference-engine-midas','models/model-small_opset19.onnx',local_dir='data/midas')
snapshot_download('AxonData/Display_replay_attacks',repo_type='dataset',local_dir='data/axon')
PY
tar xzf data/nuaa_meta/nuaaaa.tar.gz -C data/nuaa
tar xzf data/casiafasd_meta/casiafasd.tar.gz -C data/casiafasd
# Axon clips -> data/axon_frames/<label>/<clip>/f_%03d.jpg at 2 fps (ffmpeg)

R="uv run --with onnxruntime --with opencv-python-headless --with numpy python"
$R calibrate_offline.py --dataset nuaa      --root data/nuaa      --limit 500 --conf 0.40 --out data/rows-nuaa.jsonl
$R calibrate_offline.py --dataset casiafasd --root data/casiafasd --limit 450 --conf 0.20 --out data/rows-casiafasd.jsonl
$R calibrate_offline.py --dataset flat      --root data/axon_frames --limit 200 --conf 0.40 --out data/rows-axon.jsonl

uv run --with numpy python ../tools/fit_liveness.py \
    data/rows-nuaa.jsonl data/rows-casiafasd.jsonl data/rows-axon.jsonl \
    --max-far 0.01 --holdout 0.3 --seed 7 \
    --output-thresholds data/thresholds-offline.json
```

Texture ONNX comes from `face_rec_api/models/onnx/` (`liveness_minifasnet.onnx`,
`liveness_minifasnet_v1se.onnx`); the detector from
`models/convert/det_500m_640.onnx`.

## Changes to `tools/fit_liveness.py`

Two, both minimal:

- `depth_planarity` / `depth_score` added to `FEATURES`, a `depth_score` ROC and
  a `fused_with_depth` ROC added to `report()`. Captures taken with no depth
  model still fit — the columns arrive as NaN and each ROC drops only its own
  missing rows.
- `roc_threshold` sorted the ROC points by FPR alone (`argsort(..., stable)`),
  which leaves ties in candidate order — that is TPR-**descending**, so a
  perfectly separable feature ends its FPR==0 run at TPR==0 and the trapezoid
  rule charges the curve for a descent that is not in it. Measured: AUC 0.917
  instead of 1.0 on a 6+6-row perfectly separated set. Now `np.lexsort((tpr, fpr))`.
  `tests/test_liveness_temporal.py` and `tests/test_depth_liveness.py` (47 cases)
  pass unchanged.
