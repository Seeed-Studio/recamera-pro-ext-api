# App Center HTTP routes

[API 索引](index.md) · [鉴权、响应和 WS 契约](http.md)

覆盖 server.py 的所有 HTTP 路由分支；带可选后缀/多个 action 的分支在说明中逐项展开。
`app_id` 为 `[a-z0-9-]{1,64}`，task/upload id 为 32 位小写十六进制；builtin 仅在明确允许的接口适用。

## DELETE /api/app-center/v1/apps/{app_id}

- 请求：无业务 body。
- 返回：202，卸载 operation。
- 特性：删除目标应用，系统应用可能拒绝；客户端应在用户授权后调用。

源码路由：`DELETE /api/app-center/v1/apps/([a-z0-9-]{1,64})`

## DELETE /api/app-center/v1/store/tasks/{task_id}

- 请求：无业务 body。
- 返回：{task}。
- 特性：只取消尚未提交安装的任务；关闭页面不会隐式取消。

源码路由：`DELETE /api/app-center/v1/store/tasks/([0-9a-f]{32})`

## DELETE /api/app-center/v1/trust/owners/{fingerprint}

- 请求：64 位十六进制公钥指纹。
- 返回：移除结果。
- 特性：只移除可管理 owner key，vendor key 不能通过此接口删除。

源码路由：`DELETE /api/app-center/v1/trust/owners/([0-9a-fA-F]{64})`

## DELETE /api/app-center/v1/uploads/{upload_id}

- 请求：无业务 body。
- 返回：上传取消/清理结果。
- 特性：只能取消可取消阶段；不把取消预检当成已回滚安装。

源码路由：`DELETE /api/app-center/v1/uploads/([0-9a-f]{32})`

## GET /api/app-center/v1/apps

- 请求：无 body。
- 返回：应用列表，含 manifest/UI 信息、运行/资源状态及可执行动作。
- 特性：配置与运行状态可能变化，按实际 actions/state 决定操作可用性。

源码路由：`GET /api/app-center/v1/apps`

## GET /api/app-center/v1/apps/{app_id}/{config|logs}

- 请求：config 无 body；logs 可传 tail（默认 200）。
- 返回：config 返回 schema/values 等配置；logs 返回有界日志尾部。
- 特性：普通应用需已安装；builtin 的配置有专用适配。日志不应公开泄露凭据。

源码路由：`GET /api/app-center/v1/apps/([a-z0-9-]{1,64})/(config|logs)`

## GET /api/app-center/v1/apps/{app_id}/visualization

- 请求：无 body。
- 返回：该 App 的叠加能力、stream_burn_in 与影响范围。
- 特性：受 manifest render/输出契约限制。

源码路由：`GET /api/app-center/v1/apps/([a-z0-9-]{1,64})/visualization`

## GET /api/app-center/v1/apps/{app_id}/workflow-models[/cloud-records]

- 请求：无 body。
- 返回：模型/任务快照；cloud-records 返回当前云登录可见的模型记录（最多 20 条）。
- 特性：需已安装 workflow 应用；云记录依赖平台登录，不是匿名公共模型列表。

源码路由：`GET /api/app-center/v1/apps/([a-z0-9-]{1,64})/workflow-models(?:/(cloud-records))?`

## GET /api/app-center/v1/apps/{app_id}/workflows[/runtime]

- 请求：runtime 可传 include_result=true、include_overlay=true。
- 返回：无后缀返回工作流 id/name；runtime 返回部署/video/latest/service_only 等运行快照。
- 特性：需 x-workflow-ui v1；查询当前实例身份，不能混用重启前后的结果。

源码路由：`GET /api/app-center/v1/apps/([a-z0-9-]{1,64})/workflows(?:/(runtime))?`

## GET /api/app-center/v1/apps/{app_id}/workflows/preview

- 请求：query pipeline_id、output；可传 If-None-Match。
- 返回：图像/预览字节和内容类型，可能返回 304 或 PreviewUnavailable 错误。
- 特性：只代理 manifest/当前应用授权的预览源，不接受任意 URL。

源码路由：`GET /api/app-center/v1/apps/([a-z0-9-]{1,64})/workflows/preview`

## GET /api/app-center/v1/events

- 请求：SSE 长连接；当前实现不读取 Last-Event-ID，不提供断点续传。
- 返回：text/event-stream，id 和 data 行；空闲每 15 秒发送 heartbeat 注释。
- 特性：用于控制面状态更新；断线后重新查询 apps/operations 快照，再订阅实时事件。不是模型张量数据接口。

源码路由：`GET /api/app-center/v1/events`

## GET /api/app-center/v1/operations

- 请求：无 body。
- 返回：{operations:[...]}，每项含 id/type/app_id/status/progress/error 等。
- 特性：用准确 operation id 匹配任务；当前 server 没有 GET /operations/{id} 路由。

