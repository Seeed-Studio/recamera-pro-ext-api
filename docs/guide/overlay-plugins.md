# 应用自带叠加插件（ui.overlay）

插件让你随应用包交付**一个自包含 HTML 文档**，由 App Center 前端把它挂进沙箱 iframe，在实时视频上画你自己应用的结果 UI——多行列表、圆角面板、自定义排版、骨架、动效这类声明式画布表达不了的形状。

适用范围、坐标约定、声明式回退与「显示样式」覆盖的对照见 [overlay-customization.md](./overlay-customization.md)；本文只讲插件。

事实来源：

- 服务端（manifest 契约、安装闸门、取数字节）：`market/appmgr/manifest.py`、`market/appmgr/installer.py`、`market/appmgr/server.py`、`market/appmgr/paths.py`、`market/appmgr/schema/manifest-v2.schema.json`
- 前端宿主（装载、通道、消息、失败与回退）：前端仓 `src/components/inference/results/AppOverlayPlugin.js`（提交 `130edb8`（wire 契约与失败锁）/ `d4e6668`，2026-09-29，分支 `feat/overlay-plugin-host`），父级装配 `src/components/preview/PreviewAIResults.js`
- 示例：`examples/12-overlay-plugin/`（最小）、`apps/eldercare-monitor/`（完整）

## 1. 它是什么，什么时候该用

一个应用的结果在浏览器里有两条渲染路径：

| | 声明式画布 | 叠加插件 |
|---|---|---|
| 声明位置 | manifest `render` 块（`boxes`/`quads`/`keypoints`/`geometry`/`subtitles`/`events`），可被设备上的「显示样式」覆盖修改 | manifest `ui.overlay` 指向包内一个 HTML 文档 |
| 谁画 | 前端 `resultOverlay.js` 通用绘制器 | 你的文档自己的 canvas/DOM |
| 能画什么 | 框、四边形、骨架点、字幕、geometry 图元（point/line/polyline/polygon），样式受渲染契约与用户覆盖限制 | 任意排版：列表、面板、状态条、动效、自定义字体（内联） |
| 用户改样式 | 生效（「显示样式」面板 / render-override 接口） | 不生效——插件收到的是合并后的 `render` 值，但画什么由插件自己决定 |
| 能看到的数据 | 自己应用的结果 | 自己应用的结果（宿主投影后的字段，§3.4） |

**用插件当声明式不够用时**：每人一个 `ID · 状态 · 置信度` 胶囊、右侧转录滚动面板、底部状态条、任意圆角与动效。骨架、检测框这类标准图元继续声明式画更省事，而且声明式图层就是插件失败时的兜底（见 §5）。

**插件只能看到自己应用的数据。** 宿主把每个插件会话绑死在一个来源身份上（`appId + instance + generation`），只投递来源完全匹配的帧（`AppOverlayPlugin.js:160-167, 275-290`）。跨应用订阅不开放——要在一个面板里同时显示姿态和语音，就把两个生产者放进同一个应用（`apps/eldercare-monitor/app.py:6`）。

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

宿主自己拼 `srcdoc`，顺序固定（`AppOverlayPlugin.js:130-137`）：

```
<!doctype html><html><head>
  <meta charset="utf-8">
  <meta http-equiv="Content-Security-Policy" content="<宿主拥有的 CSP>">
</head><body>
  <script>/* 宿主 bootstrap：在任何包代码之前跑 */</script>
  <你交付的整个 HTML 文档>
</body></html>
```

bootstrap 做两件事（`AppOverlayPlugin.js:108-128`）：

1. `new MessageChannel()`，把 `port1` 定义成 `window.__overlayPort`，**`writable: false, configurable: false`**——包代码改不了、删不掉、也换不了通道；
2. 把 `port2` 用 `window.parent.postMessage({channel:"recamera.overlay.plugin", protocol:1, type:"bootstrap", session:"<会话 id>"}, "*", [port2])` 交给宿主。

宿主只在同时满足"来源是本 iframe 的 contentWindow + channel/protocol/type/session 匹配 + 恰好一个 port"时接受这个端口（`AppOverlayPlugin.js:617-639`），并且一个会话只接受一次；第二次 bootstrap 记一次错误（测试 `AppOverlayPlugin.test.js:504-524`）。端口是唯一数据通路：宿主**从不**用 `contentWindow.postMessage` 投递数据。

