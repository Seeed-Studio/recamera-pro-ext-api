# 硬件预处理加速（RGA letterbox / `App.model_frame`）

> 适用设备：reCamera Pro（RV1126B / recamera_v2）。
> 读者：**方案商 / app 开发者**——想让自己的视觉 app 少花每帧几十毫秒在 Python 缩放上，**只改一行类属性**。
>
> 事实来源：`kit/app.py`（`App.model_frame` 声明 + 主循环选路）、`kit/adapters/official.py`（`_convert` 三态返回、`_rga_letterbox()`、三层回退）、`kit/adapters/_rga.py`（librga ctypes 绑定）、`kit/adapters/frame_source.py`（`Frame.model_data` / `Frame.model_info`）、`kit/runtime/preprocess.py`（`letterbox()` / `LetterboxInfo` 几何契约）。契约测试 `kit/adapters/test_official.py`。真机 A/B 原始记录见下方 §4。

## 0. 定位

模型推理前要把相机帧变成模型输入：**等比缩放 + 灰边填充（letterbox）**。默认这步在 Python 里做，1280×720 → 640×640 实测 **38–43 ms/帧**。

RV1126B 的 **RGA**（2D 图形加速器）能直接对帧代理给出的 dma-buf 做 NV12 缩放 + 色彩转换。app 侧开启方式是**一行类属性**，不改 manifest、不改帧源、不写任何 RGA 代码。

**但收益只在一种模式下成立**（2026-08-14 真机 A/B，详见 §4）：

- **`hw-direct`：端到端 +55%**。真正的赢点不是"省掉 Python letterbox"，而是**连全分辨率 NV12→RGB 转换都跳过了** —— 模型只需要 640×640，就不必先转出一张 1280×720 的 RGB。
- **`hw`：+0.8%，即噪声**。它保留原图，所以**仍要付全分辨率转换**，只是把 letterbox 挪给 RGA 并额外多做一次 RGA resize。分段计时里 `pre` 从 40 ms 变成 0 是**仪表假象**（这步移到了计时点之前），吞吐并没有变。

> 结论：**只有能放弃原始分辨率像素的 app 才拿得到加速。** 需要原图的 app 目前留在 `"cpu"`。

## 1. 如何调用

在你的 `App` 子类上设 `model_frame`：

```python
from kit.app import App, run_app

class MyApp(App):
    id = "my-app"
    postproc = "detect"
    model_frame = "hw"          # ← 就这一行
```

三个取值：

| 取值 | 行为 | `frame.data` 是什么 | 适用 |
|---|---|---|---|
| `"cpu"`（默认） | 主循环里 Python letterbox | 全分辨率原图 | 需要原图像素、且没走 `crop_roi_hw` 的 app |
| `"hw"` | RGA 产出 letterbox 放进 `frame.model_data`，**同时**保留原图 | **全分辨率原图** | 机制可用，但**实测无吞吐收益**，默认不开 |
| `"hw-direct"` | RGA 产出的 letterbox **就是** `frame.data`，连全分辨率 NV12→RGB 转换也省掉 | **模型尺寸的 letterbox 图** | **只消费框 / 关键点坐标、从不读 `frame.data`** 的 app（+55%） |
| `"hw-roi"` | 同 `hw-direct`（跳过全分辨率转换），但**保留 NV12 dma-buf**，app 用 `self.crop_roi_hw(frame, box, out_size, pad)` 按需从 dma-buf 上 RGA 裁 ROI | **模型尺寸的 letterbox 图** | **检测→裁 ROI→二级模型**的级联 app（face / facemesh / ppocr），ROI 必须走 `crop_roi_hw`，收益**待真机 A/B**（见 §7） |

### 怎么选

**判据只有一条：推理之后，你的 app 还需要读原始分辨率的像素吗？**

