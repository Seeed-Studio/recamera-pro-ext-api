# motion-daemon 接口定义

> 2026-11 更新：本服务已作为**固件系统应用**接入 AppMgr（App Center 可见、
> 平台 API 管理），并桥接进 Result Hub。以下为全部对外数据面。

## 0. Result Hub（平台统一结果总线，前端消费入口）

appmgr 内置 forwarder 把本服务状态桥接进 Result Hub（`trust: in-process`）：

- 订阅：`ws://127.0.0.1:8125/ws/ai/results/v2`（本机）或经 nginx 的
  `wss://<设备>/ws/ai/results/v2`；订阅过滤 `sources: ["motion-detect"]`
- 每条 canonical envelope 的字段：
  - `source`: `{"kind": "system", "id": "motion-detect", "trust": "in-process"}`
  - **`summary.motion`**(bool)：是否运动中 ← 前端主字段
  - `summary.score` / `summary.changed_ratio` / `summary.changed_pixels`
  - `summary.daemon_running`(bool)：服务是否存活（false = 服务离线终态）
  - `results[]` 恒为空（不输出检测框）
- 发布纪律：状态变化即发 + 10s 心跳；服务停止时补发 `motion:false` 终态

---

设备侧守护进程对外提供两个方向的接口：

- **HTTP 服务端**（daemon 监听）：参数查询/设置、状态查询 —— 一次性短连接
- **WebSocket 客户端**（daemon 主动连出）：检测状态推送 —— 长连接

---

## 1. HTTP 参数/状态接口

### 1.1 基本信息

| 项 | 值 |
|---|---|
| 基础地址 | `http://<设备IP>:<http_port>`，`http_port` 默认 `8099`，`http_host` 默认 `0.0.0.0` |
| 鉴权 | `http_token` 非空时，所有请求要求请求头 `Authorization: Bearer <http_token>`；为空则不鉴权 |
| 请求/响应体 | 均为 JSON（`Content-Type: application/json`），POST body 上限 65536 字节 |
| 并发 | 多线程处理；任何接口异常只返回 500，不影响检测主循环 |

### 1.2 `GET /config` — 查询当前参数

响应 `200`：当前生效的完整配置对象。

```json
{
  "sq_diff_threshold": 400.0,
  "min_changed_ratio": 0.005,
  "motion_hold_ms": 3000,
  "baseline_frames": 5,
  "current_frames": 2,
  "baseline_gap_ms": 1000,
  "sample_fps": 5.0,
  "analysis_width": 160,
  "heartbeat_sec": 30,
  "http_host": "0.0.0.0",
  "http_port": 8099,
  "http_token": ""
}
```

### 1.3 `POST /config` — 设置参数（部分更新）

请求体：JSON 对象，只包含要改的键。

```json
{"sq_diff_threshold": 600, "min_changed_ratio": 0.01}
```

响应 `200`：

| 字段 | 含义 |
|---|---|
| `applied` | 已接受并**立即热生效**的键值（同时写回 config.json 持久化） |
| `ignored` | 被拒绝的键值（未知键 / 类型错误 / 超出范围） |
| `restart_required` | 已保存但需重启进程才生效的键：`sample_fps`（源端降帧）；`http_host`/`http_port`/`http_token`（监听口不热迁移） |

```json
{
  "applied": {"sq_diff_threshold": 600, "min_changed_ratio": 0.01},
  "ignored": {"foo": 1},
  "restart_required": []
}
```

### 1.4 `GET /state` — 当前运动状态快照

```json
{"motion": true, "ts": 1793842795.06, "score": 0.1896, "changed_ratio": 0.02535, "changed_pixels": 365}
```

| 字段 | 类型 | 含义 |
|---|---|---|
| `motion` | bool | 当前是否处于运动状态（含 hold 滞回） |
| `score` | float 0~1 | 最近一次判定的变化强度（√均方差/255） |
| `changed_ratio` | float 0~1 | 变化像素占比 |
| `changed_pixels` | int | 变化像素数（分析图 160×90=14400 中的个数） |
| `ts` | float | Unix 秒 |

### 1.5 参数表（`POST /config` 可设项）

| 键 | 类型 | 范围 | 默认 | 生效 | 含义 |
|---|---|---|---|---|---|
| `sq_diff_threshold` | float | 1~65025 | 400 | 热 | 平方差阈值（≈灰度差 20 的平方） |
| `min_changed_ratio` | float | 0~1 | 0.005 | 热 | 运动判定最小变化像素占比 |
| `motion_hold_ms` | int | 0~600000 | 3000 | 热 | 运动消失后的保持时间（防抖） |
| `baseline_frames` | int | 1~240 | 5 | 热 | 基准画面长度（帧） |
| `current_frames` | int | 1~120 | 2 | 热 | 当前画面长度（帧） |
| `baseline_gap_ms` | int | 0~600000 | 1000 | 热 | 基准与当前画面的时间间隔 |
| `sample_fps` | float | 0.2~30 | 5.0 | **重启** | 分析采样率（换算源端 fps_divisor） |
| `analysis_width` | int | 32~640 | 160 | 热* | 分析分辨率宽度（*重建检测缓冲） |
| `heartbeat_sec` | int | 5~3600 | 30 | 热 | 心跳间隔（秒） |
| `http_host` / `http_port` / `http_token` | str / int / str | 0~65535 | 0.0.0.0 / 8099 / 空 | **重启** | 本接口自身；`http_port=0` 关闭接口 |

### 1.6 错误码

| 码 | 场景 |
|---|---|
| 400 | body 不是合法 JSON / 不是对象 / Content-Length 非法 |
| 401 | `Authorization` 缺失或与 `http_token` 不匹配 |
| 404 | 路径不存在 |
| 500 | 服务端内部错误（消息中含原因） |

---

## 2. WebSocket 直推通道（已移除）

系统应用形态下结果默认进平台 Result Hub（见第 0 节），不再保留独立的
WS 直推通道。历史上该通道的消息格式为 `{"type":"state","motion":...}`；
如有旧对接方，请改订阅 Result Hub（`sources:["motion-detect"]`）。
