# FFmpeg 接入：把帧代理的帧喂进 FFmpeg

> 事实来源：帧代理线格式 `librecamera_ext` C ABI（`recamera_ext.h`：`rc_ext_frame_*`，
> 96 字节 `frame_hdr` + 每帧一个 dma-buf fd）、设备实测（本文 §实测）。
> 桥接分两层：命令行 `ffmpeg`（rawvideo 管道，已实测）；libav* C API（`DRM_PRIME`
> 零拷贝，需自带 rkmpp 版 ffmpeg）。

## 定位（先读这段）

方案商在自己的进程里，把帧代理扇出的相机帧接进 FFmpeg，去做编码、转码、封装、推流。

- **在设备已有匹配扩展固件后，桥接是纯方案商侧代码。** 无需为每个 FFmpeg
  应用再改/重编固件或动分区；你的进程连
  `/run/recamera/frame.sock`（或用 SDK `rc_ext_frame_*`）拿帧，喂给 FFmpeg。
- 两条路：
  1. **命令行 `ffmpeg`（已实测，最省事）**：`mmap` 读帧 → 紧凑 NV12 写进
     `ffmpeg -f rawvideo -pix_fmt nv12 -s WxH -i -` 的 stdin。有一次 CPU 拷贝（丢零拷贝），
     但零依赖、开箱即用。
  2. **libav\* C API + `AV_PIX_FMT_DRM_PRIME`（零拷贝，条件性）**：dma-buf fd →
     `AVDRMFrameDescriptor` → `AVFrame`，直接喂硬件编码器。**需要一个开了
     `--enable-rkmpp`（或有可用 V4L2 M2M 编码节点）的 ffmpeg**——本固件自带的 ffmpeg
     两者都没有（见下），所以此路给出正确代码供方案商在自带 ffmpeg 上用。

## 前置条件与设备支持（已在 RV1126B recamera_v2 实测）

设备自带 `/usr/bin/ffmpeg`：

| 能力 | 设备实测结果 |
|------|------|
| 版本 | ffmpeg 4.4.4（aarch64，`--enable-libdrm`） |
| DRM 硬件加速 | `-hwaccels` 列出 `drm`；`-pix_fmts` 有 `drm_prime`（`..H..` 硬件像素格式） |
| `rawvideo` demuxer | 有（`-f rawvideo -pix_fmt nv12`） |
| 可用编码器 | `mjpeg`（软）、`h264_v4l2m2m`、`hevc_v4l2m2m`（V4L2 M2M 封装器） |
| **未编入** | 无 `--enable-rkmpp`（无 `h264_rkmpp`/`hevc_rkmpp`）、无 libx264/libx265 |
| V4L2 M2M 编码节点 | **不可用**：设备 RK 硬编经 `/dev/mpp_service`（MPP 库），非标准 V4L2 M2M；实测 `h264_v4l2m2m` 报 `Could not find a valid device` |

结论：

- **rawvideo 管道 + 软件编码（mjpeg 等）：已实测可用。**
- **`h264_v4l2m2m` / `hevc_v4l2m2m`：本固件不可用**（RK 编码器是 MPP `/dev/mpp_service`，
  ffmpeg 未走 rkmpp）。要在设备上硬编 H.264/H.265，方案商需自带一个开了 `--enable-rkmpp`
  的 ffmpeg，或直接消费 rkipc 已提供的 RTSP（`rkipc` 内部用 `librockchip_mpp.so` 硬编）。

## 帧代理线格式

同 `gstreamer-integration.md` §帧代理线格式：`/run/recamera/frame.sock`（`AF_UNIX` +
`SOCK_SEQPACKET`）→ Hello/HelloAck → FrameSubscribe/Ack → 每帧 96B `frame_hdr` + 一个
dma-buf fd（`SCM_RIGHTS`）→ 消费后发 8 字节 `<seq>` 归还并 close 自己的 fd。默认
1280×720 NV12，`plane[0]=Y (offset/stride/vstride)`、`plane[1]=UV`。

用 SDK 更省事：`rc_ext_frame_open` → `rc_ext_frame_next(&f,ms)` 拿 `f.fd`/`f.plane[]`/
`f.pts_us`，`rc_ext_frame_map(&f)` 得 CPU 可读指针（已做 dma-buf cache sync），
`rc_ext_frame_release(&f)` 归还。

## 路 1：rawvideo 管道 → 命令行 ffmpeg（已实测）

方案商侧读帧 → **去掉 stride padding 拼成紧凑 NV12**（Y 每行取前 W 字节、UV 每行取前 W
字节）→ 写进 `ffmpeg` 的 stdin。

```python
import subprocess, struct
W, H = 1280, 720
cmd = ["ffmpeg", "-hide_banner", "-y",
       "-f", "rawvideo", "-pix_fmt", "nv12", "-s", "%dx%d" % (W, H), "-r", "30",
       "-i", "-",
       "-frames:v", "60", "-c:v", "mjpeg", "/tmp/snap.jpg"]   # 软编，实测可用
ff = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)

# 对每一帧（mm = mmap 的 dma-buf，plane 布局来自 frame_hdr / rc_ext_frame_*）：
#   dma-buf 需在读前后做 cache sync（DMA_BUF_IOCTL_SYNC START/END|READ）；
#   用 SDK 的 rc_ext_frame_map()/release() 会自动帮你 sync。
packed = bytearray()
for row in range(H):                       # Y
    base = p0_off + row * p0_stride
    packed += mm[base:base + W]
for row in range(H // 2):                  # UV (NV12 交织)
    base = p1_off + row * p1_stride
    packed += mm[base:base + W]
ff.stdin.write(packed)
sock.send(struct.pack("<Q", seq))          # 归还该帧

ff.stdin.close(); ff.wait()
```

