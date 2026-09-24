# reCamera Pro fall detection

The app keeps pose inference on the RKNN NPU and uses only NumPy on the CPU for
tracking and fall logic. Every `track_id` owns an independent 48-frame temporal
window, learned classifier, geometry state machine, and event counter.

The production default is strict confirmation:

- only geometry (hip drop plus a horizontal cue within the motion window) may
  arm `suspected`; a temporal-positive window alone never leaves `normal`;
- `fallen` is reached only from `suspected` and requires a temporal probability
  of at least 0.8 on three evaluations and a valid lying pose (torso angle and
  box aspect both above threshold) in the current frame;
- a first-frame lying pose, missing pose, or invalid pose cannot create an event;
- set `temporal_confirmation_required=false` only for explicit legacy
  geometry-only bring-up.

The packaged `jetson-yolo11s-pose-optimized-v1` profile began as a transfer
fallback. A strict Pro-native experiment is now complete, but the transfer
profile remains the production default because it performed better on the same
frozen Pro Subject-4 traces. It uses the same
pelvis-centred 56-value frame representation, 48 frames at a 15 FPS timebase,
six temporal bins plus standard-deviation/delta/span features, and a 32-unit
MLP. It is stored in `models/temporal_yolo11s_pose_v1.json.gz`. Pro packaging
includes the complete `models/` tree, so deployment needs no Torch,
Ultralytics, sklearn, or separate Python module.

## RV1126B live validation

Firmware V1.0.4 was validated through the official appMgr single-active-app
API on 2026-08-13. A signed 0.2.0 package containing `app.py`, the manifest,
the RKNN pose model and the transfer temporal profile passed signature
verification and ran the live camera path. A 60-second WebSocket capture
produced 783 messages (13.05 FPS), with inference mean/P95 35.89/39.36 ms and
full pipeline mean/P95 77.80/85.99 ms. appMgr reported 19–21% NPU load,
51.2–52.5 °C and 836.6–839.9 MB total system memory used.

The scene contained a visible person in only three result frames and no fall
edge. These numbers validate installation and performance only; they are not
an accuracy result.

The strict 160-clip native experiment used S1–S2 to fit, S3 to select, S1–S3
to refit/freeze, then read S4 for the first time. The native candidate scored
70.37% accuracy, 75.0% recall and 69.23% F1 on the 27 clean S4 clips, with
three early alerts. The unchanged transfer profile scored 81.48% accuracy,
91.67% recall and 81.48% F1 on those exact traces, with one early alert.
Therefore `temporal_yolo11s_pose_v1.json.gz` stays the default and
`temporal_recamera_pro_rknn_v1.json.gz` is retained as an auditable experiment,
not promoted. Overall pose coverage was 87.27%; S4 Fall coverage was 72.84%
versus 91.77% for ADL, which is a material frontend limitation.

## Train a Pro-native temporal MLP

The pose frontend changes the confidence, missing-joint and spatial-error
distribution seen by the temporal model. A native profile therefore means:
the **same frozen RV1126B RKNN pose model** extracts every trace used to train,
select and test the MLP. It does not mean retraining YOLO itself.

The split firewall is enforced by two separate commands:

1. Subjects 1-2 fit candidate MLPs.
2. Subject 3 selects mask/hidden-size/regularization/threshold/consecutive count.
3. The selected configuration is refit on Subjects 1-3 and frozen with hashes.
4. Only the `test` command may read Subject 4; it cannot modify the profile.

On a reCamera Pro, extract development traces with NPU inference:

```bash
cd /userdata/local/apps/fall-detection
PYTHONPATH=/userdata/local /userdata/rknnenv/bin/python \
  tools/extract_pose_traces.py \
  --model models/yolo11n_pose_rawhead_int8.rknn \
  --dataset /data/GMDCSA24 --output /data/traces/recamera-pro-rknn \
  --subjects 1,2,3 --resume
```

On a training host, freeze the dependency-free deployment profile. `sklearn`
is used here only to fit the MLP and is not part of the device runtime:

```bash
uv run --with numpy --with scikit-learn python \
  apps/fall-detection/tools/train_freeze_temporal_mlp.py freeze \
  --traces /data/traces/recamera-pro-rknn --dataset /data/GMDCSA24 \
  --pose-model apps/fall-detection/models/yolo11n_pose_rawhead_int8.rknn \
  --profile apps/fall-detection/models/temporal_recamera_pro_rknn_v1.json.gz \
  --checkpoint apps/fall-detection/evaluation/temporal_recamera_pro_rknn_v1.checkpoint.json
```

After the checkpoint exists, extract Subject 4 on the same Pro and run the
read-only frozen test:

```bash
PYTHONPATH=/userdata/local /userdata/rknnenv/bin/python \
  tools/extract_pose_traces.py \
  --model models/yolo11n_pose_rawhead_int8.rknn \
  --dataset /data/GMDCSA24 --output /data/traces/recamera-pro-rknn \
  --subjects 4 --resume

uv run --with numpy --with scikit-learn python \
  apps/fall-detection/tools/train_freeze_temporal_mlp.py test \
  --traces /data/traces/recamera-pro-rknn --dataset /data/GMDCSA24 \
  --pose-model apps/fall-detection/models/yolo11n_pose_rawhead_int8.rknn \
  --profile apps/fall-detection/models/temporal_recamera_pro_rknn_v1.json.gz \
  --checkpoint apps/fall-detection/evaluation/temporal_recamera_pro_rknn_v1.checkpoint.json \
  --report apps/fall-detection/evaluation/temporal_recamera_pro_rknn_v1.s4.json
```

A temporary RV1126B reCamera Pro target is now registered in Fleet. Firmware
V1.0.4 has passed signed-package installation and live camera/NPU/WebSocket
validation. RK3576 and RK3588 remain incompatible substitutes for an RV1126B
RKNN artifact. The strict native result and same-trace fallback comparator are
stored in `evaluation/`; the better fallback remains the release default.
