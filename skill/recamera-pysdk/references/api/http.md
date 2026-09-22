# HTTP、SSE 与 WebSocket 契约

[API 索引](index.md) · [全部 HTTP 路由逐项说明](http-routes.md)

这里记录 ext 仓库提供的 AppMgr 控制面、结果订阅和 Kit 控制适配器。
不把设备固件中网络、存储、ISP 等其他项目的全部 HTTP API 视为本 SDK API。
源码基线见索引；服务地址、鉴权及硬件能力仍需与目标固件匹配。

## 连接、响应和错误

- 浏览器使用设备同源 `/api/app-center/v1/...` 和 `/ws/ai/results/v2`；
  由固件 nginx 负责认证和代理。WebSocket 的跨源请求被拒绝，写 HTTP 请求
  还有来源校验。不要绕开认证直接暴露本机端口，也不要伪造受信代理头。
- 本机 SSH 部署按 [runtime-validation.md](../runtime-validation.md) 使用官方入口。
  HTTPS/WSS 使用已验证的证书或该 helper 的 SSH 信任绑定，不关闭证书验证。
- AppMgr HTTP 成功响应直接是 JSON 对象，**不是**统一的 `{code:0,data:...}`。
  CGI 控制接口则有自己的业务码；HTTP 200 不能替代业务成功检查。
- `201` 表示创建，`202` 表示已接受异步任务，均不能据此认定应用已运行。
  查询准确的 task/operation id，观察终态和错误，再核对当前应用实例。
- v1 常见失败：`400` 参数/包/资源声明无效，`404` 不存在，`409` 冲突/忙/
  确认不一致，`502` 下载失败，`503` 服务或事件容量不可用，`507` 暂存空间不足。
  返回通常为 `{error:string,code?:string}`，例如
  `sensecraft_login_required`。预览、上传等路由还可返回自己的限制错误。
  不对所有 4xx 自动重试；先修正请求或刷新状态。
- 普通 JSON 请求使用 `application/json`；上传按路由区分 multipart 和原始字节。
  大文件必须有准确 `Content-Length`，不要把原始字节上传误写成 JSON/base64。

## 本地包安装与运行状态

1. `GET /api/app-center/v1/policy`：读取 manifest 版本、上传大小/字段和信任策略。
2. `POST /api/app-center/v1/uploads`：multipart `package`，可选 `signature`。
   返回 `{upload_id,preflight}`。预检包含 `manifest`、`manifest_version`、
   `release_id`、`compatibility`、`permissions`、`resources`、`dependencies`、
   `python`、`instances`、`health`、`signature`、`checks`、`warnings`、
   `start_admission` 和 `install_context`。
3. 经用户授权，把**本次预检的完整 permissions 对象**原样带回：

   ```json
   {
     "upload_id": "<本次上传返回的 id>",
     "permissions_confirmed": true,
     "permissions": {}
   }
   ```

   这里 `{}` 仅适用于预检权限确实为空的包；不是绕过权限确认的固定值。
   `install_context.confirmation_required` 若包含
   `running_upgrade_confirmed` 或 `force_reinstall_confirmed`，只有已取得
   对应升级/重装授权后才发送该字段 `true`。来源/channel 由服务端分配。
4. `POST /api/app-center/v1/apps` 返回 `{operation,idempotent_replay?}`。
   对同一个 upload id 和完全相同的确认内容可重复取得原 operation；改变确认
   内容会冲突。若请求超时，先查 operation，不重新上传并盲目重复安装。
5. `GET /api/app-center/v1/operations` 返回 `{operations:[...]}`。
   按 `operation.id` 匹配；没有 `/operations/{id}` 单项查询路由。
   记录包含 `id,type,app_id,status,progress:{percent},message,error,created_at,updated_at`，
   安装另有 `upload_id` 等关联字段。状态由 `queued` 进入 `running`，终态为
   `succeeded` 或 `failed`；时间为 Unix 秒。百分比 100 也可能是失败。
