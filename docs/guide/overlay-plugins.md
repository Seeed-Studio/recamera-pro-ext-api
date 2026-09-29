# 应用自带叠加插件（ui.overlay）

插件让你随应用包交付**一个自包含 HTML 文档**，由 App Center 前端把它挂进沙箱 iframe，在实时视频上画你自己应用的结果 UI——多行列表、圆角面板、自定义排版、骨架、动效这类声明式画布表达不了的形状。

适用范围、坐标约定、声明式回退与「显示样式」覆盖的对照见 [overlay-customization.md](./overlay-customization.md)；本文只讲插件。

事实来源：

- 服务端（manifest 契约、安装闸门、取数字节）：`market/appmgr/manifest.py`、`market/appmgr/installer.py`、`market/appmgr/server.py`、`market/appmgr/paths.py`、`market/appmgr/schema/manifest-v2.schema.json`
- 前端宿主（装载、通道、消息、失败与回退）：前端仓 `src/components/inference/results/AppOverlayPlugin.js`（提交 `be5726a` / `9b63caa`，2026-09-28），父级装配 `src/components/preview/PreviewAIResults.js`
- 示例：`examples/12-overlay-plugin/`（最小）、`apps/eldercare-monitor/`（完整）

## 1. 它是什么，什么时候该用

一个应用的结果在浏览器里有两条渲染路径：

| | 声明式画布 | 叠加插件 |
|---|---|---|
| 声明位置 | manifest `render` 块（`boxes`/`quads`/`keypoints`/`geometry`/`subtitles`/`events`），可被设备上的「显示样式」覆盖修改 | manifest `ui.overlay` 指向包内一个 HTML 文档 |
| 谁画 | 前端 `resultOverlay.js` 通用绘制器 | 你的文档自己的 canvas/DOM |
| 能画什么 | 框、四边形、骨架点、字幕、geometry 图元（point/line/polyline/polygon），样式受渲染契约与用户覆盖限制 | 任意排版：列表、面板、状态条、动效、自定义字体（内联） |
| 用户改样式 | 生效（「显示样式」面板 / render-override 接口） | 不生效——插件收到的是合并后的 `render` 值，但画什么由插件自己决定 |
| 能看到的数据 | 自己应用的结果 | 自己应用的结果（同一份 envelope） |

**用插件当声明式不够用时**：每人一个 `ID · 状态 · 置信度` 胶囊、右侧转录滚动面板、底部状态条、任意圆角与动效。骨架、检测框这类标准图元继续声明式画更省事，而且声明式图层就是插件失败时的兜底（见 §5）。

**插件只能看到自己应用的数据。** 宿主把每个插件会话绑死在一个来源身份上（`appId + instance + generation`），只投递来源完全匹配的帧（`AppOverlayPlugin.js:134-141, 174-198`）。跨应用订阅不开放——要在一个面板里同时显示姿态和语音，就把两个生产者放进同一个应用（`apps/eldercare-monitor/app.py:6`）。

**别把插件当成"后端"**：它不发起网络请求，也拿不到设备 API（§4）。

## 2. 随包交付

### 2.1 目录与文件

```
<app>/
├── manifest.json          # ui.overlay = {entry, sha256}
└── web/
    └── overlay.html       # 单文件自包含文档
```

单文件、自包含，是因为宿主不给你任何外部资源加载能力（§4 的 CSP）：CSS/JS 必须内联，图片只能是 `data:` URI，不能引 `<link>`、外链脚本、字体、`url()`。`examples/12-overlay-plugin/test_overlay_plugin.py:87-100` 逐条扫这些模式，可以直接拿来当 lint。

### 2.2 manifest 声明

```json
"ui": {
  "overlay": {
    "entry": "web/overlay.html",
    "sha256": "32656587ed1960fb9731e2d4e5759473956721b2e9f94b732483358fd2363433"
  }
}
```

