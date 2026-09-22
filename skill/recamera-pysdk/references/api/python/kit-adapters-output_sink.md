# kit.adapters.output_sink

[API 索引](../index.md) · [接口特性与边界](../features.md)

源码基线：[kit/adapters/output_sink.py](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/output_sink.py)；签名由 AST 提取，不导入硬件依赖。

声明式输出格式化与通道组件：raw JSON、受限 Jinja、HA、WS、MQTT、HTTP、UART。托管 App 的输出配置交给平台装配。

源码说明中的历史验证记录、迁移目标和旧示例不代表当前固件可用；应用入口、资源准入和接口可用性以本页补充及接口特性文档为准。

## 模块契约（源码）

Unified configurable inference-result output (internal/OUTPUT_SINK_SPEC.md).

`ConfigurableSink` is a `ResultSink` that turns the kit's per-frame result into
configurable, concurrent outputs without any transport/formatting code in apps.
It builds ONE canonical envelope per frame, filters once, formats once per
channel, and best-effort publishes to every channel with per-channel failure
isolation.

Layout (why this is a separate module, not more of result_sink.py)
------------------------------------------------------------------
The three small ABCs (`OutputChannel`/`OutputFormatter`/`OutputMessage`) live in
`result_sink.py` beside `ResultSink`. Everything heavy -- `ConfigurableSink`,
the four channels, and the three formatters -- lives here so the legacy sink
path stays untouched. MQTT/LWT/discovery primitives are REUSED from
`mqtt_sink.py` (`_MqttConnection`, `ha_discovery_topic`, `ha_discovery_payload`),
never re-implemented.

Canonical envelope (spec §1)::

    {"app": "...", "timestamp": <epoch ms>, "seq": <int>,
     "frame": {"width": W, "height": H, "pts": <camera ts>},
     "results": [...], "events": [...]}

## 常量与类型别名

以下为该版本源码值；默认地址不等于所有固件都开放该服务。

```python
EDGE_KINDS = frozenset({'fall', 'line_cross', 'blink', 'yawn', 'wake', 'transcript', 'listen_timeout', 'rep_completed', 'set_completed', 'workout_complete'})
```


```python
MAX_TEMPLATE_LEN = 16 * 1024
```


```python
MAX_RENDER_LEN = 256 * 1024
```


```python
MAX_NS_ITEMS = 2000
```


```python
MAX_TEMPLATE_AST_NODES = 2048
```


```python
MAX_TEMPLATE_LITERAL_LEN = 4096
```


```python
MAX_TEMPLATE_LOOPS = 8
```


```python
MAX_TEMPLATE_LOOP_DEPTH = 1
```


```python
MAX_TEMPLATE_REPEAT_CHARS = 64 * 1024
```


```python
MAX_TEMPLATE_REPEAT_ITEMS = 4096
```


```python
MAX_TEMPLATE_INTEGER_BITS = 4096
```


```python
MAX_TEMPLATE_POW_EXPONENT = 16
```


## kit.adapters.output_sink.is_edge_event

```python
def is_edge_event(event: object) -> bool
```

Classify a discrete business edge across bundled/compatible apps.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/output_sink.py#L62)

## kit.adapters.output_sink.build_namespace

```python
def build_namespace(envelope: dict, *, app_id: str, device_id: str='') -> dict
```

Build the restricted jinja namespace (spec §4) from a canonical envelope.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/output_sink.py#L121)

## kit.adapters.output_sink.make_restricted_env

```python
def make_restricted_env()
```

A sandboxed jinja2 Environment: StrictUndefined, autoescape off, no
loader/imports, whitelisted filters only. Raises RuntimeError if jinja2 is
unavailable so callers can degrade to Raw mode.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/output_sink.py#L240)

## kit.adapters.output_sink.generate_mapping_templates

```python
def generate_mapping_templates(rows: List[dict]) -> List[dict]
```

Group visual mapping rows by rendered-topic template and emit one
JSON-object body template per topic.

