# kit.diagnostics

[API 索引](../index.md) · [接口特性与边界](../features.md)

源码基线：[kit/diagnostics.py](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/diagnostics.py)；签名由 AST 提取，不导入硬件依赖。

结构化日志、敏感 URL 脱敏及重复告警限流；日志级别不等同于应用健康状态。

源码说明中的历史验证记录、迁移目标和旧示例不代表当前固件可用；应用入口、资源准入和接口可用性以本页补充及接口特性文档为准。

## 模块契约（源码）

Logging helpers shared by the public reCamera Python interfaces.

The module deliberately is not named ``logging``.  ``kit/run.py`` also
supports direct path execution, where its own directory is first on
``sys.path``; a sibling named ``logging.py`` would then shadow Python's
standard-library module before the package bootstrap can run.

Importing a library must never call :func:`logging.basicConfig` or replace the
application's handlers.  The kit therefore installs only a ``NullHandler`` and
leaves configuration to ``kit.run`` or to the embedding application.

## 常量与类型别名

以下为该版本源码值；默认地址不等于所有固件都开放该服务。

```python
LOGGER_NAME = 'recamera'
```


## kit.diagnostics.get_logger

```python
def get_logger(name: str='') -> logging.Logger
```

Return a namespaced logger without changing global logging state.

``get_logger("media.rga")`` returns ``recamera.media.rga``.  Passing an
already-qualified name is also supported.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/diagnostics.py#L45)

## kit.diagnostics.configure_logging

```python
def configure_logging(level: int | str=logging.INFO, stream: Optional[IO[str]]=None) -> logging.Handler
```

Configure the ``recamera`` logger for a command-line application.

The function is idempotent: a handler previously created by this function
is updated rather than duplicated.  Other handlers installed by the host
application are left untouched.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/diagnostics.py#L69)

## kit.diagnostics.redact_url

```python
def redact_url(value: str) -> str
```

Remove credentials and secret query values from a URL for logging.

The scheme, host, port, path, and non-secret query parameters are retained
so the resulting diagnostic remains actionable.  Malformed/non-URL text is
handled conservatively by masking ``user:password@`` patterns.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/diagnostics.py#L103)

## kit.diagnostics.WarningLimiter

```python
class WarningLimiter
```

Thread-safe limiter for repetitive warning messages.

Embedded media loops can encounter the same recoverable fault every frame.
``warning(key, ...)`` logs the first ``limit`` occurrences and then emits a
single suppression notice.  Counters remain queryable for health metrics.

### kit.diagnostics.WarningLimiter.__init__

```python
def __init__(self, logger: logging.Logger, limit: int=3) -> None
```

构造实例并保存/校验上述参数；参数默认值见签名。是否在构造时打开设备或加载模型，以本类的生命周期说明为准；构造方法返回 None。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/diagnostics.py#L144)

### kit.diagnostics.WarningLimiter.warning

```python
def warning(self, key: str, message: str, *args, **kwargs) -> int
```

Record and conditionally log one occurrence; return total count.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/diagnostics.py#L152)

### kit.diagnostics.WarningLimiter.count

```python
def count(self, key: str) -> int
```

Return the total number of occurrences recorded for ``key``.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/diagnostics.py#L169)