| 约束 | 规则 | 位置 |
|---|---|---|
| 只支持 manifest v2 | 服务端要求 `manifest_version == 2` 且存在 `ui` | `server.py:1159-1162` |
| `ui` 是封闭对象 | 只能有 `overlay` 一个键 | `manifest.py:210-211` |
| `ui.overlay` 是封闭对象 | 只能有 `entry`、`sha256`，两个都必填 | `manifest.py:212-214` |
| `entry` | 必须匹配 `web/[A-Za-z0-9._/-]{1,120}\.html`，且不能含 `..` 段 | `manifest.py:215-221`、`manifest-v2.schema.json:322-327` |
| `sha256` | 64 位小写十六进制 | `manifest.py:222-223` |

一个应用只有一个 overlay 入口，没有多文档、没有 `enabled` 开关（schema 封闭，写别的键会被拒）。

### 2.3 安装期闸门

安装时（`installer.py:679` 调用 `_validate_declared_overlay_payload`）逐条校验：

| 检查 | 失败结果 |
|---|---|
| 声明的 entry 必须出现在包内（且已在 BOM `files.sha256` 里登记） | 安装被拒（`manifest.py:1567-1572`） |
| 必须是常规文件，符号链接/硬链接直接拒 | 安装被拒（`installer.py:644-651`） |
| 大小 ≤ 256 KiB | 安装被拒（`manifest.py:1573-1577`、`installer.py:652-656`） |
| 声明 `sha256` 必须等于包内该文件实际字节的摘要 | 安装被拒（`manifest.py:1578-1581`） |

任一不满足，**整个安装被拒**，不是"插件被跳过、应用照装"。

上限默认 256 KiB（`manifest.py:39`）；运行期读取上限取 `paths.MAX_OVERLAY_BYTES`，默认同为 256 KiB，可用环境变量 `APPMGR_MAX_OVERLAY_BYTES` 改（`paths.py:241`）。manifest 侧的 256 KiB 是编译进去的常量，不受该环境变量影响。

### 2.4 设备侧取数接口

宿主取字节用这一条：

```
GET /api/app-center/v1/apps/<id>/overlay?h=<manifest 声明的 sha256>
```

| 状态 | 条件 | 实现 |
|---|---|---|
| 200 | 字节与 `?h=` 相符 | `text/plain; charset=utf-8`、`X-Content-Type-Options: nosniff`、`Cache-Control: no-store`、`ETag: <sha256 前 16 位>`（`server.py:4114-4121`） |
| 400 | 缺 `?h=`，或不是 64 位小写十六进制 | `server.py:4086-4097` |
| 404 | 应用未安装 / manifest 没声明 `ui.overlay` / 声明损坏 / entry 不可读（符号链接、非常规文件、超限、路径穿越） | `server.py:1155-1185`、`server.py:4098-4104` |
| 409 | `?h=` 与实际字节不符（升级窗口里新旧摘要并存，或安装树被改） | `server.py:4106-4112` |

两个刻意的设计：

- **必须带 `?h=`**，没有"不校验直接给字节"的路径：客户端把取数绑定到 manifest 声明的摘要上（`server.py:4085-4087`）。
- **`text/plain`**：这些字节只在宿主拼的沙箱 `srcdoc` 里当文档解释，直接在这个 URL 上做顶层导航不会执行（`server.py:1150-1154`、`server.py:4118-4120`）。

读取走"逐级 `O_NOFOLLOW`、常规文件、限长"的打开器（`server.py:980-1010`），和图标同一套，所以安装树被篡改也出不了自己应用目录。

### 2.5 改了 HTML 之后

文档字节变了，manifest 里的 `sha256` 必须同步，否则安装被拒（摘要不符），即使装上也会被路由 409 挡住：

```bash
NEW=$(shasum -a 256 web/overlay.html | awk '{print $1}')
# 把 manifest.json 的 ui.overlay.sha256 改成 $NEW
uv run pytest examples/12-overlay-plugin -q   # 示例自带这条断言
```

