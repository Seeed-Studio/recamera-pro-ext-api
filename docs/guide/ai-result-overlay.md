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
   `type=source_invalidated` 是例外的生命周期控制：按消息携带的精确
   `app/instance/generation` 立即清除旧 frame，并拒绝另一条 formatted/raw WS 延迟到达
   的同 tuple；不得按 app id 通配清掉已经出现的新 generation。
2. 按 `source.id + stream.id` 保存最新帧；多应用可同时显示或由用户筛选。
3. 读取 `stream.width/height/coordinate_space` 和每个 result 的 `spaces`，禁止通过
   “坐标是否小于 1”猜测归一化。
4. App 的坐标空间由当前已安装 manifest v2 的 `output.fields[].coord` 决定，可为 pixel
   或 normalized；payload 自带声明不可信。未声明/冲突，或 pixel 缺 `width/height` 时
   Hub 标成 `unknown`，渲染器必须拒绝该 shape。
5. 视频使用 `object-fit: contain` 时，坐标映射到实际画面矩形并加入 letterbox 偏移；
   canvas 位图尺寸还要乘 `devicePixelRatio`。

`frame.geometry[]` 是应用自定义绘制的统一入口，支持 `point/line/polyline/polygon`；
box、quad、keypoints、pose 可由 Python Kit helper 转换。渲染器只接受 Hub 注入的
`space` 及 envelope 中的可信 `render.geometry`，图元自带的 `space/render/stream/source`
不能提升权限。`space=unknown`、未知图元或越出声明坐标范围的图元必须跳过，但不能因此
清掉同一 frame 中其他合法 results/geometry。

`camera.frames` 只授予资源权限，不代表当前进程实际使用的帧源。只有 appmgr 受控启动
为该 instance/generation 选择 official frame.sock VI pipe0/ch1，并把独立的可信 stream
contract 交给 Hub 时，Hub 才使用 `stream.id=main`，对应 main preview `/live/0`；缺失或
非法 contract 均不关联视频流。应用历史字段 `camera-0`/`main` 只是诊断值。前端还应在
rotation/aspect 不匹配时 fail closed，不能把 main 坐标硬套到另一条码流。

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

消息模板是额外的 formatted 文本投影，不是绘制数据的替代品。raw 与 formatted frame
均保留 canonical `geometry[]` 和 `render`；前端开启模板后仍应从 frame 绘制，不能只因
存在 `payload` 就跳过叠加。

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
| appmgr detection OSD bridge / `osd-in.sock` | 与浏览器并列消费 raw v2 | 是 |

设备级烧录通过 `PUT /api/app-center/v1/visualization` 配置。启用时
`osd.sources` 至少包含一个应用，不能保存 `enabled=true,sources=[]`；旧固件遗留的这类
矛盾配置在读取、bridge 启动和 API 展示时统一按 disabled 处理。只有 manifest v2 同时
使用 `output.contract_version=2`、`render.schema_version=1`，且
`render.stream_osd.supported` 明确包含 `boxes`，并直接声明至少一个
`results[].box` 且所有别名使用同一明确 xyxy 坐标空间的应用可选。对早期严格 v2 契约，仅当
`render.boxes` 存在、且恰好声明一个非 derived 的 `results[].box`，坐标明确为
`pixel_xyxy` 或 `normalized_xyxy` 时，控制面才投影等价能力；不根据应用 id、版本、
payload 或坐标数值猜测。其他旧应用需要升级或重新安装兼容版本。

bridge 是 Result Hub 已认证 raw v2 envelope 的异步 observer，与浏览器 WS 为并列消费者；
它不反向修改 raw，也不阻塞推理 ingress。浏览器设置页在切换“实时/可视化”页签时应保留
同一个视频预览组件，设备级烧录设置本身不会暂停 WebRTC 或浏览器 canvas 绘制。
