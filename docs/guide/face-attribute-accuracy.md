# 人脸属性准确度：实测数字、已修问题、怎么继续测

面向 `apps/face-analysis`（三级级联：YOLOv8n-face → FairFace age/gender/race → HSEmotion）。

评测 harness 在 `model_conversion/recamera_fairface/eval/`，直接 import `recamera_pro/kit/pipeline.py` 的 `square_roi_geometry` / `crop_square_roi`，所以测的就是设备端的裁剪几何本身，不是另写一份近似。

**测量条件**：FairFace val 抽样 n=2000（seed=42），三个 arm 共用同一份 stage-1 缓存；检测失败 58 张（2.90%）在所有 arm 中一并剔除，可比子集 **1942**。非配对 1σ 噪声：p≈0.70 处 **±1.04 pt**，p≈0.94 处 **±0.54 pt**。全部在 float ONNX 上做，**未包含设备端 RKNN 量化的影响**，那是独立的一维。

---

## 一、实测总表

| arm | gender | race | age | age±1 |
|---|---|---|---|---|
| **A 官方 dlib 对齐 chip（参考）** | 0.9392 | 0.7029 | 0.6169 | 0.9665 |
| B 设备裁剪 `crop_pad=0.25` | 0.8908 | 0.5505 | 0.4655 | 0.8538 |
| B 设备裁剪 `crop_pad=0.15` | 0.9258 | 0.6359 | 0.5268 | 0.9089 |
| B 设备裁剪 `crop_pad=0.10` | 0.9367 | 0.6576 | 0.5484 | 0.9305 |
| **B 设备裁剪 `crop_pad=-0.05`（现默认）** | **0.9542** | **0.7122** | **0.6195** | **0.9614** |
| C 相似变换对齐（FaceMesh） | 0.9439 | 0.6864 | 0.5978 | 0.9537 |

相对 Arm A 的差（百分点）：

```
crop_pad=0.25    gender -4.84   race -15.24   age -15.14   age±1 -11.28
crop_pad=0.10    gender -0.26   race  -4.53   age  -6.85   age±1  -3.60
crop_pad=-0.05   gender +1.49   race  +0.93   age  +0.26   age±1  -0.51   ← 噪声内，读作"打平"
对齐 (C)         gender +0.46   race  -1.65   age  -1.91   age±1  -1.29
```

### 结论一：轴对齐正方形裁剪本身不掉点，掉点的是 `crop_pad` 取值

`crop_pad=-0.05` 时，普通方形裁剪与"喂 FairFace 自己的对齐 chip"在统计上打平（三个 head 全部落在 ±1.04 pt 噪声内）。

**曾经的判断"预处理错配是最大的单点损失"是对的，但归因错了**——错配的不是"缺少旋转对齐"，而是**取景松紧**。

### 结论二：`crop_pad` 最优值是 **-0.05**，负数

完整扫描单峰，峰在 -0.05，平台区约 -0.07 ~ -0.03：

| `crop_pad` | gender | race | age | age±1 |
|---|---|---|---|---|
| -0.15 | 0.9248 | 0.6880 | 0.5927 | 0.9593 |
| -0.10 | 0.9403 | 0.7127 | 0.6102 | 0.9583 |
| -0.07 | 0.9464 | **0.7168** | 0.6159 | 0.9567 |
| **-0.05** | **0.9542** | 0.7122 | **0.6195** | **0.9614** |
| -0.03 | 0.9470 | 0.7122 | 0.6097 | 0.9604 |
| 0.00 | 0.9485 | 0.7044 | 0.6097 | 0.9547 |
| +0.05 | 0.9459 | 0.6936 | 0.5767 | 0.9434 |
| +0.10 | 0.9367 | 0.6576 | 0.5484 | 0.9305 |
| +0.25 | 0.8908 | 0.5505 | 0.4655 | 0.8538 |
| +0.40 | 0.8213 | 0.4140 | 0.3764 | 0.7539 |

负号的物理含义：yolov8n-face 输出的框比 FairFace 训练所用的 dlib face rect 更松，方形化之后取景又会再涨一圈，所以要往回收约 5% 才落回训练分布。

**这条推翻了一次错误推理**：从"权重名叫 `res34_fair_align_multi_7`、训练用 dlib chip padding=0.25"推到"把 `crop_pad` 设成 0.25"是错的——两个 padding 量的是不同的矩形。曾把默认值从 0.15 改到 0.25，方向正好反了，race 因此掉约 8.5 个点。