## 3. 运行时契约

### 3.1 宿主如何装载

宿主自己拼 `srcdoc`，顺序固定（`AppOverlayPlugin.js:104-112`）：

```
<!doctype html><html><head>
  <meta charset="utf-8">
  <meta http-equiv="Content-Security-Policy" content="<宿主拥有的 CSP>">
</head><body>
  <script>/* 宿主 bootstrap：在任何包代码之前跑 */</script>
  <你交付的整个 HTML 文档>
</body></html>
```

bootstrap 做两件事（`AppOverlayPlugin.js:82-100`）：

1. `new MessageChannel()`，把 `port1` 定义成 `window.__overlayPort`，**`writable: false, configurable: false`**——包代码改不了、删不掉、也换不了通道；
2. 把 `port2` 用 `window.parent.postMessage({channel:"recamera.overlay.plugin", protocol:1, type:"bootstrap", session:"<会话 id>"}, "*", [port2])` 交给宿主。

宿主只在同时满足"来源是本 iframe 的 contentWindow + channel/protocol/type/session 匹配 + 恰好一个 port"时接受这个端口（`AppOverlayPlugin.js:440-462`），并且一个会话只接受一次；第二次 bootstrap 记一次错误（测试 `AppOverlayPlugin.test.js:362-384`）。端口是唯一数据通路：宿主**从不**用 `contentWindow.postMessage` 投递数据。

iframe 的属性：`sandbox="allow-scripts"`、`referrerPolicy="no-referrer"`、绝对定位铺满叠加容器、`pointerEvents: none`、`z-index: 3`（`AppOverlayPlugin.js:337-351`）。

### 3.2 握手

插件就绪后经端口发**一条** ready，宿主才开始投递数据：

```js
window.__overlayPort.postMessage({
  channel: "recamera.overlay.plugin",
  protocol: 1,
  type: "ready",
  seq: 1,
});
```

ready 之前宿主不会投递任何帧或事件，即使数据已经到达（`AppOverlayPlugin.js:303-307`；测试 `:416-440`）。

### 3.3 宿主 → 插件消息

每条消息都有同一个信封：`{channel, protocol, seq, ...}`，`seq` 是宿主自增序号（从 1 起，每发一条加一）。**插件不需要校验宿主的 `seq`**，只有宿主校验插件的。

当前宿主只发三种类型（三个 `postToPlugin` 调用点，`AppOverlayPlugin.js:212, 419, 486`）：

| `type` | 时机 | 实际携带的字段 |
|---|---|---|
| `init` | 宿主接受端口之后、ready 之前，立即发一次 | `locale`（界面语言，如 `"zh"` / `"en"`）、`geometry: {width, height}`（覆盖层容器 CSS px，四舍五入；容器还没测到时为 `0`，之后由 `geometry` 消息补齐——前端测试断言的初值就是 `0,0`）、`capabilities: {maxMessageBytes: 8192}` |
| `data` | 插件 ready 之后，源数据变化时 | `frames`（**本次新增**的帧 envelope 数组）、`events`（事件项数组，每项多一个 `dedupeKey`）、`render`（仅在 render 变化时携带；ready 后第一次投递必带） |
| `geometry` | 容器尺寸变化后（ready 之后） | `geometry: {width, height}`（容器 CSS px） |

**没有 `teardown` 消息。** 会话结束（组件卸载、切换来源、失败回退）就是移除 iframe + 关闭端口 + 中止在途 fetch（`AppOverlayPlugin.js:353-380`）——插件不会收到任何"要关了"的通知。

注意消息名：当前宿主发的是**批量** `data`，不是早期的逐帧 `frame` / 逐条 `event`。历史上按 `frame` 写的插件在当前宿主下收不到任何结果；插件应处理 `data`，从 `data.frames[i]` 里读结果。

### 3.4 `data.frames[i]` 里有什么