Returns a list of render specs: ``[{"topic": <topic_tmpl>, "body":
<jinja_str>, "task": <task>, "generated_from_mapping": True}]``. Row order is
preserved for deterministic diffs. Optional rows (``omit_if_none`` default
True) are wrapped in ``{% if <source> is defined and <source> is not none %}``.

The body is built with a `namespace` accumulator joined at the end, so the
result is ALWAYS valid JSON regardless of which optional rows drop -- no
dangling commas, and the JSON object's literal `{` never collides with a
jinja `{%`/`{{` delimiter. Target names are JSON-escaped keys; values use
the `tojson` filter.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/output_sink.py#L354)

## kit.adapters.output_sink.RawJsonFormatter

```python
class RawJsonFormatter(OutputFormatter)
```

Serialize the canonical envelope compactly, no field loss (spec §4).

继承接口：[kit.adapters.result_sink.OutputFormatter](kit-adapters-result_sink.md)。

### kit.adapters.output_sink.RawJsonFormatter.format

```python
def format(self, envelope: dict, *, channel: str=None) -> List[OutputMessage]
```

将规范结果 envelope 按此 formatter 规则编码为 OutputMessage 列表（含 body/topic 等通道负载）；channel 可用于通道筛选。格式化本身不建立网络连接。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/output_sink.py#L416)

## kit.adapters.output_sink.Jinja2Formatter

```python
class Jinja2Formatter(OutputFormatter)
```

Render per-topic JSON bodies from compiled jinja2 templates.

`specs` is a list of ``{"topic": <topic_tmpl_str or None>, "body":
<body_tmpl_str>, "task": ...}``. The topic template interpolates only `app`
and a sanitized `device_id` (spec §4) in a separate tiny env; wildcards
(`+`/`#`), NUL and empty topics are rejected. A render error or a body that
renders empty drops only that message.

继承接口：[kit.adapters.result_sink.OutputFormatter](kit-adapters-result_sink.md)。

### kit.adapters.output_sink.Jinja2Formatter.__init__

```python
def __init__(self, specs: List[dict], *, app_id: str, device_id: str='', env=None)
```

构造实例并保存/校验上述参数；参数默认值见签名。是否在构造时打开设备或加载模型，以本类的生命周期说明为准；构造方法返回 None。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/output_sink.py#L432)

### kit.adapters.output_sink.Jinja2Formatter.format

```python
def format(self, envelope: dict, *, channel: str=None) -> List[OutputMessage]
```

将规范结果 envelope 按此 formatter 规则编码为 OutputMessage 列表（含 body/topic 等通道负载）；channel 可用于通道筛选。格式化本身不建立网络连接。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/output_sink.py#L463)

## kit.adapters.output_sink.HaDiscoveryFormatter

```python
class HaDiscoveryFormatter(OutputFormatter)
```

Home Assistant MQTT-Discovery + availability formatter (spec §5).

WRAPS the reusable `ha_discovery_topic`/`ha_discovery_payload` helpers and
`MqttSink._build_state` -- it does not re-implement discovery or state
aggregation. Owns all availability semantics: `on_channel_ready` returns
retained `online` + retained discovery configs; the kit (not apps/templates)
controls the status topic/payloads.

继承接口：[kit.adapters.result_sink.OutputFormatter](kit-adapters-result_sink.md)。

### kit.adapters.output_sink.HaDiscoveryFormatter.__init__

```python
def __init__(self, *, app_id: str, node: str, base_topic: str='recamera', discovery_prefix: str='homeassistant', entities: Optional[List[dict]]=None, device_name: str='reCamera Pro')
```

构造实例并保存/校验上述参数；参数默认值见签名。是否在构造时打开设备或加载模型，以本类的生命周期说明为准；构造方法返回 None。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/output_sink.py#L495)

### kit.adapters.output_sink.HaDiscoveryFormatter.format

```python
def format(self, envelope: dict, *, channel: str=None) -> List[OutputMessage]
```

将规范结果 envelope 按此 formatter 规则编码为 OutputMessage 列表（含 body/topic 等通道负载）；channel 可用于通道筛选。格式化本身不建立网络连接。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/output_sink.py#L510)

