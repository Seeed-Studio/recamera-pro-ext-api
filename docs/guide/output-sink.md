# 输出组件（声明式结果输出 / `ConfigurableSink`）

> 适用设备：reCamera Pro（RV1126B / recamera_v2）。
> 读者：**方案商 / app 开发者**——想把 app 的推理结果发到 MQTT / HTTP / UART / WS，而**不在 app.py 里写任何发送代码**。
>
> 事实来源：实施 Spec `../../internal/OUTPUT_SINK_SPEC.md`（权威：`ConfigurableSink` 类结构、通道/格式器契约、manifest 契约、Jinja2 命名空间、逐 app 集成清单，均带 `file:line`）。本文是用法文档，实现细节看 Spec。落地代码在 `kit/adapters/output_sink.py`，manifest 校验测试 `apps/test_output_manifests.py`。

## 0. 定位

app 产出的每帧结果envelope（`results` / `events` / `frame` / `timestamp` / `seq`）由 kit 统一转成**可配置、可并发的外部输出**。app 只在 manifest 里**声明**要输出什么、默认发到哪，运行时 kit 负责编码 + 发送。**app.py 的 `run()` 里对输出一行代码都不用写。**

与其他输出通路的区别：
- **统一结果查看/软件叠加** → [ai-result-overlay.md](./ai-result-overlay.md)与 [result-hub-v2.md](./result-hub-v2.md)（canonical WS 8125；legacy app WS 8124 保留）。
- **结果注入 OSD/推送**（框进 RTSP 与已经进行的录像，但不启动录像）→ [README.md](./README.md) §4 结果注入。
- **托管应用按 AI 结果启动录像** → manifest v2 [`record_trigger`](./app-package-v2.md#managed-recording-triggers)。
- **本文（输出组件）** = 把结构化结果**声明式**发到 MQTT/HTTP/UART/WS 外部消费者，含 Home Assistant 即插即用。

## 1. 接入：manifest 声明，app 零代码

opt-in 只要两件事：`capabilities` 加 `"output"`，并给一个 `output` 块。**不声明 `output` capability = 现状完全不变**（不注入任何输出配置、不加载 `ConfigurableSink`、app 自己的输出代码照跑）。

以 `apps/yolo-detector/manifest.json` 的真实 `output` 块为例：

```jsonc
{
  "capabilities": ["output"],
  "output": {
    "default_channel": ["ws"],          // 默认发到哪些通道（可多选，见 §3）
    "default_mode": "raw",              // 默认格式：raw | custom | ha（见 §4）
    "fields": [                         // 字段声明：能被映射/模板引用的字段清单
      {"name": "box",      "from": "results[].box",      "type": "bbox<float>[4]", "coord": "pixel_xyxy", "description": "Detection box, original-frame xyxy pixels"},
      {"name": "cls",      "from": "results[].cls",      "type": "integer",         "description": "COCO-80 class id"},
      {"name": "cls_name", "from": "results[].cls_name", "type": "string",          "description": "COCO-80 class label"},
      {"name": "score",    "from": "results[].score",    "type": "float",           "description": "Detection confidence"},
      {"name": "detection_label", "from": "events[kind=detection].label", "type": "string", "event_kind": "detection", "description": "Class label of the detection event"}
    ],
    "default_mapping": [                 // 默认可视化映射（source→target→topic，见 §4）
      {"source": "detection.count", "target": "count", "topic": "recamera/{{ app }}/count", "task": "detection"},
      {"source": "detection.entries", "target": "detections", "topic": "recamera/{{ app }}/detections", "task": "detection"}
    ]
  }
}
```

字段说明：

| 键 | 必填 | 含义 |
|---|---|---|
| `fields[].name` | 是 | 字段逻辑名（映射/模板里可引用） |
| `fields[].from` | 是 | 只读点路径（`results[].box`、`events[kind=detection].label`、`geometry[]`），带 `[]` 列表投影；**不是任意 Jinja**，是取值路径 |
| `fields[].type` | 是 | 类型标注（`string`/`integer`/`float`/`bbox<float>[4]`/`object<number>` 等） |
| `fields[].description` | 是 | 人读说明，前端字段选择器展示 |
| `fields[].coord` | 否 | 坐标系（如 `pixel_xyxy`、`pixel_quad`；`geometry[]` 只能用 `pixel_points`/`normalized_points`），格式器不得静默改写 |
| `fields[].event_kind` / `unit` / `optional` | 否 | 事件种类绑定 / 单位 / 可空标注 |
| `default_channel` | 否 | 默认通道，字符串或列表，载入时归一成列表 |
| `default_mode` | 否 | `raw` / `custom` / `ha` |
| `templates` | 否 | 五任务默认 Jinja2 模板（detection/classification/keypoint/segmentation/tracking） |
| `default_mapping` | 否 | 默认可视化映射行（见 §4） |

用户在前端改的所有值（选通道、填 broker、改模板）覆盖在 manifest 默认之上；app 不感知。

## 2. 每帧结果 envelope（映射/模板的输入）

kit 在 sink 入口把每帧规范成一个 envelope（Spec §1）：

```json
{
  "app": "yolo-detector",
  "timestamp": 1723500000123,     // Unix epoch 毫秒，sink 入口生成
  "seq": 42,                       // 每 app/进程单调递增
  "frame": {"width": 1920, "height": 1080, "pts": 123.456},
  "results": [],                   // app 产出的结果项
  "events": [],                    // app 产出的业务事件
  "geometry": []                   // 可选 canonical 绘制图元
}
```

映射和模板都是对这个 envelope 取值。

`geometry[]` 支持 `point/line/polyline/polygon`，每项统一使用 `points:[[x,y],...]`。
它随 raw/custom 输出保留；Result Hub 会按严格 manifest v2 声明重新验证并注入可信坐标空间。
完整字段、style 上限与 manifest `render.geometry` 示例见
[result-hub-v2.md](./result-hub-v2.md#通用-geometry-primitives)。

## 3. 通道（可多选并发）

| 通道 | 连接配置 | 说明 |
|---|---|---|
| **MQTT** | `dMqtt`（`sURL` broker、`iPort` 默认 1883、`sClientId`、`sUsername`/`sPassword`、`sTopic` base/state topic） | 支持 HA 上下线（§5）。手写最小 MQTT，无 paho 依赖 |
| **HTTP** | `dHttp`（`sUrl` POST 目标、`sToken` bearer） | stdlib `urllib` POST，有界队列，永不阻塞推理 |
| **UART** | `dUart`（`sPort` 逻辑选择、`sPortDev` 如 `/dev/ttyS2`） | 换行分隔，路径白名单限 `/dev/ttyS*`。波特率/权限已核实，见 §3.1。仍默认 feature-gate（`RECAMERA_UART_ENABLE`） |
| **WS** | canonical :8125 `/ws/ai/results/v2`；legacy :8124 `/appcenter/ws/results` | 管理 app 的 primary sink 始终先进 appmgr 8124 gateway，再非阻塞镜像到 Result Hub。`output_channels:["ws"]` 不再为每个 app 建独立端口 |

多个通道可同时激活；一个通道失败被隔离 + 限速日志，不拖累其余通道和推理（复用 `MultiSink` 的扇出失败隔离）。

### 3.1 UART 实测结论（2026-08-15 真机，kernel 6.1.157）

**设备上只有两个串口**，`/dev/ttyS1` 不存在，示例配置应写 `/dev/ttyS2`：

| 节点 | 权限 | 当前波特率 | 占用 | 可用性 |
|---|---|---|---|---|
| `/dev/ttyS2` (4,66) | `crw-rw---- root:dialout` | 9600 | 空闲（`fuser` 无输出） | ✅ **可用** |
| `/dev/ttyS4` (4,68) | `crw-rw---- root:dialout` | 115200 | 被 pid 575 `/oem/usr/bin/tmir` 占用 | ❌ 官方串口服务在用，勿抢 |

- **不是控制台**。内核 `console=ttyFIQ0`，`/proc/consoles` 只有 `ttyFIQ0`（fiq-debugger，`/dev/ttyFIQ0`）。两个 `ttyS*` 都没有 getty/控制台占用。
- **权限**：节点属 `root:dialout` 0660。appmgr 拉起的 app 进程实测 `Uid: 0`（`/proc/<pid>/status`），**以 root 运行，能直接打开**。SSH 登录用户 `admin`（uid 1000，仅 `admin` 组，不在 `dialout`）手动跑同样代码会 `Permission denied` —— 调试时要注意这个差别。
- **波特率：代码不设置，沿用内核/前一使用者留下的线路设置**。`kit/adapters/output_sink.py:644` 的 `UartChannel._open_device()` 只做 `os.open(port_dev, O_WRONLY|O_NONBLOCK)`，全文无 `termios`/`tcsetattr`/`pyserial`。实测 `stty -F /dev/ttyS2` 当前是 **9600 8N1**，`/dev/ttyS4` 是 115200。硬件 `base_baud=1500000`（dmesg：`21170000.serial: ttyS2 ... is a 16550A`）。
  → 需要指定波特率的方案商，当前只能在 app 外部先 `stty -F /dev/ttyS2 <baud>`，或等 `UartChannel` 增加 termios 配置（未实现）。
- `serial@21170000`（ttyS2）无 DMA 通道，走中断模式（dmesg：`failed to request DMA, use interrupt mode`），高波特率大吞吐场景需自行验证丢帧。

## 4. 格式模式：raw / custom / ha

`default_mode`（前端可改）决定 envelope 怎么编码：

### 4.1 raw（原始 JSON）

`RawJsonFormatter` 把 canonical envelope 紧凑序列化，无字段丢失。零配置的兜底模式，风险最低。

### 4.2 custom（可视化映射 + Jinja2）

两种视图，同一底层格式器，永不产生两份发布。`template_mode` 是显式互斥选择：`mapping` 只使用 `output_mapping`，`template` 只使用 `dTemplate`；不再用“mapping 非空就静默覆盖自由模板”的隐式优先级。旧配置没有该键时仍兼容原行为（有 mapping 选 mapping，否则选 template），manifest 带 `default_mapping` 的既有包默认值为 `mapping`：

**① 可视化映射行**（`default_mapping` / 前端映射表）——`source → target → topic`：

```jsonc
[
  {
    "source": "detection.count",              // 取值表达式（envelope 命名空间）
    "target": "person_count",                 // 输出 JSON 里的键
    "topic":  "recamera/{{ app }}/people",    // 目标 topic，只可插 app + 消毒后的设备 id
    "task":   "detection",                     // 默认取 UI 选中任务
    "omit_if_none": true                        // 默认 true
  }
]
```

生成规则：按渲染后 topic 分组，每组生成一个 JSON 对象模板，`target` 映射到 `source` 表达式，`tojson` 编码值，可空行包 `{% if <source> is defined and <source> is not none %}`。生成的模板存进 `dTemplate.s<Task>` 并标 `generated_from_mapping:true`；切到自由模板模式清标记但**不销毁映射行**。topic 拒绝通配符（`+` `#`）、NUL、空 topic。

**② 自由 Jinja2 模板**——直接写模板，命名空间（Spec §4）：

| 命名空间 | 含义 |
|---|---|
| `app` / `timestamp` / `seq` / `frame` / `results` / `events` | canonical envelope 值 |
| `summary` / `metrics` / `render` | 同一批结果的状态、性能与可信渲染声明 |
| `stream_id` / `inference_time_ms` / `pipeline_ms` | Kit 实际输出中的顶层流与耗时字段 |
| `detection.count` | 过滤后检测类结果数 |
| `detection.entries` | 规范化 `{box,score,class_id,label,raw}` 列表，几何保留其声明坐标系 |
| `keypoints` | 带关键点的结果；`classification` 分类类；`tracking` 跟踪；`segmentation` 带 mask |
| `events.<kind>` | 某种事件列表，如 `events.fall` / `events.metrics` / `events.transcript`；`events.all` 取全部 |

受限环境：`StrictUndefined`、autoescape off（JSON 非 HTML）、只放 `tojson`/`default`/`length`/`selectattr`/`map`/`min`/`max`/`sum` 等；**无**文件加载器、import/include、Python 内部属性访问、用户可调对象。强制模板长度、渲染载荷与命名空间条目上限。渲染出错只丢那条通道消息，不终止推理；Result Hub 另用有界异步 worker 隔离慢模板，不阻塞 raw/推理 ingress。

`template_mode` / `dTemplate` / `output_mapping` 的 apply mode 均为 `live`：SIGHUP 后
`ConfigurableSink` 会用已保存的 manifest/app metadata 与新的 effective config
重新 resolve/build formatter。编译失败保留上一个已知正常的 formatter，不再依赖
不存在于 config.json 的私有 `_formatter` 对象。Result Hub 的 formatted
订阅使用同一受限 formatter，raw v2 envelope 永不被模板替换。Hub 对一个认证 ingress
batch 的完整 `results + events + summary` 只渲染一次并缓存；frame/status/event 的
canonical 拆分不会造成多次模板副作用或“最后只剩 event”的预览漂移。

### 4.3 ha（Home Assistant Discovery）

`HaDiscoveryFormatter` 按 manifest 的 `ha_entities` 定义 + canonical 字段发 HA MQTT Discovery 报文（`homeassistant/<component>/<node>/<obj>/config`），即插即用：HA 自动建实体。`unique_id` 稳定构造，防重复实体。上下线见 §5。

## 5. Home Assistant 上下线（LWT + availability，kit 内置）

kit 独占所有 availability 消息，app 和模板不能覆盖：

- 状态 topic `<base>/<app>/status`。
- MQTT 连接时用 retained LWT（`will_payload="offline"`, retain）连上，publish retained `online`，每次重连后重发 retained discovery。
- app 激活/连接 → 该 app publish `online`；优雅停止/切换 → publish `offline`；进程意外死亡由 LWT 兜底 `offline`。
- Discovery 报文含 `state_topic` / `availability_topic` / `payload_available` / `payload_not_available`。

这套复用 `kit/adapters/mqtt_sink.py` 现有 discovery/LWT 原语，不重复实现。

## 6. 不用 kit 输出组件：app 自己发

**不声明 `output` capability** → kit 完全不介入输出。app 在 `run()` 里自己 `import paho` / `requests` 发（带 per-app 依赖，见 [per-app-dependencies.md](./per-app-dependencies.md)）。

两者可混用：声明 `output` 让 kit 发软件叠加/MQTT/HA，同时 app 自己再发一路别的外部 topic。**约束**：opt-in 后 kit 拥有配置的外部通道，app **不要**再自己往同一个外部 topic 发同一份数据（会重复发布）。

## 7. 真机验证（P3b，已验证）

在设备 + 本地 MQTT broker 上验证通过（Spec §9 P3）：

- **raw JSON**：canonical envelope 完整发到 MQTT，消费端收全字段。
- **HA Discovery**：`homeassistant/sensor/.../config` 报文含 `state_topic` / `availability_topic` / `unique_id` / `device`，HA 自动建实体。
- **上下线 LWT**：连上 retained `online`、优雅停 `offline`、断连由 LWT 置 `offline`。
- **custom jinja2**：可视化映射生成模板 + 自由模板两路渲染并发布通过。

> **已修复的历史问题**：`ConfigurableSink` 现在会在 MQTT 首次连接前完成
> `on_ready` 绑定，HA discovery/上线状态不再等到下一次重连；模板热更新也会
> 从普通 effective config 重建 formatter，失败保留旧版。

## 8. 依赖

- **Jinja2**（custom/ha 模板渲染）：设备 `/userdata/rknnenv` 已装 **3.1.2**。经 uv 管理（`uv add jinja2`），release wheel 集含 Jinja2 + MarkupSafe 离线 wheel，装进 `/userdata/rknnenv`。
- raw 模式无 Jinja2 依赖，是最低风险兜底。
