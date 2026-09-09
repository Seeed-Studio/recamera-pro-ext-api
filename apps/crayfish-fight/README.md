# crayfish-fight — 螯虾打斗/骚扰行为监测

水槽内螯虾的攻击行为监测 app。逐帧检测每只虾、投票判定性别，对**持续靠近的虾对**在交互 ROI 上分类 `fight` / `harass`——单帧模型做即时粗筛，**8 帧拼图时序模型**看运动做确认，只上报事件（视频不出场），并按配额落盘触发样本用于再训练。

目标设备：reCamera Pro（RV1126B，3 TOPS NPU）。方案背景见 `/Users/harvest/project/crayfish-fight/docs/PLAN.md` §1.1、§3.2、§3.3。

---

## 1. 架构（四级漏斗）

```
相机帧 (NV12, 原分辨率)
  │
  ├─ self.pre()                        RGA 硬件 letterbox → 640×640 uint8
  │
  ▼ 级 1：检测（每帧）
  │   models.crayfish_det (yolo11 raw-head, int8)
  │   → kit.runtime.postprocess.detect.postprocess
  │   → box（原图像素 xyxy）+ score，单类 crayfish
  │
  ▼ 身份：kit.logic.tracker.Tracker（IoU + 中心距两段匹配）
  │   → track_id；以下一切都按 track 归属，不按检测下标
  │
  ├─ 级 A：性别（只在新 track 的前 N 帧）
  │     crop_roi_hw(box, 128, pad=+0.10) → models.sex_cls
  │     → softmax < sex_min_conf 记 unknown → 多数票 → settled 后不再跑
  │
  ▼ 级 2：靠近触发（纯算术，零推理）
  │   对每对可见 track：IoU>0 或 中心距 < k × 平均框对角线
  │   → 滑窗 proximity_hits / proximity_window（默认 5/8，约 0.5 s @10-15fps）
  │
  ▼ 级 3：行为分类（只对触发的虾对）
  │   union_box(两框) → crop_roi_hw(224, pad=+0.15) → models.behavior_cls
  │   → BehaviorStateMachine：连续 behavior_streak 帧同判定才上报，
  │     连续 behavior_release 帧未确认才释放（迟滞）
  │
  ▼ 级 4：时序确认（只对攒够 8 帧的虾对，每帧最多 temporal_budget 次）
  │   级 3 那张 224² ROI 顺手存进 PairFrameBuffer（不额外裁剪）
  │   → 攒够 8 帧 → TemporalScheduler 轮转调度（同一对间隔 ≥ temporal_stride 帧）
  │   → 2×4 拼成 896×448 → 2:1 降采样 448² → models.behavior_temporal
  │   → 置信度 ≥ temporal_min_conf 才投票，且**一票顶两票**进同一个状态机
  │
  ├─ 采集落盘：**每次触发**（不论分类结果）按配额存整帧 + ROI + json
  │            → `${APPMGR_APPDATA_DIR:-/userdata/local/appdata}/crayfish-fight/captures/<date>/`（json 带 temporal_verdict / temporal_conf）
  │
  ▼ self.emit()
      events: detection（每只虾，带 sex）+ behavior（每对，start/change/end，
              带 temporal_verdict / temporal_conf）
      results: 检测原始记录，供 /appcenter overlay 与 output mapping 消费
```

计算量：检测每帧 1 次；性别分类只在每条新 track 的前 10 帧各 1 次；行为分类只在触发帧、每对 1 次；时序分类每帧**最多 1 次**（与虾对数量无关）。行为在时间上稀疏，这是漏斗的全部意义。

### 时序确认（级 4）：原理、参数与算力

**为什么需要它。** 行为是运动：`fight` 与 `harass` 的差别在于两只虾**怎么动**，不在于某一帧长什么样。单帧分类器看不到运动，PLAN §3.3 实测它的 `fight` 召回最高只有 1/14——不是调参能救的，是输入里根本没有那个信息。

**怎么做到零架构改动。** 不换网络、不上 3D 卷积、不需要 RV1126B 的 rknn 不支持的算子：把同一对虾**连续 8 帧的 ROI 裁剪拼成一张图**，仍然交给 yolo11n-cls。运动被编码成了空间上的排列，模型自己学会读它。