iframe 的属性：`sandbox="allow-scripts"`、`referrerPolicy="no-referrer"`、绝对定位铺满叠加容器、`pointerEvents: none`、`z-index: 3`（`AppOverlayPlugin.js:497-511`）。

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

ready 之前宿主不会投递任何帧或事件，即使数据已经到达（`AppOverlayPlugin.js:454-460`；测试 `:556-580`）。

### 3.3 宿主 → 插件消息

每条消息都有同一个信封：`{channel, protocol, session, seq, ...}`（`AppOverlayPlugin.js:250-259`）。`session` 是宿主给这次会话的 id，与 bootstrap 里那个一致；`seq` 是宿主自增序号（从 1 起，每发一条加一）。**插件不需要校验宿主的 `seq`**，只有宿主校验插件的。

宿主发六种类型（统一由 `postToPlugin` 封装，调用点 `AppOverlayPlugin.js:596, 309, 313, 326, 337, 664`）：

| `type` | 时机 | 实际携带的字段 |
|---|---|---|
| `init` | 宿主接受端口之后、ready 之前，立即发一次 | `locale`（界面语言，如 `"zh"` / `"en"`）、`render`（会话开始时的渲染契约，可能是 `null`）、`capabilities: {maxMessageBytes: 8192}`，加 §3.7 那份坐标上下文（顶层平铺 + 同值放在 `geometry` 里） |
| `frame` | 插件 ready 之后，来源有一条新的帧记录 | v2 envelope 的绘制字段：`frame_id`、`results`、`geometry`（**图元数组**）、`metrics`、`summary`、`render`，加坐标上下文（顶层平铺） |
| `status` | 本次最新记录是 `metrics` 类，没有帧可画 | `metrics`、`summary` |
| `event` | 有未投递的事件 | `events`（数组，每项多一个 `dedupeKey`） |
| `render` | 渲染契约变了，但本次没有别的可投 | `render` |
| `geometry` | ready 之后容器尺寸变化（`ResizeObserver`） | 坐标上下文（顶层平铺 + 同值放在 `geometry` 里） |

**只有 `frame` 带 v2 envelope，而且不是原样转发**：宿主取出来源记录、按来源过滤与 id 去重后，**投影**成上表那六个绘制字段（`AppOverlayPlugin.js:231-247`）。envelope 的 `source`、`time`、`schema`、`schema_version`、`seq` 不随 `frame` 下发——`source` 由宿主自己比对（§3.5）。`frame_id` 就是 envelope 的 `id`。

**`geometry` 这个名字在两类消息里是两种东西**：`init` / `geometry` 消息里它是坐标上下文对象；`frame` 消息里它是应用画的图元数组（`AppOverlayPlugin.js:215` vs `:236`）。读之前先判类型（`Array.isArray`）。

**没有 `teardown` 消息。** 会话结束（组件卸载、来源身份变化、叠加开关关闭、失败回退）就是移除 iframe + 关闭端口 + 中止在途 fetch（`AppOverlayPlugin.js:513-549`）——插件不会收到任何"要关了"的通知，做清理只能依赖文档随 iframe 一起销毁。示例插件里那些 `case "teardown"` 分支在当前宿主下不会触发。

### 3.4 `frame` 消息里有什么

`frame` 是 v2 envelope 的投影，不是原样转发（`AppOverlayPlugin.js:231-247`）：

| 字段 | 说明 |
|---|---|
| `frame_id` | envelope 的 `id`（字符串；宿主按它去重，`AppOverlayPlugin.js:280-285`） |
| `results` | 该帧的检测结果数组（`box` / `quad` / `keypoints` / `score` / `track_id` / `state` …）；非数组一律给 `[]` |
| `geometry` | 应用用 `GeometryBuilder` 画的图元数组，非数组给 `[]`。**这一条消息里它不是坐标上下文**（§3.3） |
| `metrics` | 指标对象，缺省 `null` |
| `summary` | envelope 的 `summary`，但宿主只透传**字符串**；v2 envelope 的 `summary` 是对象（`result_hub.py:332-341`），所以实际恒为 `""`（`AppOverlayPlugin.js:235`） |
| `render` | 帧上带的渲染契约，缺省回落到父级给宿主的那份；可能为 `null` |
| `stream` / `coordinate_space` | 该帧声明的流尺寸与坐标空间；宿主归一化（`id` 缺省 `"main"`、`width`/`height` 转数字，`AppOverlayPlugin.js:217-229`） |
| `viewport` / `dpr` / `containerWidth` / `containerHeight` | 见 §3.7 |

