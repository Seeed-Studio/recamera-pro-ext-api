# kit.logic

[API 索引](../index.md) · [接口特性与边界](../features.md)

源码基线：[kit/logic/__init__.py](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/logic/__init__.py)；签名由 AST 提取，不导入硬件依赖。

应用侧 CPU 业务逻辑组件命名空间；状态机依赖正确模型输出、坐标与时间戳，不替代模型。

源码说明中的历史验证记录、迁移目标和旧示例不代表当前固件可用；应用入口、资源准入和接口可用性以本页补充及接口特性文档为准。

## 模块契约（源码）

Reusable, app-agnostic logic helpers (geometry + temporal state machines).

These are deliberately model-free: they operate on decoded pose dicts
(kit.runtime.postprocess.pose output) and plain floats so that fall-detection,
fitness-trainer, and future pose apps share one implementation.
