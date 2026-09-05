# face-recognition — on-device evaluation (reCamera Pro, RV1126B)

Date: 2026-09-04/05. Device: `recamera-pro-test` (RV1126B, 2 GB RAM, single-core NPU), official frame.sock 1280×720 NV12, RGA letterbox to 640×640.
Models: `scrfd500m_640_fp16.rknn`, `arcface_mbf_fp16.rknn`, `liveness_minifasnet_fp16.rknn` (all FP16, rknn-toolkit2 2.3.2). model_tag `rv1126b:scrfd500m+mbf512@fp16`.

## Throughput (ws sink, 30-message windows)

| Condition | fps | inference_time_ms avg / max | pipeline_ms avg |
|---|---|---|---|
| 1 face, liveness off | **7.9** | 102 / 122 | 110 |
| 1 face, liveness on | 7.4 | 104 / 112 | — |

Detector dominates (fp16 SCRFD at 640). Per-track embedding runs every `embed_interval=5` frames.

## Accuracy

| Check | Result |
|---|---|
| Same probe image, device fp16 NPU embedding vs Mac fp32 onnxruntime (same decoder/alignment code) | cosine **0.99992** |
| PC simulator vs onnxruntime, random inputs | SCRFD 9 outputs ≥ 0.99993, ArcFace ≥ 0.99999, liveness 1.0 |
| Live camera, person enrolled from 5 frames (0.77 s) then recognised | score **0.85–0.87**, `stable=true` (47×56 px face, `min_face_px` lowered to 40 for the test) |
| Cross identity | 0.006 |
| Anchor convention change (+0.5 → none), same person old vs new template | cosine 0.22 → galleries must be re-enrolled after that change |

## Liveness

MiniFASNet runs (fps 7.4) but at a 47 px face P(real) was 0.15–0.30 (judged spoof). Needs a re-test with a face ≥ 100 px before enabling by default.

Raw data: `smoke-ws-20260905.jsonl`, `run-20260905-recognize.log`, `gallery-device.json`, `fp32_ref.py` + `probe_003301.*`.
