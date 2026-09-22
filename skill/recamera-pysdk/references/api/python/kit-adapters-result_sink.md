# kit.adapters.result_sink

[API 索引](../index.md) · [接口特性与边界](../features.md)

源码基线：[kit/adapters/result_sink.py](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/result_sink.py)；签名由 AST 提取，不导入硬件依赖。

旧字典结果协议、严格发送、输出网关及兼容 WS。emit 的 pts 为秒，box 为原图像素；受管应用使用 App.emit。

源码说明中的历史验证记录、迁移目标和旧示例不代表当前固件可用；应用入口、资源准入和接口可用性以本页补充及接口特性文档为准。

## 模块契约（源码）

ResultSink adapter for reCamera Pro (Rockchip RV1126B).

L0 adapter layer (see docs/guide/kit-design.md §0 / §0.5). The application never
touches a sink directly -- the `App` base class owns one and calls `emit()` for
every processed frame. The concrete backend is swappable behind the `ResultSink`
ABC:

* `StdoutSink`   -- prints one JSON line per frame. Zero deps, debug/CI use.
* `WsResultSink` -- broadcasts the structured per-frame JSON to every connected
                    WebSocket client on a local port, for the `/appcenter`
                    overlay panel to subscribe to and draw boxes on top of the
                    go2rtc preview. Pure stdlib (socket + threading + hashlib);
                    a hand-rolled RFC6455 server frames text messages itself, so
                    NO `websockets`/`aiohttp`/etc. dependency is pulled in.
* `GatewayResultSink` -- appmgr-managed publisher.  It sends NDJSON to one Unix
                         socket; appmgr owns the sole WebSocket listener, so
                         multiple applications never contend for :8124.

Why our own lightweight WS (not the official :8123)
---------------------------------------------------
Per §0.5 the official :8123 result stream belongs to rkipc's own inference and
we must not squat on it. Our self-hosted apps emit their results on a separate
port we own (default 8124). When the official OSD/RGN injection interface lands,
a new `OsdInjectSink` implementation drops in behind this same ABC and the
capability registry selects it -- application code does not change.

Wire format (one JSON object per frame, newline-free, one WS text message):
    {
      "type": "results",
      "app": "<app-id>",
      "pts": 12345.678,          # frame capture timestamp (monotonic seconds)
      "seq": 42,                 # monotonic frame counter
      "results": [ {box,cls,cls_name,score}, ... ],   # raw detections
      "events":  [ ... ]         # app-level events from on_results()
    }

## 常量与类型别名

以下为该版本源码值；默认地址不等于所有固件都开放该服务。

```python
WS_MAX_CLIENTS = _env_int('RECAMERA_WS_MAX_CLIENTS', 32)
```


```python
WS_MAX_PER_IP = _env_int('RECAMERA_WS_MAX_PER_IP', 8)
```


```python
WS_CLIENT_QUEUE = _env_int('RECAMERA_WS_CLIENT_QUEUE', 64)
```


```python
WS_SEND_TIMEOUT = _env_float('RECAMERA_WS_SEND_TIMEOUT', 2.0)
```


```python
WS_LAG_LIMIT = _env_int('RECAMERA_WS_LAG_LIMIT', 128)
```


```python
ResultPublisher = ResultSink
```


## kit.adapters.result_sink.effective_bind_host

```python
def effective_bind_host(host: Optional[str]) -> str
```

Resolve the requested bind host, DEFAULTING TO LOOPBACK (C9).

The result stream is published behind the nginx JWT edge, which reverse-
proxies /appcenter/ws/results to 127.0.0.1:<port>. Binding loopback means
only nginx (already authenticated) and root can reach the raw port -- a LAN
peer cannot open an unauthenticated subscription. None/""/localhost/
loopback/local all resolve to 127.0.0.1. An explicit routable address
(e.g. "0.0.0.0") is honoured for the documented LAN-direct case, but that
exposes UNAUTHENTICATED results and must be opted into on purpose.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/result_sink.py#L94)

