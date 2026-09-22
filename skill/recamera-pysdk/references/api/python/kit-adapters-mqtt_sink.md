# kit.adapters.mqtt_sink

[API 索引](../index.md) · [接口特性与边界](../features.md)

源码基线：[kit/adapters/mqtt_sink.py](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/mqtt_sink.py)；签名由 AST 提取，不导入硬件依赖。

MQTT 结果与 Home Assistant Discovery；依赖 paho、可达 broker 及匹配的应用权限。

源码说明中的历史验证记录、迁移目标和旧示例不代表当前固件可用；应用入口、资源准入和接口可用性以本页补充及接口特性文档为准。

## 模块契约（源码）

MqttSink -- Home Assistant / MQTT result publisher for reCamera Pro (2nd gen).

L0 adapter (same ABC as WsResultSink). Where WsResultSink feeds the /appcenter
overlay a rich per-frame stream (pixel boxes, keypoints, ...), MqttSink feeds a
*home-automation* audience: a compact per-frame state document plus retained
Home Assistant MQTT-Discovery configs so HA auto-creates one entity per app
signal (detection count, fall state, entry/exit counts, QR text, ...).

Why a hand-rolled client (no paho-mqtt)
---------------------------------------
We only ever PUBLISH (QoS 0) -- never subscribe. MQTT 3.1.1 CONNECT + PUBLISH +
PINGREQ is a few dozen bytes of framing, so we implement it directly on a
stdlib socket (~150 LOC below). Zero new dependencies enter the shared device
venv or any app package. A background thread owns the socket: it connects,
publishes the retained discovery configs + an "online" availability message,
then keeps the link alive with PINGREQ and transparently reconnects (re-arming
discovery) after any drop. `emit()` is best-effort and never blocks or raises
into the inference loop -- a dead broker degrades to "WS only", exactly the
behaviour when MQTT is left unconfigured.

MQTT state document (published to <base_topic>/<app>/state each processed frame)
    {
      "app": "yolo-detector",
      "pts": 123.456, "seq": 42,
      "results_count": 3,                 # len(results)
      "person_count": 2,                  # visible person pose results
      "fallen_count": 1,                  # visible person results in fall state
      "counts_by_kind": {"detection": 3}, # tally of events[].kind
      "class_counts": {"person": 1, ...}, # tally of results[].cls_name
      "summary": { ... },                 # scalar event fields (fall aggregate-safe)
      "events": [ ... ]                   # app events (no pixel boxes dropped;
                                          #   kept small -- raw results omitted)
    }
HA entity `value_template`s (declared in each app manifest's `ha_entities`)
reference this document, e.g. `{{ value_json.results_count }}` or
`{{ value_json.summary.fall_detected }}`.

## kit.adapters.mqtt_sink.device_identifier

```python
def device_identifier() -> str
```

Stable per-device id for the HA `device.identifiers` grouping.

RECAMERA_SN env (set by the platform) wins; else the U-Boot `sn`; else the
hostname. Sanitised to [a-z0-9_] so it is topic/entity-id safe.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/mqtt_sink.py#L170)

## kit.adapters.mqtt_sink.ha_discovery_topic

```python
def ha_discovery_topic(discovery_prefix: str, node: str, app_id: str, ent: dict) -> str
```

`<prefix>/<component>/recamera_<node>_<app>/<object_id>/config`.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/mqtt_sink.py#L196)

## kit.adapters.mqtt_sink.ha_discovery_payload

```python
def ha_discovery_payload(node: str, app_id: str, state_topic: str, status_topic: str, device_name: str, ent: dict) -> dict
```

The retained HA MQTT-Discovery config document for one entity.

Availability (`availability_topic`/`payload_available`/`payload_not_available`)
and a stable `unique_id` are always emitted so HA marks the entity
online/offline off the LWT and never creates duplicates on reconnect.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/mqtt_sink.py#L205)

## kit.adapters.mqtt_sink.MqttSink

```python
class MqttSink(ResultSink)
```

Best-effort HA/MQTT publisher. Construct once per app run alongside the
WS sink (see kit.app.run_app). Never raises into emit().

继承接口：[kit.adapters.result_sink.ResultSink](kit-adapters-result_sink.md)。

### kit.adapters.mqtt_sink.MqttSink.__init__

```python
def __init__(self, *, host: str, port: int=1883, app_id: str='app', base_topic: str='recamera', discovery_prefix: str='homeassistant', username: str='', password: str='', entities: Optional[List[dict]]=None, device_name: str='reCamera Pro', keepalive: int=30, verbose: bool=False)
```

构造实例并保存/校验上述参数；参数默认值见签名。是否在构造时打开设备或加载模型，以本类的生命周期说明为准；构造方法返回 None。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/mqtt_sink.py#L243)

### kit.adapters.mqtt_sink.MqttSink.set_frame_size

```python
def set_frame_size(self, w: int, h: int) -> None
```

设置后续结果的原始画面宽、高（像素），供坐标换算/消息封装使用；本身不发送结果。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/mqtt_sink.py#L449)

### kit.adapters.mqtt_sink.MqttSink.emit

```python
def emit(self, payload: dict, pts: float) -> None
```

将业务 payload 与秒级 pts 封装后发布到配置的 MQTT topic，返回 None；本地调用完成不等于 broker/订阅者已消费。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/mqtt_sink.py#L533)

### kit.adapters.mqtt_sink.MqttSink.client_count

```python
def client_count(self) -> int
```

返回此 sink/channel 当前可报告的客户端数；零或非零都不证明某条业务结果已被远端消费。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/mqtt_sink.py#L547)

### kit.adapters.mqtt_sink.MqttSink.close

```python
def close(self) -> None
```

关闭本对象持有的连接/线程/设备等资源。具体幂等性、在途任务及失败处理见该类生命周期说明；不要在关闭后继续发送或读取。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/mqtt_sink.py#L551)
