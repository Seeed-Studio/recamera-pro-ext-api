# hand-gesture — MediaPipe 手势识别 / MediaPipe Gesture Recognition

在 RV1126B NPU 上运行 **MediaPipe Gesture Recognizer** 官方四级管线的完整复刻：
SSD 手掌检测（192）→ 旋转 ROI 手部关键点（224，21 点）→ 手势 embedder →
预置分类器，输出 8 类手势（None / Closed_Fist / Open_Palm / Pointing_Up /
Thumb_Down / Thumb_Up / Victory / ILoveYou）、左右手、21 个归一化关键点，
并带逐手滑动窗口多数投票防抖。

模型来自 Google 官方 `gesture_recognizer.task`，转为 FP16 RKNN。四张官方测试图
与官方 `mediapipe` Python 包端到端标签一致；设备端 NPU 输出与主机 TFLite CPU
参考在同一批真实帧上数值一致（余弦相似度 1.0000）。

> **模型文件不随本仓库分发**（`.gitignore` 排除 `*.rknn`）。manifest 中
> `artifacts[]` 声明了各模型的 sha256/size，打包时校验。转换脚本与参考
> TFLite 的提取流程见模型工程目录。

---

## 1. 管线

```text
frame (RGB, 全分辨率, cpu 模式)
  │  letterbox 192（黑边，MediaPipe BORDER_ZERO 约定）
  ▼
[1] hand_detector_fp16.rknn        NPU · SSD 2016 锚点 + sigmoid + NMS
  │  每掌：腕→中指根旋转角 → shift(-0.55)/scale(2.5) 旋转矩形
  ▼  cv2.warpPerspective 旋转裁剪 224
[2] hand_landmarks_detector_fp16.rknn  NPU · 21 关键点 + 左右手 + presence
  │  关键点投影回原图 → LandmarksToMatrix 宽高比归一化
  ▼
[3] gesture_embedder               CPU numpy · 掌根中心化 + 跨度归一化 → 128 维
  ▼
[4] canned_gesture_classifier      CPU numpy · → 8 类概率
```

[3][4] 不上 NPU 的原因：kit 托管推理只支持单输入 uint8 图像模型，而
embedder 是多输入 float 向量模型；两者合计只有几万次乘加，numpy 复刻在
CPU 上是微秒级，比 NPU 调度开销低两个数量级，且 float32 精度逐位对齐
TFLite（最大误差 7e-7）。

## 2. 接口

结果走 WS（manifest `output.sink: "ws"`，`render` 声明 boxes + `hand21`
关键点骨架）。每只手一条：

### results[]

| 字段 | 类型 | 说明 |
|---|---|---|
| `box` | `[x1,y1,x2,y2]` | 21 关键点包围盒（像素，**含指尖**，非检测器掌框） |
| `score` | float | palm 检测置信度 |
| `gesture` / `label` / `class_name` | string | 投票稳定后的手势标签 |
| `gesture_raw` | string | 当前帧投票前的原始标签（调试抖动用） |
| `vote_count` | int | 稳定标签在当前窗口内的票数 |
| `gesture_conf` | float | 分类 softmax 置信度 |
| `hand` | `"Left"/"Right"` | 已对非镜像相机取反 |
| `presence` | float | landmarker 手部存在分；低于 `min_presence` 的手被丢弃 |
| `keypoints` | `[[x,y]×21]` | 归一化坐标（`spaces.keypoints = normalized_points`） |

### events[]

每手一条扁平 `gesture` 事件：`{kind, gesture, gesture_conf, hand, score,
box, keypoints, gesture_raw, vote_count}`。

## 3. 配置项（全部 live 热更新）

| key | 默认 | 说明 |
|---|---|---|
| `confidence` | 0.5 | palm 检测阈值 |
| `nms` | 0.3 | NMS IoU |
| `max_hands` | 2 | 最大手数（1~4） |
| `roi_shift_y` | -0.55 | 关键点 ROI 垂直偏移；负值向手指方向。29 个设备/官方样本网格搜索选定 |
| `roi_scale` | 2.5 | 关键点 ROI 相对手掌框的放大倍数 |
| `min_presence` | 0.5 | 关键点存在门限，低于则不做分类 |
| `gesture_min_conf` | 0.5 | 低于此值输出 None |
| `vote_window` | 5 | 逐手滑动投票窗口帧数；1 = 关闭投票 |
| `vote_min_votes` | 0 | 切换稳定标签所需最少票数；0 = 自动多数 |

## 4. 已知边界（调参前请先读）

- **canned 模型语义严格**：只有"标准"姿态才给出手势标签，四指自然微曲
  的半张手掌会被判 `None`——这是模型设计行为，不是故障。
- **低对比/强逆光会让 palm detector 置信度崩塌**（实测 det 0.8→0.2）；
  CLAHE/gamma 等增强会偏离训练分布反而更差，请改善成像条件。
- **临界姿态的 None↔手势抖动由投票平滑**；动作需保持约 `vote_window/2`
  帧以上才会确立输出。
- **RGA 枚举坑**：kit `_rga.py` 的 `RK_FORMAT_YCbCr_420_SP` 必须与设备
  librga 实际枚举一致（RV1126B v1.10.5_[11] 实测 NV12=0xA00，而非 0xE00）。
  若设备端识别全空但回放正常，先检查帧颜色是否偏蓝；防御开关见
  `app.py` 顶部注释（`RECAMERA_RGA=0` 强制 OpenCV 转换路径）。

## 5. 测试

```sh
uv run pytest apps/hand-gesture/tests/        # 主机端纯逻辑测试，无需设备
```

覆盖：宽高比归一化（真实设备 Victory 帧回归）、投票窗口/滞回/track 过期/
多手独立/RGA 防御开关位置。