envelope 本身的完整字段（`schema` / `source` / `time` / `seq` / `type`）见 [result-hub-v2.md](./result-hub-v2.md)：这些要么由宿主消费掉（`source` 做来源过滤），要么不转发给插件。

### 3.5 投递语义

| 规则 | 行为 | 位置 |
|---|---|---|
| 帧来源过滤 | 记录的 `source.kind` 必须为 `app`，且 `id`/`instance`/`generation` 与本会话全等，否则丢弃（比对的是 envelope 原始记录，不是投影后的消息） | `AppOverlayPlugin.js:160-167, 278` |
| 帧 id 去重 | 按 envelope 的 `id` 去重，本会话已记账的不再处理；保留最近 512 个 id | `:280-285`（`MAX_DELIVERED_FRAMES = 512`） |
| 帧槽（latest-wins） | 一次投递里即使有多条新帧记录，也只发**最后一条**；同一批里被它超过的中间帧记了账但不发 | `:305-311` |
| 帧槽只装帧 | 只有 `type == "frame"` 的记录能进帧槽；`metrics`/`status` 记录进不去，走 `status` 消息——它们没有可画内容，`stream` 尺寸为空，若进槽还会污染坐标上下文 | `:272, 286-287`（测试 `AppOverlayPlugin.test.js:329-353`） |
| 事件来源 | 事件项由父级按来源预筛；项上若仍带 `source`，要求与本会话全等 | `:291-292` |
| 事件去重键 | `dedupeKey = event.event_id ?? event.id ?? event.value.event_id ?? event.value.id`，宿主把它**加在事件对象上**再投递 | `:169-173, 322-325` |
| 事件队列 | 未投递事件最多排 64 条，超出丢最早的 | `:297-299`（`MAX_EVENT_QUEUE = 64`） |
| 事件记账 | 已投递 key 记账超过 512 时整表清空，之后同 key 的事件会再投一次 | `:301-303`（`MAX_DELIVERED_EVENT_KEYS = 512`） |
| render | 本会话首投必带；之后只在 JSON 稳定键变化时随 `frame` 携带，或单独发一条 `render` | `:332-338` |
| 空投递 | 本次既无新帧、无新事件，render 也没变，则不发消息 | `:274-340` |

**替换 vs 追加**：`frame` / `status` / `render` 是**快照替换**语义——最新一条就是当前状态，插件应整体覆盖而不是累积；`event` 是**追加**语义——事件按到达顺序排队投递，转录这类内容由插件自己累积、自己定上限。

**去重是"每条会话自己发过什么"，不是"全局发过什么"。** 父级交给宿主的是它为该来源保留的有界窗口（`frames` = `layer.sourceState.records`、`events` = `.events`、`render` = `.render`，`PreviewAIResults.js:235-237`；窗口大小 `MAX_SOURCE_RECORDS = 80`、`MAX_SOURCE_EVENTS = 60`，`src/contexts/resultProtocol.js:5-6, 832-843`），宿主只发这条会话还没发过的 id。所以：正常运行时每帧只投一次；**新会话挂载时会先把窗口里最近的记录当作"新帧"投一遍**，之后才只投增量（前端测试 `AppOverlayPlugin.test.js:556-580`：ready 之前不投，ready 时一次性放出最新保留帧 + 两条排队事件）。事件同理——父级保留一个有界事件窗口，新会话会把窗口内的事件再投一次（按 `dedupeKey` 对本会话去重）。重放/切换来源后看到的是窗口内的近期记录，不是完整历史；插件必须对"同一 `frame_id` / 同一 `dedupeKey` 再来一次"免疫。

### 3.6 插件 → 宿主消息

只有两种被受理，其余一律记一次错误：

| 字段 | 要求 |
|---|---|
| `channel` | `"recamera.overlay.plugin"` |
| `protocol` | `1` |
| `type` | `"ready"` 或 `"error"` |
| `seq` | 有限数字，且**严格大于**上一次收到的 `seq`（首条可以用 1） |
| 体积 | `JSON.stringify` 后 ≤ 8192 字节 |

