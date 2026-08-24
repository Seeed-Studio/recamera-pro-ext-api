# GStreamer 接入：把帧代理的 dma-buf 帧喂进 GStreamer pipeline

> 事实来源：帧代理线格式 `librecamera_ext` C ABI（`recamera_ext.h`：`rc_ext_frame_*`，
> 96 字节 `frame_hdr` + 每帧一个 dma-buf fd）、设备实测（本文 §实测）。
> 桥接只用 GStreamer 公开 API（`GstDmaBufAllocator` / `appsrc` / `GstVideoMeta`）。

## 定位（先读这段）

方案商在自己的进程里，把帧代理扇出的相机帧接进标准 GStreamer pipeline，去做编码、推流、转码、显示或二次处理。

- **在设备已有匹配扩展固件后，桥接是纯方案商侧代码。** 无需为每个
  GStreamer 应用再改/重编固件或动分区；你的进程连
  `/run/recamera/frame.sock` 拿帧，用 GStreamer 的公开 dmabuf API 把 fd 包成
  `GstBuffer`，其余是标准 pipeline。
- **首选零拷贝**：dma-buf fd → `GstDmaBufAllocator` 包 `GstBuffer`（带 plane 布局的
  `GstVideoMeta`）→ `appsrc` → 下游 element。帧内存不复制，只把 fd 交给 GStreamer。
- rkipc 自身已用 MPP 出 H.264/H.265 并提供 RTSP；帧代理解决的是"把**原始帧**接进
  别的框架"这件事，不与官方编码流冲突。

## 前置条件与设备支持（已在 RV1126B recamera_v2 实测）

| 组件 | 设备实测结果 |
|------|------|
| GStreamer | 1.22.6 |
| `appsrc` | 有（`/usr/lib/gstreamer-1.0/libgstapp.so`） |
| `GstDmaBufAllocator` | 有（`libgstallocators-1.0.so` + `GstAllocators-1.0.typelib`） |
| `GstVideoMeta` | 有（`GstVideo-1.0.typelib`） |
| Python gi（PyGObject） | 有；`gi.require_version` 可加载 Gst/GstApp/GstAllocators/GstVideo |
| 可用编码/输出 element | `videoconvert`、`jpegenc`、`pngenc`、`filesink`、`fakesink`、`rtph264pay`、`rtspclientsink`、`udpsink`、`v4l2src/sink` |
| **无** 的 element | RK 硬件编码插件（无 `mpph264enc`/`mpph265enc`/`rockchipmpp`）、`kmssink`、`multifilesink` |

结论：**zero-copy dmabuf → appsrc → pipeline 在本固件上可用且已实测**。下游软件路径
（`videoconvert` + `pngenc`/`jpegenc` + `filesink`）可直接编码/存帧。GStreamer 侧**无 RK
硬件编码插件**——要 H.264/H.265 硬编，用 FFmpeg 路（见 `ffmpeg-integration.md`）里的 MPP
方案，或直接消费 rkipc 已有的 RTSP。

## 帧代理线格式（桥接需要的字段）

- socket：`/run/recamera/frame.sock`，`AF_UNIX` + `SOCK_SEQPACKET`。
- 握手：发 `Hello{version_min=1,version_max=1}`，收 `HelloAck`（error 字段==0 即成功）。
- 订阅：发 `FrameSubscribe{fps_divisor=1}`，收 `FrameSubscribeAck`（含 width/height/pool_depth）。
- 每帧一条 SEQPACKET：**96 字节 `frame_hdr` + 经 `SCM_RIGHTS` 传来的一个 dma-buf fd**
  （用 `MSG_CMSG_CLOEXEC` 收）。
- `frame_hdr`（小端）：`magic('RCFR')/ver/flags/seq/pts_us/w/h/fourcc/buf_size/chn_id/n_planes`
  + `plane[3]{offset,stride,vstride}`。当前默认 **1280×720 NV12**（`plane[0]=Y, plane[1]=UV`）。
- **消费完发 8 字节 `<seq>` 回代理**做归还，并 `close()` 自己那份 fd（`SCM_RIGHTS` 会 dup
  一份底层 open file，双方各自 close）。**必须归还**，否则很快耗尽 pool 而丢帧。

> 拿 fd 也可以走 SDK C ABI：`rc_ext_frame_open()` → `rc_ext_frame_next(&f, timeout)`
> 后 `f.fd` 即 dma-buf fd，`f.plane[i]` 即布局，`rc_ext_frame_release(&f)` 归还。
> C 程序推荐直接用它，省去自己实现握手/protobuf。

## 桥接核心：dma-buf fd → GstBuffer（零拷贝）

关键三步：`GstDmaBufAllocator` 把 fd 包成 `GstMemory`；`append_memory` 组成 `GstBuffer`；
`gst_buffer_add_video_meta_full` 写入真实 plane 的 offset/stride，让下游按 vstride 对齐读取，
**不要**让下游从 width/height 反推布局。

### Python（gi / PyGObject）— 已实测