```
8 张 224² ROI（级 3 已经裁好的那张，顺手存下，不额外裁）
   → 2 行 × 4 列 拼成 896×448      帧 i 落在 row=i//4, col=i%4
        ┌─────┬─────┬─────┬─────┐   上排 = 最早的 4 帧
        │  0  │  1  │  2  │  3  │   下排 = 最新的 4 帧
        ├─────┼─────┼─────┼─────┤   （阅读顺序，与训练脚本一致）
        │  4  │  5  │  6  │  7  │
        └─────┴─────┴─────┴─────┘
   → 2:1 水平降采样 → 448×448 → models.behavior_temporal
```

拼图规范的事实来源是 `/Users/harvest/project/crayfish-fight/scripts/temporal_proto.py:collage()`——**模型学的就是这个网格**，转置或换行列顺序对它就是另一张图。所以几何被单独抽成 `logic.collage_slots()` 并做了单测（8 张纯色图逐格断言位置、无缝无重叠），`app.py` 只负责按这些矩形拷像素。灰边填充值两侧都是 114（RGA 的 ROI 越界填充 = `temporal_proto.crop()` 的 PIL letterbox 填充），模型不会见到没见过的边框。

最后那步 896×448 → 448×448 是**精确的 2:1 水平降采样**（纵向本来就是 448），实现为相邻两列取均值：不依赖 cv2、没有插值核可选错、设备和主机结果一致。训练用的是 PIL BICUBIC，在真实拼图上实测两者平均绝对差 **0.1–0.43 / 255**（p99 = 1–5），远在 int8 量化台阶之内；而降采样**之前**的 896×448 画布与 `temporal_proto.collage()` 逐字节相同。

**时序判定一票顶两票。** 时序结果进的是同一个 `BehaviorStateMachine`，只是权重为 2（单帧为 1）。默认 `behavior_streak=3` 下：

| 证据组合 | 累计 streak | 是否上报 |
|---|---|---|
| 单帧 ×2 | 2 | 否 |
| 单帧 ×3 | 3 | 是 |
| 单帧 ×1 + 时序 ×1 | 3 | 是 |
| 时序 ×2 | 4 | 是 |

单帧路径保留原来的响应速度（3 帧即可起报），时序路径两次确认即可独立起报。**释放侧不加权**：`behavior_release` 仍按"判定次数"计，一次时序 `none` 只花掉 1 个 miss，不会靠一次序列判定就把事件拍死。置信度低于 `temporal_min_conf` 时时序模型**弃权**（既不投票也不计 miss），而不是投 `none`——不确定的序列读数不该去扣单帧路径正在确认的事件。

**算力：为什么几十对虾也压不垮。** 448² 的 yolo11n-cls 单次约 **13 GFLOPs**，大约是 224² 单帧的 4 倍。它不按虾对数量增长，被两个独立的闸门钉成常数：

- `temporal_budget`（默认 1）：**每帧最多 1 次时序推理**，不管有多少对虾就绪。这一条就足以把新增负载变成常数——8 只虾有 28 对，30 对和 2 对花的是同样的钱。
- `temporal_stride`（默认 4）：同一对虾两次推理之间至少隔 4 帧。@12 fps 约每 0.33 s 一个新判定，而缓冲区的 8 帧覆盖约 0.66 s——相邻两次判定的窗口重叠一半，事件不会从两个窗口之间漏过去。

调度是**最久未跑优先**（没跑过的排最前），所以就绪对数超过预算时每对轮流被服务，不会被最小的 track id 长期占住。

内存也是有界的：缓冲区是这个 app 唯一跨帧持有像素的地方，每对 8 × 224 × 224 × 3 = **1.15 MB**。`max_pairs`（默认 6，约 7 MB）封顶，超限时**按靠近持续时长降序保留**——持续靠近才像事件，贴了三帧就走的多半是两只虾擦肩而过。虾对一旦掉出触发集，缓冲立刻释放（`PairFrameBuffer.retain()`），不等状态机迟滞结束。

**上报字段。** `behavior` 事件与落盘 json 都带 `temporal_verdict` / `temporal_conf`，是该对**最近一次**序列级判定（不一定来自当前帧，因为有 stride 和 budget）。该对还没被看过时是 `null`——"序列模型说 none"和"序列模型还没看"是两回事，消费方能分得开。

