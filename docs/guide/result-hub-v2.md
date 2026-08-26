# 统一 AI Result Hub v2

Result Hub 把固件内建 AI 与 App Center 多应用结果汇入同一个、可鉴权、可订阅的
WebSocket。旧接口继续存在，迁移不要求已有消费者立即改代码。

## 入口与兼容性

| 用途 | 本机入口 | 浏览器入口 | 状态 |
|---|---|---|---|
| canonical v2 | `127.0.0.1:8125` | `/ws/ai/results/v2` | 新接口，app + builtin |
| App legacy | `127.0.0.1:8124` | `/appcenter/ws/results` | 原样保留，仅 app |
| builtin legacy | `127.0.0.1:8123` | `/ws/inference/results` | 原样保留，模板后文本 |

公网入口由 nginx 的 `/_jwt_verify` 保护，并拒绝浏览器跨源 WebSocket。
8125 只监听 loopback。App 的 8124 消息先原样广播，再以非阻塞 hook 进入 Hub；
Hub、模板或消费者失败不会改变 legacy 结果。

## canonical 外层

每条 raw 消息都有相同的外层字段：

```json
{
  "schema": "recamera.ai.result",
  "schema_version": 2,
  "type": "frame",
  "id": "yolo-detector:3:42:frame",
  "source": {
    "kind": "app",
    "id": "yolo-detector",
    "app_id": "yolo-detector",
    "instance": "...",
    "generation": 3,
    "trust": "peercred"
  },
  "seq": 42,
  "time": {"wall_ms": 1780000000000, "pts_us": 12345678},
  "stream": {
    "id": "main",
    "width": 1280,
    "height": 720,
    "coordinate_space": "pixel_xyxy"
  },
  "results": [],
  "events": [],
  "metrics": {},
  "summary": {},
  "render": {},
  "extensions": {}
}
```

`type` 的数据类型为 `frame | event | status | metrics`。连接控制还会收到
`hello | snapshot`；formatted 订阅收到 `type=formatted`，其原始语义位于
`extensions.raw_type`。

身份字段由服务端赋值：

- app：8124 ingress 的 SO_PEERCRED PID 必须匹配 appmgr 当前
  `app_id/instance/generation`；payload 中同名字段会被覆盖。应用 payload 自带的
  `render` 也会被丢弃；Hub 只在认证 hello 控制路径读取当前已安装 manifest v2，按
  精确的 app/instance/generation 缓存其 `render`，然后注入 raw、snapshot 与
  formatted envelope。结果热路径不读取 manifest。
- app 的 `time.wall_ms` 使用 gateway 接收时刻；payload 自报的未来/旧时间仅保存在
  `extensions.reported_timestamp(_ms)` 作诊断，不能跨 generation 抢占最新状态。切换
  generation 会清除该 app 的旧 frame/status/event、ingress 与客户端待发项，并在最终
  缓存/广播前再次 CAS，阻止已经进入 normalize 的旧进程回填。
- builtin：只接受严格 system hello，并经可替换的系统 identity resolver；payload
  中的 `source_id` 不能改变 canonical `source`。

## 坐标

- App 坐标只信任当前 generation 的已安装 manifest v2 `output.fields[].from/coord`。
  Hub 会按 result/event kind 给 `box/quad/keypoints` 及其 direct alias 注入 `spaces`；
  payload 自带 `space/spaces` 被丢弃，未声明、冲突或复杂/derived 路径均为 `unknown`。
  manifest 可声明 pixel 或 normalized。pixel 缺 `width/height` 时强制为 `unknown`。
- 当前 `camera.frames` 平台合约对应 frame.sock VI pipe0/ch1 与浏览器 main preview
  (`/live/0`)；canonical `stream.id=main`。payload 的 `camera-0` 仅记录在
  `extensions.reported_stream_id`，不能选择主/子码流。
- builtin protobuf 坐标是归一化画面坐标：
  `stream.coordinate_space=normalized_xyxy`。
