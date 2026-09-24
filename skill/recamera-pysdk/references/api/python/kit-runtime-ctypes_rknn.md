# kit.runtime.ctypes_rknn

[API 索引](../index.md) · [接口特性与边界](../features.md)

源码基线：[kit/runtime/ctypes_rknn.py](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/runtime/ctypes_rknn.py)；签名由 AST 提取，不导入硬件依赖。

平台推理后端实现及 RKNN ABI 结构。服务拥有 context/绑定 IO；普通 scheduled App 通过 self.models 调用，不能直接绕过调度。

源码说明中的历史验证记录、迁移目标和旧示例不代表当前固件可用；应用入口、资源准入和接口可用性以本页补充及接口特性文档为准。

## 模块契约（源码）

Direct ``librknnrt.so`` bindings for a single static caller input.

Image graphs (4D, NHWC or NCHW) take a uint8 NHWC caller array. Other graphs
(for example a 3D float32 sequence) take an array whose shape equals the
graph's declared dims and whose dtype is uint8, int8, float16 or float32; the
descriptor carries that dtype and the graph's own fmt.

The August 19 legacy-path investigation observed reference cycles retained by
RKNNLite 2.3.2 and compared direct bindings against that wrapper. Those historical
RSS and latency measurements do not establish the behavior of the new inference
daemon; its managed lifecycle needs separate device validation.

Inputs use pass_through=0 so librknnrt converts to the graph's native layout and
dtype. Static image models can reuse bound DMA input/output buffers; float32
outputs retain the graph's declared shape. Unsupported binding configurations
fall back to the general API in a fresh context. ESK_RKNN_IO_MODE=legacy disables
binding; ESK_RKNN_IO_MODE=bound requires it. Unsupported caller contracts remain
on the RKNNLite backend in auto mode.

All native entry points have ctypes prototypes to avoid pointer truncation on
AArch64. Device access is subject to the installed /dev/rknpu permissions.

## 常量与类型别名

以下为该版本源码值；默认地址不等于所有固件都开放该服务。

```python
LIB_CANDIDATES = ('/usr/lib/librknnrt.so', '/oem/usr/lib/librknnrt.so', '/userdata/sdk/lib/librknnrt.so')
```


```python
RKNN_SUCC = 0
```


```python
RKNN_MAX_DIMS = 16
```


```python
RKNN_MAX_NAME_LEN = 256
```


```python
RKNN_QUERY_IN_OUT_NUM = 0
```


```python
RKNN_QUERY_INPUT_ATTR = 1
```


```python
RKNN_QUERY_OUTPUT_ATTR = 2
```


```python
RKNN_QUERY_PERF_DETAIL = 3
```


```python
RKNN_QUERY_SDK_VERSION = 5
```


```python
RKNN_QUERY_NATIVE_INPUT_ATTR = 8
```


```python
RKNN_QUERY_NATIVE_OUTPUT_ATTR = 9
```


```python
RKNN_QUERY_NATIVE_NHWC_OUTPUT_ATTR = 11
```


```python
RKNN_TENSOR_UINT8 = 3
```


```python
RKNN_TENSOR_FLOAT32 = 0
```


```python
RKNN_TENSOR_FLOAT16 = 1
```


```python
RKNN_TENSOR_INT8 = 2
```


```python
RKNN_TENSOR_NHWC = 1
```


```python
RKNN_TENSOR_NCHW = 0
```


```python
RKNN_TENSOR_NC1HWC2 = 2
```


```python
RKNN_TENSOR_UNDEFINED = 3
```


```python
RKNN_MEMORY_SYNC_TO_DEVICE = 1
```


```python
RKNN_MEMORY_SYNC_FROM_DEVICE = 2
```


```python
RKNN_NPU_CORE_AUTO = 0
```


```python
rknn_context = ctypes.c_uint64
```


## kit.runtime.ctypes_rknn.RknnTensorAttr

```python
class RknnTensorAttr(ctypes.Structure)
```

``rknn_tensor_attr``, field order and types verbatim from the header.

``fl`` (int8) sitting in front of ``zp`` (int32) is the one place a hand-
packed layout would go wrong; ctypes inserts the same three padding bytes
the C compiler does, so this must NOT be declared ``_pack_``-ed.

### C 结构字段

