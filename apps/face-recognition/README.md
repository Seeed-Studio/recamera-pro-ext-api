# face-recognition

On-device 1:N face identification for reCamera Pro (RV1126B). SCRFD-500M detects
faces and five landmarks, each face is warped to the canonical 112x112 ArcFace
pose and embedded to 512-D, and the embedding is matched against an enrolled
gallery by cosine similarity. Optional liveness: a two-model MiniFASNet texture
ensemble, similarity-compensated five-point motion, and sampled FaceMesh blink
detection, fused per track.

设备端 1:N 人脸识别。SCRFD-500M 检测人脸与五点关键点，对齐到 112x112 后提取 512 维
特征，与已注册人脸库做余弦比对。可选活体检测：双模型 MiniFASNet 纹理集成、经相似变换
补偿的五点运动、抽样 FaceMesh 眨眼，按跟踪融合。

## Models

| id | file | input NHWC | preprocessing | output |
|---|---|---|---|---|
| `scrfd` | `models/scrfd500m_640_fp16.rknn` | `[1,640,640,3]` RGB | `(x-127.5)/128`, baked in | 9 tensors: score/bbox/kps per stride 8/16/32 |
| `arcface` | `models/arcface_mbf_fp16.rknn` | `[1,112,112,3]` RGB | `(x-127.5)/127.5`, baked in | `[1,512]`, L2-normalized by the app |
| `liveness` | `models/liveness_minifasnet_fp16.rknn` | `[1,80,80,3]` **BGR** | none | `[1,3]` logits, `softmax[1] = P(real)` |
| `liveness_v1se` | `models/liveness_minifasnet_v1se_fp16.rknn` | `[1,80,80,3]` **BGR** | none | `[1,3]` logits, `softmax[1] = P(real)` |
| `facemesh` | `models/face_landmark_fp16.rknn` | `[1,192,192,3]` RGB | none | `[1,1404]` landmarks + `[1,1]` presence |

All take **uint8** — the normalization is inside the rknn graph. The SCRFD
output ORDER is not a contract; `scrfd.py` maps the nine tensors by shape
(rows 12800/3200/800 → stride 8/16/32, cols 1/4/10 → score/bbox/kps).

The two MiniFAS heads differ only in crop: `liveness` sees a **2.7x** expansion
of the detection box, `liveness_v1se` a **4.0x** one, so the second also sees
whatever frames the face — a phone bezel, a paper edge. Each crop is cut fresh
from the frame; the wide view is not a resize of the narrow one. `P_texture` is
the arithmetic mean of the two `softmax[1]` values.

The three liveness models are optional: leave the files out and keep
`liveness_enabled` false. Note that the kit currently preloads every declared
model regardless of `optional`, so a *declared* file that is missing will stop
startup — remove the entry from `manifest.json` as well if you ship without it.

## HTTP command interface

Bound to `127.0.0.1:<cmd_port>` (default 8126; 8125 is reserved by the result hub). Enrollment writes the gallery,
so this is a loopback control channel. `cmd_port: 0` disables it.

```bash
# enroll from the live camera: average 5 embeddings of the biggest centred face
curl -s localhost:8126/cmd -d '{"op":"enroll","name":"alice","source":"camera","frames":5}'

# enroll from a still (must contain exactly one face)
curl -s localhost:8126/cmd -d "{\"op\":\"enroll\",\"name\":\"alice\",\"source\":\"image\",\"image_b64\":\"$(base64 -w0 alice.jpg)\"}"

curl -s localhost:8126/cmd -d '{"op":"remove","name":"alice"}'
curl -s localhost:8126/cmd -d '{"op":"list"}'
curl -s localhost:8126/cmd -d '{"op":"reload"}'    # re-read the gallery from disk
curl -s localhost:8126/gallery                     # same as {"op":"list"}

# record calibration features for 20 s (label: real | print | screen)
curl -s localhost:8126/cmd -d '{"op":"liveness_capture","label":"real","seconds":20}'
```

Every response: `{"op", "ok", "model_tag", "users", ...}`, plus `"err"` when
`ok` is false. Both enroll sources execute **inside the frame loop** (the NPU is
single-core, and `camera` needs frames only the loop has); the HTTP request
blocks up to 15 s waiting for the result.

## Result stream

`results[]` is `{box: pixel_xyxy, label: name | "unknown", score, cls: 0}` — the
kit's official adapter normalizes and burns these onto the OSD. `extra` adds:

```json
{"model_tag": "rv1126b:scrfd500m+mbf512@fp16", "enrolled": 3,
 "faces": [{"track_id": 1, "bbox": [0.12, 0.13, 0.32, 0.4], "det_score": 0.93,
            "name": "alice", "score": 0.61, "live": true, "liveness_score": 0.98,
            "stable": true, "gated": false, "reason": null,
            "liveness": {"score": 0.93, "texture": 0.98, "motion": 0.42,
                         "blink": true, "decision": "live",
                         "reason": "texture+motion"}}]}
```

