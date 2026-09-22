# kit.ai

[API 索引](../index.md) · [接口特性与边界](../features.md)

源码基线：[kit/ai/__init__.py](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/ai/__init__.py)；签名由 AST 提取，不导入硬件依赖。

强类型结果及几何图元的公共导出；字段语义和坐标空间必须显式匹配。

源码说明中的历史验证记录、迁移目标和旧示例不代表当前固件可用；应用入口、资源准入和接口可用性以本页补充及接口特性文档为准。

## 模块契约（源码）

Typed, validated AI result objects for reCamera application workflows.

The public names are defined in :mod:`kit.ai.results` and re-exported here so
applications can use the compact ``from kit.ai import Detection`` form without
expanding the historical top-level :mod:`kit` namespace.

## 公开导出 / 别名

| 导入名称 | 定义与完整说明 |
| --- | --- |
| `AIResult` | [kit.ai.results.AIResult](kit-ai-results.md) |
| `Box` | [kit.ai.results.Box](kit-ai-results.md) |
| `Classification` | [kit.ai.results.Classification](kit-ai-results.md) |
| `CoordinateSpace` | [kit.ai.results.CoordinateSpace](kit-ai-results.md) |
| `Detection` | [kit.ai.results.Detection](kit-ai-results.md) |
| `GeometryBuilder` | [kit.geometry.GeometryBuilder](kit-geometry.md) |
| `GeometryError` | [kit.geometry.GeometryError](kit-geometry.md) |
| `Keypoint` | [kit.ai.results.Keypoint](kit-ai-results.md) |
| `LegacyResultSink` | [kit.ai.publisher.LegacyResultSink](kit-ai-publisher.md) |
| `ModelToPixel` | [kit.ai.publisher.ModelToPixel](kit-ai-publisher.md) |
| `Pose` | [kit.ai.results.Pose](kit-ai-results.md) |
| `PublishReport` | [kit.ai.publisher.PublishReport](kit-ai-publisher.md) |
| `ResultBatch` | [kit.ai.results.ResultBatch](kit-ai-results.md) |
| `ResultBatchPublisher` | [kit.ai.publisher.ResultBatchPublisher](kit-ai-publisher.md) |
| `Segmentation` | [kit.ai.results.Segmentation](kit-ai-results.md) |
| `Track` | [kit.ai.results.Track](kit-ai-results.md) |
| `geometry_box` | [kit.geometry.box](kit-geometry.md) |
| `geometry_keypoints` | [kit.geometry.keypoints](kit-geometry.md) |
| `geometry_line` | [kit.geometry.line](kit-geometry.md) |
| `geometry_point` | [kit.geometry.point](kit-geometry.md) |
| `geometry_polygon` | [kit.geometry.polygon](kit-geometry.md) |
| `geometry_polyline` | [kit.geometry.polyline](kit-geometry.md) |
| `geometry_pose` | [kit.geometry.pose](kit-geometry.md) |
| `geometry_quad` | [kit.geometry.quad](kit-geometry.md) |
| `publish_result_batch` | [kit.ai.publisher.publish_result_batch](kit-ai-publisher.md) |
| `to_legacy_payload` | [kit.ai.publisher.to_legacy_payload](kit-ai-publisher.md) |
