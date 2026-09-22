# kit.workflow

[API 索引](../index.md) · [接口特性与边界](../features.md)

源码基线：[kit/workflow/__init__.py](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/workflow/__init__.py)；签名由 AST 提取，不导入硬件依赖。

同步 typed workflow 与有界队列公共导出；组合阶段不自动创建线程，也不自动仲裁 NPU。

源码说明中的历史验证记录、迁移目标和旧示例不代表当前固件可用；应用入口、资源准入和接口可用性以本页补充及接口特性文档为准。

## 模块契约（源码）

Minimal production workflow primitives for reCamera Python applications.

The package intentionally exposes a synchronous sequential pipeline and a
separate bounded thread-safe input queue.  It does not claim to be an async DAG
engine, an NPU scheduler, an rkipc resource lease, or a substitute for appmgr's
device-ownership transition.

## 公开导出 / 别名

| 导入名称 | 定义与完整说明 |
| --- | --- |
| `BackpressureTimeoutError` | [kit.workflow.queue.BackpressureTimeoutError](kit-workflow-queue.md) |
| `CancellationToken` | [kit.workflow.node.CancellationToken](kit-workflow-node.md) |
| `CloseReport` | [kit.workflow.runtime.CloseReport](kit-workflow-runtime.md) |
| `DropPolicy` | [kit.workflow.queue.DropPolicy](kit-workflow-queue.md) |
| `InputQueue` | [kit.workflow.queue.InputQueue](kit-workflow-queue.md) |
| `Pipeline` | [kit.workflow.runtime.Pipeline](kit-workflow-runtime.md) |
| `PutResult` | [kit.workflow.queue.PutResult](kit-workflow-queue.md) |
| `PutStatus` | [kit.workflow.queue.PutStatus](kit-workflow-queue.md) |
| `QueueClosedError` | [kit.workflow.queue.QueueClosedError](kit-workflow-queue.md) |
| `QueueError` | [kit.workflow.queue.QueueError](kit-workflow-queue.md) |
| `QueueStats` | [kit.workflow.queue.QueueStats](kit-workflow-queue.md) |
| `QueueTimeoutError` | [kit.workflow.queue.QueueTimeoutError](kit-workflow-queue.md) |
| `ResourceKind` | [kit.resources.ResourceKind](kit-resources.md) |
| `Stage` | [kit.workflow.node.Stage](kit-workflow-node.md) |
| `StageCloseFailure` | [kit.workflow.runtime.StageCloseFailure](kit-workflow-runtime.md) |
| `StageError` | [kit.workflow.node.StageError](kit-workflow-node.md) |
| `WorkflowBackpressureError` | [kit.workflow.queue.WorkflowBackpressureError](kit-workflow-queue.md) |
| `WorkflowCancelled` | [kit.workflow.node.WorkflowCancelled](kit-workflow-node.md) |
| `WorkflowCleanupError` | [kit.workflow.node.WorkflowCleanupError](kit-workflow-node.md) |
| `WorkflowCloseReport` | [kit.workflow.runtime.WorkflowCloseReport](kit-workflow-runtime.md) |
| `WorkflowClosedError` | [kit.workflow.node.WorkflowClosedError](kit-workflow-node.md) |
| `WorkflowContext` | [kit.workflow.node.WorkflowContext](kit-workflow-node.md) |
| `WorkflowError` | [kit.workflow.node.WorkflowError](kit-workflow-node.md) |
| `WorkflowResourceError` | [kit.workflow.node.WorkflowResourceError](kit-workflow-node.md) |
| `WorkflowTimeout` | [kit.workflow.node.WorkflowTimeout](kit-workflow-node.md) |