源码路由：`GET /api/app-center/v1/operations`

## GET /api/app-center/v1/policy

- 请求：无 body。
- 返回：manifest.required_version、upload 字段/容量限制、trust 渠道和公钥管理策略。
- 特性：在上传前读取实际固件策略；Web 未签名例外不自动适用于商店。

源码路由：`GET /api/app-center/v1/policy`

## GET /api/app-center/v1/recording/sources

- 请求：无 body。
- 返回：当前可用于应用触发录像的来源和能力。
- 特性：枚举不启动录像；触发仍遵循 manifest 授权和系统录像配置。

源码路由：`GET /api/app-center/v1/recording/sources`

## GET /api/app-center/v1/resources

- 请求：无 body。
- 返回：资源、分配、容量和冲突相关状态。
- 特性：状态快照仅作观测，真正申请由 AppMgr 生命周期完成。

源码路由：`GET /api/app-center/v1/resources`

## GET /api/app-center/v1/results/status

- 请求：无 body。
- 返回：Result Hub 状态/计数；服务未运行为 503。
- 特性：实际结果通过 WS 获取；状态正常不证明指定实例已发出新结果。

源码路由：`GET /api/app-center/v1/results/status`

## GET /api/app-center/v1/store/catalog

- 请求：无 body。
- 返回：规范化 catalog，含 schema、apps、icon_url 和不可安装原因。
- 特性：设备从固定 CDN 读取，客户端不能让此接口代理任意 URL。

源码路由：`GET /api/app-center/v1/store/catalog`

## GET /api/app-center/v1/store/tasks

- 请求：无 body。
- 返回：{tasks:[...]}。
- 特性：用于页面重开后恢复下载/预检/安装进度。

源码路由：`GET /api/app-center/v1/store/tasks`

## GET /api/app-center/v1/store/tasks/{task_id}

- 请求：无 body。
- 返回：{task}，含状态、progress、error、inspection/operation。
- 特性：下载百分比不是安装百分比；进入安装后跟踪 operation。

源码路由：`GET /api/app-center/v1/store/tasks/([0-9a-f]{32})`

## GET /api/app-center/v1/trust

- 请求：无 body。
- 返回：vendor/owner 公钥公开元信息与策略。
- 特性：仅查询，不返回私钥。

源码路由：`GET /api/app-center/v1/trust`

## GET /api/app-center/v1/visualization

- 请求：无 body。
- 返回：全局 osd 策略和桥接状态。
- 特性：编码流叠加影响 preview/RTSP/录像/快照；不是单个浏览器 canvas 开关。

源码路由：`GET /api/app-center/v1/visualization`

## GET /api/appMgr/assets

- 请求：query paths（逗号分隔候选工件路径）。
- 返回：工件存在/大小/摘要等检查结果。
- 特性：只作受限工件探测，不是任意文件下载。

源码路由：`GET /api/appMgr/assets`

## GET /api/appMgr/config

- 请求：query id。
- 返回：应用配置。
- 特性：兼容旧 UI；新接口为 /apps/{id}/config。

源码路由：`GET /api/appMgr/config`

## GET /api/appMgr/icon

- 请求：query id，h 可选为 16 位内容摘要。
- 返回：图标二进制及 MIME；摘要不匹配返回 412。
- 特性：内容地址缓存，不能把同版本重装后的新图标缓存在旧 h 下。

源码路由：`GET /api/appMgr/icon`

## GET /api/appMgr/list

- 请求：无 body。
- 返回：legacy 应用列表。
- 特性：兼容旧控制面；新集成使用 v1 /apps。

源码路由：`GET /api/appMgr/list`

## GET /api/appMgr/metrics

- 请求：无 body。
- 返回：应用/系统指标快照。
- 特性：观测接口，采样时间和字段由固件版本决定。

源码路由：`GET /api/appMgr/metrics`

## GET /api/appMgr/mqtt

- 请求：无 body。
- 返回：全局 MQTT 配置。
- 特性：全局配置，不应与某 App 独立输出配置混同。

源码路由：`GET /api/appMgr/mqtt`

## GET /api/appMgr/resources

- 请求：无 body。
- 返回：资源分配快照。
- 特性：legacy 入口，新集成使用 v1 /resources。

源码路由：`GET /api/appMgr/resources`

## GET /api/appMgr/resultGateway

- 请求：无 body。
- 返回：结果网关状态，未运行返回 503。
- 特性：诊断信息，不是应用上传结果的 HTTP 数据面。

源码路由：`GET /api/appMgr/resultGateway`

## GET /api/appMgr/resultHub

- 请求：无 body。
- 返回：Result Hub 状态，未运行返回 503。
- 特性：legacy 入口，业务订阅使用规范 WS。

源码路由：`GET /api/appMgr/resultHub`

## GET /api/appMgr/runtime