**关掉它。** `temporal_enabled=false`（live 热更）立刻退回纯单帧路径，并当场释放缓冲的像素。`models/behavior_temporal_int8.rknn` 缺失时告警一次后自动降级，不会让 app 起不来。

### 代码分布

| 文件 | 内容 |
|---|---|
| `app.py` | 每帧流水线：pre / infer / post / crop / emit / 落盘 |
| `logic.py` | **跨帧业务逻辑，纯 Python**：`SexVoter` / `ProximityWindow` / `BehaviorStateMachine` / `CaptureQuota` / `CaptureDecider` / `PairFrameBuffer` / `TemporalScheduler` + 触发几何 + 拼图几何 `collage_slots()` |
| `tests/test_logic.py` | 上述逻辑的单测，Mac 上 `uv run pytest` 直接跑，不需要设备 |
| `tools/export_onnx.py` | Mac 上导出 4 个 ONNX（检测 raw-head，分类整图） |
| `tools/convert_rknn.sh` | x86 Docker 内 ONNX → RKNN int8（**本机不能跑**） |
| `manifest.json` | 4 个模型声明、config_schema、output.fields、render |

`logic.py` 不 import numpy / kit / rknn，所以它能在 Mac 上被完整测试；`app.py` 只做"这一帧要做什么"，不持有跨帧判定。

### 坐标契约

内部一律 **原图像素 xyxy**（`kit.runtime.postprocess.detect` 的输出空间）。`emit()` 的 sink（`OfficialResultSink`）按 `frame.w/h` 归一化到 `[0,1]` 后再注入 OSD——这是 AGENTS.md 里最常踩的那条约定，app 侧传像素、manifest 里声明 `"coord": "pixel_xyxy"`，与 `yolo-detector` 一致。唯一在 app 内做归一化的地方是采集 json 的 `union_box_norm`，让落盘记录与分辨率无关。

### 为什么是 `model_frame = "hw-roi"`

级 A 与级 3 都要**对相机原始帧**做 per-object crop。`hw-direct` 模式下没有 ROI cropper 且 `frame.data` 就是 letterbox，裁出来是错的像素。`hw-roi` 让 crop 直接走 RGA 读 dma-buf（同 `face-analysis`）。所有 ROI 必须走 `self.crop_roi_hw`，不能碰 `frame.data`。

---

## 2. 模型清单与指标

| id | 权重 | 输入 | 类别（顺序 = ultralytics `model.names`） | 指标 |
|---|---|---|---|---|
| `crayfish_det` | `runs/det_v1_n/best.pt`（yolo11n） | 640×640 | `crayfish`（单类） | mAP50 **0.864** |
| （备选） | `runs/det_v1_s/best.pt`（yolo11s） | 640×640 | 同上 | mAP50 **0.889** |
| `sex_cls` | `runs/sex_cls/best.pt`（yolo11n-cls） | 128×128 | `0=female, 1=male` | top1 **0.820** |
| `behavior_cls` | `runs/behavior_cls/best.pt`（yolo11n-cls） | 224×224 | `0=fight, 1=harass, 2=none` | top1 **0.788** |
| `behavior_temporal` | `runs/behavior_temporal_v3/weights/best.pt`（yolo11n-cls） | 448×448（8 帧拼图） | `0=fight, 1=harass, 2=none` | 序列级异常二分类 recall **0.95** / precision **0.905** |

指标口径：**混合域 val**（现场侧拍 + 外部公开数据），**行为分类器为小样本摸底基线**，不是现场验收数字。

类序不是约定俗成，是从权重里读出来的（`tools/export_onnx.py` 会打印并写进 `export/export_summary.json`），并写死在 `logic.py` 的 `SEX_LABELS` / `BEHAVIOR_LABELS` / `TEMPORAL_LABELS` 与 `manifest.json` 的 `models[].classes`。**改模型必须同步这三处**，否则 label 会整体错位且没有任何报错。

---

## 3. 部署步骤

依据 `docs/guide/model-onboarding.md`。

### 3.1 导出 ONNX（Mac）