### kit.adapters.output_sink.HaDiscoveryFormatter.on_channel_ready

```python
def on_channel_ready(self, channel: OutputChannel) -> List[OutputMessage]
```

返回需要发布的 HA discovery OutputMessage 列表；根据通道状态生成发现配置，格式化器本身不负责连接 broker。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/output_sink.py#L519)

## kit.adapters.output_sink.WsChannel

```python
class WsChannel(OutputChannel)
```

Adapts an existing WsResultSink (reuse -- no second RFC6455 server).

The wrapped sink is constructed with ``preserve_envelope=True`` so the
canonical envelope built once upstream is broadcast verbatim (no second
seq/timestamp).

### 字段 / 默认值

`@dataclass` 未显式定义构造器时，由下列字段生成；无默认值的字段必填。

```python
name = 'ws'
```

继承接口：[kit.adapters.result_sink.OutputChannel](kit-adapters-result_sink.md)。

### kit.adapters.output_sink.WsChannel.__init__

```python
def __init__(self, ws: Optional[WsResultSink]=None, *, host: str='127.0.0.1', port: int=8124, app_id: str='app', own: Optional[bool]=None)
```

构造实例并保存/校验上述参数；参数默认值见签名。是否在构造时打开设备或加载模型，以本类的生命周期说明为准；构造方法返回 None。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/output_sink.py#L548)

### kit.adapters.output_sink.WsChannel.publish

```python
def publish(self, message: OutputMessage) -> None
```

将 OutputMessage 交给底层 WebSocket sink，返回 None；没有连接客户端时不代表发送到远端。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/output_sink.py#L557)

### kit.adapters.output_sink.WsChannel.client_count

```python
def client_count(self) -> int
```

返回此 sink/channel 当前可报告的客户端数；零或非零都不证明某条业务结果已被远端消费。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/output_sink.py#L564)

### kit.adapters.output_sink.WsChannel.close

```python
def close(self) -> None
```

关闭本对象持有的连接/线程/设备等资源。具体幂等性、在途任务及失败处理见该类生命周期说明；不要在关闭后继续发送或读取。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/output_sink.py#L570)

## kit.adapters.output_sink.MqttChannel

```python
class MqttChannel(OutputChannel)
```

Publish-only MQTT channel over the reused `_MqttConnection` primitive.