- 请求：query name，默认 voice。
- 返回：运行时安装/可用状态。
- 特性：兼容运行时管理，应用开发不应自动替换共享 runtime。

源码路由：`GET /api/appMgr/runtime`

## GET /health

- 请求：无 body。
- 返回：200 或 503，{service: 'appmgr', ready: bool}。
- 特性：服务本机健康检查；ready 不证明某个应用或 NPU 已就绪。外部是否代理此路径取决于 nginx。

源码路由：`GET /health`

## POST /api/app-center/v1/apps

- 请求：JSON upload_id、permissions_confirmed:true、与预检精确一致的 permissions；按 install_context 需要提供 running_upgrade_confirmed/force_reinstall_confirmed。
- 返回：202，{operation, idempotent_replay?}。
- 特性：异步安装；同 upload_id 相同确认可幂等重放，不同确认被拒绝。等待 operation 终态。

源码路由：`POST /api/app-center/v1/apps`

## POST /api/app-center/v1/apps/{app_id}/{start|stop|restart}

- 请求：空 body 或有效 JSON body。
- 返回：202，生命周期 operation。
- 特性：三种动作分别启动、停止、重启目标 App；异步完成，PID 存在不等于 ready/running。

源码路由：`POST /api/app-center/v1/apps/([a-z0-9-]{1,64})/(start|stop|restart)`

## POST /api/app-center/v1/apps/{app_id}/output/{preview|test}

- 请求：preview: {values:{...},task:'detection'}；test: {values:{...},channel:'http'或'mqtt'}；上限 256 KiB。
- 返回：preview: {sample:true,messages:[{body,topic}]}；test: {ok,channel,status?,topic?,reason?}。
- 特性：preview 不保存/发送；test 会真实发送测试消息，HTTP 仅 2xx 视为响应成功、MQTT 仅表示本地写出。builtin 可用，普通 App 需 output capability。

源码路由：`POST /api/app-center/v1/apps/([a-z0-9-]{1,64})/output/(preview|test)`

## POST /api/app-center/v1/apps/{app_id}/workflow-models/remove

- 请求：JSON {model_id:...}。
- 返回：模型移除结果。
- 特性：受 busy gate 与模型引用/所有权检查约束，不直接删除任意路径。

源码路由：`POST /api/app-center/v1/apps/([a-z0-9-]{1,64})/workflow-models/remove`

## POST /api/app-center/v1/apps/{app_id}/workflow-models/tasks[/{task_id}/{resume|cancel|source|dataset}]

- 请求：创建 JSON 含 mode/model_id/filename/metadata/cloud_id/dataset，按 mode 校验；resume/cancel 用 JSON；source/dataset 用 application/octet-stream + Content-Length。
- 返回：200，{task}。
- 特性：创建/恢复/取消和两个二进制上传槽位分别处理；不接受 chunked 上传。模型转换依赖平台 worker，不是应用端执行任意 shell。详见 http.md。

源码路由：`POST /api/app-center/v1/apps/([a-z0-9-]{1,64})/workflow-models/tasks(?:/([0-9a-f]{32})/(resume|cancel|source|dataset))?`

## POST /api/app-center/v1/apps/{app_id}/workflows/editor-session

- 请求：JSON {}，非空对象拒绝。
- 返回：工作流画布临时会话授权。
- 特性：不向浏览器暴露应用长期口令；画布联网条件与本地推理执行分开。

源码路由：`POST /api/app-center/v1/apps/([a-z0-9-]{1,64})/workflows/editor-session`

## POST /api/app-center/v1/recording/migration/acknowledge

- 请求：精确 JSON {acknowledge:true}。
- 返回：{migration:...}。
- 特性：记录用户已确认录像规则迁移，不作为应用触发接口。

源码路由：`POST /api/app-center/v1/recording/migration/acknowledge`

## POST /api/app-center/v1/store/tasks

- 请求：JSON {app_id,version,package_sha256}。
- 返回：202，{task,idempotent_replay}。
- 特性：后端按固定目录解析下载地址；最多四个活跃任务，单下载 worker，与安装队列分开。

源码路由：`POST /api/app-center/v1/store/tasks`

## POST /api/app-center/v1/store/tasks/{task_id}/install

- 请求：与本地安装同样的 permissions_confirmed/permissions 及必要升级确认，不传 upload_id。
- 返回：202，{task,operation}。
- 特性：商店渠道要求受信包，不继承本地 Web 上传的未签名例外；安装按 mutation 队列执行。

源码路由：`POST /api/app-center/v1/store/tasks/([0-9a-f]{32})/install`

## POST /api/app-center/v1/trust/owners

- 请求：JSON 公钥及显式确认字段，见 http.md 的 owner-key 请求。
- 返回：201（新增）或 200（已有），公钥记录。
- 特性：设备管理操作，不在 skill 的应用开发流程中自动执行。