判定在 `AppOverlayPlugin.js:152-158`，超限/不递增/未知 type 都会累计到错误计数（测试 `:670-695`）。所以插件没法用通道做日志通道——诊断信息只能画在画布上。

### 3.7 坐标、尺寸与缩放

| 项 | 事实 |
|---|---|
| 坐标上下文 | `init` / `frame` / `geometry` 三种消息都带同一份上下文：顶层平铺 `viewport`、`dpr`、`stream`、`coordinate_space`、`containerWidth`、`containerHeight`；`init` 与 `geometry` 还把它同值放进 `geometry`（`AppOverlayPlugin.js:188-215`） |
| `viewport` | 容器里**视频内容框**的矩形 `{x, y, width, height}`（CSS px）。两个播放器都是无边框 `object-fit: contain`，宿主用 `calculateContainViewport({containerWidth, containerHeight, videoWidth: stream.width, videoHeight: stream.height})` 算出内容框，和声明式画布走同一套映射：`screenX = viewport.x + p.x * viewport.width`。stream 尺寸未知时退化成整个容器（`AppOverlayPlugin.js:188-206`） |
| `stream` | `{id, width, height, coordinate_space}`，应用声明的流尺寸与坐标空间；`coordinate_space` 另有一份顶层平铺的副本（`:217-229`） |
| 坐标换算 | 以 `pixel_` 开头（如 `pixel_xyxy`、`pixel_points`）表示原始帧像素：先除以 `stream.width` / `stream.height` 归一化，再乘 `viewport.width` / `viewport.height` 并加 `viewport.x` / `viewport.y` |
| `containerWidth` / `containerHeight` | 宿主元素 contentRect 的 CSS px（四舍五入、不小于 0）。`init` 时容器还没测到就是 `0`，之后由 `geometry` 消息补齐（`:188-190, 654-669`） |
| `dpr` | 宿主下发：会话建立时读一次 `window.devicePixelRatio`（非正数时取 `1`），之后不跟随显示器变化（`:488-490`）。插件也可以自己再读一遍兜底 |
| iframe 尺寸 | iframe 铺满容器（`position:absolute; inset:0`），所以文档里的 `window.innerWidth/innerHeight` 就等于容器尺寸——上下文还没到或 `viewport` 为 0 时这是可用兜底 |
| `scale = clamp(containerWidth / 900, 0.8, 1.5)` | 这是 `apps/eldercare-monitor/web/overlay.html:186-189` 的**示例约定**（字号、圆角、面板宽随容器缩放），不是宿主保证。它自己的兜底是 `viewport.width`，再不行才用 `900`。你可以照抄，也可以自己定 |

### 3.8 会话身份与生命周期预算

- 会话身份 `identity = appId | instance | generation | sha256`（`AppOverlayPlugin.js:384-386`）。身份一变，宿主重建会话、重新取数、重新挂载。
- **就绪窗口 2 秒**（`PLUGIN_SETUP_DEADLINE_MS = 2000`，`:42`）：**从文档挂载起算**（`iframe.srcdoc` 赋值并入树那一刻，`:704-712`），只量"插件什么时候发 ready"。
- **取数预算 6 秒**（`PLUGIN_FETCH_DEADLINE_MS = 6000`，`:46`）：覆盖取字节 + 核 sha256，超时中止请求（`:723-733`）。两个计时器是分开的，冷启动取数慢不会被误判成插件不响应。
- 插件最多犯 3 次错误（`PLUGIN_ERROR_LIMIT = 3`，`:39`）。
- 瞬时失败最多自动重挂 1 次（`PLUGIN_REARM_LIMIT = 1`，`:49`），见 §5。

## 4. 安全模型

插件是一个 **opaque origin 的沙箱文档**，它拿到的能力只有"读宿主投递的数据 + 画"：

| 能做 | 依据 |
|---|---|
| 读写自己的 DOM/canvas、内联脚本与样式 | CSP `script-src 'unsafe-inline'` / `style-src 'unsafe-inline'` |
| 用 `data:` URI 画图 | CSP `img-src data:` |
| 读 `window.__overlayPort` 收数据 | 宿主 bootstrap 注入 |