```python
import gi
gi.require_version("Gst", "1.0"); gi.require_version("GstApp", "1.0")
gi.require_version("GstAllocators", "1.0"); gi.require_version("GstVideo", "1.0")
from gi.repository import Gst, GstAllocators, GstVideo

Gst.init(None)
pipe = Gst.parse_launch(
    "appsrc name=src is-live=true format=time do-timestamp=true "
    "! videoconvert ! pngenc ! filesink location=/tmp/frame.png")
src = pipe.get_by_name("src")
src.set_property("caps", Gst.Caps.from_string(
    "video/x-raw,format=NV12,width=%d,height=%d,framerate=30/1" % (W, H)))
dmabuf = GstAllocators.DmaBufAllocator.new()
pipe.set_state(Gst.State.PLAYING)

# 对每一帧（fd, buf_size, plane 布局来自 frame_hdr / rc_ext_frame_next）：
#   注意 alloc() 需显式把 allocator 作为第一个实参（gi 的 static 形式）。
#   allocator 会接管所交 fd（释放 GstMemory 时 close 它），所以传 os.dup(fd)，
#   自己那份 fd 仍要 close 并向代理发 seq 归还。
mem = GstAllocators.DmaBufAllocator.alloc(dmabuf, os.dup(fd), buf_size)
buf = Gst.Buffer.new()
buf.append_memory(mem)
GstVideo.buffer_add_video_meta_full(
    buf, GstVideo.VideoFrameFlags.NONE, GstVideo.VideoFormat.NV12, W, H,
    2, [p0_off, p1_off, 0, 0], [p0_stride, p1_stride, 0, 0])
src.push_buffer(buf)          # -> Gst.FlowReturn.OK

os.close(fd)                  # 自己那份 fd
sock.send(struct.pack("<Q", seq))   # 向帧代理归还此帧
```

完整可运行示例（含握手/订阅/收 fd）：本仓库
`recamera_rk/m2_scratch/dev/gst_dmabuf_test.py`。

### C（用 SDK C ABI 拿 fd，最省事）

```c
#include <gst/gst.h>
#include <gst/allocators/gstdmabuf.h>
#include <gst/video/video.h>
#include "recamera_ext.h"

GstAllocator *dmabuf = gst_dmabuf_allocator_new();
rc_ext_frame_t *h = rc_ext_frame_open(NULL, &err);
rc_ext_frame_buf_t f;
while (rc_ext_frame_next(h, &f, 1000) == 0) {
    /* allocator 接管 fd -> 用 dup(f.fd) 交给它 */
    GstMemory *mem = gst_dmabuf_allocator_alloc(dmabuf, dup(f.fd), f.buf_size);
    GstBuffer *buf = gst_buffer_new();
    gst_buffer_append_memory(buf, mem);
    gsize offset[GST_VIDEO_MAX_PLANES] = { f.plane[0].offset, f.plane[1].offset };
    gint  stride[GST_VIDEO_MAX_PLANES] = { f.plane[0].stride, f.plane[1].stride };
    gst_buffer_add_video_meta_full(buf, GST_VIDEO_FRAME_FLAG_NONE,
        GST_VIDEO_FORMAT_NV12, f.width, f.height, 2, offset, stride);
    gst_app_src_push_buffer(GST_APP_SRC(appsrc), buf);  /* 消费 buf 引用 */
    rc_ext_frame_release(h, &f);   /* 归还该帧 + close f.fd */
}
```

## 下游 pipeline 怎么接

`appsrc` 之后是标准 GStreamer，随设备已装 element 组合：

- 存一帧 PNG：`... ! videoconvert ! pngenc ! filesink location=/tmp/frame.png`
- 连续 JPEG/转码：`... ! videoconvert ! jpegenc ! ...`
- 推 RTSP（到你自己的 RTSP 服务器，H.264 软编需自带编码器 element；本固件无软/硬 H.264
  enc，此路需方案商自带插件）：`... ! videoconvert ! <h264enc> ! rtph264pay ! rtspclientsink`
- 丢弃只测吞吐：`... ! videoconvert ! fakesink sync=false`

`videoconvert` 会按 `GstVideoMeta` 的 stride/offset 正确读取带 padding 的 NV12（本固件
1280×720 的 Y stride 有对齐 padding），无需手工去 padding。

## 退化路径（若某设备缺 dmabuf/appsrc）

若目标固件的 GStreamer 缺 `libgstallocators` 或 `appsrc`，退化为方案商侧 `mmap`+`memcpy`
成紧凑 raw 帧后喂 `appsrc`（`caps` 仍是 `video/x-raw,format=NV12,...`，但用
`Gst.Buffer.new_wrapped(bytes)`）。**此路丢零拷贝**（多一次 CPU 拷贝），但兼容性最好。
本固件不需要退化——已实测零拷贝路可用。

## 实测

设备：RV1126B / recamera_v2（`192.168.42.1`），跑方案商侧 `/userdata/rkipc.m2` 帧代理
（不改 `/oem/usr/bin/rkipc` 原厂二进制）。

- **已实测 · PNG 快照**：`gst_dmabuf_test.py png` → dma-buf fd 经 `GstDmaBufAllocator` →
  `GstBuffer`+`GstVideoMeta` → `appsrc ! videoconvert ! pngenc ! filesink` →
  产出 1280×720 PNG（1.9 MB，画面正常、颜色正确）。
- **已实测 · 连续吞吐**：`gst_dmabuf_test.py bench 90` → 零拷贝喂
  `appsrc ! videoconvert ! fakesink`，**90 帧 3.03s，29.8 fps**（跟满相机 30fps）。
- dmesg 全程干净（无 `fifo overflow`/`csibdg`/`oops`/`paging`）。

证据图：`recamera_rk/m2_scratch/dev/evidence/gst_frame.png`。