```python
_fields_ = [('index', ctypes.c_uint32), ('n_dims', ctypes.c_uint32), ('dims', ctypes.c_uint32 * RKNN_MAX_DIMS), ('name', ctypes.c_char * RKNN_MAX_NAME_LEN), ('n_elems', ctypes.c_uint32), ('size', ctypes.c_uint32), ('fmt', ctypes.c_int), ('type', ctypes.c_int), ('qnt_type', ctypes.c_int), ('fl', ctypes.c_int8), ('zp', ctypes.c_int32), ('scale', ctypes.c_float), ('w_stride', ctypes.c_uint32), ('size_with_stride', ctypes.c_uint32), ('pass_through', ctypes.c_uint8), ('h_stride', ctypes.c_uint32)]
```

## kit.runtime.ctypes_rknn.RknnInputOutputNum

```python
class RknnInputOutputNum(ctypes.Structure)
```

RKNN ABI 输入/输出张量数量结构，字段布局必须与固件 librknnrt 匹配。

### C 结构字段

```python
_fields_ = [('n_input', ctypes.c_uint32), ('n_output', ctypes.c_uint32)]
```

## kit.runtime.ctypes_rknn.RknnInput

```python
class RknnInput(ctypes.Structure)
```

RKNN 输入 ABI 结构，包含索引、数据指针、大小、dtype、布局与 pass-through 标志；平台后端负责缓冲有效期。

### C 结构字段

```python
_fields_ = [('index', ctypes.c_uint32), ('buf', ctypes.c_void_p), ('size', ctypes.c_uint32), ('pass_through', ctypes.c_uint8), ('type', ctypes.c_int), ('fmt', ctypes.c_int)]
```

## kit.runtime.ctypes_rknn.RknnOutput

```python
class RknnOutput(ctypes.Structure)
```

RKNN 输出 ABI 结构，包含 float 转换、预分配、索引、数据指针及大小。

### C 结构字段

```python
_fields_ = [('want_float', ctypes.c_uint8), ('is_prealloc', ctypes.c_uint8), ('index', ctypes.c_uint32), ('buf', ctypes.c_void_p), ('size', ctypes.c_uint32)]
```

## kit.runtime.ctypes_rknn.RknnSdkVersion

```python
class RknnSdkVersion(ctypes.Structure)
```

RKNN runtime/driver 版本字符串 ABI 结构。

### C 结构字段

```python
_fields_ = [('api_version', ctypes.c_char * 256), ('drv_version', ctypes.c_char * 256)]
```

## kit.runtime.ctypes_rknn.RknnTensorMem

```python
class RknnTensorMem(ctypes.Structure)
```

LP64 ``rknn_tensor_mem`` from the installed RKNN 2.3.2 header.

### C 结构字段

```python
_fields_ = [('virt_addr', ctypes.c_void_p), ('phys_addr', ctypes.c_uint64), ('fd', ctypes.c_int32), ('offset', ctypes.c_int32), ('size', ctypes.c_uint32), ('flags', ctypes.c_uint32), ('priv_data', ctypes.c_void_p)]
```

## kit.runtime.ctypes_rknn.CtypesUnsupportedModel

```python
class CtypesUnsupportedModel(RuntimeError)
```

The graph's own input contract is outside what this backend handles.

Raised by ``init_runtime`` after the native context exists; the caller
must ``release()`` it.  RKNNLite may still run such a graph, so callers
that did not declare an input contract can fall back to it.

## kit.runtime.ctypes_rknn.InputContractError

```python
class InputContractError(ValueError, TypeError)
```

A caller tensor does not match the graph input; raised before any
driver call.  Subclasses TypeError for callers of the older dtype check.

## kit.runtime.ctypes_rknn.RknnIOBuffer

```python
class RknnIOBuffer
```

Model-owned DMA allocation; descriptors are borrowed until release.

Do not close its fd. Allocate separate buffers per client/model alias; the
model's default buffers are private scratch space. The caller must serialize
allocation, binding, inference and release with its existing driver lock,
and wait for all external writers/readers before reuse or release.

### kit.runtime.ctypes_rknn.RknnIOBuffer.__init__

```python
def __init__(self, owner, mem, *, kind, index, shape, strides, dtype)
```