```bash
cd /Users/harvest/project/crayfish-fight
uv run --with ultralytics --with onnx --with onnxslim python \
  /Users/harvest/project/recamera/recamera_pro/apps/crayfish-fight/tools/export_onnx.py \
  --runs-dir ./runs --out-dir ./export

# 检查 IR / opset / 输出名
uv run --with onnx python \
  /Users/harvest/project/recamera/recamera_pro/models/convert/inspect_onnx.py export/det_v1_n.onnx
```

检测器导出的是 **raw head**（6 个 leaf-Conv：3×box[1,64,H,W] + 3×cls[1,1,H,W]），DFL/NMS 留在设备侧 numpy；分类器整图导出（输出 `[1,2]` / `[1,3]`）。全部静态 batch 1。

### 3.2 转 RKNN（x86 Docker，rknn-toolkit2 2.3.x）

```bash
ONNX_DIR=/workspace/export OUT_DIR=/workspace/rknn \
CALIB_DET=/workspace/calib/det_site_300 \
CALIB_SEX=/workspace/calib/sex_300 \
CALIB_BEHAVIOR=/workspace/calib/behavior_300 \
  bash apps/crayfish-fight/tools/convert_rknn.sh
```

校准集三份、各按各自输入分布取（脚本头部有取图命令）：检测器用 300 张现场 1080p 整帧，性别用 300 张单虾 crop，行为用 300 张双虾 union ROI。拿整帧去校准 128px 分类器是掉点最快的方式。

先 `QUANT=fp16` 打通链路再上 int8（model-onboarding §3）。归一化（/255）烘进 rknn，设备端喂 **raw uint8**，app 里不要再除 255。

### 3.3 上板验证 → 装包 → 激活

```bash
# 三个 rknn 放进 apps/crayfish-fight/models/，文件名与 manifest.json 一致：
#   det_v1_n_rawhead_int8.rknn / sex_cls_int8.rknn / behavior_cls_int8.rknn
# 按应用中心发布指南构建并安装签名 v2 包，包含 lock/BOM/声明的模型制品
# docs/guide/app-center-publishing.md
# 安装后通过 appmgr 申请调度资源
python3 -m appmgr start crayfish-fight
```

### 3.4 验证输出

WS `127.0.0.1:8124` 或 MQTT。每帧一条 payload：

- `results[]`：`{box, cls, cls_name, score, track_id, sex, sex_conf}`
- `events[]`：`kind=detection`（每只虾）与 `kind=behavior`（`phase` = `start` / `change` / `end`，带 `track_ids`、`label`、`confidence`、`duration_sec`、union `box`）

`behavior` 事件只在**状态变化**时出现，不会每帧刷；持续中的打斗不重复上报。

---

## 4. 参数调优

全部在 `manifest.json` 的 `config_schema` 里，除 `capture_dir` / `capture_full_frame_px` 外都是 `apply:"live"`（SIGHUP 热更，不重启、不丢 track / 不丢票 / 不关掉进行中的事件）。