`frames` 元素是 Result Hub v2 的完整 envelope（宿主原样转发，只做来源过滤和 id 去重），字段集合见 [result-hub-v2.md](./result-hub-v2.md)：

| 字段 | 说明 |
|---|---|
| `schema` / `schema_version` | 固定 `"recamera.ai.result"` / `2` |
| `type` | `frame` / `event` / `status` / `metrics` |
| `id` | 帧 id，宿主按它去重（`AppOverlayPlugin.js:179`） |
| `source` | `{kind:"app", id, app_id, instance, generation, trust}`——会话身份比对用的就是这四个字段（`:134-141`） |
| `seq` | 应用侧发布序号 |
| `time` | `{wall_ms, pts_us}` |
| `stream` | `{id, width, height, coordinate_space}` |
| `results` | 该帧的检测结果数组（`box` / `quad` / `keypoints` / `score` / `track_id` / `state` …） |
| `events` | 同一帧携带的事件 |
| `geometry` | 应用用 `GeometryBuilder` 画的图元数组 |
| `metrics` / `summary` / `render` | 指标、摘要、渲染契约 |

### 3.5 投递语义

| 规则 | 行为 | 位置 |
|---|---|---|
| 来源过滤 | 帧的 `source.kind` 必须为 `app`，且 `id`/`instance`/`generation` 与本会话全等，否则丢弃 | `AppOverlayPlugin.js:134-141, 176-178` |
| 帧去重 | 按 `frame.id` 去重，**本会话**已发过的不再发；保留最近 512 个 id 的记账窗口 | `:179-186`（`MAX_DELIVERED_FRAMES = 512`） |
| 事件去重键 | `dedupeKey = event.event_id ?? event.id ?? event.value.event_id ?? event.value.id`，宿主把它**加在事件对象上**再投递 | `:143-147, 205-208` |
| 事件队列 | 未投递事件最多排 64 条，超出丢最早的；已投递 key 记账超过 512 时整表清空 | `:187-201`（`MAX_EVENT_QUEUE = 64`） |
| render | 只在 JSON 稳定键变化时携带；不变时该字段不带有效值 | `:202-217` |
| 空投递 | 本次既无新帧、无新事件，render 也没变，则不发消息 | `:204` |

**去重是"每条会话自己发过什么"，不是"全局发过什么"。** 父级交给宿主的是一段有界记录窗口（`sourceState.records`，按 `type:id` 唯一定长保留最多 80 条，`resultProtocol.js:5, 822-829`），宿主只发这条会话还没发过的 id。所以：正常运行时每帧只投一次；**新会话挂载时会先把窗口里最近的那几条记录当作"新帧"投一遍**，之后才只投增量。事件同理——父级同样保留一个有界事件窗口，新会话会把窗口内的事件再投一次（按 `dedupeKey` 对本会话去重）。重放/切换来源后看到的是窗口内的近期记录，不是完整历史；插件必须对"同一 `frame.id` / 同一 `dedupeKey` 再来一次"免疫。

### 3.6 插件 → 宿主消息

只有两种被受理，其余一律记一次错误：

| 字段 | 要求 |
|---|---|
| `channel` | `"recamera.overlay.plugin"` |
| `protocol` | `1` |
| `type` | `"ready"` 或 `"error"` |
| `seq` | 有限数字，且**严格大于**上一次收到的 `seq`（首条可以用 1） |
| 体积 | `JSON.stringify` 后 ≤ 8192 字节 |

判定在 `AppOverlayPlugin.js:126-132`，超限/不递增/未知 type 都会累计到错误计数（测试 `:539-567`）。所以插件没法用通道做日志通道——诊断信息只能画在画布上。

### 3.7 坐标、尺寸与缩放