完整可运行示例（握手/订阅/收 fd/mmap/sync）：本仓库
`recamera_rk/m2_scratch/dev/ffmpeg_feed_test.py`（`mjpeg` 已实测；`h264` 分支用于在
自带 rkmpp/V4L2M2M-enc 的 ffmpeg 上验证）。

> 硬件编码怎么办：把上面的 `-c:v mjpeg` 换成 `-c:v h264_rkmpp`（需自带 rkmpp 版 ffmpeg），
> 输入仍是同一条 rawvideo 管道。或改推流：
> `... -i - -c:v <enc> -f rtsp rtsp://<你的服务器>/live`。

## 路 2：dma-buf fd → DRM_PRIME AVFrame（零拷贝，需自带 rkmpp ffmpeg）

用 libav* C API 时，不必把帧拷进内存：把 dma-buf fd 描述成 `AVDRMFrameDescriptor`，挂到
一个 `AV_PIX_FMT_DRM_PRIME` 的 `AVFrame` 上，直接喂给硬件编码器（如 `h264_rkmpp`）。
**这段是给自带 rkmpp/DRM-PRIME-encoder 的 ffmpeg 用的正确代码**（本固件自带 ffmpeg 无此
编码器，未在设备实测）。

```c
#include <libavutil/hwcontext.h>
#include <libavutil/hwcontext_drm.h>
#include <libavutil/frame.h>
#include "recamera_ext.h"      // rc_ext_frame_* : f.fd / f.plane[] / f.buf_size

// 每帧：把帧代理的 dma-buf fd 包成一个 DRM_PRIME AVFrame（NV12，两平面同一 object）。
AVFrame *frame = av_frame_alloc();
frame->format = AV_PIX_FMT_DRM_PRIME;
frame->width  = f.width;
frame->height = f.height;

AVDRMFrameDescriptor *desc = av_mallocz(sizeof(*desc));
desc->nb_objects = 1;
desc->objects[0].fd   = dup(f.fd);          // 交给 AVFrame 生命周期管理
desc->objects[0].size = f.buf_size;
desc->objects[0].format_modifier = DRM_FORMAT_MOD_LINEAR;

desc->nb_layers = 1;
desc->layers[0].format    = DRM_FORMAT_NV12;
desc->layers[0].nb_planes = 2;
desc->layers[0].planes[0].object_index = 0;                 // Y
desc->layers[0].planes[0].offset       = f.plane[0].offset;
desc->layers[0].planes[0].pitch        = f.plane[0].stride;
desc->layers[0].planes[1].object_index = 0;                 // UV
desc->layers[0].planes[1].offset       = f.plane[1].offset;
desc->layers[0].planes[1].pitch        = f.plane[1].stride;

// 用 buf/opaque 挂上 desc，并在 free 回调里 close(desc->objects[0].fd) + av_free(desc)。
frame->data[0] = (uint8_t *)desc;
frame->buf[0]  = av_buffer_create((uint8_t *)desc, sizeof(*desc),
                                  drm_desc_free /* 你的回调 */, NULL, 0);
frame->pts = f.pts_us;   // 或按编码器时基折算

avcodec_send_frame(enc_ctx, frame);   // enc_ctx = h264_rkmpp，pix_fmt=DRM_PRIME
// ... avcodec_receive_packet 取 H.264/H.265 码流 ...
av_frame_free(&frame);                // 触发 buf[0] 释放 -> close fd
rc_ext_frame_release(h, &f);          // 归还该帧给代理
```

要点：`object_index/offset/pitch` 必须用 `frame_hdr` 的真实 plane 布局，别从 width/height
反推；两平面在同一个 dma-buf object 里，靠 `offset` 区分。

## OpenCV（一句带过）

不需要本文的桥接：SDK 的 `FrameSource` 已把帧暴露成零拷贝 numpy。

```python
from recamera_ext import FrameSource
import cv2
with FrameSource() as src:
    for frame in src:
        bgr = frame.to_bgr()     # NV12 -> BGR（一次转换拷贝）
        cv2.imwrite("/tmp/f.jpg", bgr)   # 或直接喂你的 cv2 处理
        break
```

## 实测

设备：RV1126B / recamera_v2（`192.168.42.1`），跑方案商侧 `/userdata/rkipc.m2` 帧代理
（不改 `/oem/usr/bin/rkipc` 原厂二进制）。

- **已实测 · rawvideo → mjpeg 软编**：`ffmpeg_feed_test.py mjpeg` → 从 frame.sock 收帧、
  mmap+dma-buf sync、去 padding 拼紧凑 NV12 → 管道喂 `ffmpeg -f rawvideo -pix_fmt nv12
  -s 1280x720 -i - -c:v mjpeg` → 产出有效 JPEG（82 KB，1280×720，画面正常、颜色正确，
  ffmpeg rc=0）。
- **未通过（环境不支持，非代码问题）· h264_v4l2m2m 硬编**：同一 rawvideo 管道换
  `-c:v h264_v4l2m2m`，ffmpeg 报 `Could not find a valid device` / `can't configure
  encoder`。根因：RK 编码器经 `/dev/mpp_service`（MPP），本固件 ffmpeg 未编入 rkmpp、也无
  V4L2 M2M 编码节点。**桥接代码本身正确**；设备上做硬编需自带 rkmpp 版 ffmpeg 或走 §路 2，
  或直接用 rkipc 的 RTSP。
- dmesg 全程干净（无 `fifo overflow`/`csibdg`/`oops`/`paging`）。

证据图：`recamera_rk/m2_scratch/dev/evidence/ffmpeg_snap.jpg`。