- **不需要**（只用框 / 关键点坐标）→ **`"hw-direct"`**，实测 +55%。
- **需要，且是"裁 ROI / 透视裁剪喂二级模型"这类级联**（face / facemesh / ppocr）→ **`"hw-roi"`**：把每次 `crop_square_roi(frame.data, …)` / `perspective_crop(frame.data, …)` 换成 `self.crop_roi_hw(frame, …)`（见 §7），ROI 直接从 dma-buf 上 RGA 裁，省掉每帧全分辨率 NV12→RGB。收益**待真机 A/B**。
- **需要原图、但不方便走 `crop_roi_hw`** → **留在 `"cpu"`**。`"hw"` 在这类 app 上实测没有吞吐收益（§4），还会让结果与 CPU 路径不再逐像素一致；除非你在自己的场景里实测出收益，否则不必开。

> ⚠️ `"hw-direct"` 下 `frame.data` **不再是原始像素**，而是 letterbox 后的模型图。如果 app 还去裁它，裁到的是缩放+带灰边的图 —— 这是唯一会出错的用法，所以判据要照上面走。

仓库内 9 个 app 的实际取值：

| 模式 | app |
|---|---|
| `hw-direct` | `fall-detection`、`retail-vision`、`yolo-detector`、`fitness-trainer` |
| `hw-roi`（dma-buf 裁 ROI，见 §7） | `face-analysis`（已接入示范） |
| `cpu`（需要原图像素） | `facemesh-reader`、`ppocr-reader`（可按 §7 迁到 `hw-roi`，暂未迁） |
| 不适用 | `qrcode-reader`（无模型推理）、`voice-transcribe`（无视频帧模型；其默认 RK ASR 仍通过 `ExternalNpuLease`/rkipc broker 使用 NPU） |

## 2. 坐标契约（三种模式完全一致）

**无论哪种模式，`frame.w` / `frame.h` 始终是原始相机几何**，后处理产出的框 / 关键点坐标也始终在原始像素空间。

- 变换信息统一由 `frame.model_info` 携带，类型是 `kit/runtime/preprocess.py` 的 `LetterboxInfo`（`scale` / `pad_w` / `pad_h` / `orig_w` / `orig_h`），**与 CPU `letterbox()` 产出的同构**。
- 后处理反映射、结果 sink 的归一化（按 `frame.w/h`）两条路径共用同一套代码，不需要判断帧从哪来。

主循环选路（`kit/app.py`）：

```python
info   = getattr(frame, "model_info", None)
padded = getattr(frame, "model_data", None)      # "hw"
if padded is None:
    if info is not None:
        padded = frame.data                       # "hw-direct"
    else:
        padded, info = letterbox(frame.data, self.input_size)   # "cpu"
```

灰边填充值为 **114**（与 CPU 路径一致）。

## 3. 回退与约束

开关只表达**意图**。以下任一情况不满足，都会**自动回退到 CPU letterbox**，几何契约不变，不报错、不中断：

| 前提 | 不满足时 |
|---|---|
| `needs_model = True` | 纯 CPU app 本来就不做 letterbox |
| 帧源是官方帧代理（`/run/recamera/frame.sock`），能给 dma-buf fd | RTSP / snapshot 后端无 fd → 走 CPU |
| librga 可用且导出 `imresize_t` | 旧固件符号缺失 → 走 CPU |
| NV12 几何为偶数（源和目标） | 奇数几何 → 走 CPU |
| Y 平面 offset == 0 | fd-wrap 前提不成立 → 走 CPU |
| 模型输入为正方形（`input_size` × `input_size`） | 非方形输入不适配 |

回退是**三层**的：RGA letterbox 失败 → 全分辨率 RGA 转换 → OpenCV。任一层出错只 latch 关掉该层优化并打日志，不会把 app 带崩。

`model_frame` 写成非法值（如 `"HW"`）会**直接报错**，不静默降级 —— 避免手滑导致白白没有加速。

设备日志里可以确认实际走了哪条：

```
[OfficialFrameSource] preprocess backend: RGA (hardware)
[OfficialFrameSource] preprocess path: RGA direct NV12 resize 1280x720 -> RGB 640x640 + gray pad
[OfficialFrameSource] preprocess path: RGA aux NV12 resize 1280x720 -> RGB 640x640 + gray pad
```
`direct` = `hw-direct`，`aux` = `hw`。出现 `latching to full RGB` / `latching to OpenCV` 即表示已回退。

## 4. 实测数据

### `hw-direct`（真机 A/B，2026-08-13）