6. 安装与启动分开：预检的 `start_admission` 把实际资源/依赖准入延后到启动，
   `install_context.requires_manual_start` 说明是否需手动启动。
   启动/停止/重启 POST 也返回异步 operation，卸载 DELETE 同样如此。
   安装成功不证明有可用 NPU、模型输出正确或新实例已产出结果。

同源本地 Web 上传的未签名许可不扩展到商店或直接上传渠道；旧版
`unsigned_confirmation` 是兼容响应，不应被当成新客户端额外必填门槛。

## 配置、显示、输出与录像

- `GET/PUT /apps/{id}/config`：PUT `{values:{...}}`，保存配置前遵循返回
  schema；是否热更新以实现/响应为准。旧 `/api/appMgr/config` 使用
  `{id,config:{...}}`，两者不要混用。
- `/apps/{id}/visualization` 的 PUT **仅接受** `{stream_burn_in:boolean}`。
  它改变编码流叠加，影响实时预览、RTSP、录像及截图，不只是网页 canvas。
  应用须在 manifest 声明可用渲染契约。内置应用开启叠加走内置配置。
- 全局 `/visualization` PUT `{osd:{enabled:boolean,sources:[app_id,...]}}`
  影响来源集合；应用级开关经服务端原子更新集合，避免客户端覆盖其他来源。
- `/apps/{id}/output/preview`：`{values:{...},task:"detection"}` 返回
  `{sample:true,messages:[{body,topic}]}`，不保存、不发网络消息。
  `/output/test`：`{values:{...},channel:"http"|"mqtt"}` **会真实发送**；
  返回 `{ok,channel,status?,topic?,reason?}`。HTTP 2xx 与 MQTT 本地写出
  仍不能证明外部业务已处理。普通应用需要 output capability，builtin 可用。
- `/recording/sources` 只枚举来源；`/recording/migration/acknowledge`
  仅接受 `{acknowledge:true}`，确认配置迁移。实际录制请求使用
  `App.request_recording()` 并遵守 [recording.md](../recording.md)，
  普通应用不能使用 AppMgr 专用 `RecordSink`。
- 日志、metrics、resources、results/status 都是观测接口。
  历史指标不能作为停止应用仍在运行的证据；结合当前 lifecycle/instance/generation。

## 商店与公钥管理

商店目录由设备从固定 CDN 拉取。创建任务 POST `/store/tasks` 的 body 为
`{app_id,version,package_sha256}`，返回 `{task,idempotent_replay}`；不能指定任意下载 URL。
最多四个活跃任务，下载由单 worker 处理；下载/预检与异步安装队列相互区分。
关闭页面不会中断下载。`GET /store/tasks` 恢复任务列表，单项 GET 跟踪进度，
单项 DELETE 在安装提交前取消。`POST /store/tasks/{id}/install` 使用与本地安装
相同的权限及必要升级确认字段，但不需要 upload_id。后续用返回 operation 确认安装。

公钥管理属于设备管理，文档覆盖不代表应用应自动调用：

```json
{
  "label": "owner-key-label",
  "public_key": "<合法的公钥文本>",
  "confirm_trust": true
}
```

POST `/trust/owners` 验证上面的显式确认，返回新建或已有记录；GET `/trust`
查询公开元信息。DELETE `/trust/owners/{fingerprint}` 使用不带 `sha256:`
前缀的 64 位十六进制指纹；vendor 公钥不可由该 owner 接口删除。不上传私钥。

## Workflow 专用 API

只供已安装且声明 `x-workflow-ui` v1 的应用使用。AppMgr 代理已验证的应用服务，
校验当前实例和 generation；不接受任意 URL，不泄露应用长期凭据。
`/workflows` 列出工作流，`/workflows/runtime` 可用
`include_result=true`、`include_overlay=true` 请求附加数据；
`/workflows/preview?pipeline_id=...&output=...` 返回图像或 304（If-None-Match），
不是 JSON 推理张量。`/workflows/editor-session` POST 精确空对象 `{}` 建立临时会话。