## kit.adapters.result_sink.offer_latest_wins

```python
def offer_latest_wins(q: 'queue.Queue', item) -> bool
```

Put `item` on a bounded queue, dropping the OLDEST if full (latest-wins).

Returns True if an old item had to be dropped to make room. A live-video
overlay wants the freshest frame, never a backlog, so a slow reader loses
stale frames instead of the producer blocking (C10).

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/result_sink.py#L111)

## kit.adapters.result_sink.admit_reason

```python
def admit_reason(n_total: int, n_from_ip: int, max_clients: int, max_per_ip: int) -> Optional[str]
```

Return None if a new client may be admitted, else a short refusal reason.

Two independent caps (C10): a global fd budget, and a per-IP cap so one peer
cannot consume the whole budget by itself.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/result_sink.py#L134)

## kit.adapters.result_sink.ResultSink

```python
class ResultSink(ABC)
```

Abstract result publisher. The App base class owns one; apps never call it.

### kit.adapters.result_sink.ResultSink.emit

```python
@abstractmethod
def emit(self, payload: dict, pts: float) -> None
```

Publish one frame's structured result payload.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/result_sink.py#L216)

### kit.adapters.result_sink.ResultSink.emit_meta

```python
def emit_meta(self, payload: dict) -> None
```

Publish an out-of-band meta message NOT tied to a frame's results
(e.g. the periodic pipeline `metrics` event: FPS + per-stage latency).

Default: no-op. Only sinks whose audience wants live telemetry override
it -- WsResultSink broadcasts it to the /appcenter debug panel, while
MqttSink deliberately ignores it (an empty HA state doc every second is
noise). This keeps metrics strictly additive: existing results/events/
MQTT behaviour is untouched.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/result_sink.py#L220)

### kit.adapters.result_sink.ResultSink.emit_checked

```python
def emit_checked(self, payload: dict, pts: float) -> None
```

Publish and surface synchronous acceptance failures.

Historical :meth:`emit` implementations are intentionally best-effort
so one telemetry/output backend cannot stop an inference loop.  Typed
callers that must not mistake a local rejection for success use this
additive method.  The default delegates to ``emit``; sinks that hide
native/fan-out failures override it.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/result_sink.py#L232)

### kit.adapters.result_sink.ResultSink.request_recording

```python
def request_recording(self, event_kind: str, pts: float) -> bool
```

Only a managed gateway implements recording; telemetry sinks do not.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/result_sink.py#L244)

### kit.adapters.result_sink.ResultSink.set_frame_size

```python
def set_frame_size(self, w: int, h: int) -> None
```

Tell the sink the current frame's pixel dimensions.

The App base loop calls this once per frame BEFORE emit(). Only sinks
that must convert pixel coordinates need it: OfficialResultSink divides
box/keypoint pixel coords by (w, h) to get the normalized [0,1] fractions
the extension-API OSD renderer requires. Default: no-op, so the
WS/stdout/MQTT workaround sinks (which keep their own pixel/JSON
convention and wire format) are completely unaffected.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/result_sink.py#L248)

### kit.adapters.result_sink.ResultSink.set_frame_size_checked

```python
def set_frame_size_checked(self, w: int, h: int) -> None
```

Set geometry while surfacing synchronous backend failures.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/result_sink.py#L260)

### kit.adapters.result_sink.ResultSink.on_config_reload

```python
def on_config_reload(self, config: dict) -> None
```

Live-apply a config change (SIGHUP) to this sink.

`App._maybe_reload` calls this on the app's sink with the freshly
re-read effective config, so a sink whose behaviour is config-driven
(ConfigurableSink: output filters / formatter template) picks up
apply:"live" changes without a restart -- for EVERY app, whatever loop
shape it uses. Default: no-op; sinks with no config react to nothing.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/result_sink.py#L265)

### kit.adapters.result_sink.ResultSink.stats

```python
def stats(self) -> dict
```