| 项 | 事实 |
|---|---|
| 坐标空间 | 每帧自己的 `stream.coordinate_space`。以 `pixel_` 开头（如 `pixel_xyxy`、`pixel_points`）表示原始帧像素，先除以 `stream.width` / `stream.height` 归一化，再乘容器宽高得到屏幕位置 |
| 容器尺寸 | 用宿主消息里的 `geometry.width/height`（CSS px）；`geometry` 消息在尺寸变化时推送 |
| iframe 尺寸 | iframe 铺满叠加容器（`position:absolute; inset:0`），所以文档里的 `window.innerWidth/innerHeight` 就等于容器尺寸——`geometry` 还没到或为 0 时这是可用兜底 |
| `dpr` | 宿主**不**下发，插件自己用 `window.devicePixelRatio` 设置 canvas 后端缓冲 |
| `viewport` / `containerWidth` | 宿主**不**下发。`examples/12` 与 eldercare 插件里能读 `viewport`、`stream`、`dpr`、`containerWidth` 的分支，只有旧版逐帧消息才填得上；当前宿主下这些值不会被填充 |
| `scale = clamp(containerWidth / 900, 0.8, 1.5)` | 这是 `apps/eldercare-monitor/web/overlay.html:186-189` 的**示例约定**（字号、圆角、面板宽随容器缩放），不是宿主保证。你可以照抄，也可以自己定 |

### 3.8 会话身份与生命周期预算

- 会话身份 `identity = appId | instance | generation | sha256`（`AppOverlayPlugin.js:256-258`）。身份一变，宿主重建会话、重新取数、重新挂载。
- **2 秒就绪窗口**，从 fetch 开始计时，覆盖取数、核哈希、挂载、插件 ready 全过程（`PLUGIN_SETUP_DEADLINE_MS = 2000`，`:530-534`）。
- 插件最多犯 3 次错误（`PLUGIN_ERROR_LIMIT = 3`，`:390-393`）。

## 4. 安全模型

插件是一个 **opaque origin 的沙箱文档**，它拿到的能力只有"读宿主投递的数据 + 画"：

| 能做 | 依据 |
|---|---|
| 读写自己的 DOM/canvas、内联脚本与样式 | CSP `script-src 'unsafe-inline'` / `style-src 'unsafe-inline'` |
| 用 `data:` URI 画图 | CSP `img-src data:` |
| 读 `window.__overlayPort` 收数据 | 宿主 bootstrap 注入 |

| 不能做 | 依据 |
|---|---|
| 发任何网络请求（fetch / XHR / WebSocket / EventSource / beacon） | CSP `connect-src 'none'`（`AppOverlayPlugin.js:36`） |
| 读设备 Cookie / localStorage / sessionStorage / IndexedDB | `sandbox="allow-scripts"`（没有 `allow-same-origin`）→ opaque origin（`:340`） |
| 读父文档、官方前端的状态或接口 | 同上；取数字节是宿主用 same-origin 凭据取回来再内联的，插件自己不请求（`:492-521`） |
| 加载外部 CSS/JS/字体/图片/媒体 | CSP `default-src 'none'`、`font-src 'none'`、`media-src 'none'` |
| 嵌子 iframe / Worker | CSP `frame-src 'none'`、`worker-src 'none'` |
| 改文档 base、提交表单 | CSP `base-uri 'none'`、`form-action 'none'` |
| 换掉数据通道 | `__overlayPort` 是 `writable:false, configurable:false` 的不可配置属性（`:86-90`） |
| 看别的应用的数据 | 会话身份绑定 + 帧来源全等过滤（`:134-141`）；服务端只从该应用自己的安装目录取字节（`server.py:1147-1185`） |
| 接收用户交互 | 宿主 iframe `pointerEvents: none`（`:349`）——点击、拖拽都落不到插件上 |

### 残余风险（宿主不拦或只在事后拦）

