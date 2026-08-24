# crayfish-fight — 螯虾打斗/骚扰行为监测

水槽内螯虾的攻击行为监测 app。逐帧检测每只虾、投票判定性别，对**持续靠近的虾对**在交互 ROI 上分类 `fight` / `harass`，只上报事件（视频不出场），并按配额落盘触发样本用于再训练。

目标设备：reCamera Pro（RV1126B，3 TOPS NPU）。方案背景见 `/Users/harvest/project/crayfish-fight/docs/PLAN.md` §1.1、§3.2。

---

## 1. 架构（三级漏斗）

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
  ├─ 采集落盘：**每次触发**（不论分类结果）按配额存整帧 + ROI + json
  │            → /userdata/crayfish/<date>/
  │
  ▼ self.emit()
      events: detection（每只虾，带 sex）+ behavior（每对，start/change/end）
      results: 检测原始记录，供 /appcenter overlay 与 output mapping 消费
```

计算量：检测每帧 1 次；性别分类只在每条新 track 的前 10 帧各 1 次；行为分类只在触发帧、每对 1 次。行为在时间上稀疏，这是三级漏斗的全部意义。

### 代码分布

| 文件 | 内容 |
|---|---|
| `app.py` | 每帧流水线：pre / infer / post / crop / emit / 落盘 |
| `logic.py` | **跨帧业务逻辑，纯 Python**：`SexVoter` / `ProximityWindow` / `BehaviorStateMachine` / `CaptureQuota` / `CaptureDecider` + 触发几何 |
| `tests/test_logic.py` | 上述逻辑的单测，Mac 上 `uv run pytest` 直接跑，不需要设备 |
| `tools/export_onnx.py` | Mac 上导出 4 个 ONNX（检测 raw-head，分类整图） |
| `tools/convert_rknn.sh` | x86 Docker 内 ONNX → RKNN int8（**本机不能跑**） |
| `manifest.json` | 3 个模型声明、config_schema、output.fields、render |

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

指标口径：**混合域 val**（现场侧拍 + 外部公开数据），**行为分类器为小样本摸底基线**，不是现场验收数字。

类序不是约定俗成，是从权重里读出来的（`tools/export_onnx.py` 会打印并写进 `export/export_summary.json`），并写死在 `logic.py` 的 `SEX_LABELS` / `BEHAVIOR_LABELS` 与 `manifest.json` 的 `models[].classes`。**改模型必须同步这三处**，否则 label 会整体错位且没有任何报错。

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
# 设备上：先证明模型能被 librknnrt 加载
python3 device_verify.py /tmp/det_v1_n_rawhead_int8.rknn      # 返回 0 才算过

# 三个 rknn 放进 apps/crayfish-fight/models/，文件名与 manifest.json 一致：
#   det_v1_n_rawhead_int8.rknn / sex_cls_int8.rknn / behavior_cls_int8.rknn
python3 market/packaging/build.py apps/crayfish-fight          # 整树进包
# 应用中心安装 → 激活（单活语义，激活即停掉上一个占相机的 app）
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
| `capture_mode` | trigger | 采集策略开关，见下节。`trigger`=所有触发都采（标注原因）；`alarm_gated`=只采 alarm/suspect |
| `suspect_conf` | 0.30 | `capture_reason=suspect` 的判定阈值：状态机未确认，但本帧原始判定 fight/harass 且置信度 ≥ 此值 |
| `capture_per_minute` / `capture_per_day` | 6 / 2000 | 落盘配额。长时间打斗会每次推理都想存图，靠这个压住；日配额防止无人值守把 eMMC 写满 |

调参次序建议：先 `conf` 让检测出框 → 再 `proximity_k` 让该触发的触发（看落盘量）→ 最后 `behavior_streak` / `behavior_min_conf` 压误报。

### 采集落盘

`/userdata/crayfish/<YYYY-MM-DD>/HHMMSS_mmm_t<a>-<b>_{frame.jpg,roi.jpg,.json}`，sidecar json 里带 `capture_reason`（`alarm` / `suspect` / `plain`）：

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
3. **性别只投一次**：`sex_vote_frames` 帧后 settle，之后不再更新。若这条 track 前 10 帧恰好都是背对镜头，这只虾整段都会是 unknown（或错）。track 断掉重生成新 id 时会重新投票。
4. **指标是混合域 val**：0.864 / 0.820 / 0.788 都不是现场验收数字。现场侧拍与公开数据的域差距在 PLAN §3.1 里有实测记录（v0 在现场图上 conf 0.25 下 0 框）。
5. **落盘的"整帧"不是原始分辨率**：`hw-roi` 下 `frame.data` 是 letterbox，整帧是通过同一个 RGA cropper 以 `capture_full_frame_px`（默认 1280）取的全 FOV 方图，边缘有灰边填充。要原生分辨率归档需要另一条取帧路径。
6. **未上真机**：本 app 的 rknn 转换与设备验证尚未执行，`models/` 目录为空。上板前必须走完 §3.2 / §3.3。

---

## 6. 单元测试

```bash
cd /Users/harvest/project/recamera/recamera_pro
uv run --with pytest pytest apps/crayfish-fight/tests/ -q
```

覆盖：触发几何（尺度无关性、边界严格性、union+clip）、性别投票（多数票 / 低置信记 unknown / settle 后不再改 / 平票）、滑窗触发（N 中 M、容忍漏帧、分开后衰减、pair 遗忘）、行为状态机（连续确认才起、单帧噪声不释放、迟滞、fight↔harass 切换、超时关闭、`none` 永不触发）、落盘配额（分钟窗滚动、日配额跨天重置、`minute_fraction` 用量占比）、采集决策 `CaptureDecider`（alarm 恒采且不受配额压力影响、suspect 阈值判定、`trigger` 模式下 plain 触发采集且配额过半让路、`alarm_gated` 模式跳过 plain、非法 mode 回退 trigger）。