- 每个 result 的 `spaces` 逐 shape 声明，例如：

```json
{
  "box": [10, 20, 100, 160],
  "keypoints": [[20, 30, 0.9]],
  "spaces": {
    "box": "pixel_xyxy",
    "keypoints": "pixel_points"
  }
}
```

单 shape 结果另带兼容字段 `space`。`box`、`quad`、polygon 与 points 不会被
错误标成同一种表示。builtin 的 `{left,top,right,bottom}` box 对象保持原结构。

## 订阅

服务端第一条消息一定是 `type=hello`。客户端不发配置时使用 `raw + 全部来源 +
全部数据类型`。浏览器发送的 WebSocket frame 必须按 RFC 6455 masked：

```json
{
  "type": "subscribe",
  "view": "raw",
  "sources": ["*"],
  "types": ["frame", "event", "status", "metrics"]
}
```

也可只看一个应用的格式化结果：

```json
{
  "type": "subscribe",
  "view": "formatted",
  "sources": ["yolo-detector"],
  "types": ["frame", "event"]
}
```

订阅生效后先收到 `snapshot` 控制消息，再收到匹配的最新帧、状态和短期事件。snapshot
抓取/排队与 live publish 使用同一个发布 fence，不会出现新帧先到、旧 replay 后到的
latest-wins 倒退。
重订阅可能重放相同事件，消费者按 `event_id` 去重。

## raw 与 formatted

raw 是认证后、模板前的 canonical envelope，永远保留。一个认证 ingress batch 可以
拆成 frame/status/多个 event raw record；这些 record 共享 `extensions.batch_id`。
formatted 对**完整原始 batch**（results + events + summary + 性能字段）只渲染一次，
不会把最后一个 event 误当成整批输出，也不会覆盖 raw：

```json
{
  "schema": "recamera.ai.result",
  "schema_version": 2,
  "type": "formatted",
  "id": "...:formatted:0",
  "source": {"kind": "app", "id": "yolo-detector"},
  "seq": 42,
  "time": {"wall_ms": 1780000000000, "pts_us": 12345678},
  "stream": {},
  "results": [],
  "events": [],
  "metrics": {},
  "summary": {},
  "render": {},
  "extensions": {
    "raw_id": "batch:yolo-detector:3:19",
    "raw_type": "frame",
    "batch_id": "batch:yolo-detector:3:19",
    "batch_types": ["frame", "status", "event"],
    "projection": "authenticated_ingress_batch"
  },
  "payload": "{\"count\":1}",
  "content_type": "application/json",
  "profile": "app:custom"
}
```

- app 使用已安装 manifest 与 appmgr effective config 中的 `iMode`、`dTemplate`、
  `output_mapping`；配置仍由现有 App Center API 写入。
- builtin 只读 `/userdata/config/notify.json` 的 `dTemplate`，按 task 选择模板。
- 两者都复用 Kit 的 `SandboxedEnvironment`、`StrictUndefined`、过滤器白名单、
  16 KiB 模板和 256 KiB 输出上限；不调用 notify 的普通 Jinja Environment。
- 没有模板或渲染失败时，formatted 返回 compact raw JSON；raw 订阅及其他来源不受影响。
- raw-only 时不读模板、不执行 Jinja。只有 formatted subscriber 才把 batch 投到独立的
  有界 formatter worker；结果按 batch 缓存，重放/多客户端不会重复执行 formatter。
  worker 队列优先 edge，格式化 I/O/渲染不占 generation fence 或 inference ingress。

## 缓冲与重放

- `frame`：每个 `(source, stream)` 只保存最新值；慢客户端只丢旧帧。
- `status/summary`：每来源保存最新值，重连/重订阅后恢复。
- `event`：拆成独立消息，服务端生成 namespaced `event_id`，并在有界、短 TTL
  环形队列内去重与重放。生产者原 ID 保存在 `producer_event_id`。