| 不能做 | 依据 |
|---|---|
| 发任何网络请求（fetch / XHR / WebSocket / EventSource / beacon） | CSP `connect-src 'none'`（`AppOverlayPlugin.js:57-69`） |
| 读设备 Cookie / localStorage / sessionStorage / IndexedDB | `sandbox="allow-scripts"`（没有 `allow-same-origin`）→ opaque origin（`:500`） |
| 读父文档、官方前端的状态或接口 | 同上；取数字节是宿主用 same-origin 凭据取回来再内联的，插件自己不请求（`:672-720`） |
| 加载外部 CSS/JS/字体/图片/媒体 | CSP `default-src 'none'`、`font-src 'none'`、`media-src 'none'` |
| 嵌子 iframe / Worker | CSP `frame-src 'none'`、`worker-src 'none'` |
| 改文档 base、提交表单 | CSP `base-uri 'none'`、`form-action 'none'` |
| 换掉数据通道 | `__overlayPort` 是 `writable:false, configurable:false` 的不可配置属性（`:110-117`） |
| 看别的应用的数据 | 会话身份绑定 + 帧来源全等过滤（`:160-167`）；服务端只从该应用自己的安装目录取字节（`server.py:1147-1185`） |
| 接收用户交互 | 宿主 iframe `pointerEvents: none`（`:509`）——点击、拖拽都落不到插件上 |

### 残余风险（宿主不拦或只在事后拦）

- **插件可以把自己导航走。** `allow-scripts` 允许文档改自己的 `location`，CSP 里没有 `navigate-to`，宿主也没有拦。宿主在检测到该 iframe 发生第二次 `load` 时结束会话（`:641-648`，并按 §5 重挂一次），但那是**导航之后**——插件已经收到的、属于它自己应用的数据跟着这次导航出去了。这和一个应用的 Python 侧本来就能把结果发到 `output` 通道是同一量级的能力，不是新的越权面，但它确实不是"绝对出不去"。
- **插件可以画满整个画面。** 覆盖层铺满视频区域、`z-index: 3`，宿主不做避让、不检测它盖住了什么。挡不挡关键内容是插件作者的责任。
- **宿主不检测"画得对不对"。** 只检测取数、摘要、就绪、错误条数、端口协议。插件逻辑错、画面错、性能差，只会表现为画面不对，不会触发回退。

## 5. 失败与回退

失败时用户的观感是固定的：**这个来源退回声明式画布，应用照常可用**。宿主撤销会话后，父级把这个来源的声明式图层放回来（`PreviewAIResults.js:63-72, 160-168, 230-241` 的 `pluginOverlayKeys` 过滤），其余应用与页面不受影响。

失败分两类（`endSession({lock, retryable})`，`AppOverlayPlugin.js:551-563`）：**瞬时**失败（取数或就绪超时、文档自我导航）先自动重挂一次，预算用尽后才是终局；**内容**失败（取数被拒、摘要不符、插件错误刷屏）直接终局。

| 失败 | 宿主行为 | 终局条件 |
|---|---|---|
| 取数被拒（非 2xx、网络错） | 结束会话 | **立即锁**（`:713-718`；只有超时那条路径 `retryable` 为真） |
| 取数超 6 秒预算 | 中止请求，重挂一次 | 第二次超时后锁（`:723-733`；测试 `AppOverlayPlugin.test.js:374-405`） |
| 字节摘要 ≠ manifest 摘要 | 结束会话、不挂载 | **立即锁**（`:693-696`；测试 `:355-373`） |
| 挂载后 2 秒内没有 ready | 结束会话、中止在途 fetch，重挂一次 | 第二次仍无 ready 则锁（`:708-712`；测试 `:407-441`） |
| 插件发了第 3 条错误 | 结束会话、关闭端口、移除 iframe | **立即锁**（`:565-569`；测试 `:442-481`） |
| 端口消息不合法（seq 不递增 / >8 KiB / 未知 type） | 记一次错误，累计到 3 才结束 | 累积到 3 |
| 插件发生第二次 `load`（自我导航） | 结束会话，重挂一次 | 再发生则锁（`:641-648`；测试 `:636-669`） |
| 用户关掉叠加开关 / 来源断连（`enabled` 为假） | 结束会话 | 不锁（测试 `:582-601`） |