设备 `recamera-pro-test`（RV1126B，固件 V1.0.4，内核 6.1.157），1280×720 NV12 帧代理，模型输入 640×640，pose 模型，经官方 appMgr 单活 API 切换，测试期间无第二个相机/NPU 应用。原始记录：`apps/fall-detection/evaluation/pro-rga-preprocess-ab-20260813.md`。

| 路径 | 端到端 WS fps（12.02 s） | appMgr fps | preprocess | RKNN 推理 | 后处理 |
|---|---:|---:|---:|---:|---:|
| **RGA direct** | **18.13** | 17.5–18.9 | **0.0 ms** | 42.0–45.3 ms | 1.9–2.4 ms |
| Python letterbox（回退） | 12.14 | 11.4–12.4 | 38.2–43.1 ms | 35.3–37.9 ms | 1.3–1.6 ms |

**端到端吞吐 +49.3%,每帧去掉约 40 ms 的 Python 预处理。** 该轮无 `direct preprocess failed` / `RGA convert failed` / VPSS FIFO 错误 / 内核 Oops。

坐标契约实测：`Frame.w/h` 保持 `1280x720`，仅 `Frame.data` 为 640×640；`model_info` 为 `scale=0.5, pad_w=0, pad_h=140, orig_w=1280, orig_h=720`；灰边严格 `(114,114,114)`。

### `hw` vs `hw-direct`（真机 A/B，2026-08-14）

同设备，每组稳态 60 s，经 appMgr 单活 API 切换：

| app | 模式 | fps | pre ms | infer ms | post ms |
|---|---|---:|---:|---:|---:|
| ppocr-reader（480） | `hw` | 8.05 | 0.00 | 79.65 | 8.87 |
| ppocr-reader（480） | `cpu` | 7.99 | 40.53 | 72.30 | 4.47 |
| retail-vision（640） | **`hw-direct`** | **19.10** | 0.00 | 36.27 | 6.36 |
| retail-vision（640） | `cpu` | 12.31 | 39.29 | 31.69 | 5.02 |

- **`hw-direct`：+55.2%**，与 8-13 那轮 +49.3% 相互印证。
- **`hw`：+0.8%，噪声级，没有实际收益。** `pre` 从 40.5 ms 变 0 是**仪表假象**：`hw` 的 RGA letterbox 在 `src.frames()` 里完成，发生在分段计时起点之前，活儿移出了统计桶而不是消失。`hw` 仍然要付全分辨率 NV12→RGB，并且**多做一次 RGA resize**。
- 两种硬件模式下 `infer_ms` 都上升（ppocr +10%、retail +14%），**原因未确认**，怀疑 RGA 与 NPU 的内存带宽争用；待查。

### 像素一致性（20 帧，1280×720 → 640）

几何契约 **20/20 全对**：`frame.data` 保持 `(720,1280,3)`、`model_data` 为 `(640,640,3)`、`model_info` 与 CPU `letterbox()` **逐字段相同**、灰边严格 `114`；日志确认走的是 RGA aux 路径，无回退。

但**像素并不与 CPU 逐点相同**：`max_abs_diff` 131–137，`mean_abs_diff` 2.075，46.5% 的像素有差异、5.79% 差值 > 8。原因是重采样实现不同——`kit/runtime/preprocess.letterbox` 用 PIL BILINEAR（下采样时**带抗锯齿**），RGA 用普通 2-tap 双线性（2× 缩小时**会混叠**）。两条路径共用同一套 RGA NV12→RGB 转换，**不是色彩空间问题**。

实际影响（ppocr，12 帧同帧双路径对照）：**0/12 帧结果完全一致**。框坐标差 3.4–14.3 px；11/12 帧框数相同，1 帧 HW 检出而 CPU 漏检；文本 10/12 一致，2 帧读成 `店` / `古` 之差。HW 侧分数普遍更高（检测 0.93–0.96 vs 0.87–0.90，识别 0.64–0.67 vs 0.47–0.56）——**不是质量下降，但结果在两种模式间不可复现**。切换模式会改变输出，不要假设逐位一致。

### ROI 契约（`hw` 模式）

