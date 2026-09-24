# kit.logic.recording

[API 索引](../index.md) · [接口特性与边界](../features.md)

源码基线：[kit/logic/recording.py](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/logic/recording.py)；签名由 AST 提取，不导入硬件依赖。

把显式业务事件交给 App.request_recording；必须有 manifest 录像声明与 AppMgr 授权。

源码说明中的历史验证记录、迁移目标和旧示例不代表当前固件可用；应用入口、资源准入和接口可用性以本页补充及接口特性文档为准。

## 模块契约（源码）

Application-owned recording decisions; never infer them from display events.

## kit.logic.recording.request_configured_recording

```python
def request_configured_recording(app, event_kind, pts)
```

Gate an explicit business edge with live opt-in and per-kind cooldown.

Defaults are off. Only successful local queueing starts the cooldown; this
is not a recording acknowledgement. At most the declared event kinds are
retained, and presentation/ML state is unaffected by configuration changes.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/logic/recording.py#L5)