- **插件可以把自己导航走。** `allow-scripts` 允许文档改自己的 `location`，CSP 里没有 `navigate-to`，宿主也没有拦。宿主在检测到该 iframe 发生第二次 `load` 时结束会话（`:464-472`），但那是**导航之后**——插件已经收到的、属于它自己应用的数据跟着这次导航出去了。这和一个应用的 Python 侧本来就能把结果发到 `output` 通道是同一量级的能力，不是新的越权面，但它确实不是"绝对出不去"。
- **插件可以画满整个画面。** 覆盖层铺满视频区域、`z-index: 3`，宿主不做避让、不检测它盖住了什么。挡不挡关键内容是插件作者的责任。
- **宿主不检测"画得对不对"。** 只检测取数、摘要、就绪、错误条数、端口协议。插件逻辑错、画面错、性能差，只会表现为画面不对，不会触发回退。

## 5. 失败与回退

失败时用户的观感是固定的：**这个来源退回声明式画布，应用照常可用**。宿主撤销会话后，父级把这个来源的声明式图层放回来（`PreviewAIResults.js:63, 160-167, 229-240` 的 `pluginOverlayKeys` 过滤），其余应用与页面不受影响。

| 失败 | 宿主行为 | 是否上锁 |
|---|---|---|
| 取数非 2xx、网络错、被中止 | 结束会话，不自动重试 | **锁**（`:522-525`） |
| 字节摘要 ≠ manifest 摘要 | 结束会话，不挂载 | **锁**（`:513-518`） |
| 2 秒内没有 ready（含取数、核哈希、挂载时间） | 结束会话、中止在途 fetch | **锁**（`:530-534`） |
| 插件发了第 3 条错误 | 结束会话、关闭端口、移除 iframe | **锁**（`:390-393`） |
| 端口消息不合法（seq 不递增 / >8 KiB / 未知 type） | 记一次错误，累计到 3 才结束 | 累积 |
| 插件发生第二次 `load`（自我导航） | 结束会话 | 不锁（`:464-472`） |
| 用户关掉叠加开关 / 来源断连 | 结束会话 | 不锁（测试 `:442-464`） |

**锁的粒度与清除**：锁记的是一个 `identity` 字符串。它只挡完全相同的会话（同 app、同 instance、同 generation、同 sha256）；`generation` 变化（应用重启、换实例）、manifest 摘要变化、或整页刷新都会解锁并重新尝试（`:270-285`；测试 `:351-359`）。所以"停不下来的重试"和"永久黑名单"都不会出现。

## 6. 本地怎么调

### 6.1 两个可抄的示例

| 位置 | 内容 |
|---|---|
| `examples/12-overlay-plugin/` | 最小示例：应用每帧只发两个 geometry 图元（巡检区 polygon + 一个带 label 的 point），插件画两个启动 chip 和到达的数据；无模型、无 NPU。带一个离线测试 |
| `apps/eldercare-monitor/` | 完整示例（`web/overlay.html` 931 行）：每人一个胶囊（`ID 01 · 站立 · 96%`，圆点 + 圆角框 + COCO-17 骨架）、右侧转录面板（宽 30% 容器宽、上限 470 px，新到先上滚）、波形、状态条；同一个应用里 pose 线程 + ASR 线程各自发布，插件只收自己应用的数据 |

`examples/12` 的离线测试覆盖了四件事，改插件后直接跑：

```bash
uv run pytest examples/12-overlay-plugin -q
```

- 图元能过严格构建与 Hub 清洗（`sanitize_geometry`）
- manifest 与 `ui.overlay` 能过 v2 校验
- `ui.overlay.sha256` 等于 `web/overlay.html` 的实际摘要（`test_overlay_plugin.py:80-83`）
- 文档自包含：无 `<link`、`src=`、`href=`、`url(`、`@import`、`http(s)://`、`fetch(`、`XMLHttpRequest`、`WebSocket`、`localStorage`、`sessionStorage`、`document.cookie`、`parent.document`、`window.open`（`:87-100`）

### 6.2 用本地 harness 仿真宿主

不装到设备也能调：自己写一个本地 HTML 当"宿主"，照抄 `AppOverlayPlugin.js` 的四步——