**锁的粒度与清除**：锁记的是一个 `identity` 字符串。它只挡完全相同的会话（同 app、同 instance、同 generation、同 sha256）。清除它的都是宿主里的"新会话触发"：身份变化（`generation` 变化、应用升级、换实例，`:399-404`）、叠加开关重新打开（`false → true`，`:406-416`）、整页刷新（锁是组件状态，随页面一起没，`:423-437`）；每个触发同时把重挂预算清零。对应测试：`AppOverlayPlugin.test.js:442-481`（重开开关解锁）、`:482-503`（刷新后不解锁）、`:602-635`（generation 变化重建会话）。所以"停不下来的重试"和"永久黑名单"都不会出现。

**注意 `enabled` 的合成**：父级交给宿主的是 `叠加开关 && 流已连接 && 该来源本身启用`（`PreviewAIResults.js:239`）。所以流断连和手动关开关走的是同一条路——重新连上/重新打开都会清锁并重挂。

## 6. 本地怎么调

### 6.1 两个可抄的示例

| 位置 | 内容 | 认哪些宿主消息 |
|---|---|---|
| `examples/12-overlay-plugin/` | 最小示例（`web/overlay.html` 201 行）：应用每帧只发两个 geometry 图元（巡检区 polygon + 一个带 label 的 point），插件画两个启动 chip 和到达的数据；无模型、无 NPU。带一个离线测试 | `init` / `geometry` / `frame` / `event`，各自有分支；`status` / `render` 走 `default` 分支只重画一次；另有一个 `case "teardown"` 在当前宿主下不会触发（`web/overlay.html:161-185`） |
| `apps/eldercare-monitor/` | 完整示例（`web/overlay.html` 931 行）：每人一个胶囊（`ID 01 · 站立 · 96%`，圆点 + 圆角框 + COCO-17 骨架）、右侧转录面板（宽 30% 容器宽、上限 470 px，新到先上滚）、波形、状态条；同一个应用里 pose 线程 + ASR 线程各自发布，插件只收自己应用的数据 | `init` / `frame`（兼容旧拼写 `data`）/ `event` / `geometry`，各自有分支；`status` / `render` 走 `default` 只重画；`metrics` / `teardown` 两个 case 在当前宿主下不会触发（`web/overlay.html:392-401`）。它的 `summary` 分支要求对象，而宿主只透传字符串（§3.4），所以该分支不生效——它写的 `S.summary` 全文件只写不读（`:96, 356, 378`），语音活动仍由帧内 `metrics` 驱动（`:264`） |

`examples/12` 的离线测试覆盖了四件事，改插件后直接跑：

```bash
uv run pytest examples/12-overlay-plugin -q
```

- 图元能过严格构建与 Hub 清洗（`sanitize_geometry`）
- manifest 与 `ui.overlay` 能过 v2 校验
- `ui.overlay.sha256` 等于 `web/overlay.html` 的实际摘要（`test_overlay_plugin.py:82`）
- 文档自包含：无 `<link`、`src=`、`href=`、`url(`、`@import`、`http(s)://`、`fetch(`、`XMLHttpRequest`、`WebSocket`、`localStorage`、`sessionStorage`、`document.cookie`、`parent.document`、`window.open`（`:85-95`）

### 6.2 用本地 harness 仿真宿主

不装到设备也能调：自己写一个本地 HTML 当"宿主"，照抄 `AppOverlayPlugin.js` 的四步——

1. 拼 `srcdoc = CSP meta + <script>bootstrap</script> + 你的插件文档`（bootstrap 抄 `AppOverlayPlugin.js:108-128`）；
2. 放进 `<iframe sandbox="allow-scripts">`；
3. 在宿主页监听 `window` 的 `message`，认 `type:"bootstrap"` 且 `event.ports.length === 1`，取 `event.ports[0]`，`port.start()`；
4. 端口上先发 `init`，收到插件的 `ready` 后按 §3.3 的形状投递**你构造的合成记录**（合成帧仍要带完整的 `source`/`id`/`stream`/`results`，因为来源过滤和去重发生在宿主拿到的原始记录上——你要么自己实现这一步，要么按 `frame` 消息的形状直接把结果发给插件）。

这样可以在没有设备、没有应用运行的情况下迭代排版和坐标换算。harness 里放几个开关（有/无语音电平、单人/两人、追加事件、卸载、重新挂载）就能覆盖大部分分支。

### 6.3 最小起步骨架

一个能跑通握手、能画一帧的最小插件（记得把它的 sha256 写回 manifest）：