对 `crop_square_roi` / `perspective_crop` 注入合成检测强制走一遍：两者拿到的都是 `(720,1280,3)` —— **确认是原始分辨率，不是模型图**。`hw` 模式的语义正确。

### 未验证 / 存疑

- **face-analysis 的 A/B 无效**：现场是空办公室，零检出，12/12"一致"属于空对空，需要有人脸的场景重测。
- ppocr 的 OCR 样本只有画面边缘弧面包装上的一个小字，**对识别质量的证据强度弱**。
- `infer_ms` 上升的原因未定位。

## 5. 自己直接调用 RGA？

`kit/adapters/_rga.py` 的 `RgaNV12ToRGB` 接受的是 **dma-buf fd + NV12 平面信息**（`resize_nv12_to_rgb(...)`、`crop_nv12_to_rgb(...)`），不是 numpy 数组。它服务的是"帧代理原始缓冲区 → 模型输入"这一段。

原来这里写着"app 里对 numpy 图像做的二次裁剪用不上这个组件、要上硬件需另开一条 numpy/fd 路径、不在实现范围内"。**这条路径现在实现了**，见 §7：`model_frame = "hw-roi"` + `App.crop_roi_hw(frame, box, out_size, pad)` 让级联 app 在 `frame.data` 之外，直接从 dma-buf 上 RGA 裁 ROI，不必先转出全分辨率 RGB 再在 numpy 上裁。

## 6. 一句话

**不读原图像素的视觉 app 加一行 `model_frame = "hw-direct"`，实测端到端 +55%**（赢在跳过全分辨率 NV12→RGB，不只是省 Python letterbox）；**检测→裁 ROI→二级模型的级联 app 用 `model_frame = "hw-roi"` + `self.crop_roi_hw(...)`，从 dma-buf 上按需裁 ROI，同样跳过全分辨率转换（收益待真机 A/B，§7）**；其余需要原图像素的 app 留在 `"cpu"` —— `"hw"` 机制可用但实测无吞吐收益。几何契约、灰边值、后处理代码在各模式下一致，任何前提不满足都自动回退，不会出错；但 RGA 与 PIL 的重采样不同、且 `hw-roi` 越界处用灰边而非边缘复制，**换模式会让输出不再逐像素一致**。

## 7. `hw-roi`：级联 app 的 dma-buf 按需裁剪

### 7.1 解决什么

`hw`（§4）在需要原图的级联 app 上只有 +0.8%，原因是它**仍然生成全分辨率 RGB**：为了让 `crop_square_roi(frame.data, …)` 有原图可裁，每帧都要把 1280×720 NV12 转一张全分辨率 RGB，内存带宽成本没省掉。

`hw-roi` 换个思路：**不预先生成全分辨率 RGB**，而是保留相机的 NV12 dma-buf，等 app 真的要某个 ROI 时，用 RGA 从 dma-buf 上**直接裁 + 缩放到目标尺寸**。stage-1 仍吃 RGA letterbox（`frame.data` 就是 letterbox 图，同 `hw-direct`）。检测框拿到后，每张脸 / 每个文本框的 ROI 走硬件裁，绕开"全分辨率转换 + numpy 裁 + PIL resize"三步。

### 7.2 怎么用（app 侧改动）

两步，控制流仍在 Python：

```python
class MyCascadeApp(App):
    model_frame = "hw-roi"          # ① 声明模式

    def run(self):
        for frame in self.frames():
            x = self.pre(frame)                    # stage-1 吃 RGA letterbox
            dets = post(self.models.det.infer(x.data), x.info, ...)
            for d in dets[:k]:
                roi, roi_map = self.crop_roi_hw(   # ② 换掉 crop_square_roi(frame.data,…)
                    frame, d["box"], OUT_SIZE, pad)
                self.models.stage2.infer(roi)
```

`crop_roi_hw` 返回 `(roi_uint8_HWC_RGB, roi_map)`，与 `kit.pipeline.crop_square_roi` **同契约**——`roi_map` 逐字段相同（两条路径共用一个几何函数 `square_roi_geometry`），可直接换用。