1. 拼 `srcdoc = CSP meta + <script>bootstrap</script> + 你的插件文档`（bootstrap 抄 `AppOverlayPlugin.js:82-100`）；
2. 放进 `<iframe sandbox="allow-scripts">`；
3. 在宿主页监听 `window` 的 `message`，认 `type:"bootstrap"` 且 `event.ports.length === 1`，取 `event.ports[0]`，`port.start()`；
4. 端口上先发 `init`，收到插件的 `ready` 后按 §3.3 的 `data` 形状投递**你构造的合成 envelope**（合成帧也要带完整 `source`/`id`/`stream`/`results`，否则插件的来源过滤会直接丢掉它）。

这样可以在没有设备、没有应用运行的情况下迭代排版和坐标换算。harness 里放几个开关（有/无语音电平、单人/两人、追加事件、卸载、重新挂载）就能覆盖大部分分支。

### 6.3 最小起步骨架

一个能跑通握手、能画一帧的最小插件（记得把它的 sha256 写回 manifest）：

```html
<!doctype html><meta charset="utf-8">
<canvas id="c" style="position:absolute;inset:0;width:100%;height:100%"></canvas>
<script>
(function () {
  var port = window.__overlayPort, c = document.getElementById("c");
  var ctx = c.getContext("2d"), seq = 0, stream = null, geo = {width: 0, height: 0};
  var dpr = window.devicePixelRatio || 1;
  function fit() {
    var w = geo.width || window.innerWidth, h = geo.height || window.innerHeight;
    c.width = w * dpr; c.height = h * dpr;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }
  function px(p) {                       // 帧像素 -> 屏幕 CSS px
    var x = p[0], y = p[1];
    if (stream && /^pixel_/.test(stream.coordinate_space || "")) {
      x = x / stream.width; y = y / stream.height;
    }
    return [x * (geo.width || window.innerWidth), y * (geo.height || window.innerHeight)];
  }
  function draw(frames) {
    fit(); ctx.clearRect(0, 0, c.width, c.height);
    (frames || []).forEach(function (frame) {
      stream = frame.stream || stream;
      (frame.results || []).forEach(function (r) {
        if (!r.box) return;
        var a = px([r.box[0], r.box[1]]), b = px([r.box[2], r.box[3]]);
        ctx.strokeStyle = "#ffb4a2"; ctx.lineWidth = 2;
        ctx.strokeRect(a[0], a[1], b[0] - a[0], b[1] - a[1]);
      });
    });
  }
  if (!port) return;                     // 没有宿主 bootstrap：什么都别做
  port.onmessage = function (e) {
    var d = e.data;
    if (!d || d.channel !== "recamera.overlay.plugin" || d.protocol !== 1) return;
    if (d.geometry) geo = d.geometry;
    if (d.type === "geometry" || d.type === "init") fit();
    if (d.type === "data") draw(d.frames);
  };
  port.postMessage({ channel: "recamera.overlay.plugin", protocol: 1, type: "ready", seq: ++seq });
})();
</script>
```

要点：一个文件、零外链、只读 `__overlayPort`、只发一条 `ready`、从 `data.frames[i]` 读结果、坐标按 `frame.stream.coordinate_space` 换算。

## 7. 已知限制

