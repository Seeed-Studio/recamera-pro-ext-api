# kit.config

[API 索引](../index.md) · [接口特性与边界](../features.md)

源码基线：[kit/config.py](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/config.py)；签名由 AST 提取，不导入硬件依赖。

manifest、用户配置、schema 展平和有效配置解析；应用路径来自 AppMgr 上下文，避免按 __file__ 猜安装根目录。

源码说明中的历史验证记录、迁移目标和旧示例不代表当前固件可用；应用入口、资源准入和接口可用性以本页补充及接口特性文档为准。

## 模块契约（源码）

config.py -- unified app configuration loading for the reCamera Pro Kit.

Single source of truth for an app's *effective* configuration:

    effective config = manifest.config_schema defaults
                       overlaid by  <appdata>/<id>/config.json  (user settings,
                                    legacy fallback: <app_dir>/config.json)
                       overlaid by  explicit CLI overrides (manual --conf/--iou)

Before this module each app duplicated a `_flatten_schema()` helper and re-read
its own manifest.json (fall-detection) or an env-named JSON file (retail-vision's
RETAIL_CONFIG). That is now one code path here, so the /appcenter parameter panel
can write config.json and every app picks the change up identically.

Backward compatible: no config.json == use manifest defaults == the old
behaviour. The appmgr side has its own stdlib-only mirror of the flatten/validate
logic in market/appmgr/config.py (appmgr must not import the kit package).

## kit.config.schema_items

```python
def schema_items(manifest: Optional[dict]) -> Dict[str, dict]
```

Return {key: item_spec} from a manifest `config_schema`.

★Canonical form is GROUPED★: `config_schema.groups[].items[] = {key, type,
default, ...}`. All in-repo apps use it and the frontend SchemaForm renders
by group, so there is exactly one main code path here.

A legacy FLAT `config_schema[key] = {type, default, ...}` (no `groups`) is
still accepted for third-party packages built before the unification; it is
normalised to the grouped form by `_flat_to_grouped` and logged once per
process. Support for it will be dropped -- publish grouped.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/config.py#L28)

## kit.config.flatten_schema

```python
def flatten_schema(manifest: dict) -> Dict[str, Any]
```

Return {key: default} for every schema item that declares one.

Items with no "default" (e.g. retail zone/line controls) are omitted -- they
only exist in config.json once the user draws them.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/config.py#L68)

## kit.config.load_manifest

```python
def load_manifest(app_dir: str) -> dict
```

Read <app_dir>/manifest.json, or {} if missing/corrupt.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/config.py#L78)

## kit.config.appdata_root

```python
def appdata_root() -> str
```

Root of the user-data tree that survives app upgrades.

Mirrors appmgr's paths.APPDATA_DIR (same env var, same default). Read at
call time so a test / a manually launched app can redirect it.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/config.py#L87)

## kit.config.app_id_of_dir

```python
def app_id_of_dir(app_dir: str, manifest: Optional[dict]=None) -> str
```

The app id owning `app_dir`: manifest `id` if sane, else the dir name.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/config.py#L96)

## kit.config.user_config_path

```python
def user_config_path(app_dir: str, manifest: Optional[dict]=None) -> str
```

Where this app's user config lives.

Canonical: <appdata_root>/<id>/config.json -- OUTSIDE the install dir, so an
app upgrade (which swaps /userdata/local/apps/<id>/ wholesale) can no longer
delete the user's settings. Falls back to the legacy in-app path while that
file still exists (appmgr migrates it on the next read/write/install; the kit
side only ever READS, it never moves files from the app process).

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/config.py#L106)

## kit.config.load_user_config

```python
def load_user_config(app_dir: str, manifest: Optional[dict]=None) -> Dict[str, Any]
```

Read the user's config.json overrides, or {} if absent/corrupt.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/config.py#L123)

## kit.config.effective_config

```python
def effective_config(app_dir: str, manifest: Optional[dict]=None) -> Dict[str, Any]
```

Merge manifest defaults with the user's config.json (config.json wins).

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/config.py#L133)

## kit.config.app_dir_of

```python
def app_dir_of(app) -> str
```

Best-effort install directory of a running App instance.

The app object's class lives in the app's app.py, so its module __file__
directory IS the install dir (where manifest.json / config.json sit). Falls
back to CWD (appmgr launches each app with cwd=app_dir).

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/config.py#L143)
