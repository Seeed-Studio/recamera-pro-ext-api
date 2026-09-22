# kit.run

[API 索引](../index.md) · [接口特性与边界](../features.md)

源码基线：[kit/run.py](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/run.py)；签名由 AST 提取，不导入硬件依赖。

AppMgr 的 Python 入口加载器与 CLI。用于 host smoke／启动契约检查，业务 App 继承 App，不直接重写启动器。

源码说明中的历史验证记录、迁移目标和旧示例不代表当前固件可用；应用入口、资源准入和接口可用性以本页补充及接口特性文档为准。

## 模块契约（源码）

kit.run -- the ONE launcher for a reCamera Pro app (internal/KIT_APP_SHAPE_SPEC.md §5.1).

    python3 -m kit.run <app_dir|app.py> [--model ... --sink ... --port ...]
    python3 /userdata/local/kit/kit/run.py <app_dir>        # no PYTHONPATH needed

Why this file exists
--------------------
Every app.py used to carry ~40 identical lines that guessed where `kit/` lives
(KIT_PARENT / KIT_DIR env, `..`, `../..`, `/userdata/local/apps`, ...) and
pushed it onto sys.path. That knowledge is DEPLOYMENT LAYOUT, not application
logic, and 9 apps had 9 byte-identical copies of it. It is also dead in
production: `market/appmgr/supervisor.py:start()` already exports PYTHONPATH +
KIT_PARENT before exec'ing the app. It only ever served "developer ssh's onto
the device and runs the app by hand".

So the knowledge moves HERE, to one place that cannot be wrong: this module is
`<KIT_PARENT>/kit/run.py`, therefore KIT_PARENT is two dirname() calls up. No
probing, no env vars, no device fallbacks. That derivation runs before any
`kit.*` import below, so `python3 path/to/kit/run.py` works with an empty
PYTHONPATH, and `python3 -m kit.run` (how appmgr launches apps) works too.

What it does
------------
1. put KIT_PARENT on sys.path (from this file's own location);
2. put the APP DIR on sys.path, so an app may `import` its own sibling modules;
3. import `<app_dir>/<entry>` (entry from manifest.json, default `app.py`)
   under a unique module name -- NOT `__main__`, so the app's own
   `if __name__ == "__main__": run_app(...)` tail stays inert here;
4. find the single `kit.app.App` subclass defined in it;
5. hand the remaining argv to `kit.app.run_app` -- identical CLI to before
   (`--model / --sink / --port / --source / --url / --quiet / ...`).

`python3 app.py` keeps working unchanged when kit is already importable (that
tail is still there, and appmgr's PYTHONPATH still makes it resolvable).

## 常量与类型别名

以下为该版本源码值；默认地址不等于所有固件都开放该服务。

```python
KIT_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
```


```python
USAGE = 'usage: python3 -m kit.run <app_dir|app.py> [app options]\n       (app options are the usual --model/--sink/--port/--source/--url/--quiet/...; run with an app and --help to list them)\n'
```


## kit.run.RunError

```python
class RunError(ConfigurationError)
```

Bad target / unloadable app -- reported as a one-line error, not a traceback.

继承接口：[kit.errors.ConfigurationError](kit-errors.md)。

## kit.run.resolve_entry

```python
def resolve_entry(target: str) -> Tuple[str, str]
```

`target` (an app dir OR an entry file) -> (app_dir, entry_path).

For a directory we honour the manifest's `entry` field (same contract the
supervisor uses), defaulting to `app.py`.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/run.py#L68)

## kit.run.load_app_module

```python
def load_app_module(entry_path: str)
```

Import the app's entry file with its own directory on sys.path.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/run.py#L110)

## kit.run.find_app

```python
def find_app(mod) -> App
```

Return the App INSTANCE this module wants run.

Preference order:
  1. an explicit `APP` attribute (instance or class) -- the escape hatch;
  2. the single App subclass DEFINED in this module;
  3. the single App subclass visible in it (covers a re-exported class).
Several leaf candidates is an error, not a coin flip.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/run.py#L132)

## kit.run.main

```python
def main(argv: Optional[List[str]]=None) -> int
```

解析应用启动参数并运行 Kit 入口；由 AppMgr/命令行调用，不在 App.run 内重复调用。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/run.py#L172)