Owns a background connect/keepalive/reconnect thread mirroring
`MqttSink._run/_open`. On every (re)connect it invokes `on_ready()` (wired by
ConfigurableSink to the formatter's `on_channel_ready`) and publishes the
returned messages retained -- this is how HA discovery + `online` re-arm
after a drop. LWT (`will_topic`/`will_payload`) covers unexpected death.

### 字段 / 默认值

`@dataclass` 未显式定义构造器时，由下列字段生成；无默认值的字段必填。

```python
name = 'mqtt'
```

继承接口：[kit.adapters.result_sink.OutputChannel](kit-adapters-result_sink.md)。

### kit.adapters.output_sink.MqttChannel.__init__

```python
def __init__(self, *, host: str, port: int=1883, client_id: str='recamera', username: str='', password: str='', keepalive: int=30, default_topic: str='', will_topic: str='', will_payload: bytes=b'offline', will_retain: bool=True, offline_on_close: bool=True, on_ready: Optional[Callable[[], List[OutputMessage]]]=None, verbose: bool=False, autostart: bool=True)
```

构造实例并保存/校验上述参数；参数默认值见签名。是否在构造时打开设备或加载模型，以本类的生命周期说明为准；构造方法返回 None。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/output_sink.py#L590)

### kit.adapters.output_sink.MqttChannel.start

```python
def start(self) -> None
```

启动 MQTT 客户端连接/网络循环，返回 None；连接结果和错误由后续状态体现。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/output_sink.py#L619)

### kit.adapters.output_sink.MqttChannel.publish

```python
def publish(self, message: OutputMessage) -> None
```

按消息 topic/qos/retain 发布格式化负载，返回 None；不能据此断言对端已经处理。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/output_sink.py#L695)

### kit.adapters.output_sink.MqttChannel.client_count

```python
def client_count(self) -> int
```

返回此 sink/channel 当前可报告的客户端数；零或非零都不证明某条业务结果已被远端消费。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/output_sink.py#L699)

### kit.adapters.output_sink.MqttChannel.close

```python
def close(self) -> None
```

关闭本对象持有的连接/线程/设备等资源。具体幂等性、在途任务及失败处理见该类生命周期说明；不要在关闭后继续发送或读取。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/output_sink.py#L702)

## kit.adapters.output_sink.HttpChannel

```python
class HttpChannel(OutputChannel)
```

POST each message to a URL via stdlib urllib on a background worker.

A bounded queue (drop-oldest) guarantees `publish()` never blocks inference.
Retries only network errors and 429/5xx with capped backoff; other 4xx are
dropped. `Authorization: Bearer <token>` is sent when a token is set.

### 字段 / 默认值

`@dataclass` 未显式定义构造器时，由下列字段生成；无默认值的字段必填。

```python
name = 'http'
```

继承接口：[kit.adapters.result_sink.OutputChannel](kit-adapters-result_sink.md)。

### kit.adapters.output_sink.HttpChannel.__init__

```python
def __init__(self, *, url: str, token: str='', timeout: float=5.0, queue_size: int=32, max_retries: int=2, autostart: bool=True)
```

构造实例并保存/校验上述参数；参数默认值见签名。是否在构造时打开设备或加载模型，以本类的生命周期说明为准；构造方法返回 None。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/output_sink.py#L719)

### kit.adapters.output_sink.HttpChannel.start

```python
def start(self) -> None
```

启动有界 HTTP 输出工作线程，返回 None；需要配对 close。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/output_sink.py#L733)

### kit.adapters.output_sink.HttpChannel.publish

```python
def publish(self, message: OutputMessage) -> None
```

将消息放入 HTTP 发送队列，返回 None；异步工作线程执行网络请求。队列/网络失败不等于推理失败。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/output_sink.py#L738)

### kit.adapters.output_sink.HttpChannel.close

```python
def close(self) -> None
```

关闭本对象持有的连接/线程/设备等资源。具体幂等性、在途任务及失败处理见该类生命周期说明；不要在关闭后继续发送或读取。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/output_sink.py#L790)

## kit.adapters.output_sink.UartChannel

```python
class UartChannel(OutputChannel)
```

Newline-delimited writes to an allow-listed UART (spec §2, feature-gated).

Production enablement is gated (`enabled=False` -> no-op) until baud/parity/
ownership are verified on-device (spec §10). A test may inject a writable
file descriptor (`fd=`) to exercise framing without hardware; otherwise the
device path must match `/dev/ttyS*`.

### 字段 / 默认值

`@dataclass` 未显式定义构造器时，由下列字段生成；无默认值的字段必填。

```python
name = 'uart'
```

继承接口：[kit.adapters.result_sink.OutputChannel](kit-adapters-result_sink.md)。

### kit.adapters.output_sink.UartChannel.__init__

```python
def __init__(self, *, port_dev: str='', fd: Optional[int]=None, enabled: bool=False, max_payload: int=4096)
```

构造实例并保存/校验上述参数；参数默认值见签名。是否在构造时打开设备或加载模型，以本类的生命周期说明为准；构造方法返回 None。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/output_sink.py#L806)

### kit.adapters.output_sink.UartChannel.publish

```python
def publish(self, message: OutputMessage) -> None
```

向配置串口写入格式化消息，返回 None；需设备权限、正确串口和波特率。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/output_sink.py#L826)

### kit.adapters.output_sink.UartChannel.close

```python
def close(self) -> None
```

关闭本对象持有的连接/线程/设备等资源。具体幂等性、在途任务及失败处理见该类生命周期说明；不要在关闭后继续发送或读取。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/output_sink.py#L838)

## kit.adapters.output_sink.ConfigurableSink

```python
class ConfigurableSink(ResultSink)
```

A ResultSink that builds one canonical envelope per frame, filters once,
formats once per channel, and best-effort publishes with per-channel failure
isolation (spec §2).

继承接口：[kit.adapters.result_sink.ResultSink](kit-adapters-result_sink.md)。

### kit.adapters.output_sink.ConfigurableSink.__init__

```python
def __init__(self, *, app_id: str, channels: List[OutputChannel], formatter: OutputFormatter, filters: Optional[dict]=None, device_id: str='', verbose: bool=False, formatter_builder: Optional[Callable[[dict], OutputFormatter]]=None)
```

构造实例并保存/校验上述参数；参数默认值见签名。是否在构造时打开设备或加载模型，以本类的生命周期说明为准；构造方法返回 None。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/output_sink.py#L855)

### kit.adapters.output_sink.ConfigurableSink.set_filters

```python
def set_filters(self, filters: Optional[dict]) -> None
```

更新 category/label/min_score 过滤条件，不修改模型推理本身，返回 None。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/output_sink.py#L894)

### kit.adapters.output_sink.ConfigurableSink.on_config_reload

```python
def on_config_reload(self, config: dict) -> None
```

Live-apply filter/template changes. Structural channel changes are
apply:"restart" and never reach here (spec §3).

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/output_sink.py#L904)

### kit.adapters.output_sink.ConfigurableSink.set_frame_size

```python
def set_frame_size(self, w: int, h: int) -> None
```

设置后续结果的原始画面宽、高（像素），供坐标换算/消息封装使用；本身不发送结果。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/output_sink.py#L921)

### kit.adapters.output_sink.ConfigurableSink.emit

```python
def emit(self, payload: dict, pts: float) -> None
```

依次执行结果过滤、输出速率控制、格式化和通道发布，返回 None；被过滤或限速时可以没有输出。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/output_sink.py#L998)

### kit.adapters.output_sink.ConfigurableSink.emit_meta

```python
def emit_meta(self, payload: dict) -> None
```

按该 sink 的能力处理配置/元信息；不是一次模型推理，也不证明前端已经收到配置。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/output_sink.py#L1029)

### kit.adapters.output_sink.ConfigurableSink.client_count

```python
def client_count(self) -> int
```

返回此 sink/channel 当前可报告的客户端数；零或非零都不证明某条业务结果已被远端消费。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/output_sink.py#L1038)

### kit.adapters.output_sink.ConfigurableSink.close

```python
def close(self) -> None
```

关闭本对象持有的连接/线程/设备等资源。具体幂等性、在途任务及失败处理见该类生命周期说明；不要在关闭后继续发送或读取。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/output_sink.py#L1047)

## kit.adapters.output_sink.resolve_output_config

```python
def resolve_output_config(manifest: dict, eff: dict) -> dict
```

Merge the manifest `output` block defaults with persisted config.json
values (eff). Kit-side mirror of appmgr's injected `output` schema group.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/output_sink.py#L1070)

## kit.adapters.output_sink.build_formatter

```python
def build_formatter(mode: str, cfg: dict, *, app_id: str, node: str, base_topic: str, entities: List[dict], device_name: str, discovery_prefix: str='homeassistant', fallback_raw: bool=True) -> OutputFormatter
```

Pick and construct the formatter for the resolved mode (spec §3/§4/§5).

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/output_sink.py#L1108)

## kit.adapters.output_sink.assemble_output_sink

```python
def assemble_output_sink(app, app_dir: str, manifest: dict, eff: dict, *, base_topic: str='recamera', discovery_prefix: str='homeassistant', verbose: bool=False)
```

Build a ConfigurableSink for apps that declare `capabilities:["output"]`.

Returns ``(sink_or_None, opted_in)``. When the app has NOT opted in,
``opted_in`` is False and the caller keeps the legacy sink path entirely
unchanged (bypass, spec §3.1). When opted in but no external channel is
configured (e.g. WS-only, covered by the primary overlay), the sink is None
but ``opted_in`` is True, so the legacy MQTT path is NOT engaged.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/output_sink.py#L1147)
