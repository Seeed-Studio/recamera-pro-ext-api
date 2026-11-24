# Motion Detection 上报协议

应用 ID 固定为 `motion-detect`。固件预装和商店安装使用相同源码、相同上报实现。

## Home Assistant / MQTT

状态 topic：`recamera/motion-detect/state`。业务部分示例（外层还保留原有计数和事件字段）：

```json
{
  "app": "motion-detect",
  "pts": 123.456,
  "seq": 1,
  "summary": {
    "motion": true,
    "score": 0.1896,
    "changed_ratio": 0.02535,
    "changed_pixels": 365,
    "person_count": 0,
    "fallen_count": 0
  },
  "events": [
    {"kind": "motion", "score": 0.1896, "changed_ratio": 0.02535, "changed_pixels": 365},
    {"kind": "motion_state", "motion": true, "score": 0.1896, "changed_ratio": 0.02535, "changed_pixels": 365}
  ]
}
```

`pts` 是相机 VI PTS 秒，不是 Unix 时间。`motion` 为布尔值；运动结束或空闲心跳为 false。运动开始发送一次 `motion` 事件；`motion_state` 在状态变化和心跳时发送。消息外层的 `results_count`、`person_count`、`fallen_count` 为 0，`class_counts` 为空，`counts_by_kind` 按事件种类计数。

在线状态 topic：`recamera/motion-detect/status`，retained `online` / `offline`，断线包含 offline LWT。Discovery 配置使用原来的 topic、unique_id、设备标识及模板：

| object_id | 类型 | HA 读取路径 |
|---|---|---|
| `motion` | binary_sensor，device_class=motion | `summary.motion`，true → ON，false → OFF |
| `motion_score` | sensor | `summary.score` |
| `motion_changed_pixels` | sensor | `summary.changed_pixels` |

## 平台结果

平台 WebSocket `/ws/ai/results/v2` 的来源 ID 仍为 `motion-detect`，迁移后的身份为经过 AppMgr 验证的应用身份。应用发送 `status` envelope，`summary` 包含 `motion`、`score`、`changed_ratio`、`changed_pixels`、`daemon_running`；状态变化即发，并独立保持 10 秒平台心跳（不改变 HA 的 `heartbeat_sec`）。正常退出时发送 `motion:false`、`daemon_running:false`；异常退出由平台应用生命周期处理离线状态。

同一来源还发布带归一化 `geometry[]` 标签的 `frame` envelope，供浏览器绘制左上角叠加。标签在运动状态切换时立即更新，并定期刷新最新的检测值。

平台 envelope 与 HA 消息独立，不改变 HA MQTT 字段。原来按 `source.kind=system` 过滤的客户端应调整为按来源 ID 订阅。

## 配置

通过应用中心配置页或 `GET/POST /api/appMgr/config?id=motion-detect` 使用现有平台配置协议。配置项、类型、范围和热更新策略见 `manifest.json` 的 `config_schema`；`sample_fps` 需重启，其余 UI 参数热更新。

安装后的应用不再开放历史 daemon `:8099` 的 `/config` 和 `/state` 调试端点。保留在源码中的独立 SDK 调试脚本不属于发布包。
