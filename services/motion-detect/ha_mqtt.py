"""ha_mqtt -- Home Assistant MQTT Discovery 上报（复用设备自带 kit 的 MqttSink）。

保持与 App Center 版 manifest 完全一致的 HA 契约：
  - Discovery：homeassistant/<component>/<node>/<obj>/config（retained）
  - 状态：recamera/motion-detect/state（summary.motion/score/...）
  - 上下线：recamera/motion-detect/status（online/offline + LWT 兜底）

kit 是设备平台自带组件；本模块仅在配置了 mqtt_host 时启用。
MqttSink 内部自带 连接/重连/keepalive 后台线程，emit 永不抛异常，
检测主线程不会被网络阻塞。
"""


class HaMqttBridge:
    """把检测事件转成 HA 状态。未配置或 kit 不可用时为空操作。"""

    HA_ENTITIES = [
        {"component": "binary_sensor", "object_id": "motion",
         "name": "Motion", "device_class": "motion",
         "value_template":
             "{{ 'ON' if value_json.summary.motion | default(false) "
             "else 'OFF' }}"},
        {"component": "sensor", "object_id": "motion_score",
         "name": "Motion Score", "state_class": "measurement",
         "icon": "mdi:motion-sensor",
         "value_template": "{{ value_json.summary.score | default(0) }}"},
        {"component": "sensor", "object_id": "motion_changed_pixels",
         "name": "Motion Changed Pixels", "state_class": "measurement",
         "icon": "mdi:motion-sensor",
         "value_template":
             "{{ value_json.summary.changed_pixels | default(0) }}"},
    ]

    def __init__(self, cfg: dict, log=print):
        self.log = log
        self._sink = None
        host = str(cfg.get("mqtt_host") or "").strip()
        if not host:
            return
        try:
            from kit.adapters.mqtt_sink import MqttSink
        except Exception as exc:                       # kit 缺失等
            self.log(f"[mqtt] kit MqttSink 不可用（{exc}），HA 上报关闭")
            return
        try:
            port = int(cfg.get("mqtt_port") or 1883)
        except (TypeError, ValueError):
            port = 1883
        self._sink = MqttSink(
            host=host, port=port, app_id="motion-detect",
            username=str(cfg.get("mqtt_user") or ""),
            password=str(cfg.get("mqtt_pass") or ""),
            entities=list(self.HA_ENTITIES),
            device_name="reCamera Pro")
        self.log(f"[mqtt] HA Discovery 上报：{host}:{port} "
                 f"topic=recamera/motion-detect/state")

    @property
    def enabled(self) -> bool:
        return self._sink is not None

    def emit(self, events, pts: float) -> None:
        """events：kit 风格事件列表；summary 由 MqttSink 聚合标量字段。"""
        if self._sink is None:
            return
        try:
            self._sink.emit({"results": [], "events": list(events)},
                            float(pts))
        except Exception as exc:                       # 绝不影响检测主循环
            self.log(f"[mqtt] emit error: {exc}")

    def close(self) -> None:
        if self._sink is not None:
            try:
                self._sink.close()
            except Exception:
                pass
            self._sink = None
