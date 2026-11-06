# Bundled model provenance

- Artifact: `models/model.rknn`
- SHA-256: `26f251e28320192f4a781aa33565ed6264567d108c1a77b667cdc98f54fb97be`
- Size: 4,860,688 bytes
- Source: the user's existing CVSprout training run 5, conversion 3.
- Architecture: Ultralytics YOLO26n detector, one class `person`.
- Export: static ONNX `[1,3,640,640]` input and `[1,5,8400]` decoded XYWH plus class-score output; `end2end=false`, `nms=false`.
- Device input: uint8 RGB NHWC; mean 0 / std 255 is embedded by conversion.
- Target: RV1126B, RKNN-Toolkit2 2.3.2, INT8.
- Device quality/performance acceptance: not performed as part of offline packaging.

The application code license does not replace the model's license. Ultralytics
publishes its software and models under AGPL-3.0 with an alternative Enterprise
license: <https://www.ultralytics.com/license>. Preserve the applicable upstream
license and training-data permissions when distributing or replacing this model.
The underlying training data and pretrained checkpoints are not embedded in this
application package. A model trained on a different dataset must be independently
checked for deployment accuracy and distribution rights.