Best-effort send diagnostics for this sink (default: empty).

Sinks that can count what they published override this so an app (or a
health probe) can read cumulative counters -- e.g. OfficialResultSink
surfaces the SDK's local `sent`/`oversize_rejected`/`send_error` tallies.
Local counters only: a frame accepted locally that the server later
drops is not reflected here until the server-ACK protocol lands
(docs/guide/result-push.md).

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/result_sink.py#L276)

### kit.adapters.result_sink.ResultSink.close

```python
def close(self) -> None
```

关闭本对象持有的连接/线程/设备等资源。具体幂等性、在途任务及失败处理见该类生命周期说明；不要在关闭后继续发送或读取。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/result_sink.py#L287)

### kit.adapters.result_sink.ResultSink.__enter__

```python
def __enter__(self)
```

进入上下文管理器，返回其受管对象；与 __exit__ 配对使用，避免异常路径遗留资源。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/result_sink.py#L290)

### kit.adapters.result_sink.ResultSink.__exit__

```python
def __exit__(self, *exc)
```

离开上下文并执行本类的清理方法；异常传播/清理失败语义见类说明，不把退出视作任务已完成。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/result_sink.py#L293)

## kit.adapters.result_sink.StdoutSink

```python
class StdoutSink(ResultSink)
```

Debug sink: print one compact JSON line per frame to stdout.

### kit.adapters.result_sink.StdoutSink.emit

```python
def emit(self, payload: dict, pts: float) -> None
```

把带时间戳的结果输出到 stdout，返回 None；适合调试，不会自动出现在官方预览。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/result_sink.py#L309)

### kit.adapters.result_sink.StdoutSink.emit_meta

```python
def emit_meta(self, payload: dict) -> None
```

按该 sink 的能力处理配置/元信息；不是一次模型推理，也不证明前端已经收到配置。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/result_sink.py#L314)

### kit.adapters.result_sink.StdoutSink.emit_checked

```python
def emit_checked(self, payload: dict, pts: float) -> None
```

Publish and surface synchronous acceptance failures.

Historical :meth:`emit` implementations are intentionally best-effort
so one telemetry/output backend cannot stop an inference loop.  Typed
callers that must not mistake a local rejection for success use this
additive method.  The default delegates to ``emit``; sinks that hide
native/fan-out failures override it.

此方法定义于基类 `ResultSink`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/result_sink.py#L232)

### kit.adapters.result_sink.StdoutSink.request_recording

```python
def request_recording(self, event_kind: str, pts: float) -> bool
```

Only a managed gateway implements recording; telemetry sinks do not.

此方法定义于基类 `ResultSink`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/result_sink.py#L244)

### kit.adapters.result_sink.StdoutSink.set_frame_size

```python
def set_frame_size(self, w: int, h: int) -> None
```

Tell the sink the current frame's pixel dimensions.

The App base loop calls this once per frame BEFORE emit(). Only sinks
that must convert pixel coordinates need it: OfficialResultSink divides
box/keypoint pixel coords by (w, h) to get the normalized [0,1] fractions
the extension-API OSD renderer requires. Default: no-op, so the
WS/stdout/MQTT workaround sinks (which keep their own pixel/JSON
convention and wire format) are completely unaffected.

此方法定义于基类 `ResultSink`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/result_sink.py#L248)

### kit.adapters.result_sink.StdoutSink.set_frame_size_checked

```python
def set_frame_size_checked(self, w: int, h: int) -> None
```

Set geometry while surfacing synchronous backend failures.

此方法定义于基类 `ResultSink`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/result_sink.py#L260)

### kit.adapters.result_sink.StdoutSink.on_config_reload

```python
def on_config_reload(self, config: dict) -> None
```

Live-apply a config change (SIGHUP) to this sink.

`App._maybe_reload` calls this on the app's sink with the freshly
re-read effective config, so a sink whose behaviour is config-driven
(ConfigurableSink: output filters / formatter template) picks up
apply:"live" changes without a restart -- for EVERY app, whatever loop
shape it uses. Default: no-op; sinks with no config react to nothing.