源码路由：`POST /api/app-center/v1/trust/owners`

## POST /api/app-center/v1/uploads

- 请求：multipart/form-data：package 为 .tar.gz；可选 signature 服从 policy；必须有有效 Content-Length，限制以 policy 为准。
- 返回：201，upload id 与 preflight（manifest、permissions、release_id、install_context 等）。
- 特性：上传/预检不等于安装。来源与渠道由服务端确定，不接受客户端伪造。

源码路由：`POST /api/app-center/v1/uploads`

## POST /api/appMgr/activate

- 请求：JSON {id}，可为应用 id、builtin 或 none。
- 返回：激活/停用控制结果。
- 特性：旧全局激活模式，会涉及内置/外部应用切换；新开发优先独立 v1 生命周期。

源码路由：`POST /api/appMgr/activate`

## POST /api/appMgr/config

- 请求：JSON {id,config:{...}}。
- 返回：配置应用结果。
- 特性：legacy body 与 v1 的 {values:{...}} 不同。

源码路由：`POST /api/appMgr/config`

## POST /api/appMgr/install

- 请求：JSON {path,signature?}。
- 返回：legacy 安装结果。
- 特性：旧安装入口；不借它绕过 v1 来源/权限/确认策略。

源码路由：`POST /api/appMgr/install`

## POST /api/appMgr/mqtt

- 请求：JSON {mqtt:{...}} 或 MQTT 配置对象。
- 返回：保存结果。
- 特性：修改全局 MQTT；仅在用户要求设备配置时调用。

源码路由：`POST /api/appMgr/mqtt`

## POST /api/appMgr/putModel

- 请求：原始模型字节；X-Filename、X-Target-Path、可选 X-Sha256、Content-Length。
- 返回：模型暂存/写入结果。
- 特性：受允许目标路径/模型大小限制；不是向任意文件写入接口。

源码路由：`POST /api/appMgr/putModel`

## POST /api/appMgr/restart

- 请求：JSON {id}。
- 返回：legacy 重启结果。
- 特性：重新生成运行实例，旧结果不能用作新实例验收。

源码路由：`POST /api/appMgr/restart`

## POST /api/appMgr/runtime

- 请求：JSON name（默认 voice）、path、signature 可选。
- 返回：运行时安装结果。
- 特性：平台管理入口；不属于普通应用包部署操作。

源码路由：`POST /api/appMgr/runtime`

## POST /api/appMgr/start

- 请求：JSON {id}。
- 返回：legacy 启动结果。
- 特性：与 v1 生命周期保持相同资源约束。

源码路由：`POST /api/appMgr/start`

## POST /api/appMgr/stop

- 请求：JSON，可选 id。
- 返回：legacy 停止结果。
- 特性：省略 id 有全局/旧活动应用语义；新集成始终使用有明确 app_id 的 v1 stop。

源码路由：`POST /api/appMgr/stop`

## POST /api/appMgr/switch

- 请求：JSON {id}。
- 返回：旧互斥切换结果。
- 特性：可能影响其他运行应用，不作为独立启动的替代。

源码路由：`POST /api/appMgr/switch`

## POST /api/appMgr/uninstall

- 请求：JSON {id}。
- 返回：legacy 卸载结果。
- 特性：新集成用 v1 DELETE；同样属于删除动作。

源码路由：`POST /api/appMgr/uninstall`

## POST /api/appMgr/upload

- 请求：原始应用包字节，X-Filename 与 Content-Length。
- 返回：暂存上传结果。
- 特性：legacy 上传接口；新开发使用 v1 multipart/preflight 流程，不混用请求形状。

源码路由：`POST /api/appMgr/upload`

## PUT /api/app-center/v1/apps/{app_id}/config

- 请求：JSON {values:{...}}。
- 返回：保存/应用配置的结果，包含运行时适用状态。
- 特性：参数验证与热更新/需重启语义以 schema 为准；不要自行编辑设备配置文件替代此接口。

源码路由：`PUT /api/app-center/v1/apps/([a-z0-9-]{1,64})/config`

## PUT /api/app-center/v1/apps/{app_id}/visualization

- 请求：JSON {stream_burn_in:bool}，不接受其他字段。
- 返回：更新后的该 App visualization 视图。
- 特性：并发安全地更新全局来源集合；builtin 的开启由系统内置叠加配置管理。

源码路由：`PUT /api/app-center/v1/apps/([a-z0-9-]{1,64})/visualization`

## PUT /api/app-center/v1/visualization

- 请求：JSON {osd:{enabled:bool,sources:[app_id,...]}}。
- 返回：保存后的全局 visualization 视图。
- 特性：校验已安装应用及其 stream OSD 能力；全局变更影响所有编码消费者。

源码路由：`PUT /api/app-center/v1/visualization`