```html
<!doctype html><meta charset="utf-8">
<canvas id="c" style="position:absolute;inset:0;width:100%;height:100%"></canvas>
<script>
(function () {
  var port = window.__overlayPort, c = document.getElementById("c");
  var ctx = c.getContext("2d"), seq = 0, stream = null, results = [];
  var vp = null, dpr = window.devicePixelRatio || 1;
  function applyContext(d) {          // init / frame / geometry 都带坐标上下文
    // frame 消息里 d.geometry 是图元数组，别拿它当上下文
    var g = d.geometry && !Array.isArray(d.geometry) ? d.geometry : d;
    if (g.viewport) vp = g.viewport;  // {x, y, width, height}：视频内容框，CSS px
    if (g.dpr > 0) dpr = g.dpr;
    if (g.stream) stream = g.stream;
  }
  function box() {                    // 上下文还没到时的兜底：整个 iframe
    return vp && vp.width > 0 ? vp
      : {x: 0, y: 0, width: window.innerWidth, height: window.innerHeight};
  }
  function fit() {
    var b = box();
    c.width = Math.max(1, Math.round(b.width * dpr));
    c.height = Math.max(1, Math.round(b.height * dpr));
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }
  function px(x, y) {                 // 帧坐标 -> 屏幕 CSS px
    var b = box();
    if (stream && stream.width > 0 && /^pixel_/.test(stream.coordinate_space || "")) {
      x = x / stream.width; y = y / stream.height;
    }
    return [b.x + x * b.width, b.y + y * b.height];
  }
  function draw() {
    var b = box();
    fit(); ctx.clearRect(0, 0, b.width, b.height);
    results.forEach(function (r) {
      if (!r.box) return;
      var p1 = px(r.box[0], r.box[1]), p2 = px(r.box[2], r.box[3]);
      ctx.strokeStyle = "#ffb4a2"; ctx.lineWidth = 2;
      ctx.strokeRect(p1[0], p1[1], p2[0] - p1[0], p2[1] - p1[1]);
    });
  }
  if (!port) return;                  // 没有宿主 bootstrap：什么都别做
  port.onmessage = function (e) {
    var d = e.data;
    if (!d || d.channel !== "recamera.overlay.plugin" || d.protocol !== 1) return;
    if (d.type === "frame") {
      results = d.results || [];      // frame 是快照语义：整批替换
      if (d.stream) stream = d.stream;
    }
    applyContext(d);                  // status / render / event 只更新上下文并重画
    draw();
  };
  port.postMessage({ channel: "recamera.overlay.plugin", protocol: 1, type: "ready", seq: ++seq });
})();
</script>
```

要点：一个文件、零外链、只读 `__overlayPort`、只发一条 `ready`、从 `frame` 消息读结果（快照替换，不是累加）、坐标按 `stream.coordinate_space` 归一化后落在 `viewport` 上、`dpr` 用宿主给的值、`geometry` 在 `frame` 消息里是图元数组不是坐标上下文。

## 7. 已知限制