**`face-analysis` 已作为示范接入**（`apps/face-analysis/app.py`）：改动就是把 `model_frame = "cpu"` 改成 `"hw-roi"`，把两处 `crop_square_roi(frame.data, r["box"], …)` 改成 `self.crop_roi_hw(frame, r["box"], …)`——**约 5 行**，循环体、模型调用、跨帧聚合、事件结构一律不变。等价性由 `kit/tests/test_face_shape_equivalence.py` 守（fake source 不提供硬件 cropper 时，`crop_roi_hw` 自动回退到同一个 numpy `crop_square_roi`，逐字段对拍旧路径仍全过）。

### 7.3 契约与回退

- **`frame.data` 在 `hw-roi` 下是 letterbox 图，不是原图**。ROI 必须走 `crop_roi_hw`，不能再直接裁 `frame.data`（会裁到缩放+带灰边的模型图）。`frame.w/h` 与后处理坐标仍是原始相机几何。
- **越界填充差异**：`crop_square_roi` 对超出画面的方框边缘做 **edge 复制**；RGA 路径填 **灰 114**。几何（裁哪块、缩到多大、`roi_map`）完全一致，只有越界那圈边像素不同——与 §4 记录的"换模式不逐像素一致"同类。
- **多级回退，任一前提不满足都不报错**：
  | 前提 | 不满足时 |
  |---|---|
  | librga 可用 | → 全分辨率 + numpy 裁（`crop_roi_hw` 自动回退） |
  | librga 导出 `improcess_t`（裁剪算子） | 源退回 `hw`：`frame.data` 恢复为原图、不挂 cropper，`crop_roi_hw` 走 numpy | 
  | 帧源是官方 dma-buf 帧代理 | RTSP/snapshot 无 fd → 无 cropper → numpy 裁原图 |
  | Y 平面 offset == 0、模型输入为方形 | 不满足 → letterbox 那步先回退，连带无 cropper |
  单次硬件裁失败（罕见，正常由首帧探测拦掉）→ 打日志 + 返回灰 ROI，**绝不**拿 letterbox 当原图裁出错误区域，也不会把循环带崩。

### 7.4 实现位置

- `kit/adapters/_rga.py`：`crop_nv12_to_rgb(fd, w, h, y_stride, y_vstride, src_rect, dst_size, dst_window, out, pad_value)`——`improcess_t` 一次完成 NV12 dma-buf 裁剪 + 缩放 + NV12→RGB；`can_crop()` 探测 `improcess_t` 是否存在。NV12 色度 2×2 子采样，源矩形按偶数对齐。
- `kit/pipeline.py`：`square_roi_geometry(frame_h, frame_w, box, out_size, pad)`——padded-square 几何的唯一实现，`crop_square_roi` 与 `hw-roi` 共用，保证 `roi_map` 逐字节一致。
- `kit/adapters/official.py`：`hw_roi` 开关、`_crop_roi()`（复用 per-size scratch buffer）、`_FrameRoiCropper`（绑定当前借用帧的 dma-buf，仅当帧步内有效）。
- `kit/app.py`：`model_frame = "hw-roi"` 选路、`App.crop_roi_hw()`（硬件优先 / numpy 回退 / 失败灰 ROI）。

### 7.5 性能

**收益（+多少 fps）待真机 A/B benchmark。** 本批只做实现 + 离线正确性验证（几何/尺寸/通道与 numpy 参考对拍、越界灰边、回退与 dispatch，见 `kit/adapters/test_rga_roi.py`），**未在设备上测过吞吐，未编造 fps 数字**。理论收益来自每帧省掉一次全分辨率 NV12→RGB（1280×720 约 2.7 MB）+ numpy 裁 + PIL resize，但 §4 已记录两个未定变量：硬件模式下 `infer_ms` 会升（疑 RGA/NPU 带宽争用），以及 RGA 与 NPU 并发的实际表现；ROI 裁剪引入的 RGA 调用次数（每帧 k 个 ROI）也需在真机上确认没有把 RGA 打满。**上设备后按 §4 的方式做稳态 A/B，再回填本节。**
> 本页已有的性能数字属于历史 RGA 路径测试。新的常驻 DMA IO 与 RGA 直写路径见
> [共享推理 IO](./shared-inference-io.md)，其实际性能需单独验证，不能沿用历史提升比例。