| 参数 | 默认 | 什么时候调 |
|---|---|---|
| `conf` / `iou` | 0.35 / 0.45 | 现场域偏移大时先降 `conf`（宁多勿漏，靠级 2/3 过滤）；叠压场景框分裂就降 `iou` |
| `max_animals` | 8 | 每帧参与跟踪的最大虾数，按缸内实际数量设 |
| `track_max_lost` | 30 | 丢失容忍帧。太大：长时间遮挡后 id 被接到另一只身上，性别票被污染；太小：一次叠压就换 id，性别要重投 |
| `sex_vote_frames` | 10 | 每条新 track 跑几帧性别分类。加大更稳但更贵 |
| `sex_min_conf` | 0.65 | 低于此值该帧记 unknown。现场分不清性别时宁可 unknown（客户允许） |
| `proximity_k` | 1.2 | **靠近判定的主旋钮**。中心距 < k × 平均框对角线。调大触发多、误触发多；调小漏触发。侧拍前后错位造成的"看起来靠近"靠级 3 的 `none` 类挡 |
| `proximity_window` / `proximity_hits` | 8 / 5 | 滑窗"N 中 M"。是 5-of-8 不是 5 连续：漏检一帧不清零 |
| `roi_pad` | 0.15 | 交互 ROI 外扩。举起的螯足常在体框之外；调太大 ROI 被背景水稀释 |
| `behavior_streak` | 3 | 连续几帧同判定才上报。分类器 top1 0.788，1 帧不算证据 |
| `behavior_release` | 3 | 连续几帧未确认才释放（迟滞）。短暂分开的一次打斗算一次，不算三次 |
| `behavior_min_conf` | 0.5 | 低于此置信度的帧不算确认 |
| `temporal_enabled` | true | 时序确认总开关。关掉立即退回纯单帧路径并释放缓冲像素 |
| `max_pairs` | 6 | 最多缓存几对虾的 8 帧序列。每对 1.15 MB，这是唯一跨帧持有像素的地方；超限按靠近持续时长降序保留 |
| `temporal_stride` | 4 | 同一对虾两次时序推理的最小间隔帧数。调大更省算力但判定更陈旧；不要超过 8（缓冲窗口长度），否则相邻判定的窗口不再重叠 |
| `temporal_budget` | 1 | **每帧**最多几次时序推理。这是把时序成本钉成常数的那个旋钮，与虾对数量无关。设 0 等于关掉时序推理但仍继续缓冲 |
| `temporal_min_conf` | 0.5 | 低于此置信度时序模型弃权（不投票也不计 miss），而不是投 `none` |
| `capture_mode` | trigger | 采集策略开关，见下节。`trigger`=所有触发都采（标注原因）；`alarm_gated`=只采 alarm/suspect |
| `suspect_conf` | 0.30 | `capture_reason=suspect` 的判定阈值：状态机未确认，但本帧原始判定 fight/harass 且置信度 ≥ 此值 |
| `capture_per_minute` / `capture_per_day` | 6 / 2000 | 落盘配额。长时间打斗会每次推理都想存图，靠这个压住；日配额防止无人值守把 eMMC 写满 |

调参次序建议：先 `conf` 让检测出框 → 再 `proximity_k` 让该触发的触发（看落盘量）→ 最后 `behavior_streak` / `behavior_min_conf` 压误报。时序侧只在算力吃紧时动 `temporal_budget` / `temporal_stride`，判定质量问题优先看 `temporal_min_conf`。

### 采集落盘

Managed default is `${APPMGR_APPDATA_DIR:-/userdata/local/appdata}/crayfish-fight/captures/<YYYY-MM-DD>/HHMMSS_mmm_t<a>-<b>_{frame.jpg,roi.jpg,.json}`; the sidecar json carries `capture_reason`（`alarm` / `suspect` / `plain`）：

- `alarm`：`BehaviorStateMachine` 已确认事件（起始/切换/持续中）
- `suspect`：状态机还没确认（`behavior_streak` 未攒够），但本帧分类器原始判定已经是 fight/harass 且置信度 ≥ `suspect_conf`
- `plain`：以上都不是的普通靠近触发（含 `none` 判定）

**默认 `capture_mode="trigger"`：所有触发都存，不看分类结果**——被判成 `none` 的触发，要么是真负例（透视重叠——正是 `none` 类最缺的难例），要么是漏判，两种都是再训练要的样本；按结果过滤会精确地饿死修复它的数据。之所以不默认按 alarm/suspect 过滤，是因为当前行为分类器召回还低（PLAN §3.1 的小样本 val），闸门会把大量真实打斗帧连同 `none` 一起挡在落盘之外。`capture_reason` 只做标注，供离线按类别配平/加权，不做在线过滤。

配额紧张时 `plain` 先让路：`CaptureQuota` 最近一分钟用量达到 50% 后，本分钟内新的 `plain` 触发不再落盘，把剩余配额留给 `alarm`/`suspect`；`alarm`/`suspect` 只受配额硬上限约束，不受这个软阈值影响。

`capture_mode="alarm_gated"` 留作以后行为分类器召回达标后切换：同样的 `alarm`/`suspect` 判定，但 `plain` 触发直接跳过，不进配额、不落盘，把落盘完全收窄到行为正例。

写盘失败只告警一次，不会把检测拖垮。

---

## 5. 已知限制