构造实例并保存/校验上述参数；参数默认值见签名。是否在构造时打开设备或加载模型，以本类的生命周期说明为准；构造方法返回 None。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/runtime/ctypes_rknn.py#L186)

### kit.runtime.ctypes_rknn.RknnIOBuffer.fd

```python
@property
def fd(self)
```

返回绑定 IO 的 DMA-BUF 文件描述符；调用方不得自行 close 所属 runtime 的 fd。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/runtime/ctypes_rknn.py#L201)

### kit.runtime.ctypes_rknn.RknnIOBuffer.offset

```python
@property
def offset(self)
```

返回该 IO 缓冲的字节偏移。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/runtime/ctypes_rknn.py#L206)

### kit.runtime.ctypes_rknn.RknnIOBuffer.size

```python
@property
def size(self)
```

返回该 IO 缓冲的字节大小；不能用逻辑 shape 忽略 native stride。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/runtime/ctypes_rknn.py#L211)

### kit.runtime.ctypes_rknn.RknnIOBuffer.describe

```python
def describe(self)
```

返回 IO 缓冲的描述信息，供后端诊断与绑定使用。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/runtime/ctypes_rknn.py#L215)

## kit.runtime.ctypes_rknn.library_path

```python
def library_path() -> str
```

First existing candidate, or "" -- used to decide whether to even try.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/runtime/ctypes_rknn.py#L241)

## kit.runtime.ctypes_rknn.CtypesRknnModel

```python
class CtypesRknnModel
```

One ``rknn_context``, same surface as ``RknnLiteModel``.

``infer(array) -> list[np.ndarray]`` of dequantized float32 tensors
shaped by the graph's declared output dims.

One ``rknn_input`` array and one ``rknn_output`` array are allocated at
runtime initialization and reused for every call. Native work is deferred
until init_runtime() so callers can acquire their driver lock first.

### 字段 / 默认值

`@dataclass` 未显式定义构造器时，由下列字段生成；无默认值的字段必填。

```python
backend = 'ctypes'
```

### kit.runtime.ctypes_rknn.CtypesRknnModel.__init__

```python
def __init__(self, path: str='', core_mask: Optional[int]=None, io_mode: Optional[str]=None, image_inputs: bool=True)
```

构造实例并保存/校验上述参数；参数默认值见签名。是否在构造时打开设备或加载模型，以本类的生命周期说明为准；构造方法返回 None。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/runtime/ctypes_rknn.py#L347)

### kit.runtime.ctypes_rknn.CtypesRknnModel.load_rknn

```python
def load_rknn(self, path: str) -> int
```

Record the model path; defer all native work to init_runtime.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/runtime/ctypes_rknn.py#L393)

### kit.runtime.ctypes_rknn.CtypesRknnModel.init_runtime

```python
def init_runtime(self, *, core_mask: Optional[int]=None, **kwargs) -> int
```

初始化 native context，并按 io_mode 选择 legacy 或绑定 IO。成功返回 RKNN_SUCC；auto 在绑定能力不足时允许回退，显式 bound 会报错。core_mask 传递给 runtime；本方法不授予 NPU 资源权限。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/runtime/ctypes_rknn.py#L402)

### kit.runtime.ctypes_rknn.CtypesRknnModel.released

```python
@property
def released(self)
```

Whether release() completed and no native context remains.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/runtime/ctypes_rknn.py#L559)

### kit.runtime.ctypes_rknn.CtypesRknnModel.native_cleanup_failed

```python
@property
def native_cleanup_failed(self)
```

A native memory/context destruction failed; retain driver quarantine.

This lifetime latch is distinct from a failed run/bind/sync, which fences
this model but does not by itself prove that cleanup is uncertain. It
also covers failures inside allocation rollback, before a buffer can be
returned to the caller. A later successful release does not clear it.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/runtime/ctypes_rknn.py#L564)

### kit.runtime.ctypes_rknn.CtypesRknnModel.shared_io_size_bytes

```python
@property
def shared_io_size_bytes(self)
```

One private IO set's observed size; recheck actual new allocations.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/runtime/ctypes_rknn.py#L575)

### kit.runtime.ctypes_rknn.CtypesRknnModel.output_specs

```python
@property
def output_specs(self)
```

Stable caller output contract, including for legacy runtimes.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/runtime/ctypes_rknn.py#L582)

### kit.runtime.ctypes_rknn.CtypesRknnModel.allocate_input_buffer