`live` and `liveness_score` are unchanged for existing consumers: `live` is a
nullable bool, and `liveness_score` is the **texture EMA** — deliberately not
the fused score, which mixes in motion and would silently change meaning.

`liveness.decision` is `pending` | `live` | `spoof`:

* **pending** until at least `liveness_min_samples` texture samples AND either
  usable motion, a blink, or `liveness_timeout_sec` elapsed. While pending the
  app publishes **no name** and spends no embedding — a name published before
  the verdict settles cannot be un-published by the verdict.
* **live** immediately on a blink, which latches for the life of the track. The
  absence of a blink is never evidence of a spoof.
* **spoof** clears the track's accumulated identity evidence, so a phone screen
  cannot accumulate its way to a match.

Motion is the residual left after the best similarity transform (scale +
rotation + translation) between consecutive five-point sets is removed: a photo
waved at the lens is rigid, so the fit explains it and only detector jitter
survives. Residuals are normalised by the face's short side, and a residual at
or below `liveness_motion_noise_floor` is reported as `null` and **dropped from
the fusion** rather than scored as zero — standing still is not a spoof.

## Liveness calibration

`liveness_capture` appends one row per eligible face per frame to
`<app_dir>/liveness_capture.jsonl` — model outputs and box geometry only, no
pixels, no embeddings, no names. Both texture heads run every frame during a
capture so the file cannot fill with repeated cached values.

```json
{"P_tex_v2": 0.73, "P_tex_v1se": 0.69, "motion_residual": 0.0061,
 "correlation": 0.42, "EAR": 0.24, "blink": false, "face_px_size": 118.0,
 "track_id": 7, "label": "real", "ts": 1788680000.125}
```

Record one capture per label, then fit offline (stdlib + numpy):

```bash
python3 tools/fit_liveness.py real.jsonl print.jsonl screen.jsonl \
    --max-far 0.01 --holdout 0.3 --output-thresholds thresholds.json
```

It reports ROC/AUC, the Youden operating point and the threshold at a stated
false-accept bound for each feature and for the fused score, and writes the
suggested `liveness_*` config keys. `--holdout` partitions **whole tracks**:
consecutive rows from one track are near-duplicates, so a row-level split
reports an ROC the device will not reproduce.

The shipped motion thresholds and fusion weights are provisional. SCRFD jitter
varies with face size, exposure and pose, so calibrate on a capture recorded
through the installed camera before relying on the numbers.

## Gallery

Managed default path is `${APPMGR_APPDATA_DIR:-/userdata/local/appdata}/face-recognition/gallery/<sanitized model_tag>.json`;
`FACE_GALLERY_DIR` or the legacy `gallery_path` config overrides the directory. Written atomically (temp file +
rename).

```json
{"_meta": {"model_tag": "rv1126b:scrfd500m+mbf512@fp16", "dim": 512,
           "threshold": 0.4, "updated": "2026-01-01T00:00:00+00:00", "format": 2},
 "users": {"alice": {"emb": [512 floats], "n": 5, "ts": "..."}}}
```

**The `model_tag` gate is not cosmetic.** The ArcFace weights differ per
accelerator, so an embedding enrolled under one tag scores cosine ≈ 0 against
another — and that failure is silent (nobody matches, no error). Loading a
gallery whose `_meta.model_tag` or `dim` does not match the running model raises
`GalleryMismatch`; the app then runs with an EMPTY gallery and puts
`extra.gallery_error` on every frame rather than quietly recognising no one.

## Deploy

1. Put the `.rknn` files in `models/` (the three liveness models only if
   `liveness_enabled` will be true; see Models above).
2. Build and install a signed v2 package using the
   [App Center publishing guide](../../docs/guide/app-center-publishing.md).
3. Start the installed app through appmgr (device permissions apply):

```bash
python3 -m appmgr start face-recognition
```

## Tests

Hardware-free — the frame source and the rknn engine are stubbed, and nothing
imports `rknn` or `librknnrt`.

```bash
uv run pytest apps/face-recognition/tests -q
```

## Measured on device (2026-09-05)

RV1126B, FP16 models: **7.9 fps** with one face (inference 102 ms avg), live recognition score 0.85–0.87,
device fp16 embedding vs fp32 reference cosine 0.9999. Liveness on: 7.4 fps, not yet validated at close range.
Details and raw logs remain in the evaluation materials on the original
source branch [`55a852d`](https://github.com/Seeed-Studio/recamera-pro-ext-api/tree/55a852d/apps/face-recognition/evaluation/README.md).