1. **叠压场景检测框分裂**：`harass`（一只骑在另一只背上）恰好是检测器最难的构图，容易把两只合成一框或把一只切成两框。合成一框时该对根本进不了级 2；这类样本靠落盘回收后重训检测器解决，不是调参能解决的。
2. **行为分类器是小样本基线**：top1 0.788 来自小样本混合域 val，现场泛化未验证。`fight` / `harass` 的混淆、以及侧拍透视重叠被误判成 `harass`，都需要现场数据重训后才有结论。当前 `behavior_streak=3` 的默认值是按"分类器不可靠"设的保守值。
3. **时序模型的 fight/harass 互混未解决**：temporal_v3 的 val 上 `fight` 召回 4/9，但漏的**都被判成 `harass`、没有一个漏成 `none`**（PLAN §3.3）——异常本身抓得住，具体是哪一种缠斗时仍会混。异常二分类 recall 0.95 / precision 0.905 是当前可用的口径；把 `fight` 和 `harass` 当两个独立告警级别用之前需要现场复核。
4. **时序判定有 stride 延迟**：一对虾需要先攒够 8 个触发帧才会有第一次序列判定，之后每 `temporal_stride` 帧更新一次。@12 fps 首次判定约在触发后 0.66 s。要更快只能减小 `proximity_hits`（更早触发）或牺牲算力把 `temporal_stride` 调小，缓冲窗口 8 帧本身是训练时定死的。
5. **`behavior_temporal` 的 rknn 尚未转换**：`runs/behavior_temporal_v3/weights/best.pt` 已训练，但 ONNX 导出与 448² int8 量化未执行，`models/behavior_temporal_int8.rknn` 不存在。缺失时 app 会告警一次并降级到纯单帧路径。上板前必须补齐 §3.1 / §3.2（注意校准集要用**拼图**而不是单帧 ROI）。
6. **性别只投一次**：`sex_vote_frames` 帧后 settle，之后不再更新。若这条 track 前 10 帧恰好都是背对镜头，这只虾整段都会是 unknown（或错）。track 断掉重生成新 id 时会重新投票。
7. **指标是混合域 val**：0.864 / 0.820 / 0.788 都不是现场验收数字。现场侧拍与公开数据的域差距在 PLAN §3.1 里有实测记录（v0 在现场图上 conf 0.25 下 0 框）。
8. **落盘的"整帧"不是原始分辨率**：`hw-roi` 下 `frame.data` 是 letterbox，整帧是通过同一个 RGA cropper 以 `capture_full_frame_px`（默认 1280）取的全 FOV 方图，边缘有灰边填充。要原生分辨率归档需要另一条取帧路径。
9. **未上真机**：本 app 的 rknn 转换与设备验证尚未执行，`models/` 目录为空。上板前必须走完 §3.2 / §3.3。

---

## 6. 单元测试

```bash
cd /Users/harvest/project/recamera/recamera_pro
uv run --with pytest pytest apps/crayfish-fight/tests/ -q
```

覆盖：触发几何（尺度无关性、边界严格性、union+clip）、性别投票（多数票 / 低置信记 unknown / settle 后不再改 / 平票）、滑窗触发（N 中 M、容忍漏帧、分开后衰减、pair 遗忘）、行为状态机（连续确认才起、单帧噪声不释放、迟滞、fight↔harass 切换、超时关闭、`none` 永不触发）、落盘配额（分钟窗滚动、日配额跨天重置、`minute_fraction` 用量占比）、采集决策 `CaptureDecider`（alarm 恒采且不受配额压力影响、suspect 阈值判定、`trigger` 模式下 plain 触发采集且配额过半让路、`alarm_gated` 模式跳过 plain、非法 mode 回退 trigger）。

时序部分另外覆盖：拼图几何（画布 896×448、`collage_slots()` 的阅读顺序、8 张纯色图逐格位置断言、无缝无重叠全覆盖、类序 = fight/harass/none）、`PairFrameBuffer`（满 8 帧才 ready、ring 保留最新 8 帧、超 `max_pairs` 按持续时长降序淘汰且平票按 key 确定性淘汰、`retain()` 清理掉出触发集的对、`drop_track()` 连带清理该 track 的所有对）、`TemporalScheduler`（每帧预算上限、就绪对轮转不饿死、stride 拦住同一对、空帧也要推进帧计数否则 stride 会被拉长、budget=0 关闭推理但仍计帧、budget>1、`drop_track()`）、时序 2 票进状态机（时序 ×2 独立起报、单帧 ×2 不起报、单帧+时序混合凑够 streak、换 label 时按自身权重重置、**释放侧不加权**、weight<1 被钳到 1）。
