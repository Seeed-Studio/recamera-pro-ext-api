# Motion Detection / 移动检测

可由 AppMgr 安装、启停和升级的纯 CPU 应用。固件首次预装后保持关闭，用户可在应用中心启动；后续重启和升级保留用户的启停选择。无需 NPU，可以和内建推理及其他资源兼容的应用同时运行。

## 检测与生命周期

`app.py` 导出 Kit `App` 子类。为保留原服务的低开销路径，应用在 `prepare_runtime()` 中连接 SDK `FrameSource`，按 `sample_fps` 源级降帧，直接处理 NV12 Y 平面；连接失败不会发送 READY。小图副本由 `motion_core.py` 保存，跨帧不持有 SDK 借用视图。Kit 管理参数热载和信号，退出时关闭帧源和 MQTT。

配置由应用中心管理，持久化到 `/userdata/local/appdata/motion-detect/config.json`。`sample_fps` 修改后重启，其余 UI 参数热更新。`manifest.json` 的配置分组与 `config_schema.json` 保持一致，由测试检查。

## 保持兼容的 HA/MQTT 协议

`ha_mqtt.py` 沿用原服务的专用上报适配器和 Kit `MqttSink`。使用应用自己的 `mqtt_host`、`mqtt_port`、`mqtt_user`、`mqtt_pass`；`mqtt_host` 为空时关闭 HA 上报。`owns_mqtt=True` 禁止 Kit 再按全局/CLI 设置创建第二个 MQTT 发布器。

| 项目 | 契约 |
|---|---|
| 状态 | `recamera/motion-detect/state`，非 retained |
| 在线状态 | `recamera/motion-detect/status`，retained，`online` / `offline`，包含 LWT |
| 业务字段 | `summary.motion`（bool）、`summary.score`、`summary.changed_ratio`、`summary.changed_pixels` |
| 实体 | `motion`、`motion_score`、`motion_changed_pixels` |
| Discovery | `homeassistant/<component>/recamera_<node>_motion-detect/<object_id>/config` |
| unique_id | `recamera_<node>_motion-detect_<object_id>` |
| 设备标识 | `recamera_<node>`；node 沿用序列号/主机名派生逻辑 |

状态变化即发，运动开始带 `motion` 事件，每次上报带 `motion_state` 事件；未变化时按 `heartbeat_sec` 心跳。原有事件字段、消息外层及 HA 模板保持兼容。平台 Result Hub 单独接收 Kit `status` 消息，沿用变化即发和 10 秒心跳，不参与 HA 消息格式化。详见 [API.md](API.md)。

## 固件预装与商店更新

- `market/default-apps.json` 声明默认应用源码。`tools/install_platform_services.py` 使用商店同一个打包器从源码构建 manifest-v2 包，将包和摘要清单放入固件 `/usr/share/recamera/default-apps/`。
- ext4 镜像打包还会通过 SDK 的 `project/scripts/normalize_recamera_rootfs_credentials.sh` 把预装目录、清单和归档的 UID/GID 设为 `0:0`；仅在宿主机 chmod 或使用 fakeroot 不足以修正静态 mke2fs 导入的构建用户属主。最终固件合并阶段清理残留的旧 `S95motion-detectd` 启动脚本。
- 首次启动在安装恢复完成之后，通过正常 AppMgr 安装事务创建应用和 per-release Python 环境。包及其摘要由固件镜像提供信任，不需要在源码构建机放置发布私钥。这个例外只认可固件目录内的原包；上传包/商店包仍按原签名规则校验。
- `/userdata/local/appmgr/default-apps.json` 记录初始化完成状态及未完成的迁移。已安装的应用不会被固件默认包覆盖；用户卸载后也不会在下次启动时重新安装。恢复出厂清除这份记录后，会再次预装并默认关闭。
- 旧固件的配置从 `/userdata/local/motion-detect/config.json` 迁移，明确保存过的 running/stopped 选择保留。先确认旧 daemon 停止，再迁移；新固件不部署 `S95motion-detectd`。旧配置保留用于恢复。
- 商店发布使用 `.github/workflows/publish-app.yml`，指定 `motion-detect`。每次发版递增 `manifest.json` 的 `version` 和 `release.sequence`。新包必须部署在含默认应用迁移逻辑的固件上；旧固件的系统应用路由不支持同 ID 普通包。

仓中的 `motion_daemon.py`、`http_control.py`、`config.json` 仅用于历史独立 SDK 调试，不进入应用包，也不随固件启动。安装后的应用仅使用平台配置 API，不监听历史 `:8099` HTTP 接口。

## 验证

```sh
uv run pytest apps/motion-detect/tests market/appmgr/tests/test_default_apps.py tools/test_install_platform_services.py
uv run python market/packaging/build.py apps/motion-detect --out /tmp/motion-packages
```

设备验证：首次开机卡片显示已安装且关闭；手动启动后检查 HA 原实体继续更新、无重复实体；停止后 HA 显示离线；升级保留 MQTT 配置、阈值和启停选择。结果可从 `/ws/ai/results/v2` 订阅 `motion-detect` 来源查看。
