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

## Liveness v2 (2026-09-06)

Texture ensemble (MiniFASNetV2 2.7x + V1SE 4.0x) + per-track EMA + 5-point
passive motion + FaceMesh blink + optional MiDaS depth. Costs measured with the
in-app stage timer (`liveness timing` every 30 frames):

| Stage | NPU / CPU | Cost |
|---|---|---|
| FaceMesh 192 (fp16 / int8) | NPU | 16 ms / 7 ms per call; gated to faces ≥100 px, every 2 frames |
| MiniFAS ×2 | NPU + crop | 22 ms per texture-due frame (every 5) |
| MiDaS small 256 | NPU | 51 ms per texture-due frame, faces ≥150 px, off by default |
| motion (5-pt similarity residual) | CPU | 3.6 ms/face/frame (was 14 ms before the incremental rewrite) |

One face, liveness on (depth off): **7.5 fps** vs 7.7 with liveness off.
Two 46–64 px faces before the cost gates: 4.9 fps.

Offline calibration (`liveness-calibration-offline.md`, NUAA + CASIA-FASD +
display-replay, 1723 rows): texture ensemble AUC 0.993 held-out; defaults now
`t_live 0.55 / t_spoof 0.35 / motion_noise_floor 0.006`; depth inverted on
face-filling datasets (AUC 0.43) → stays disabled until calibrated on device
frames. Blink / real micro-motion could not be measured offline.