1. **只能看自己应用的数据**：会话绑定 `appId/instance/generation`，帧按来源全等过滤；跨应用订阅不开放。要合并多路数据就把它们放进同一个应用。
2. **不能调设备 API、不能发网络请求**：opaque origin + CSP `connect-src 'none'`。想上报数据请在 Python 侧用应用的 `output` 通道（[output-sink.md](./output-sink.md)）。
3. **不能交互**：宿主 iframe `pointerEvents: none`，点击/悬停/键盘都到不了插件。需要用户操作的放官方页面，不要画在插件里。
4. **没有 `teardown` 消息**：会话被拆除时插件不会收到通知，也就没法做退场动画或清理（文档随 iframe 一起销毁）。
5. **没有完整历史，也没有"这一帧是新的还是回放的"标记**：宿主只按 `frame_id`（envelope 的 `id`）对自己这条会话去重。新会话挂载时会先把父级保留的近期记录（最多 80 条，`src/contexts/resultProtocol.js:5`）当新帧投一遍，此后只投增量；反过来，超出宿主 512 个 id 记账窗口的旧 id 若再次出现，会被当成新帧再投一次。插件要幂等就得自己按 `frame_id` / `dedupeKey` 记账。
6. **未投递事件最多排 64 条**，超出丢最早的；已投递 key 记账超过 512 时整表清空，之后同 key 事件会再投一次。
7. **插件消息上限 8 KiB，且只接受 `ready`/`error`**：不能拿通道传日志或大数据。
8. **同一次投递里只有最新一帧**：宿主每个来源只保留一个帧槽，一次投递里多条新帧只发最后一条，被它超过的中间帧**记了账但不发**（`:305-311`）。插件拿不到被超过的那些帧——需要逐帧的信息就在应用侧聚合进 `geometry` / `metrics`，让最新一帧自带足够内容。
9. **`dpr` 只在会话建立时采样一次**：宿主读一次 `window.devicePixelRatio` 之后不再跟随（`:488-490`），窗口被拖到另一块缩放不同的显示器上时不会更新——要跟就自己再读一遍。`viewport` / `stream` / `containerWidth` / `containerHeight` 都随 `init` / `frame` / `geometry` 下发（§3.7）。
10. **`render.keypoints` 是封闭字段集**：manifest schema 只允许 `layout` / `point_radius` / `line_width` / `conf_min` / `skeleton`，**没有 `label`、也没有颜色字段**（`manifest-v2.schema.json:821-833`；`x-` 前缀的扩展键 schema 放行，但渲染端从不读它们）。前端绘制代码里虽有读 `keypointMapping.label` 的分支（`resultOverlay.js:904-905, 917`），但 manifest 校验会拒绝该字段，插件不能指望从 render 里拿到每类骨架的标签或配色——标签自己按 `track_id`/`state` 从 `results[]` 里拼。
11. **`frame.summary` 实际恒为空串**：宿主只透传字符串摘要，而 envelope 的 `summary` 是对象（`result_hub.py:332-341`、`AppOverlayPlugin.js:235`）。需要摘要内容就从 `metrics` 或 `results` 里取。
12. **事件时间可能退化成到达时间**：应用只发相对时钟时，envelope 的 `time` 给不出 wall time；示例插件在 `extractMs` 里对相对时钟返回 `null`，随后落到帧到达时间（`apps/eldercare-monitor/web/overlay.html:216-228`）。要精确时间戳就在应用侧发 epoch 毫秒。
13. **插件画在视频之上且铺满容器**（`z-index: 3`），可能盖住画面里的人或关键区域；宿主不做避让，也不检测遮挡。
14. **一个应用一个入口**：`ui` 只允许 `overlay` 一个键、`ui.overlay` 只允许 `entry`/`sha256`，没有多文档、没有 `enabled` 开关。
15. **改了文档字节就必须改 manifest 的 `sha256`**：不改则安装被拒；已安装但字节与声明不符时接口返回 409，宿主回退到声明式画布。
16. **协议版本固定**：`channel = "recamera.overlay.plugin"`、`protocol = 1`。宿主与插件对不上这几项的消息一律忽略（插件→宿主方向还会记一次错误）。
17. **插件只在「实时预览」页生效**：宿主组件由 `src/components/preview/PreviewPage.js` 经 `PreviewAIResults` 挂载（前端仓内 `PreviewAIResults` 只被这个页面和它的测试引用）。宿主修复在 `130edb8` / `d4e6668`（2026-09-29），前一轮的真机症状（刷新后不重挂、插件收不到帧）已在设备上复现并验证修复。设备上**当前**跑的 bundle 是否就是含这两个提交的构建，**未核实**——核实方式是看设备 `/oem/usr/www/index.html` 引用的 `static/js/main.*.js` 与 `/oem/usr/www/static/js/` 下实际存在的文件；2026-09-29 重做的构建产物是 `main.63077be5.js`，此前记录到的设备 bundle 是 `main.14aff299.js`。

## 8. 相关文档

- [overlay-customization.md](./overlay-customization.md)：声明式叠加与「显示样式」覆盖（插件的回退路径）
- [overlay-customization-design.md](./overlay-customization-design.md) / [overlay-customization-handoff.md](./overlay-customization-handoff.md)：设计、部署与交接
- [result-hub-v2.md](./result-hub-v2.md)：`frame` 消息背后那份 envelope 的完整字段与来源
- [ai-result-overlay.md](./ai-result-overlay.md)：浏览器叠加在整条结果链里的位置
- [app-package-v2.md](./app-package-v2.md)：manifest v2 打包、签名与安装
- 示例：[examples/12-overlay-plugin](../../examples/12-overlay-plugin/)（最小）、[apps/eldercare-monitor](../../apps/eldercare-monitor/)（完整）