此方法定义于基类 `ResultSink`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/result_sink.py#L265)

### kit.adapters.result_sink.StdoutSink.stats

```python
def stats(self) -> dict
```

Best-effort send diagnostics for this sink (default: empty).

Sinks that can count what they published override this so an app (or a
health probe) can read cumulative counters -- e.g. OfficialResultSink
surfaces the SDK's local `sent`/`oversize_rejected`/`send_error` tallies.
Local counters only: a frame accepted locally that the server later
drops is not reflected here until the server-ACK protocol lands
(docs/guide/result-push.md).

此方法定义于基类 `ResultSink`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/result_sink.py#L276)

### kit.adapters.result_sink.StdoutSink.close

```python
def close(self) -> None
```

关闭本对象持有的连接/线程/设备等资源。具体幂等性、在途任务及失败处理见该类生命周期说明；不要在关闭后继续发送或读取。

此方法定义于基类 `ResultSink`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/result_sink.py#L287)

### kit.adapters.result_sink.StdoutSink.__enter__

```python
def __enter__(self)
```

进入上下文管理器，返回其受管对象；与 __exit__ 配对使用，避免异常路径遗留资源。

此方法定义于基类 `ResultSink`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/result_sink.py#L290)

### kit.adapters.result_sink.StdoutSink.__exit__

```python
def __exit__(self, *exc)
```

离开上下文并执行本类的清理方法；异常传播/清理失败语义见类说明，不把退出视作任务已完成。

此方法定义于基类 `ResultSink`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/result_sink.py#L293)

### kit.adapters.result_sink.StdoutSink.__init__

```python
def __init__(self, only_nonempty: bool=False)
```

构造实例并保存/校验上述参数；参数默认值见签名。是否在构造时打开设备或加载模型，以本类的生命周期说明为准；构造方法返回 None。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/result_sink.py#L306)

## kit.adapters.result_sink.WsResultSink

```python
class WsResultSink(ResultSink)
```

Minimal broadcast WebSocket server, stdlib only.

Runs a background accept loop; each accepted client is upgraded (RFC6455
handshake) and added to a broadcast set. `emit()` serialises the payload
once and best-effort sends it to every client, dropping any that error.
Slow/dead clients never block the inference loop.

### kit.adapters.result_sink.WsResultSink.emit

```python
def emit(self, payload: dict, pts: float) -> None
```

将结果提交到该独立 WS sink，返回 None。托管 App 使用 App.emit/平台网关，不自行监听保留端口。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/result_sink.py#L502)

### kit.adapters.result_sink.WsResultSink.emit_meta

```python
def emit_meta(self, payload: dict) -> None
```

Broadcast a metrics/meta message on the SAME WS channel as results.
Tagged with its own `type` (e.g. "metrics") so the panel can demux; it
does NOT advance the results `seq` counter.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/result_sink.py#L521)

### kit.adapters.result_sink.WsResultSink.emit_checked

```python
def emit_checked(self, payload: dict, pts: float) -> None
```

Publish and surface synchronous acceptance failures.

Historical :meth:`emit` implementations are intentionally best-effort
so one telemetry/output backend cannot stop an inference loop.  Typed
callers that must not mistake a local rejection for success use this
additive method.  The default delegates to ``emit``; sinks that hide
native/fan-out failures override it.

此方法定义于基类 `ResultSink`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/result_sink.py#L232)

### kit.adapters.result_sink.WsResultSink.request_recording

```python
def request_recording(self, event_kind: str, pts: float) -> bool
```

Only a managed gateway implements recording; telemetry sinks do not.

此方法定义于基类 `ResultSink`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/result_sink.py#L244)

### kit.adapters.result_sink.WsResultSink.set_frame_size

```python
def set_frame_size(self, w: int, h: int) -> None
```

