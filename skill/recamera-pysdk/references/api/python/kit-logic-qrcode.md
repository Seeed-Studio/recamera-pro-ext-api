# kit.logic.qrcode

[API 索引](../index.md) · [接口特性与边界](../features.md)

源码基线：[kit/logic/qrcode.py](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/logic/qrcode.py)；签名由 AST 提取，不导入硬件依赖。

二维码 CPU 解码器，运行时后端可用性决定支持范围；decode 输入为图像，返回业务结果。

源码说明中的历史验证记录、迁移目标和旧示例不代表当前固件可用；应用入口、资源准入和接口可用性以本页补充及接口特性文档为准。

## 模块契约（源码）

qrcode.py -- CPU QR-code decoder (reCamera Pro logic lib).

Pure OpenCV + numpy, no NPU / no extra Python dependency. Decodes several QR
codes from one frame (mirrors the first-gen `qrcode-reader` C++ app, which used
quirc to decode multiple codes per frame). Stateless per frame, cheap enough to
run on the ARM cores every frame.

Two cv2 backends, auto-selected at construction:

  * ``cv2.QRCodeDetector`` (upstream objdetect) when the build exposes it --
    ``detectAndDecodeMulti`` handles multiple codes, no model files needed.
  * ``cv2.wechat_qrcode.WeChatQRCode`` (opencv_contrib) otherwise. The reCamera
    Pro firmware's slim cv2 4.6.0 ships ONLY this one (no QRCodeDetector), so it
    is the path used on device. It needs four small CPU (Caffe) model files --
    ``detect.prototxt / detect.caffemodel / sr.prototxt / sr.caffemodel`` -- in
    ``model_dir``. These are NOT NPU models; they run on the ARM cores. (Calling
    the WeChatQRCode empty constructor segfaults the firmware build, so real
    model paths are mandatory.)

Output shape (one dict per successfully decoded, non-empty code):
    {"text": <decoded string>, "quad": [[x,y], [x,y], [x,y], [x,y]]}
`quad` are the four corner points (integer pixels, original frame coords),
suitable for drawing an overlay polygon.

## kit.logic.qrcode.QrDecoder

```python
class QrDecoder
```

Stateless multi-QR decoder over RGB/BGR uint8 frames.

QR codes are monochrome, so RGB-vs-BGR channel order is irrelevant to
decoding. Reusing one detector across frames avoids re-allocating it each
call. `model_dir` is only consulted for the WeChatQRCode backend.

### kit.logic.qrcode.QrDecoder.__init__

```python
def __init__(self, model_dir: Optional[str]=None, isolate: Optional[bool]=None) -> None
```

构造实例并保存/校验上述参数；参数默认值见签名。是否在构造时打开设备或加载模型，以本类的生命周期说明为准；构造方法返回 None。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/logic/qrcode.py#L211)

### kit.logic.qrcode.QrDecoder.decode

```python
def decode(self, frame: np.ndarray) -> List[Dict[str, Any]]
```

使用已选择的可用后端解码图像，返回二维码结果列表；未检测到或当前无可用解码结果时返回空列表。后端依赖可能为 OpenCV 或 zxing。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/logic/qrcode.py#L247)

### kit.logic.qrcode.QrDecoder.crashes

```python
@property
def crashes(self) -> int
```

How many native decoder crashes were absorbed (isolated backend).

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/logic/qrcode.py#L257)

### kit.logic.qrcode.QrDecoder.close

```python
def close(self) -> None
```

关闭本对象持有的连接/线程/设备等资源。具体幂等性、在途任务及失败处理见该类生命周期说明；不要在关闭后继续发送或读取。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/logic/qrcode.py#L261)
