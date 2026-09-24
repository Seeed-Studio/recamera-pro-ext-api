# kit.runtime.rknnlite

[API 索引](../index.md) · [接口特性与边界](../features.md)

源码基线：[kit/runtime/rknnlite.py](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/runtime/rknnlite.py)；签名由 AST 提取，不导入硬件依赖。

平台 RKNNLite 兼容包装器：复制输出为独立 NumPy 数组，在初始化、推理返回/异常及释放时回收厂商循环缓冲；ctypes 后端不执行这些操作。普通应用使用 App 模型工厂，不直接实例化本后端。

源码说明中的历史验证记录、迁移目标和旧示例不代表当前固件可用；应用入口、资源准入和接口可用性以本页补充及接口特性文档为准。

## 模块契约（源码）

RKNNLite compatibility backend with bounded cyclic-buffer retention.

RKNNLite 2.3.2 leaves ctypes input/output buffers in reference cycles. Python's
automatic collector counts objects, not their backing bytes: a handful of ASR
outputs can retain hundreds of MiB before it runs. Collect after each vendor
call in the process that owns the runtime, including failed calls and teardown.
The ctypes backend (image/DMA or feature tensors) does not pay this cost.

This does not change tensor ownership, dtype, shape, layout or backend selection.
Live outputs remain valid across subsequent calls and release. Native context
ownership/serialization still belongs to the session or inference daemon.

## kit.runtime.rknnlite.RknnLiteRuntime

```python
class RknnLiteRuntime
```

The vendor interface with explicit collection at synchronous boundaries.

### 字段 / 默认值

`@dataclass` 未显式定义构造器时，由下列字段生成；无默认值的字段必填。

```python
backend = 'rknnlite'
collects_output_cycles = True
```

### kit.runtime.rknnlite.RknnLiteRuntime.__init__

```python
def __init__(self, *args, **kwargs)
```

在目标设备导入并构造厂商 RKNNLite；参数原样传递。本类不取得 NPU 租约，必须由平台后端或受管 session 持有所有权。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/runtime/rknnlite.py#L27)

### kit.runtime.rknnlite.RknnLiteRuntime.init_runtime

```python
def init_runtime(self, *args, **kwargs)
```

原样调用厂商初始化并返回其结果；无论成功或异常都会执行完整循环回收。初始化失败的清理与隔离仍由上层 session 负责。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/runtime/rknnlite.py#L36)

### kit.runtime.rknnlite.RknnLiteRuntime.inference

```python
def inference(self, *args, **kwargs)
```

同步调用厂商推理，返回独立拥有内存的 NumPy 输出列表，保持 dtype、shape、数值和次序；非数组元数据原样传递，None 原样返回。返回或异常时丢弃厂商输出引用并回收循环缓冲，有额外复制和 GC 开销。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/runtime/rknnlite.py#L42)

### kit.runtime.rknnlite.RknnLiteRuntime.release

```python
def release(self)
```

调用厂商释放；仅成功后清除保留的模型字节，失败时保留上下文供上层隔离/重试。无论结果均执行循环回收；不会使此前返回的独立输出失效。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/runtime/rknnlite.py#L63)