Record the current inference-frame pixel size (base loop calls this
per frame). Unlike OfficialResultSink -- which DIVIDES coords by this to
normalize -- WsResultSink keeps pixel coords verbatim and just ANNOUNCES
the reference size in the wire message so the overlay maps 1:1.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/result_sink.py#L384)

### kit.adapters.result_sink.WsResultSink.set_frame_size_checked

```python
def set_frame_size_checked(self, w: int, h: int) -> None
```

Set geometry while surfacing synchronous backend failures.

此方法定义于基类 `ResultSink`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/result_sink.py#L260)

### kit.adapters.result_sink.WsResultSink.on_config_reload

```python
def on_config_reload(self, config: dict) -> None
```

Live-apply a config change (SIGHUP) to this sink.

`App._maybe_reload` calls this on the app's sink with the freshly
re-read effective config, so a sink whose behaviour is config-driven
(ConfigurableSink: output filters / formatter template) picks up
apply:"live" changes without a restart -- for EVERY app, whatever loop
shape it uses. Default: no-op; sinks with no config react to nothing.

此方法定义于基类 `ResultSink`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/result_sink.py#L265)

### kit.adapters.result_sink.WsResultSink.stats

```python
def stats(self) -> dict
```

Best-effort send diagnostics for this sink (default: empty).

Sinks that can count what they published override this so an app (or a
health probe) can read cumulative counters -- e.g. OfficialResultSink
surfaces the SDK's local `sent`/`oversize_rejected`/`send_error` tallies.
Local counters only: a frame accepted locally that the server later
drops is not reflected here until the server-ACK protocol lands
(docs/guide/result-push.md).

此方法定义于基类 `ResultSink`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/result_sink.py#L276)

### kit.adapters.result_sink.WsResultSink.close

```python
def close(self) -> None
```

关闭本对象持有的连接/线程/设备等资源。具体幂等性、在途任务及失败处理见该类生命周期说明；不要在关闭后继续发送或读取。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/result_sink.py#L535)

### kit.adapters.result_sink.WsResultSink.__enter__

```python
def __enter__(self)
```

进入上下文管理器，返回其受管对象；与 __exit__ 配对使用，避免异常路径遗留资源。

此方法定义于基类 `ResultSink`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/result_sink.py#L290)

### kit.adapters.result_sink.WsResultSink.__exit__

```python
def __exit__(self, *exc)
```

离开上下文并执行本类的清理方法；异常传播/清理失败语义见类说明，不把退出视作任务已完成。

此方法定义于基类 `ResultSink`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/result_sink.py#L293)

### kit.adapters.result_sink.WsResultSink.__init__

```python
def __init__(self, host: str='127.0.0.1', port: int=8124, app_id: str='app', preserve_envelope: bool=False, *, max_clients: int=WS_MAX_CLIENTS, max_per_ip: int=WS_MAX_PER_IP, client_queue: int=WS_CLIENT_QUEUE, send_timeout: float=WS_SEND_TIMEOUT, lag_limit: int=WS_LAG_LIMIT)
```

构造实例并保存/校验上述参数；参数默认值见签名。是否在构造时打开设备或加载模型，以本类的生命周期说明为准；构造方法返回 None。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/result_sink.py#L343)

### kit.adapters.result_sink.WsResultSink.publish_envelope

```python
def publish_envelope(self, envelope: dict) -> None
```

