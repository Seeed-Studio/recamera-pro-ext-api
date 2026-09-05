# face-recognition

On-device 1:N face identification for reCamera Pro (RV1126B). SCRFD-500M detects
faces and five landmarks, each face is warped to the canonical 112x112 ArcFace
pose and embedded to 512-D, and the embedding is matched against an enrolled
gallery by cosine similarity. Optional MiniFASNet anti-spoofing.

设备端 1:N 人脸识别。SCRFD-500M 检测人脸与五点关键点，对齐到 112x112 后提取 512 维
特征，与已注册人脸库做余弦比对。可选 MiniFASNet 活体检测。

## Models

| id | file | input NHWC | preprocessing | output |
|---|---|---|---|---|
| `scrfd` | `models/scrfd500m_640_fp16.rknn` | `[1,640,640,3]` RGB | `(x-127.5)/128`, baked in | 9 tensors: score/bbox/kps per stride 8/16/32 |
| `arcface` | `models/arcface_mbf_fp16.rknn` | `[1,112,112,3]` RGB | `(x-127.5)/127.5`, baked in | `[1,512]`, L2-normalized by the app |
| `liveness` | `models/liveness_minifasnet_fp16.rknn` | `[1,80,80,3]` **BGR** | none | `[1,3]` logits, `softmax[1] = P(real)` |

All three take **uint8** — the normalization is inside the rknn graph. The
SCRFD output ORDER is not a contract; `scrfd.py` maps the nine tensors by shape
(rows 12800/3200/800 → stride 8/16/32, cols 1/4/10 → score/bbox/kps).

`liveness` is optional: leave the file out and keep `liveness_enabled` false.

## HTTP command interface

Bound to `127.0.0.1:<cmd_port>` (default 8125). Enrollment writes the gallery,
so this is a loopback control channel. `cmd_port: 0` disables it.

```bash
# enroll from the live camera: average 5 embeddings of the biggest centred face
curl -s localhost:8125/cmd -d '{"op":"enroll","name":"alice","source":"camera","frames":5}'

# enroll from a still (must contain exactly one face)
curl -s localhost:8125/cmd -d "{\"op\":\"enroll\",\"name\":\"alice\",\"source\":\"image\",\"image_b64\":\"$(base64 -w0 alice.jpg)\"}"

curl -s localhost:8125/cmd -d '{"op":"remove","name":"alice"}'
curl -s localhost:8125/cmd -d '{"op":"list"}'
curl -s localhost:8125/cmd -d '{"op":"reload"}'    # re-read the gallery from disk
curl -s localhost:8125/gallery                     # same as {"op":"list"}
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
            "stable": true, "gated": false, "reason": null}]}
```

## Gallery

Default path `/userdata/local/face-gallery/<sanitized model_tag>.json`;
`FACE_GALLERY_DIR` overrides the directory. Written atomically (temp file +
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

1. Put the three `.rknn` files in `models/`.
2. Copy the app directory to `/userdata/local/apps/face-recognition`.
3. Run (inference needs root):

```bash
python3 -m kit.run /userdata/local/apps/face-recognition \
    --model models/scrfd500m_640_fp16.rknn --sink ws --port 8124
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
Details and raw logs in `evaluation/README.md`.