- `extensions.delivery=edge|state` 区分需要重放的业务边沿与逐帧状态。内置保守分类
  把 fall/line-cross、blink/yawn、wake/transcript/listen-timeout 与训练完成标为 edge；
  detection/track/metrics/pose、持续的 QR/OCR/drowsiness 和普通 workout 按
  `source + event_kind` latest-wins。同一帧多个同 kind state 会合成一个完整 group
  snapshot（例如 QR A+B 都保留）；下一批整组替换，因此消失的 B 不会残留。
- state 只能替换或淘汰 state，不能挤掉 edge；edge 会先回收 state，全 edge 时才滚动
  最老 edge。内容相同的 state 使用内容 hash 去重；没有 producer ID 的真正 edge 把
  `seq/pts/index` 纳入 ID，因此 30 秒内两次相同 transcript/wake 仍是两个边沿。
- 客户端队列满时，淘汰顺序为旧 frame，再到 status/metrics；新 frame 或 status
  以及 state event；任何 state 都不能删除已经排队的 edge。连续饱和会关闭慢客户端，
  使其重连后从短期 edge replay 恢复，而不是永久静默漏事件。
- 慢客户端的发送超时只作用于 socket 写方向；只接收结果、不发送心跳或重订阅消息的
  浏览器连接不会因为服务端读线程空闲而被误关闭。

## system UDS

入口为 `/run/recamera/ai-system-results.sock`，AF_UNIX/SOCK_STREAM、UTF-8 NDJSON，
单行上限 512 KiB。首行必须精确为：

```json
{"type":"hello","protocol":"recamera-system-result@1","source":"builtin"}
```

服务端只对 hello 返回一次 `hello_ack`，结果行没有逐条 ACK。publisher 应使用有界
队列、自动退避重连。Hub 的生产 resolver 还要求 SO_PEERCRED PID 精确匹配安全读取的
`/var/run/notify_server.pid`（regular、root-owned、非 group/world writable、拒绝 symlink），
并前后复核 pidfile inode 与 `/proc/<pid>/stat` starttime、全部 UID、Python exe 和精确
`-m recamera_notify.notify_server` argv；同 UID/同 argv 的另一进程也不能获得 builtin
身份。单靠 hello 或 payload 不能获得 builtin 身份。

## 运维状态与平台 observer

- `GET /api/app-center/v1/results/status`
- legacy 管理别名：`GET /api/appMgr/resultHub`
- `/api/app-center/v1/resources` 和 app 列表中的 `result_hub`

状态包含 ingress、订阅者、最新值、事件环、格式化错误和 observer 丢弃计数。
后续平台 OSD bridge 可调用线程安全的 `ResultHub.add_observer(callback)`；每个
observer 有独立有界 worker，异常和阻塞不会进入 ingress 线程。移除时调用
`remove_observer(callback)`。若 callback 是对象方法，owner 可实现
`invalidate_source(source_id, identity=..., capability=...)`：应用 generation 或可信
manifest capability 刷新时，Hub 会在发布顺序域中清除该来源尚未消费的旧消息，并把
revoke 在 refresh 返回前同步送达。该 hook 的契约是只做内存 epoch/cache 更新和 wake，
不得执行 I/O 或阻塞；耗时的 observe/清屏发送仍留在 observer 自己的 worker 中。正在
处理旧帧的 observer 必须在产生副作用前复核 source epoch。Hub 本身不 import
`recamera_ext`。

## 浏览器调试

```js
const scheme = location.protocol === "https:" ? "wss:" : "ws:";
const ws = new WebSocket(`${scheme}//${location.host}/ws/ai/results/v2`);
ws.onmessage = ({data}) => console.log(JSON.parse(data));
ws.onopen = () => ws.send(JSON.stringify({
  type: "subscribe", view: "raw", sources: ["*"],
  types: ["frame", "event", "status", "metrics"]
}));
```

同源 Cookie 会由浏览器随握手发送。直接连设备 IP 的 8125 不可用，因为服务只绑定
loopback；设备本机诊断可直连 `ws://127.0.0.1:8125`。