Broadcast a pre-built canonical envelope verbatim (used by
ConfigurableSink's WsChannel). No second seq/timestamp/frame stamp.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/result_sink.py#L495)

### kit.adapters.result_sink.WsResultSink.client_count

```python
def client_count(self) -> int
```

返回此 sink/channel 当前可报告的客户端数；零或非零都不证明某条业务结果已被远端消费。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/result_sink.py#L530)

## kit.adapters.result_sink.GatewayResultSink

```python
class GatewayResultSink(ResultSink)
```

Publish to appmgr's authenticated Unix-domain result gateway.

appmgr injects the socket, app id, random instance id and generation.  The
first NDJSON record is a hello; the gateway validates it against the
pre-READY PID registry and acknowledges it.  Subsequent records are result
envelopes.  A bounded latest-wins writer queue keeps a slow/restarting
gateway off the inference thread.

``host`` and ``port`` are accepted for signature compatibility with
:class:`WsResultSink`; they are intentionally ignored.

### kit.adapters.result_sink.GatewayResultSink.emit

```python
def emit(self, payload: dict, pts: float) -> None
```

将 legacy payload 与秒级 pts 封装并排队交给 AppMgr，返回 None；生产者不要靠自建 socket 绕过此路由。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/result_sink.py#L762)

### kit.adapters.result_sink.GatewayResultSink.emit_meta

```python
def emit_meta(self, payload: dict) -> None
```

按该 sink 的能力处理配置/元信息；不是一次模型推理，也不证明前端已经收到配置。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/result_sink.py#L776)

### kit.adapters.result_sink.GatewayResultSink.emit_checked

```python
def emit_checked(self, payload: dict, pts: float) -> None
```

Publish and surface synchronous acceptance failures.

Historical :meth:`emit` implementations are intentionally best-effort
so one telemetry/output backend cannot stop an inference loop.  Typed
callers that must not mistake a local rejection for success use this
additive method.  The default delegates to ``emit``; sinks that hide
native/fan-out failures override it.

此方法定义于基类 `ResultSink`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/result_sink.py#L232)

### kit.adapters.result_sink.GatewayResultSink.request_recording

```python
def request_recording(self, event_kind: str, pts: float) -> bool
```

提交 recording_request（event_kind、秒级 pts、递增 seq）并返回本地队列是否接受的 bool；AppMgr 仍会校验录像授权。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/result_sink.py#L711)

### kit.adapters.result_sink.GatewayResultSink.set_frame_size

```python
def set_frame_size(self, w: int, h: int) -> None
```

设置后续结果的原始画面宽、高（像素），供坐标换算/消息封装使用；本身不发送结果。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/result_sink.py#L753)

### kit.adapters.result_sink.GatewayResultSink.set_frame_size_checked

```python
def set_frame_size_checked(self, w: int, h: int) -> None
```

Set geometry while surfacing synchronous backend failures.

此方法定义于基类 `ResultSink`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/result_sink.py#L260)

### kit.adapters.result_sink.GatewayResultSink.on_config_reload

```python
def on_config_reload(self, config: dict) -> None
```

Live-apply a config change (SIGHUP) to this sink.

`App._maybe_reload` calls this on the app's sink with the freshly
re-read effective config, so a sink whose behaviour is config-driven
(ConfigurableSink: output filters / formatter template) picks up
apply:"live" changes without a restart -- for EVERY app, whatever loop
shape it uses. Default: no-op; sinks with no config react to nothing.

此方法定义于基类 `ResultSink`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/result_sink.py#L265)

### kit.adapters.result_sink.GatewayResultSink.stats

```python
def stats(self) -> dict
```

返回 sent/dropped/send_error/queued/backend 统计字典；sent 是本地传输计数，不是消费者确认。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/result_sink.py#L782)

### kit.adapters.result_sink.GatewayResultSink.close

```python
def close(self) -> None
```

关闭本对象持有的连接/线程/设备等资源。具体幂等性、在途任务及失败处理见该类生命周期说明；不要在关闭后继续发送或读取。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/result_sink.py#L787)

### kit.adapters.result_sink.GatewayResultSink.__enter__

```python
def __enter__(self)
```

进入上下文管理器，返回其受管对象；与 __exit__ 配对使用，避免异常路径遗留资源。

此方法定义于基类 `ResultSink`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/result_sink.py#L290)

### kit.adapters.result_sink.GatewayResultSink.__exit__

```python
def __exit__(self, *exc)
```

离开上下文并执行本类的清理方法；异常传播/清理失败语义见类说明，不把退出视作任务已完成。

此方法定义于基类 `ResultSink`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/result_sink.py#L293)

### kit.adapters.result_sink.GatewayResultSink.__init__

```python
def __init__(self, host: str='127.0.0.1', port: int=8124, app_id: str='app', preserve_envelope: bool=False, *, sock: Optional[str]=None, queue_size: int=64, connect_timeout: float=5.0, send_timeout: float=0.25, **_ignored)
```

构造实例并保存/校验上述参数；参数默认值见签名。是否在构造时打开设备或加载模型，以本类的生命周期说明为准；构造方法返回 None。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/result_sink.py#L573)

### kit.adapters.result_sink.GatewayResultSink.publish_envelope

```python
def publish_envelope(self, envelope: dict) -> None
```

把已构造的完整结果 envelope 排入受管网关发送队列；不得伪造 AppMgr 注入的身份字段。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/result_sink.py#L757)

## kit.adapters.result_sink.MultiSink

```python
class MultiSink(ResultSink)
```

Fan-out sink: forward every emit() to a list of child sinks.

Lets one app run publish results to several backends at once -- e.g. the
/appcenter overlay WS *and* an MQTT/Home-Assistant broker -- without the
app or the base loop knowing there is more than one. Each child is isolated:
a raising/slow child never blocks or breaks the others (WsResultSink and
MqttSink are both already best-effort internally, so this is belt-and-braces).

### kit.adapters.result_sink.MultiSink.emit

```python
def emit(self, payload: dict, pts: float) -> None
```

向组合的各个 sink 分发 payload 和 pts，返回 None；每条下游的可用性/错误处理遵循其自身契约。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/result_sink.py#L829)

### kit.adapters.result_sink.MultiSink.emit_meta

```python
def emit_meta(self, payload: dict) -> None
```

按该 sink 的能力处理配置/元信息；不是一次模型推理，也不证明前端已经收到配置。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/result_sink.py#L884)

### kit.adapters.result_sink.MultiSink.emit_checked

```python
def emit_checked(self, payload: dict, pts: float) -> None
```

Attempt every child and report any synchronous failure.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/result_sink.py#L861)

### kit.adapters.result_sink.MultiSink.request_recording

```python
def request_recording(self, event_kind: str, pts: float) -> bool
```

将显式录像请求委托给支持此能力的下游，返回是否有下游接受的 bool；不表示磁盘已生成录像。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/result_sink.py#L875)

### kit.adapters.result_sink.MultiSink.set_frame_size

```python
def set_frame_size(self, w: int, h: int) -> None
```

设置后续结果的原始画面宽、高（像素），供坐标换算/消息封装使用；本身不发送结果。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/result_sink.py#L891)

### kit.adapters.result_sink.MultiSink.set_frame_size_checked

```python
def set_frame_size_checked(self, w: int, h: int) -> None
```

Attempt every child geometry update and aggregate failures.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/result_sink.py#L899)

### kit.adapters.result_sink.MultiSink.on_config_reload

```python
def on_config_reload(self, config: dict) -> None
```

把配置热更新传递给支持回调的下游 sink，返回 None。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/result_sink.py#L913)

### kit.adapters.result_sink.MultiSink.stats

```python
def stats(self) -> dict
```

Per-child stats keyed by child class name (only children that report
anything). Lets an app read delivery diagnostics through the fan-out
without knowing which concrete sinks are behind it.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/result_sink.py#L934)

### kit.adapters.result_sink.MultiSink.close

```python
def close(self) -> None
```

关闭本对象持有的连接/线程/设备等资源。具体幂等性、在途任务及失败处理见该类生命周期说明；不要在关闭后继续发送或读取。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/result_sink.py#L951)

### kit.adapters.result_sink.MultiSink.__enter__

```python
def __enter__(self)
```

进入上下文管理器，返回其受管对象；与 __exit__ 配对使用，避免异常路径遗留资源。

此方法定义于基类 `ResultSink`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/result_sink.py#L290)

### kit.adapters.result_sink.MultiSink.__exit__

```python
def __exit__(self, *exc)
```

离开上下文并执行本类的清理方法；异常传播/清理失败语义见类说明，不把退出视作任务已完成。

此方法定义于基类 `ResultSink`。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/result_sink.py#L293)

### kit.adapters.result_sink.MultiSink.__init__

```python
def __init__(self, sinks: List[ResultSink])
```

构造实例并保存/校验上述参数；参数默认值见签名。是否在构造时打开设备或加载模型，以本类的生命周期说明为准；构造方法返回 None。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/result_sink.py#L826)

### kit.adapters.result_sink.MultiSink.client_count

```python
def client_count(self) -> int
```

返回此 sink/channel 当前可报告的客户端数；零或非零都不证明某条业务结果已被远端消费。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/result_sink.py#L923)

## kit.adapters.result_sink.OutputMessage

```python
@dataclass(frozen=True)
class OutputMessage
```

One formatted message ready for a channel to publish.

`topic` is None for channels that carry their own destination (WS, or an
MQTT channel falling back to its default state topic); formatters that
target specific MQTT topics (mapping rows, HA discovery) set it explicitly.

### 字段 / 默认值

`@dataclass` 未显式定义构造器时，由下列字段生成；无默认值的字段必填。

```python
body: bytes
topic: Optional[str] = None
content_type: str = 'application/json'
retain: bool = False
metadata: dict = field(default_factory=dict)
```

## kit.adapters.result_sink.OutputChannel

```python
class OutputChannel(ABC)
```

A transport (ws/mqtt/http/uart). Best-effort; never raises into emit().

### 字段 / 默认值

`@dataclass` 未显式定义构造器时，由下列字段生成；无默认值的字段必填。

```python
name: str = 'channel'
```

### kit.adapters.result_sink.OutputChannel.publish

```python
@abstractmethod
def publish(self, message: 'OutputMessage') -> None
```

输出通道基类协议，具体子类负责传输 OutputMessage；不要直接把基类当作可用通道。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/result_sink.py#L1000)

### kit.adapters.result_sink.OutputChannel.client_count

```python
def client_count(self) -> int
```

返回此 sink/channel 当前可报告的客户端数；零或非零都不证明某条业务结果已被远端消费。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/result_sink.py#L1003)

### kit.adapters.result_sink.OutputChannel.close

```python
def close(self) -> None
```

关闭本对象持有的连接/线程/设备等资源。具体幂等性、在途任务及失败处理见该类生命周期说明；不要在关闭后继续发送或读取。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/result_sink.py#L1006)

## kit.adapters.result_sink.OutputFormatter

```python
class OutputFormatter(ABC)
```

Turns one canonical envelope into zero+ OutputMessages for a channel.

### kit.adapters.result_sink.OutputFormatter.format

```python
@abstractmethod
def format(self, envelope: dict, *, channel: str) -> List['OutputMessage']
```

将规范结果 envelope 按此 formatter 规则编码为 OutputMessage 列表（含 body/topic 等通道负载）；channel 可用于通道筛选。格式化本身不建立网络连接。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/result_sink.py#L1014)

### kit.adapters.result_sink.OutputFormatter.on_channel_ready

```python
def on_channel_ready(self, channel: 'OutputChannel') -> List['OutputMessage']
```

Messages to publish when a channel (re)connects -- e.g. HA discovery
+ retained `online`. Default: nothing.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/result_sink.py#L1017)

## kit.adapters.result_sink.open_result_sink

```python
def open_result_sink(kind: str='ws', **kw) -> ResultSink
```

Factory. `kind` = "ws" (broadcast) | "stdout" (debug).

Delegates to the capability registry, which probes for the official R2
result ingress and returns an `OsdInjectResultSink` when present. On today's
firmware there is no official ingress, so the registry falls back to the
workaround sink selected by `kind` and behaviour is unchanged. The "stdout"
debug sink is always honoured verbatim.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/adapters/result_sink.py#L1023)
