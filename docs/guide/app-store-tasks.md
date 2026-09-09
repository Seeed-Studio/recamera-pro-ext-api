# 设备端应用商店任务

应用商店通过设备 `appmgr` 下载应用包。浏览器不下载包、不上传 multipart，只查询目录、提交任务、读取进度并确认权限。云图标使用目录中的 `icon_url`，加载失败时回退到本地图标或默认图标。

固定目录：`https://sensecraft-statics.seeed.cc/solution-app/recamera_pro/catalog.json`。

## 接口

所有接口位于现有 JWT 保护的 `/api/app-center/v1` 下，沿用同源请求限制。

| 方法与路径 | 请求或返回 |
| --- | --- |
| `GET /store/catalog` | 设备获取并规范化的目录，保留 `schema: 1`、`apps`、`icon_url` 和不可安装原因 |
| `POST /store/tasks` | `{app_id, version, package_sha256}`；返回 HTTP 202 `{task, idempotent_replay}` |
| `GET /store/tasks` | `{tasks: [...]}`，用于刷新页面后恢复任务 |
| `GET /store/tasks/<id>` | `{task}` |
| `DELETE /store/tasks/<id>` | 显式取消尚未提交安装的任务，返回 `{task}` |
| `POST /store/tasks/<id>/install` | 确认权限和升级计划；返回 HTTP 202 `{task, operation}` |

创建任务只接受应用 ID、版本和包 SHA-256。设备重新读取固定目录，核对该身份后获取下载 URL；浏览器不能指定任意下载地址、目标路径或包内容。

安装确认请求形状与原 `/apps` 接口相同，但不传 `upload_id`，该值由任务绑定：

```json
{
  "permissions_confirmed": true,
  "permissions": {"sdk": ["frame.read", "npu.infer"]},
  "running_upgrade_confirmed": true
}
```

`permissions` 必须与任务预检结果完全一致。仅在安装计划需要且用户已确认时传入 `running_upgrade_confirmed` 或 `force_reinstall_confirmed`。这些值不应自动设为 `true`。

## 状态与恢复

正常顺序为 `queued → downloading → preflighting → awaiting_confirmation → installing → succeeded`；异常终态为 `failed` 或 `cancelled`。任务含 `id`、`app_id`、`version`、`package_sha256`、`status`、`progress`、`error`、时间戳，待确认时提供 `inspection`，进入安装后提供关联的 `operation`。

下载进度为 `progress.loaded/total/percent`；安装阶段应使用关联 `operation.progress`，不要将下载完成的 100% 当作安装已完成。前端约每两秒查询任务列表，网络失败时保留最后状态并提示暂时无法查询。

当 `task.status == "installing"` 且 `task.operation.status == "queued"` 时，界面显示“等待安装”；只有安装操作开始后才显示“安装中”。提交或确认任务后可继续浏览商店，搜索、筛选和浏览位置会在关闭任务详情后恢复。各应用独立显示任务进度和提交错误。

关闭窗口或离开页面不会取消任务。下载和预检完成后等待用户确认；确认安装后，即使浏览器关闭也会继续执行。只有显式 DELETE 才取消任务；安装已提交时不允许取消，以保留原安装事务和回滚保障。

任务元数据位于 `APPMGR_DIR/store-tasks.json`。服务重启时，中断的下载标记失败，临时文件由暂存恢复逻辑清理；仍有效的待确认任务可重新打开，已提交的安装通过原 operation journal 恢复状态。重启不承诺断点续传，也不会擅自重新执行安装。

## 与原安装机制的关系

- 独立的单个下载 worker，最多四个活跃任务，避免慢网络占用原应用生命周期 worker。相同应用的活跃任务防重。
- 一个应用安装时，其他应用可继续提交下载、完成预检并确认；它们的安装进入原 mutation 队列，按顺序执行。前一个安装失败不会阻断后续任务。四个活跃名额包括待确认与等待安装的任务，磁盘暂存配额也可能先达到上限；名额满时接口返回 `storeTaskLimitReached`。
- 以 64 KiB 块落盘，增量核对 SHA-256，限制目录与包大小、连接等待及总下载时间；失败清理本任务创建的文件。
- 下载暂存复用本地上传的数量、容量和过期清理配额。
- 使用系统 CA 校验固定 HTTPS CDN，禁止重定向，后台请求不携带设备登录凭据。
- 商店来源固定为 `app-store` / `app-center-v1-store`，必须是受信签名、自包含 manifest-v2 包，不继承本地 Web 上传的未签名例外。
- 预检及安装仍复用原 `installer.inspect` 和 `do_v1_install`，保留发布身份、签名、权限、运行中升级、强制重装确认和事务恢复。

## 构建与验证

前端与设备 `appmgr` 必须一起更新。旧服务没有 `/store/*` 接口时，前端会提示更新应用服务；本地上传入口继续使用原接口。

固件现有平台安装工具递归安装 `market/appmgr`，新模块会随源码构建进入固件；无需另加浏览器上传代理或独立端口。此变更不要求修改现有 nginx 的 JWT 通配转发配置。

后端定向测试：

```sh
uv run --frozen pytest market/appmgr/tests/test_store_download.py market/appmgr/tests/test_store_tasks.py
```

测试包含受信签名包在临时目录的完整安装、取消与安装提交竞争、配额、重启恢复、身份与签名拒绝及网络/磁盘失败；不依赖真实设备。前端还应运行相关 Jest 回归、生产构建以及深浅主题和多尺寸浏览器检查。
