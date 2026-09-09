# AI 结果输出配置

AI 推理页的“结果输出”在上方直接展示 WebSocket 消息记录和连接状态，下方展示输出配置，无需切换页签。消息支持自动跟随和暂停阅读，接收新消息不会重置下方的配置草稿。输出配置采用以下设置分组：

- **传输协议**：内置推理选择一种外部通道；应用可以同时选择 MQTT、HTTP、UART。连接参数直接展示，认证信息按需展开。应用取消所有外部通道时保留 `ws`，不会用空数组意外恢复 manifest 默认通道。
- **消息格式**：标准 JSON、字段映射、自定义模板、Home Assistant。字段映射与模板共用原有 `iMode` / `template_mode` 配置，切换时保留草稿；内置推理选择标准 JSON 并保存时清空模板。
- **发送策略**：应用的结果/事件过滤、最大发送频率及高级类别/重要事件条件。内置推理当前不提供独立限频，因此不展示该设置分组。

“配置验证”提供消息预览和发送测试。页面底部统一展示待保存状态及保存按钮，包含需重启应用生效的更改时给出提示。

模型阈值、区域等应用参数继续在应用设置中编辑。网页结果流和外部发送独立；页面显示“已连接”只表示网页结果流连接状态，输出历史也不是投递回执。

## 模板参考

选择“自定义模板”后，编辑区旁的“模板参考”随任务类型切换，提供变量含义、类型和示例。任务选择只决定正在编辑的模板，不会切换推理模型。

- 点击变量在光标位置插入；列表和对象包含 `tojson`。点击 `item` 字段时插入完整循环，并为可选字段加入缺省处理。
- 示例包括 JSON 计数消息、有结果时发送、逐项输出 JSON。已有内容时按钮明确显示“替换为此示例”，可撤销本次替换；其他任务模板保持不变。
- 内置和应用模板采用不同的数据结构，不能直接互换：

| 任务 | 内置推理 | 应用 |
| --- | --- | --- |
| 检测 | `detection.count`、`detection.entries` | `detection.count`、`detection.entries`（规范化检测项） |
| 分类 | `classification.count`、`classification.entries` | `classification` 列表，用 `length` 取数量 |
| 关键点 | `keypoints.count`、`keypoints.instances`，每项含 `points`、`point_count` | `keypoints` 列表，每项来自应用原始结果，含 `keypoints` |
| 分割 | `segmentation.count`、`segmentation.entries` | `segmentation` 列表 |
| 跟踪 | `tracking.count`、`tracking.entries` | `tracking` 列表（包含带 `track_id` 的结果或事件） |

内置推理按实际任务选择模板；应用会尝试渲染所有已配置的任务模板，仅希望有对应结果时发送应使用带条件的示例。内置通知服务当前没有 OBB 模板绑定，保留已有配置但不展示未经支持的变量或示例。

JSON 中字符串、列表、对象应使用 `| tojson`，不要额外加引号。可选字段使用 `| default(none)`，访问列表首项前先判断非空。内置通知的 `timestamp` 是格式化时间，网页记录中为毫秒数；需要一致数值时使用 `timestamp_ms`。应用时间戳使用 `timestamp`（毫秒）。

前端参考内容可单独更新。配套 appmgr 的内置预览样例已补齐模型、任务类型、类别名称、分割掩码尺寸，以及 `keypoints.instances[].points` 等字段；使用这些字段进行完整预览验证需要同步更新 appmgr。参考区不会自动保存配置或发送消息。

## 草稿预览与发送测试

需要配套新版 appmgr。设备 Web 登录鉴权与现有 `/api/app-center/v1` 路径一致；两个 POST 接口都经过 appmgr 的 mutation-origin 检查。旧固件缺少接口时，前端提示功能不可用，配置保存仍使用原接口。

| 接口 | 请求 | 返回含义 |
| --- | --- | --- |
| `POST /api/app-center/v1/apps/<id>/output/preview` | `{values: {...输出草稿}, task: "detection"}` | `{sample: true, messages: [{body, topic}]}`，明确为示例数据生成的消息 |
| `POST /api/app-center/v1/apps/<id>/output/test` | `{values: {...输出草稿}, channel: "http"或"mqtt"}` | `{ok, channel, status?, topic?, reason?}` |

`<id>` 可为 `builtin`；其他 ID 必须是声明 `output` 能力的已安装应用。请求上限 256 KiB。预览不保存、不重启应用、不连接目标、不注入推理结果；应用预览复用 Kit 格式化器，内置预览使用受限 Jinja 环境。样例字段有限，因此空预览不等同于真实数据下必定无输出。发送条件和限频不在消息格式预览中模拟。

发送测试使用固定 JSON：`type` 为 `test`，包含来源和时间戳；与自定义模板预览分开。HTTP 测试发送一次 POST，返回 2xx 才报告服务器成功响应，不跟随重定向、不读取响应正文。MQTT 测试向配置的基础主题发送，使用独立客户端 ID，不发布 retained、LWT 或 HA discovery 消息；成功仅表示消息已写入连接，需要订阅端确认。测试不会验证运行中的客户端 ID 或自定义子主题权限。UART 通过接收设备核对实际结果。

诊断有两个并发槽位，网络操作使用 5 秒 socket 超时；无自动重试。模板渲染和响应大小有界。错误响应不回显凭据或模板内容。前端在切换来源或修改草稿后丢弃过期诊断结果。
