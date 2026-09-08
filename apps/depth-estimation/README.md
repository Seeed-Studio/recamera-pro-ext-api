# depth-estimation — 单目深度估计 / Monocular Depth Estimation

在 RV1126B NPU 上跑 **MiDaS v2.1 small（256×256, FP16）**，每帧输出整帧相对深度统计、
近/中/远网格分区、画面中最近的分区，以及可选的 ROI 平面度。

对齐 SG2002 同名方案 `sscma-example-sg200x/solutions/depth-estimation/` 的输出契约
（`depth` 对象的字段名、分区网格、`inference_time_ms`），换成了更准的深度头。

> **深度是相对值、无单位，不是距离。** 而且 **MiDaS 输出的是逆深度：数值越大越近**，
> 与 SG2002 方案里 FastDepth 的符号相反。payload 里用 `depth.smaller_is_nearer: false`
> 显式声明，消费方不需要靠数值猜。

---

## 1. 接口

结果走 WebSocket `:8124`（`--sink ws --port 8124`），每帧一条 JSON。

### results[]（会被自动烧进 OSD）

画面按 `grid_cols × grid_rows` 分区，每个分区一条：

| 字段 | 类型 | 说明 |
|---|---|---|
| `box` | `[x1,y1,x2,y2]` | 分区矩形，**原始相机像素** |
| `label` / `cls_name` | `"near"｜"mid"｜"far"` | 由 `score` 分桶：≥0.66 near，≥0.33 mid，其余 far |
| `cls` | `0｜1｜2` | far / mid / near 的类别号 |
| `score` | 0..1 | 分区的**平均 proximity**，1 = 画面最近端 |

### events[]

每帧一条 `kind: "depth"` 事件，字段是 `extra` 中标量的平铺重述：
`nearest_value`、`nearest_near`、`nearest_box`、`nearest_row`、`nearest_col`、
`min`、`max`、`mean`、`p5`、`p95`、`smaller_is_nearer`、`grid`、`grid_size`。

为什么要重述一遍：声明式输出组件的 Jinja 命名空间只看得到 `results` / `events`
（`kit/adapters/output_sink.py:build_namespace`），Home Assistant 的 state 文档
只汇总 events 里的**标量**字段（`kit/adapters/mqtt_sink.py:_build_state`）。
所以 MQTT 映射写 `(events.depth | last).nearest_value`，HA 模板写
`{{ value_json.summary.nearest_value }}`，app 里一行输出代码都不用写。

### extra（WS 原始流，供叠加层/调试）

```jsonc
{
  "depth": {
    "unit": "relative",
    "smaller_is_nearer": false,       // MiDaS 是逆深度：越大越近
    "source_size": [1280, 720],
    "valid_roi": [0.0, 0.0, 1280.0, 720.0],   // letterbox 去掉灰边后的有效区（原始像素）
    "min": 14.6172, "max": 872.0, "mean": 331.4924,
    "p5": 102.0625, "p95": 620.5              // 原始模型单位
  },
  "grid": [[0.8996, 0.4539, 0.2065, 0.2855],  // grid_rows × grid_cols，行优先
           [0.8651, 0.2920, 0.0295, 0.1865],
           [0.7902, 0.4199, 0.3138, 0.5352]],
  "grid_size": [4, 3],                        // [cols, rows]
  "nearest": {"box": [0,0,320,240], "value": 0.8996, "near": 1.0, "row": 0, "col": 0},
  "rois": [ ... ],                            // 仅当配置了 depth_roi
  "depth_map": { ... },                       // 仅当 publish_map = true
  "model_tag": "rv1126b:midas_v21_small_256@fp16",
  "inference_time_ms": 59.69
}
```

**proximity（0..1 的归一化深度）** = `clip((d − p5) / (p95 − p5), 0, 1)`。
用 p5/p95 而不是 min/max 稳定量程：单个极值像素会把整帧重新缩放，让两张看起来一样的
相邻帧的网格值跳变（SG2002 的 `depth_payload.cpp` 用 p02/p98 直方图，同一个理由）。
`p95 == p5`（全平画面）时全部读作 0。

**`nearest` 选谁、报什么是两件事**：用 `near_percentile` 分位数**选**分区（一个人站在
分区角落，要能赢过一个整体中距离的分区），**报**的是那个分区的**均值**。分位数本身在任何
含近端内容的分区上都会顶到 1.0——真机实测连续 20 帧全是 1.000，当传感器用毫无信息量，
所以它只出现在 `nearest.near` 里做溯源。

### 配置（`config_schema`，全部 `apply: "live"`，SIGHUP 热生效）

| key | 类型 | 默认 | 说明 |
|---|---|---|---|
| `grid_cols` / `grid_rows` | integer | 4 / 3 | 分区网格 |
| `near_percentile` | number | 95 | 取分区内该分位数作为“最近内容”，用于选出最近分区 |
| `emit_interval` | integer | 1 | 每 N 帧输出一次；>1 时**连推理一起跳过**，不只是不发 |
| `publish_map` | boolean | false | 在 extra 里附带 64×48 base64 PNG 的 proximity 预览图 |
| `depth_roi` | string | `""` | 归一化 `[x,y,w,h]` 列表的 JSON，逐 ROI 输出 `planarity`/`relief` |

