# Workflow 应用卡片集成（x-workflow-ui v1）

Workflow 的配置、启动、停止和结果由原生 App Center 卡片管理；画布只负责编排和保存。
Inference RV1126B 使用此接口。应用源码和构建入口位于
[`apps/inference-rv1126b`](../../apps/inference-rv1126b/README.md)，执行引擎通过该目录的
`engine-source.lock.json` 引用独立仓库的固定提交。0.3.9 起新安装默认监听
`0.0.0.0:9001`，空访问口令在启动前自动生成并写入持久配置，供卡片编辑入口认证。
已有明确保存的监听地址和口令继续保留。固件需包含 `market/appmgr/workflow_ui.py`、
`server.py` 的对应路由及匹配的 App Center React 前端。仅安装应用包不会升级固件前端。

## 应用声明与配置

Manifest 扩展字段为 `"x-workflow-ui": {"version": 1}`。
应用提供 `workflow_id`、`workflow_parameters`、`video_source`、`rtsp_url`、
`workflow_fps` 和访问配置 `host`、`port`、`api_token`。
`workflow_id` 在 manifest 保持字符串类型；App Center GET config 根据本地已保存的
Workflow 动态生成 select 字段。空值显示“仅启动编辑服务”。RTSP URL 和访问口令使用
原生 schema 的 password 字段。保存配置遵循已有 apply/restart 流程。

应用启动后执行非空 workflow_id 指定的图，成功处理首帧后发送 Kit READY。
后续执行失败由 Kit/appmgr 的已有失败与有界重启策略处理。停止应用释放视频源和模型。
关闭结果窗口仅停止浏览器轮询。一次仅部署一个 Workflow；编排内容仍使用原始 JSON。

## 存储与接口

已保存工作流位于 `APPDATA_DIR/{app_id}/workflows/{sha256(id)}.json`。
文件含 `id` 和可选 `name`，其余原生画布内容不由管理器解析。管理器校验 id 与文件名、
禁止跟随符号链接，只读取常规文件，每文件最多 256 KiB、最多检查 64 个候选条目。
这使应用停止时仍能选择工作流，无需启动 Python 推理服务来枚举列表。

以下原生 Web 接口继续由 nginx 的设备 JWT 边界保护：

| API | 行为 |
| --- | --- |
| GET `/api/app-center/v1/apps/{id}/workflows` | 列举本地保存的 id/name |
| GET `/api/app-center/v1/apps/{id}/config` | 附带动态工作流选项和 workflow_ui 元数据 |
| GET `/api/app-center/v1/apps/{id}/workflows/runtime?include_result=true` | 当前部署状态和最近一次输出 |
| GET `/api/app-center/v1/apps/{id}/workflows/runtime?include_overlay=true` | 用于实时预览前端绘制的结构化图元 |
| GET `/api/app-center/v1/apps/{id}/workflows/preview` | 按 pipeline_id/output 读取当前工作流的输出图像 |
| POST `/api/app-center/v1/apps/{id}/workflows/editor-session`，JSON `{}` | 换取原生画布临时连接授权 |

管理器通过应用实际配置的口令访问固定 `127.0.0.1:port` 地址，不接受调用方提供的
URL、口令或任意代理路径，不执行模型。读取结果前后核对进程和实例 generation，
拒绝把重启前后的结果拼接。上游读取超时 3 秒、响应最多 16 MiB，不跟随 HTTP 重定向。
应用自身仍负责结果大小和帧缓冲限制。

应用的 GET `/app-center/workflow-runtime` 返回 `deployment`、`video`、`latest`、
`service_only`。其中 `latest` 为当前已部署视频实例的输出记录，读取不消费 SDK 缓冲。
卡片「查看结果」读取工作流输出图像，记录没有图像输出时显示相应提示。
实时预览通过 `include_overlay=true` 获取结构化图元，由浏览器在设备视频上绘制。
应用仍不声明设备端 OSD 能力。

画布会话通过真实 `/ui/session` 和 `/ui/builder-session` 交换，返回临时
`/ui/runtime/{grant}` 地址。长期口令不放入浏览器链接。官方画布仍需在浏览器联网加载；
本地工作流执行无须云端 API Key。官方画布 RTSP Run 的匿名预览限制与卡片持续推理
是不同入口，集成不修改官方前端。

## 验证

`market/appmgr/tests/test_workflow_ui.py` 覆盖停止时枚举、哈希存储 ID 映射、无效文件/
链接隔离、实际 HTTP 凭据传递、重定向拒绝和临时会话交换。设备测试应使用原生 Web
上传/配置/启动路径，不伪造内部认证头或 NPU 授权，不从 root shell 手工启动模型。