模型准备流程由设备管理任务执行，不是每个 App 都可调用的通用 ONNX 转换服务。
普通应用的离线转换仍读 [model-conversion.md](../model-conversion.md)。

| 创建任务字段 | 特性 |
| --- | --- |
| `mode` | `onnx`、`rknn`、`cloud`、`roboflow` 或 `auto` |
| `metadata` | onnx/rknn/cloud 必需，下面的固定推理契约 |
| `model_id` | roboflow/auto 的目标标识；auto 可从 filename 推导 |
| `filename` | auto 的 ONNX 文件名，用于推导标识，不能替代 source 上传 |
| `cloud_id` | cloud 模式必需；恢复不确定云提交时也用于匹配已有任务 |
| `dataset` | 可选 boolean，仅 onnx 模式允许 true |

`metadata` 的必填根字段：`schema_version:1,platform:"rv1126b",format:"rknn",
task:"object-detection",model_id,labels,input,outputs,postprocess`。
model_id 为 `/` 分隔的安全标识段，不允许 `.` 或 `..` 段；labels 是按真实
训练次序排列的 1–1000 个非空字符串。

- `input`：`name`、固定正整数 `shape:[1,S,S,3]`（S ≤ 1280）、
  `dtype:"uint8",layout:"NHWC",color_format:"RGB",normalization:"baked"`；
  可选 `padding_value` 为 0–255 整数，默认 114。
- `outputs`：1–12 项，每项 `name` 唯一、`shape` 为固定正整数、
  `dtype:"float32"`，总大小不超过 64 MiB。`layout`/`role` 与 decoder 匹配。
- `postprocess.scores`：`logits` 或 `probabilities`。
  `kind:"yolo-decoded"` 使用一个 BCN/BNC `[1,C,N]`/`[1,N,C]` 输出，N > C，
  `box_format:"xywh"`；C = 4 + 类数 + objectness（默认 false）。
  `kind:"yolo-end2end"` 使用一个 BNC `[1,N,6]`，`box_format:"xyxy"`、
  probabilities，`nms:false`。可选 role 仅 `detections`。
  `kind:"yolo-distance"` 按尺度提供 NCHW boxes（4 通道）和 scores（类数通道）；
  `kind:"yolo-dfl"` 使用 `reg_max:16`，boxes 为 64 通道，scores 为类数通道，
  可选 score_sum 为 1 通道，类数不能为 64。两者输出 H=W 且整除输入 S。
  `nms` 默认为 true（end2end 为 false），`topk` 默认 300；仅 distance/dfl
  且 `nms:false` 时可显式设 1–1000。
- 服务端固定 `model_file:"model.rknn"` 和 `memory_mb:64`，从真实工件计算
  SHA-256，不信任客户端提供的 hash。注册完成后仍需首轮推理验证目标与 tensor。

创建 `/workflow-models/tasks` 后，向 `/{task_id}/source` 或 `/dataset` 上传
`application/octet-stream` 原始字节并提供 Content-Length（不支持 chunked）。
`/{task_id}/resume` 和 `/cancel` 用 JSON。活动 worker 期间操作会冲突；
registered 不能取消，`submission_uncertain` 需确认已有 cloud_id，避免重复云提交。
每应用最多四个额外模型（含在途任务），任务历史有界。GET `/workflow-models`
查询模型/任务；`/cloud-records` 依赖设备 SenseCraft 登录，最多 20 条。
`POST /workflow-models/remove` `{model_id}` 受忙状态与所有权检查约束。

## SSE 控制面事件

GET `/api/app-center/v1/events` 返回 `text/event-stream`，先发送 connected 注释；
数据使用 `id:` 和 `data:` 行，空闲每 15 秒发 heartbeat 注释。它只推送
控制面事件，不输送视频或 NPU tensor。当前实现**不读取 Last-Event-ID**；
重连时重新订阅并查询 apps/operations 状态，以查询结果修复可能丢失的事件。
事件流不能替代异步任务终态查询。

## WebSocket 结果订阅