### 结论三：`crop_pad` 调对之后，对齐是**负收益**

Arm C（FaceMesh 468 点相似变换转正 + 眼距归一化）对比最优 Arm B：

```
gender -1.03 pt    race -2.57 pt    age -2.16 pt    age±1 -0.77 pt
```

race 的 -2.57 约 2.5σ，是真实劣势不是噪声。

Arm C 只在 `crop_pad ≥ 0.10` 时显著赢——**那是在跟一个已经跑偏的基线比**。

可能原因（**需核实**，本轮没做消融）：468 点 landmark 在小脸/侧脸上抖动引入额外取景方差；warp 的双线性重采样比一次 resize 多一道插值损失。

Arm C 的目标模板不是通用模板：在官方 chip 上跑同一个 FaceMesh 取 2000 张平均锚点再对称化得到（224 空间 left_eye(75.11,71.14) / right_eye(148.89,71.14) / mouth(112.00,146.31)，眼距 73.77px），也就是说对齐的目标就是训练分布本身。即便如此仍然输。

### race 混淆矩阵（`crop_pad=-0.05`）

```
                White  Black Latino East A Southe Indian Middle     acc
White             296      0     33      8      3      2     22   0.813
Black               3    232     17      0      2     12      4   0.859
Latino_Hispa       37     11    154      5     12     33     26   0.554
East Asian          2      2      8    249     41      1      1   0.819
Southeast As        0      4     16     75    152      8      0   0.596
Indian              3     18     33      2      8    173     14   0.689
Middle Easte       51      5     22      2      3     10    127   0.577
```

错误集中在三处：**Southeast Asian → East Asian（75 例，该类最大错误源）**、Latino_Hispanic 全类只有 0.554、Middle Eastern → White（51 例）。

这与一代实机现象吻合：关掉跨帧投票时，同一个人的 race 在 East_Asian / Southeast_Asian 之间来回跳（25 帧里 13/12）。

---

## 二、0.2.0 修掉的问题（与模型无关）

五条流水线逻辑问题，换任何模型都存在。

### 1. 单帧 argmax → 跨帧投票

`kit/logic/tracker.py` 给每张脸稳定身份，`kit/logic/attributes.py` 按 `track_id` 累积各 head 的概率向量，报累积和的 argmax。`*_conf` 语义变成**票份额**。

一代实机上的直接对照（同一 binary、同一个人，只切 `--evidence-decay 0`）：

```
投票开：race  East_Asian ×30 / 30 帧
投票关：race  East_Asian ×13  Southeast_Asian ×12 / 25 帧
```

**副作用需知**：累积用概率求和（soft voting），一帧极端自信的输出可以压过两帧温和的输出——这是温度标定要解决的问题。`evidence_decay < 1.0` 让旧证据指数遗忘。

### 2. 情绪按列表下标缓存 → 串人

旧实现 `self._emotion_cache[i]`，`i` 是按检测分排序后的**列表下标**不是身份。默认 `emotion_interval=1` 盖住了它，而多人时为性能调大 interval 恰恰是第一件会做的事。

修法：删掉单独的 cache，累积器本身就是 per-track 记忆。

### 3. 客群直方图统计的是"人脸帧"不是"人"

`Aggregator.maybe_count()` 对每个 `track_id` 只折入一次，时机是证据首次 `stable`。

**兼容性**：`demographics.faces` 现在是唯一人数，旧的 dwell 加权数保留为 `face_frames`。**按 0.1.0 语义读 `faces` 的消费者会读到一个数量级更小的数。**

### 4. 无质量门控

`passes_gate()` 在裁剪之前按原图人脸框短边过滤（`min_face_px`，默认 64）。被门控的脸仍在 `results` 里（`gated: true`、属性 `null`），但不跑 stage 2/3。

一代实机实测的开销对照：

```
未门控（跑 AGR + 情绪）  203.1 ms/帧
已门控（跳过 stage 2/3）  48.2 ms/帧
```

两个分类器占单帧开销的 **76%**。门控在远景多人场景是准确度和帧率的双重收益。

`min_face_px=64` 仍是**未经测量的起点**。

### 5. 隐私标记只打给 top-K

`kind`/`blur` 现在在 top-K 切片**之前**打给每一个检测。旧实现下排名第 `max_faces+1` 及以后的脸带框输出、没有 blur 标记——恰好在人多时失效。

