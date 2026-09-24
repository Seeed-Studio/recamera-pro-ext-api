# kit.runtime

[API 索引](../index.md) · [接口特性与边界](../features.md)

源码基线：[kit/runtime/__init__.py](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/runtime/__init__.py)；签名由 AST 提取，不导入硬件依赖。

模型前后处理与 session 命名空间。

源码说明中的历史验证记录、迁移目标和旧示例不代表当前固件可用；应用入口、资源准入和接口可用性以本页补充及接口特性文档为准。

## 模块契约（源码）

Inference runtime interfaces.

## 公开导出 / 别名

| 导入名称 | 定义与完整说明 |
| --- | --- |
| `DEFAULT_INFERENCE_SOCKET` | [kit.runtime.remote.DEFAULT_INFERENCE_SOCKET](kit-runtime-remote.md) |
| `INFERENCE_SOCKET_ENV` | [kit.runtime.remote.INFERENCE_SOCKET_ENV](kit-runtime-remote.md) |
| `InferenceStats` | [kit.runtime.engine.InferenceStats](kit-runtime-engine.md) |
| `ModelSpec` | [kit.runtime.engine.ModelSpec](kit-runtime-engine.md) |
| `RemoteRknnModel` | [kit.runtime.remote.RemoteRknnModel](kit-runtime-remote.md) |
| `RemoteRknnSession` | [kit.runtime.remote.RemoteRknnSession](kit-runtime-remote.md) |
| `RknnModel` | [kit.runtime.engine.RknnModel](kit-runtime-engine.md) |
| `RknnSession` | [kit.runtime.engine.RknnSession](kit-runtime-engine.md) |
| `TensorSpec` | [kit.runtime.engine.TensorSpec](kit-runtime-engine.md) |
| `configured_inference_socket` | [kit.runtime.remote.configured_inference_socket](kit-runtime-remote.md) |