| 接口 | 用途 |
| --- | --- |
| `/ws/ai/results/v2` | 统一 canonical v2，App + builtin；本机代理端口 8125 |
| `/appcenter/ws/results` | 兼容旧 App 结果，8124 |
| `/ws/inference/results` | 兼容内置结果，8123 |

连接按设备协议选择 `wss://` 或 `ws://` 并完成设备认证。收到 hello 后按需发送：

```json
{"type":"subscribe","view":"raw","sources":["yolo-detector"],"types":["frame","event","status","metrics"]}
```

sources 使用 app id（**不加 `app:` 前缀**）或 `"*"`；1–64 个非空字符串，
每个最长 128 字符。types 必须为上面四种的非空子集。view 为 raw 或 formatted。
客户端 frame 必须 masked，控制 payload 上限 16 KiB，不支持分片控制消息。
订阅后先得到 snapshot 控制消息，再得到最新匹配状态/帧和有限期事件，随后接收实时数据。
重订阅可重放事件，消费者按 event_id 去重，不应把 snapshot 当成新的业务触发。

canonical 外层固定字段：

```json
{
  "schema":"recamera.ai.result","schema_version":2,"type":"frame",
  "id":"yolo-detector:3:42:frame",
  "source":{"kind":"app","id":"yolo-detector","app_id":"yolo-detector",
            "instance":"<当前实例>","generation":3,"trust":"peercred"},
  "seq":42,"time":{"wall_ms":1780000000000,"pts_us":12345678},
  "stream":{"id":"main","width":1280,"height":720,"coordinate_space":"pixel_xyxy"},
  "results":[],"events":[],"geometry":[],"metrics":{},"summary":{},"render":{},"extensions":{}
}
```

- `time.wall_ms` 为毫秒墙钟，`pts_us` 为微秒帧时钟；不要相减计算推理延迟。
  App 墙钟由 gateway 接收时刻生成。source 和 stream 身份由服务端授权，
  payload 自报的同名身份不能替换它。
- 坐标来自已安装 manifest 的 `output.fields[].coord` 与实际流契约。
  查看每个结果的 `spaces`/兼容 `space`；未声明、冲突、缺尺寸时可为 unknown，
  不默认按像素或归一化画框。geometry 契约见 [features.md](features.md)。
- raw 保留模板处理前的规范结果；formatted 仍用规范外层，
  `type:"formatted"`，原始类型在 `extensions.raw_type`，格式化内容在扩展字段。
  消费者只需机器可读数据时优先 raw。
- 控制消息除 hello/snapshot 外还有 `source_invalidated`。收到后清除
  **该 instance/generation** 的展示状态，拒绝延迟旧结果，不能误清同 app 的新实例。
- 缓存、事件重放、发送队列均有界；它是实时订阅，不是永久消息数据库。
  慢消费者可能丢弃被新状态替代的数据或断线，重连后同步 snapshot，按 hello 中
  event_replay 的 max/ttl_s 理解恢复范围。NPU 原始 tensor 使用 ProbeSource，
  不从此 WS 推断存在原始 tensor 下载功能。

## CGI 适配器与未实现能力

完整 Python 参数见 [kit.adapters.cgi_control](python/kit-adapters-cgi_control.md)。
`CgiControl.set_inference(enable=...,model=...,fps=...)` 对应设备
`/cgi-bin/entry.cgi/model/inference?id=<id>` 的 POST，body 为 `iEnable:0|1`
以及可选 `sModel`、非负整数 `iFPS`；`get_inference()` 为 GET。
它影响内置推理配置，不能代替 NPU broker 授权。`snapshot()` 通过 FrameSource
取图再用 OpenCV 编码 JPEG，**不是**调用假想的 CGI 截图 API。

`/cgi-bin/entry.cgi/api/v1/ext/capabilities` 是静态能力声明，不证明服务健康；
当前审计固件的 subscriptions 接口尚未实现。`OfficialControl`、
`OfficialPcmSource` 是迁移占位，调用相应方法会抛出 NotImplementedError；
不能把其 docstring 中的目标设计当成可用功能。