1. **只能看自己应用的数据**：会话绑定 `appId/instance/generation`，帧按来源全等过滤；跨应用订阅不开放。要合并多路数据就把它们放进同一个应用。
2. **不能调设备 API、不能发网络请求**：opaque origin + CSP `connect-src 'none'`。想上报数据请在 Python 侧用应用的 `output` 通道（[output-sink.md](./output-sink.md)）。
3. **不能交互**：宿主 iframe `pointerEvents: none`，点击/悬停/键盘都到不了插件。需要用户操作的放官方页面，不要画在插件里。
4. **没有 `teardown` 消息**：会话被拆除时插件不会收到通知，也就没法做退场动画或清理（文档随 iframe 一起销毁）。
5. **没有完整历史，也没有"这一帧是新的还是回放的"标记**：宿主只按 `frame.id` 对自己这条会话去重。新会话挂载时会先把父级保留的近期记录（最多 80 条，`resultProtocol.js:5`）当新帧投一遍，此后只投增量；反过来，超出宿主 512 个 id 记账窗口的旧 id 若再次出现，会被当成新帧再投一次。插件要幂等就得自己按 `frame.id` 记账。
6. **未投递事件最多排 64 条**，超出丢最早的；已投递 key 记账超过 512 时整表清空，之后同 key 事件会再投一次。
7. **插件消息上限 8 KiB，且只接受 `ready`/`error`**：不能拿通道传日志或大数据。
8. **消息里没有 `viewport` / `dpr` / `containerWidth`**：当前宿主只发 `init` / `data` / `geometry`（`geometry` 里只有 `width`/`height`）。坐标基准要自己从 `frame.stream` 取，dpr 用 `window.devicePixelRatio`。
9. **`render.keypoints` 是封闭字段集**：manifest schema 只允许 `layout` / `point_radius` / `line_width` / `conf_min` / `skeleton`，**没有 `label`、也没有颜色字段**（`manifest-v2.schema.json:812-834`）。前端绘制代码里虽有读 `keypointMapping.label` 的分支（`resultOverlay.js:892`），但 manifest 校验会拒绝该字段，插件不能指望从 render 里拿到每类骨架的标签或配色——标签自己按 `track_id`/`state` 从 `results[]` 里拼。
10. **事件时间可能退化成到达时间**：应用只发相对时钟时，envelope 的 `time` 给不出 wall time；示例插件在 `extractMs` 里对相对时钟返回 `null`，随后落到帧到达时间（`apps/eldercare-monitor/web/overlay.html:216-228`）。要精确时间戳就在应用侧发 epoch 毫秒。
11. **插件画在视频之上且铺满容器**（`z-index: 3`），可能盖住画面里的人或关键区域；宿主不做避让，也不检测遮挡。
12. **一个应用一个入口**：`ui` 只允许 `overlay` 一个键、`ui.overlay` 只允许 `entry`/`sha256`，没有多文档、没有 `enabled` 开关。
13. **改了文档字节就必须改 manifest 的 `sha256`**：不改则安装被拒；已安装但字节与声明不符时接口返回 409，宿主回退到声明式画布。
14. **协议版本固定**：`channel = "recamera.overlay.plugin"`、`protocol = 1`。宿主与插件对不上这几项的消息一律忽略（插件→宿主方向还会记一次错误）。
15. **插件只在「实时预览」页生效**：宿主组件由 `src/components/preview/PreviewPage.js` 经 `PreviewAIResults` 挂载（前端仓内 `PreviewAIResults` 只被这个页面引用）。是否已随某次前端/固件构建发布到设备，**未核实**——核实方式是检查设备上前端产物里的 `AppOverlayPlugin.js` 是否发 `init`/`data`/`geometry` 三种消息。

## 8. 相关文档

- [overlay-customization.md](./overlay-customization.md)：声明式叠加与「显示样式」覆盖（插件的回退路径）
- [overlay-customization-design.md](./overlay-customization-design.md) / [overlay-customization-handoff.md](./overlay-customization-handoff.md)：设计、部署与交接
- [result-hub-v2.md](./result-hub-v2.md)：`data.frames[i]` 里那份 envelope 的完整字段与来源
- [ai-result-overlay.md](./ai-result-overlay.md)：浏览器叠加在整条结果链里的位置
- [app-package-v2.md](./app-package-v2.md)：manifest v2 打包、签名与安装
- 示例：[examples/12-overlay-plugin](../../examples/12-overlay-plugin/)（最小）、[apps/eldercare-monitor](../../apps/eldercare-monitor/)（完整）