`depth_roi` 的平面度来自 `planarity.py`（`apps/face-recognition/depth_liveness.py` 的
纯 numpy 部分，逐字复制——打包器只打自己这棵目录树，跨 app import 在设备上会失效）：
对 ROI 做最小二乘平面拟合 `d ~ a·x + b·y + c`，残差 RMS 除以整帧 p95−p5 量程。
`planarity` 接近 1.0 = 该区域是平面（照片、屏幕）；`relief` 是同一量纲下的峰峰残差。

### Home Assistant

`ha_entities` 给了两个 sensor：`nearest_depth`（`summary.nearest_value`）和
`depth_mean`（`summary.mean`）。

---

## 2. 模型来源与转换

| | |
|---|---|
| ONNX | `unity/inference-engine-midas` → `model-small_opset19.onnx`（hf-mirror） |
| 输入 | 1×3×256×256 |
| 输出 | `[1,256,256]` 相对逆深度，**越大越近** |
| 量化 | FP16，`rknn-toolkit2 2.3.2`，`--platform rv1126b` |
| 归一化 | ImageNet 的 Sub/Div **在 ONNX 图里**（节点 1 和 3），所以 RKNN 用 `--mean 0,0,0 --std 255,255,255`，**app 喂原始 uint8 RGB，不要再除 255** |
| .rknn | 33 736 622 B，`models/depth_midas_v21_small_256_fp16.rknn`（`models/` 被 gitignore） |
| 转换日志 | `models/convert/depth_midas_v21_small_256_fp16.convert.log` |

选型对比（MiDaS v2.1 small vs Depth Anything V2 Small vs FastDepth）、PC 模拟器与
onnxruntime 的 Pearson 交叉验证、平面度指标标定，见
`apps/face-recognition/evaluation/depth-model-selection.md`。

`model_frame = "hw-direct"`：深度头只看 256 letterbox，本 app 不读原分辨率像素，
所以帧源直接在 RGA 上 letterbox 进 `frame.data`，整帧 NV12→RGB 转换完全省掉。
网格框通过 letterbox 参数映射回原始相机像素，OSD 对得上。

---

## 3. 真机性能（reCamera Pro，RV1126B，1280×720 子码流）

20 条连续 WS 输出，默认配置（4×3 网格，`emit_interval=1`，无 ROI，无 depth_map）：

| | |
|---|---|
| 端到端 | **10.1 fps**（客户端观测），kit metrics 报 10.0–10.2 fps |
| `inference_time_ms` | mean **66.8**，min 63.0，median 67.1，max 72.8 |
| app 侧 numpy（kit `latency_ms.post`） | 25–27 ms |
| `pipeline_ms` | 81–110 ms |
| Traceback | 0 |

### 一个踩过的坑：`np.percentile` 比推理还贵

第一版每个分区调一次 `np.percentile`，12 次 = **54.6 ms/帧**，和 66 ms 的 NPU 推理一个量级，
端到端只有 8.05 fps。`np.percentile` 的开销主要在它自己的 Python 侧校验和插值上，与数据量无关。
换成 `np.partition` 的最近秩分位数后：

| 设备实测（`tests/_bench.py`，256×144 深度图） | 改前 | 改后 |
|---|---|---|
| `frame_stats` | 14.14 ms | 6.51 ms |
| `grid_cells 4×3` | 54.59 ms | 26.11 ms |
| `stats+prox+grid` | 67.63 ms | 36.48 ms |
| 端到端 | 8.05 fps | **10.09 fps** |

插值和最近秩的差别，远小于这张相对深度图本身的量化噪声。

---

## 4. 开发

```sh
# 单元测试（无相机、无 NPU：fake 帧源 + fake 模型）
python3 -m pytest apps/depth-estimation/tests -q

# 设备上（相机单活，restart.sh 会先停掉正在跑的 kit app）
adb push apps/depth-estimation /userdata/local/apps/
sh /userdata/local/apps/depth-estimation/restart.sh
sh /userdata/local/apps/depth-estimation/status.sh 20     # 进程 + Traceback 计数 + 日志尾
sh /userdata/local/apps/depth-estimation/stop.sh

# 设备侧 numpy 微基准
/userdata/rknnenv/bin/python /userdata/local/bench/_bench.py
```

| 文件 | 作用 |
|---|---|
| `app.py` | `run()` 主循环、`analyse()`（一帧 → results/extra）、`depth_event()` |
| `depth_map.py` | 纯 numpy 归约：有效区、统计、proximity、网格、ROI 解析、PNG 编码 |
| `planarity.py` | 平面拟合（来源见上） |
| `tests/test_depth_map.py` | 归约函数单测 |
| `tests/test_app_loop.py` | 整条循环：fake 帧源 + fake 模型，覆盖网格/nearest/ROI/publish_map/emit_interval/输出映射 |
| `tests/_bench.py` | 设备侧微基准（不进 pytest，靠 `_` 前缀避开收集） |
