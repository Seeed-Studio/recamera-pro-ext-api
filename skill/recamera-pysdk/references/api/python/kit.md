# kit

[API 索引](../index.md) · [接口特性与边界](../features.md)

源码基线：[kit/__init__.py](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/__init__.py)；签名由 AST 提取，不导入硬件依赖。

Kit 顶层惰性导出。下表列出所有公共名称及定义位置；导入本身不启动摄像头或 NPU。

源码说明中的历史验证记录、迁移目标和旧示例不代表当前固件可用；应用入口、资源准入和接口可用性以本页补充及接口特性文档为准。

## 模块契约（源码）

High-level Python API for reCamera AI applications.

The package keeps imports lazy so an audio-only application does not
accidentally import NumPy/OpenCV.  Existing ``kit.adapters`` imports remain
valid; these names form the new, documented common contract.

## 公开导出 / 别名

| 导入名称 | 定义与完整说明 |
| --- | --- |
| `AIResult` | [kit.ai.results.AIResult](kit-ai-results.md) |
| `AdapterError` | [kit.errors.AdapterError](kit-errors.md) |
| `Box` | [kit.ai.results.Box](kit-ai-results.md) |
| `BufferReleasedError` | [kit.errors.BufferReleasedError](kit-errors.md) |
| `CancellationToken` | [kit.workflow.node.CancellationToken](kit-workflow-node.md) |
| `Capabilities` | [kit.capabilities.Capabilities](kit-capabilities.md) |
| `Capability` | [kit.capabilities.Capability](kit-capabilities.md) |
| `CapabilityError` | [kit.errors.CapabilityError](kit-errors.md) |
| `CapabilityStatus` | [kit.capabilities.CapabilityStatus](kit-capabilities.md) |
| `Classification` | [kit.ai.results.Classification](kit-ai-results.md) |
| `ConfigurationError` | [kit.errors.ConfigurationError](kit-errors.md) |
| `CoordinateSpace` | [kit.ai.results.CoordinateSpace](kit-ai-results.md) |
| `Detection` | [kit.ai.results.Detection](kit-ai-results.md) |
| `Device` | [kit.device.Device](kit-device.md) |
| `DeviceCloseReport` | [kit.device.DeviceCloseReport](kit-device.md) |
| `DeviceControlError` | [kit.errors.DeviceControlError](kit-errors.md) |
| `DropPolicy` | [kit.workflow.queue.DropPolicy](kit-workflow-queue.md) |
| `ExternalNpuLease` | [kit.resources.ExternalNpuLease](kit-resources.md) |
| `Frame` | [kit.frame.Frame](kit-frame.md) |
| `GeometryBuilder` | [kit.geometry.GeometryBuilder](kit-geometry.md) |
| `GeometryError` | [kit.geometry.GeometryError](kit-geometry.md) |
| `ImageBuffer` | [kit.buffer.ImageBuffer](kit-buffer.md) |
| `ImageOperationError` | [kit.errors.ImageOperationError](kit-errors.md) |
| `ImageOps` | [kit.media.image.ImageOps](kit-media-image.md) |
| `InferenceError` | [kit.errors.InferenceError](kit-errors.md) |
| `InferenceStats` | [kit.runtime.engine.InferenceStats](kit-runtime-engine.md) |
| `InputQueue` | [kit.workflow.queue.InputQueue](kit-workflow-queue.md) |
| `InputValidationError` | [kit.errors.InputValidationError](kit-errors.md) |
| `Keypoint` | [kit.ai.results.Keypoint](kit-ai-results.md) |
| `KitError` | [kit.errors.KitError](kit-errors.md) |
| `MemoryKind` | [kit.buffer.MemoryKind](kit-buffer.md) |
| `ModelLoadError` | [kit.errors.ModelLoadError](kit-errors.md) |
| `ModelSpec` | [kit.runtime.engine.ModelSpec](kit-runtime-engine.md) |
| `Ownership` | [kit.buffer.Ownership](kit-buffer.md) |
| `Pipeline` | [kit.workflow.runtime.Pipeline](kit-workflow-runtime.md) |
| `PixelFormat` | [kit.buffer.PixelFormat](kit-buffer.md) |
| `PlaneLayout` | [kit.buffer.PlaneLayout](kit-buffer.md) |
| `Pose` | [kit.ai.results.Pose](kit-ai-results.md) |
| `PublishReport` | [kit.ai.publisher.PublishReport](kit-ai-publisher.md) |
| `PutResult` | [kit.workflow.queue.PutResult](kit-workflow-queue.md) |
| `PutStatus` | [kit.workflow.queue.PutStatus](kit-workflow-queue.md) |
| `QueueStats` | [kit.workflow.queue.QueueStats](kit-workflow-queue.md) |
| `Rect` | [kit.media.image.Rect](kit-media-image.md) |
| `RemoteRknnModel` | [kit.runtime.remote.RemoteRknnModel](kit-runtime-remote.md) |
| `RemoteRknnSession` | [kit.runtime.remote.RemoteRknnSession](kit-runtime-remote.md) |
| `ResourceBusyError` | [kit.errors.ResourceBusyError](kit-errors.md) |
| `ResourceKind` | [kit.resources.ResourceKind](kit-resources.md) |
| `ResourceTimeoutError` | [kit.errors.ResourceTimeoutError](kit-errors.md) |
| `ResultBatch` | [kit.ai.results.ResultBatch](kit-ai-results.md) |
| `ResultBatchPublisher` | [kit.ai.publisher.ResultBatchPublisher](kit-ai-publisher.md) |
| `RgaContext` | [kit.media.image.RgaContext](kit-media-image.md) |
| `RknnModel` | [kit.runtime.engine.RknnModel](kit-runtime-engine.md) |
| `RknnSession` | [kit.runtime.engine.RknnSession](kit-runtime-engine.md) |
| `Segmentation` | [kit.ai.results.Segmentation](kit-ai-results.md) |
| `Size` | [kit.media.image.Size](kit-media-image.md) |
| `Stage` | [kit.workflow.node.Stage](kit-workflow-node.md) |
| `TensorSpec` | [kit.runtime.engine.TensorSpec](kit-runtime-engine.md) |
| `Track` | [kit.ai.results.Track](kit-ai-results.md) |
| `TransformMapping` | [kit.media.image.TransformMapping](kit-media-image.md) |
| `TransformResult` | [kit.media.image.TransformResult](kit-media-image.md) |
| `TransportError` | [kit.errors.TransportError](kit-errors.md) |
| `WorkflowCleanupError` | [kit.workflow.node.WorkflowCleanupError](kit-workflow-node.md) |
| `WorkflowCloseReport` | [kit.workflow.runtime.WorkflowCloseReport](kit-workflow-runtime.md) |
| `WorkflowContext` | [kit.workflow.node.WorkflowContext](kit-workflow-node.md) |
| `get_capabilities` | [kit.capabilities.get_capabilities](kit-capabilities.md) |
| `probe_capabilities` | [kit.capabilities.probe_capabilities](kit-capabilities.md) |
| `publish_result_batch` | [kit.ai.publisher.publish_result_batch](kit-ai-publisher.md) |
| `to_legacy_payload` | [kit.ai.publisher.to_legacy_payload](kit-ai-publisher.md) |
