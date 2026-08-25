# AI 结果查看与视频软件叠加

> 统一结果协议与调试方法见 [result-hub-v2.md](./result-hub-v2.md)。

## 当前后端入口

App Center 与固件内建 AI 的统一数据源是：

```text
wss://<设备>/ws/ai/results/v2
```

nginx 在 WebSocket Upgrade 前执行官方 `/_jwt_verify`，浏览器使用登录后的同源
Cookie。上游 Result Hub 只绑定 `127.0.0.1:8125`，不会向局域网裸露无鉴权端口。

旧 App WS `/appcenter/ws/results`（8124）仍完全保留，供已有消费者兼容；它只有 app
结果，没有 builtin、订阅、状态保存和事件重放。`/ws/inference/results`（8123）仍是
builtin 的 legacy 模板后文本。新可视化不要再拼接这两条不兼容消息。

## 浏览器订阅

连接后第一条消息为 `hello`，随后是 `snapshot` 和缓存记录。默认已是 raw/all，显式
订阅示例：

```js
const scheme = location.protocol === "https:" ? "wss:" : "ws:";
const socket = new WebSocket(`${scheme}//${location.host}/ws/ai/results/v2`);

socket.addEventListener("open", () => socket.send(JSON.stringify({
  type: "subscribe",
  view: "raw",
  sources: ["*"],
  types: ["frame", "event", "status", "metrics"]
})));

socket.addEventListener("message", event => {
  const message = JSON.parse(event.data);
  if (message.schema !== "recamera.ai.result" || message.schema_version !== 2) return;
  console.log(message.type, message.source, message.results, message.events);
});
```

应用输出也可以在浏览器开发者工具中这样查看，不必读取 app 进程 stdout。进程日志与
AI 结果是两类信息：日志仍从 App Center 日志接口查看，结构化结果从上述 WS 查看。

## 软件叠加的坐标规则

软件叠加只画在浏览器画布上，**不会写入 RTSP/录像码流**。渲染器应按以下顺序处理：

1. 只消费 `type=frame`；event/status 不用于清空或重画当前框。
2. 按 `source.id + stream.id` 保存最新帧；多应用可同时显示或由用户筛选。
3. 读取 `stream.width/height/coordinate_space` 和每个 result 的 `spaces`，禁止通过
   “坐标是否小于 1”猜测归一化。
4. App 的坐标空间由当前已安装 manifest v2 的 `output.fields[].coord` 决定，可为 pixel
   或 normalized；payload 自带声明不可信。未声明/冲突，或 pixel 缺 `width/height` 时
   Hub 标成 `unknown`，渲染器必须拒绝该 shape。
5. 视频使用 `object-fit: contain` 时，坐标映射到实际画面矩形并加入 letterbox 偏移；
   canvas 位图尺寸还要乘 `devicePixelRatio`。

当前平台 `camera.frames` 来自 frame.sock VI pipe0/ch1，对应 main preview
(`/live/0`)，所以 Hub 使用 `stream.id=main`；应用历史字段 `camera-0` 只是诊断值。前端还
应在 rotation/aspect 不匹配时 fail closed，不能把 main 坐标硬套到另一条码流。

一个结果可能同时有 box 和 keypoints：

```json
{
  "spaces": {
    "box": "pixel_xyxy",
    "keypoints": "pixel_points"
  }
}
```

不能用一个 `pixel_xyxy` 标记解释 points/quad。builtin box 的
`{left,top,right,bottom}` 对象会原样保留。

## 建议的渲染回退

优先级为：

1. envelope 的 `render`；
2. App 列表返回的 manifest `render`；
3. 根据 shape 自动回退。

建议至少支持：box/label、quad/text、pose skeleton、face mesh、事件告警、字幕与
metrics/status inspector。语音应用没有视频 shape，应显示字幕/历史/状态面板，而不是
强行叠加在画面上。

canonical envelope 的 `render` 不是应用运行时数据：Hub 会丢弃 payload 自带值，仅
注入当前认证 generation 对应的已安装 manifest v2 声明。因此第 1 项可以直接作为
可视化契约使用；第 2 项主要用于连接建立前的卡片预览与兼容回退。

事件面板还应读取 `extensions.delivery`：`edge` 进入短期历史/提示，`state` 按
`source.id + extensions.event_kind` 覆盖显示。不要让高频 metrics/track/pose_state 或
持续 QR/OCR/drowsiness 观测滚动淹没 fall、line_cross、transcript 等真正边沿。同一帧
多个 QR/OCR 会作为一个 state group 展示，下一帧整组替换。

## 与烧流 OSD 的区别

| 路径 | 浏览器可视化 | 进入 RTSP/录像 |
|---|---:|---:|
| Result Hub `/ws/ai/results/v2` | 是 | 否 |
| legacy App 8124 | 可由旧消费者绘制 | 否 |
| `result-in.sock` / `OfficialResultSink` | 可同时有 metadata | 是 |

后续平台 OSD bridge 可通过 Result Hub 的异步 observer 消费已认证 raw v2 envelope；
它与浏览器 WS 是并列消费者，不允许反向修改 raw 或阻塞推理 ingress。