```python
def allocate_input_buffer(self)
```

Allocate private DMA input for one client; caller holds driver lock.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/runtime/ctypes_rknn.py#L720)

### kit.runtime.ctypes_rknn.CtypesRknnModel.allocate_output_buffers

```python
def allocate_output_buffers(self)
```

Allocate all float32 outputs for one client; caller holds driver lock.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/runtime/ctypes_rknn.py#L731)

### kit.runtime.ctypes_rknn.CtypesRknnModel.export_input_buffer

```python
def export_input_buffer(self)
```

Borrow the private default input descriptor for in-process use only.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/runtime/ctypes_rknn.py#L743)

### kit.runtime.ctypes_rknn.CtypesRknnModel.release_input_buffer

```python
def release_input_buffer(self, buf)
```

释放后端分配的绑定输入缓冲，返回 None；必须保证没有在途推理引用。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/runtime/ctypes_rknn.py#L782)

### kit.runtime.ctypes_rknn.CtypesRknnModel.release_output_buffers

```python
def release_output_buffers(self, buffers)
```

释放后端绑定输出缓冲，返回 None；外部保留的借用视图随后无效。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/runtime/ctypes_rknn.py#L792)

### kit.runtime.ctypes_rknn.CtypesRknnModel.infer_dma_buffers

```python
def infer_dma_buffers(self, input_buffer, output_buffers)
```

Run into caller-private DMA outputs, under the caller's driver lock.

External RGA/CPU input writes must have completed and flushed before this
call. Outputs are cache-synchronized before return. Clients must finish
reading before reuse; the API never transfers allocation ownership.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/runtime/ctypes_rknn.py#L816)

### kit.runtime.ctypes_rknn.CtypesRknnModel.infer_dma_into

```python
def infer_dma_into(self, input_buffer, outputs)
```

要求已初始化 bound IO；使用 DMA 输入执行同步推理，再把内部输出复制到调用者预分配的 outputs，并返回同一个 outputs 容器。目标 shape/dtype 必须匹配；不是输出零拷贝接口，属于平台后端。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/runtime/ctypes_rknn.py#L847)

### kit.runtime.ctypes_rknn.CtypesRknnModel.infer_bound_into

```python
def infer_bound_into(self, outputs)
```

Run after filling the private default input; synchronous use only.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/runtime/ctypes_rknn.py#L855)

### kit.runtime.ctypes_rknn.CtypesRknnModel.infer_into

```python
def infer_into(self, input_uint8, outputs)
```

Fill caller-owned float32 destinations (including shared memory).

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/runtime/ctypes_rknn.py#L859)

### kit.runtime.ctypes_rknn.CtypesRknnModel.infer

```python
def infer(self, input_uint8) -> List[np.ndarray]
```

One forward pass; outputs are float32.

Image graphs take uint8 NHWC. Other graphs take the declared dims in
one of the dtypes listed in ``_CALLER_TYPES``.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/runtime/ctypes_rknn.py#L893)

### kit.runtime.ctypes_rknn.CtypesRknnModel.inference

```python
def inference(self, inputs) -> List[np.ndarray]
```

RKNNLite-compatible sequence API used by sessions and the daemon.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/runtime/ctypes_rknn.py#L956)

### kit.runtime.ctypes_rknn.CtypesRknnModel.describe

```python
def describe(self) -> dict
```

返回模型输入输出张量属性及 runtime 能力描述，供诊断使用。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/runtime/ctypes_rknn.py#L964)

### kit.runtime.ctypes_rknn.CtypesRknnModel.release

```python
def release(self) -> None
```

Destroy all IO and the context; failed native cleanup is retryable.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/runtime/ctypes_rknn.py#L1008)

### kit.runtime.ctypes_rknn.CtypesRknnModel.__enter__

```python
def __enter__(self)
```

进入上下文管理器，返回其受管对象；与 __exit__ 配对使用，避免异常路径遗留资源。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/runtime/ctypes_rknn.py#L1018)

### kit.runtime.ctypes_rknn.CtypesRknnModel.__exit__

```python
def __exit__(self, *exc)
```

离开上下文并执行本类的清理方法；异常传播/清理失败语义见类说明，不把退出视作任务已完成。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/runtime/ctypes_rknn.py#L1021)