---

## 三、还没做的

### 置信度标定

`AttributeConfig.temperature`（每 head softmax 温度）和 `min_conf`（每 head 置信度下限）已接好，**默认全是 no-op**。

保持 no-op 是刻意的：ResNet 分类头系统性过度自信是已知事实，但除以多少必须在留出集上拟合。harness 已经能产出各 head 的概率分布，拟合温度是现成的下一步。

race 头最需要：混淆集中在 Southeast↔East Asian、Latino、Middle Eastern↔White 三处，一个标定过的 `min_conf` 可以把这些低置信预测压成"不确定"而不是给个错标签。

### `min_face_px` 标定

现在的 64 是拍的。harness 里按人脸像素尺寸分桶统计准确率就能定出来。

### 设备端量化的影响

上面全部在 float ONNX 上做。RKNN fp16 的实际掉点没测过，是独立的一维。

---

## 四、模型选型（2026-08-20 调研 + 实测修正）

| 级 | 结论 | 依据 |
|---|---|---|
| age/gender/race | **保持 FairFace res34** | 没有既满足商用许可、又在三个任务上全面超过它的公开模型。FaceXFormer（MIT）age 反而低约 1pp 且 114 GFLOPs |
| race 单独 | **保持** | 替代品要么 AGPL，要么没有可引用的评测数据 |
| emotion | **可换 `enet_b2_8`** | 同作者体系（EmotiEffLib，Apache-2.0），官方已给 ONNX。AffectNet-8 60.95% → 63.03%。代价 30MB / 输入 260 |
| age 连续回归 | **值得单独试转** | MiVOLO v1 face-only：Apache-2.0，IMDB-clean MAE 4.22。但 VOLO 的 outlook attention 依赖 Unfold/Fold，不在 rknn-toolkit2 已知算子表内（**需实测**），6.8 GFLOPs ≈ 1.9× ResNet-34 |
| 检测器 | **换 RetinaFace 的理由只剩许可** | 见下 |

### 检测器：换的理由变了

原先推荐 RetinaFace MobileNet0.25 有两条理由：原生 5 点关键点（做对齐）+ MIT 许可。

**第一条已被实测否掉**——对齐是负收益，不需要关键点。

**第二条仍然成立**：现用的 `yolov8n-face` 来自 `derronqi/yolov8-face`（**GPL-3.0**），上游 Ultralytics 是 **AGPL-3.0**。商用发布前需要确认这条链。

同样需要确认：FairFace 权重仓库 `dchen236/FairFace` **没有 LICENSE 文件**，只有数据集标了 CC BY 4.0。

需核实：许可结论来自仓库文件与 README 检索，不是法务意见。

**换检测器还有一个新增的注意事项**：`crop_pad=-0.05` 是**针对 yolov8n-face 的框尺度标定的**。RetinaFace 的框松紧不同，换检测器必须**重扫一遍 `crop_pad`**，否则会重蹈这次的覆辙。

---

## 五、多人场景

支持，`max_faces` 默认 5、上限 16，逐脸跑 stage 2/3。

**性能**：每帧推理次数 = 1 + 2N（N = 通过门控的脸数）。门控会砍掉小脸，所以拥挤远景的实际开销低于 2N 上限——实测门控命中时单帧从 203ms 降到 48ms。

**准确度**：多人时人均脸更小（门控命中率上升，`gated` 比例是可观测指标）。

**已知边界**：只有 top-K 进入 tracker。一个人被挤出 top-K 期间其 track 走向 lost，`track_max_lost`（默认 15 帧）之内回来能接上，超时会被当作新人重新计数一次。把 `max_faces` 设得比典型同框人数略大可以避开。

---

## 六、复现

```bash
cd model_conversion/recamera_fairface/eval
uv run python prep_data.py       # 从 hf-mirror 拉 FairFace val 两个版本
uv run python stage1_cache.py    # 跑检测 + landmark，缓存
uv run python run_eval.py        # 三个 arm
uv run python sweep_low_pad.py   # crop_pad 扫描（含负值）
uv run python final_table.py     # 总表
```

抽样 n=2000 seed=42；全量 10954 张加 `--n 0`，约 5.6 倍耗时。

**注意**：`fairface.onnx` 里**没有** baked ImageNet 归一化（设备上的 rknn 才有），喂 onnx 必须自己 `/255` + mean/std。裸 0-255 输入时 race 只有 0.177（≈1/7 随机）。
