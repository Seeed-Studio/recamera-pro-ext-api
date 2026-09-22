# kit.adapters.cgi_control

[API 索引](../index.md) · [接口特性与边界](../features.md)

源码基线：[kit/adapters/cgi_control.py](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/cgi_control.py)；签名由 AST 提取，不导入硬件依赖。

现有固件的控制面兼容适配器：设置内置推理和抓 JPEG。snapshot 是取帧编码，不是 CGI 原生抓图接口。

源码说明中的历史验证记录、迁移目标和旧示例不代表当前固件可用；应用入口、资源准入和接口可用性以本页补充及接口特性文档为准。

## 模块契约（源码）

CgiControl -- workaround control plane over the device's existing entry.cgi.

L0 adapter layer (docs/guide/adapter-bootstrap.md §2.4 R4). This is the reverse-engineered
counterpart to `OfficialControl`: instead of a future versioned control API it
drives the endpoints the shipped firmware already exposes through nginx +
`entry.cgi`, so the 9 kit apps get a working `ControlPlane` on TODAY's firmware
with zero application changes (registry.py swaps `OfficialControl` in later).

Two capabilities, two very different mechanisms
-----------------------------------------------
* set_inference(enable/model/fps)  -- a real device endpoint exists:
      POST http://127.0.0.1/cgi-bin/entry.cgi/model/inference?id=<model_id>
      body JSON, all three fields optional and co-sendable:
          {"iEnable":0|1, "sModel":"<file>", "iFPS":<int >=0>}
      (handler model_api.cpp:1052). Success -> {"code":0,"message":"success"}.
      `iFPS` is the NPU inference throttle, NOT the video encoder frame rate.
  Auth: entry.cgi behind nginx trusts 127.0.0.1 (rest_api.cpp:auth_verify top
      level pass-through for HTTP_X_INTERNAL_FROM_LOCALHOST=1), so a plain
      localhost HTTP request needs no JWT. We speak HTTP over TCP to nginx, not
      the gmgr unix socket.

* snapshot()  -- entry.cgi has NO frame-grab endpoint (confirmed). So snapshot
      is implemented as a FRAME PROXY (adapter-bootstrap decision "方案 A"): pull a
      single frame through the kit's own FrameSource (whichever the registry
      selects -- official dma-buf broker or the ffmpeg RTSP workaround), then
      JPEG-encode it with OpenCV. No new frame-connection logic is invented here.

Stdlib only for HTTP (http.client) -- no third-party dependency. cv2 + numpy are
already present for the vision apps and are imported lazily inside snapshot() so
an audio-only venv can still import this module.

## 常量与类型别名

以下为该版本源码值；默认地址不等于所有固件都开放该服务。

```python
CGI_BASE = '/cgi-bin/entry.cgi'
```


```python
INFERENCE_PATH = '/model/inference'
```


## kit.adapters.cgi_control.CgiControl

```python
class CgiControl(ControlPlane)
```

Control plane backed by the device's existing `entry.cgi` endpoints.

Signature mirrors the other adapters (accepts and ignores extra kw) so the
registry can construct it with the same `**kw` it passes everywhere.

继承接口：[kit.adapters.official.ControlPlane](kit-adapters-official.md)。

### kit.adapters.cgi_control.CgiControl.__init__

```python
def __init__(self, host: str='127.0.0.1', port: int=443, use_tls: bool=True, model_id: int=0, timeout: float=10.0, frame_url: Optional[str]=None, verbose: bool=True, **_ignored)
```

构造实例并保存/校验上述参数；参数默认值见签名。是否在构造时打开设备或加载模型，以本类的生命周期说明为准；构造方法返回 None。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/cgi_control.py#L82)

### kit.adapters.cgi_control.CgiControl.set_inference

```python
def set_inference(self, *, enable: bool, model: Optional[str]=None, fps: Optional[int]=None) -> dict
```

Enable/disable inference and optionally switch model / set NPU fps.

Maps to POST /model/inference?id=<model_id> with a JSON body carrying
only the fields actually supplied (the handler treats each key as an
independent optional update).

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/cgi_control.py#L267)

### kit.adapters.cgi_control.CgiControl.get_inference

```python
def get_inference(self) -> dict
```

Read current inference state (helper for verification / callers).

Returns the handler payload, e.g.
{"iEnable","sModel","iFPS","iActualFPS","sStatus", ...}.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/cgi_control.py#L315)

### kit.adapters.cgi_control.CgiControl.snapshot

```python
def snapshot(self) -> bytes
```

Grab one frame via the kit FrameSource and return JPEG bytes.

entry.cgi has no frame-grab endpoint, so this proxies a single frame
through whichever FrameSource the capability registry selects (official
broker or ffmpeg RTSP workaround), then JPEG-encodes it with OpenCV.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/cgi_control.py#L323)
