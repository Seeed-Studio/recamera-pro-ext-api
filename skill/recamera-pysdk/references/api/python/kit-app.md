# kit.app

[API 索引](../index.md) · [接口特性与边界](../features.md)

源码基线：[kit/app.py](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/app.py)；签名由 AST 提取，不导入硬件依赖。

应用中心推荐入口：App 子类 owns_loop=True、run(self)，经 AppMgr 生命周期运行。包含模型注册、前处理、emit、显式录像和配置热更新。

源码说明中的历史验证记录、迁移目标和旧示例不代表当前固件可用；应用入口、资源准入和接口可用性以本页补充及接口特性文档为准。

## 模块契约（源码）

App base class for the reCamera Pro Kit (see docs/guide/kit-design.md §3).

An application is a thin subclass that OWNS ITS LOOP:

    owns_loop = True
    def setup(self, config):    -- optional: derive objects from the already
                                   auto-bound config_schema params
    def run(self):              -- ★the whole pipeline, as ordinary Python★
        for frame in self.frames():
            x = self.pre(frame)
            outs = self.models.det.infer(x.data)
            ...
            self.emit(events, frame.pts, results=results)

Everything else -- opening the frame source, skipping the camera's grey warm-up
placeholder frames, NPU warm-up, model loading, SIGHUP config hot-reload,
publishing via ResultSink and the FPS / latency debug metrics -- lives here and
is never re-implemented per app.

The pre-migration callback shape (a base `run(model_path, ...)` loop dispatching
to `on_results` / `run_postproc` / `process_frame`) was removed once all apps
migrated; a frozen copy survives as the equivalence-gate oracle in
kit/tests/legacy_loop.py.

Import convention: `kit` is a package. The directory that CONTAINS `kit/` is on
sys.path (the appmgr and each app's bootstrap add it), so kit modules import each
other as `kit.adapters.*` / `kit.runtime.*`. This avoids the app.py/kit.app name
collision that a bare `app` module would cause.

## 常量与类型别名

以下为该版本源码值；默认地址不等于所有固件都开放该服务。

```python
BUILTIN_CLASS_TABLES: Dict[str, List[str]] = {'coco80': list(COCO80), 'coco': list(COCO80)}
```


## kit.app.PreparedInput

```python
@dataclass
class PreparedInput
```

What `App.pre(frame)` returns: model-space pixels + the letterbox map.

`.data` is a uint8 HWC (or 1HWC) RGB array ready for `.infer()`; `.info` is
a `LetterboxInfo`-compatible object post-processing uses to map coordinates
back to ORIGINAL camera geometry.

### 字段 / 默认值

`@dataclass` 未显式定义构造器时，由下列字段生成；无默认值的字段必填。

```python
data: Any
info: Any
```

### kit.app.PreparedInput.__iter__

```python
def __iter__(self)
```

返回本对象定义的迭代器；迭代元素与借用有效期见类说明。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/app.py#L90)

## kit.app.ModelRegistry

```python
class ModelRegistry
```

`self.models` -- attribute / index access to the manifest's `models[]`.

Every model is reachable by its manifest `id`. Convenience aliases are added
when unambiguous: the model's `task` (e.g. `.detect`), a short form of it
(`.det`, `.rec`, ...), and -- for a single-model app -- `.model`/`.first`.
An alias claimed by two models is dropped rather than resolved arbitrarily.

### kit.app.ModelRegistry.__init__

```python
def __init__(self) -> None
```

构造实例并保存/校验上述参数；参数默认值见签名。是否在构造时打开设备或加载模型，以本类的生命周期说明为准；构造方法返回 None。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/app.py#L197)

### kit.app.ModelRegistry.__getitem__

```python
def __getitem__(self, key)
```

通过整数下标选择声明顺序中的模型，或通过字符串选择 manifest id/唯一别名；越界或未知、歧义别名分别抛 IndexError/AttributeError。属性形式 self.models.<id> 使用同样的别名规则。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/app.py#L237)

### kit.app.ModelRegistry.__len__

```python
def __len__(self) -> int
```

返回当前注册模型数（manifest 声明顺序），不代表已初始化或可并行推理的数量。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/app.py#L242)

### kit.app.ModelRegistry.__iter__

```python
def __iter__(self)
```

返回本对象定义的迭代器；迭代元素与借用有效期见类说明。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/app.py#L245)

## kit.app.resolve_class_names

```python
def resolve_class_names(spec: Any, app_dir: Optional[str]=None, *, who: str='app') -> Optional[List[str]]
```

Resolve a manifest `models[].classes` declaration into a label list.

Three accepted shapes (RENDER_DECLARATION_SPEC §5 P0-3):

  1. built-in table name -- ``"coco80"``
  2. literal array       -- ``["cat", "dog"]``
  3. in-package file     -- ``"models/labels.txt"`` (one label per line,
                            ``#`` comments and blank lines ignored) or a
                            ``.json`` file holding an array of strings.

Returns ``None`` when there is nothing to resolve (``spec`` absent) or when
resolution FAILS -- failures are logged and the caller keeps its previous
value, so a typo in a manifest can never stop an app from starting.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/app.py#L378)

## kit.app.effective_render

```python
def effective_render(manifest: Optional[dict], config: Optional[Dict[str, Any]]) -> Optional[dict]
```

Merge `manifest.render` with the running config -> the EFFECTIVE block.

Returns None when the app declares nothing, so `emit()` leaves the key out
entirely and the front end keeps its shape-driven fallback (§3, backward
compatible). A None-valued config item is ignored, same rule as everywhere
else in the kit: a cleared field must not wipe a declared default.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/app.py#L519)

## kit.app.App

```python
class App
```

Base application. Subclass with `owns_loop = True` and define `run()`.

### 字段 / 默认值

`@dataclass` 未显式定义构造器时，由下列字段生成；无默认值的字段必填。

```python
id: str = 'app'
name: str = 'App'
postproc: str = 'detect'
input_size: int = 640
class_names: Sequence[str] = COCO80
owns_loop: bool = False
needs_model: bool = True
needs_frames: bool = True
model_frame: str = 'cpu'
model_dma_input: bool = False
```

### kit.app.App.__init__

```python
def __init__(self) -> None
```

构造实例并保存/校验上述参数；参数默认值见签名。是否在构造时打开设备或加载模型，以本类的生命周期说明为准；构造方法返回 None。

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/app.py#L621)

### kit.app.App.setup

```python
def setup(self, config: Dict[str, Any]) -> None
```

Read config_schema parameters. Override + call super().setup(config).

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/app.py#L666)

### kit.app.App.prepare_runtime

```python
def prepare_runtime(self) -> None
```

Prepare app-owned runtime resources before appmgr receives READY.

``setup()`` runs before the kit publishes ``self._rt``.  Applications
that need the resolved source URL, sink or other runtime options can
override this later hook instead.  It is still part of ``start()``'s
rollback transaction: if it raises, :meth:`finish` runs and appmgr never
observes a false-ready process.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/app.py#L672)

### kit.app.App.on_config_reload

```python
def on_config_reload(self, config: Dict[str, Any]) -> None
```

★Live config hot-reload hook★ (SIGHUP -> re-read config.json).

Called from the main loop when appmgr signals a LIVE-only config change.
`config` is the freshly re-read effective config (manifest defaults
overlaid by the updated config.json).

Base default: reapply ONLY the base-managed live knobs (conf/iou) and
refresh self.config. This is deliberately safe -- it just replaces
values, and NEVER rebuilds the model, frame source, or any pipeline
state. Subclasses that snapshot extra params into their own attributes
(e.g. self.max_faces, thresholds, ROI geometry) override this to reapply
those the same value-replacing way -- use the `_reload_float/_reload_int`
helpers above. Anything structural (model swap, input_size, backend,
buffer resize) must NOT be hot-reloaded -- those params are
apply:"restart" in the manifest and never reach here.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/app.py#L707)

### kit.app.App.on_params_changed

```python
def on_params_changed(self, changed: set) -> None
```

Called after SIGHUP re-bound the apply:"live" params onto `self`.

`changed` is the set of keys whose value actually differs. Override only
when a derived object must be rebuilt (state machine, cached geometry).
Plain scalar knobs need nothing -- they are already re-bound.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/app.py#L846)

### kit.app.App.start

```python
def start(self, model_path: Optional[str]=None, *, source: str='ffmpeg', url: str=DEFAULT_SUB_STREAM, sink: Optional[ResultSink]=None, n: int=0, every: int=1, skip_gray_std: float=8.0, max_gray_skip: int=120, verbose: bool=True, app_dir: Optional[str]=None, manifest: Optional[dict]=None, config: Optional[Dict[str, Any]]=None) -> 'App'
```

Prepare the kit-owned runtime for a new-shape `run()`.

Loads the manifest's `models[]` (paths made absolute against the install
dir), binds `config_schema` params onto `self`, opens the frame source and
installs the SIGHUP handler. `run_app` calls this; `finish()` tears it down.
`model_path` (the `--model` CLI flag) overrides the FIRST manifest model.

Startup is a transaction: a failure at any stage invokes ``finish``
before the original exception is re-raised.  This includes BaseException
control flow so SIGTERM/KeyboardInterrupt cannot strand an earlier model
or source acquired by this same attempt.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/app.py#L910)

### kit.app.App.verbose

```python
@property
def verbose(self) -> bool
```

The `--quiet`-derived verbosity `start()` was given (True by default).

Available to any loop-owning `run()`; the removed callback loop took it as an
argument instead.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/app.py#L1126)

### kit.app.App.source_url

```python
@property
def source_url(self) -> Optional[str]
```

The `--url` value `start()` was given, or None before start().

Frame-driven apps never need it (kit already opened the source with it);
an app that owns its own input (voice-transcribe's RTSP audio-track
demux) reads the same CLI knob from here instead of re-parsing argv.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/app.py#L1136)

### kit.app.App.finish

```python
def finish(self) -> None
```

Release every acquired resource exactly once.

Models are detached even when ``_rt`` was never established, which is
the normal shape of a second-model or setup failure.  Runtime state is
detached before invoking user/vendor cleanup so repeated calls remain
idempotent even if one cleanup callback raises.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/app.py#L1146)

### kit.app.App.frames

```python
def frames(self) -> Iterator[Frame]
```

Yield frames to the app's `run()` loop, kit-managed.

What it does for you (spec §2, and §8's "say what it hides"):
  * opens/owns the frame source (`start()`), releases each frame by
    simply advancing the iterator -- do NOT hold a frame past one turn;
  * skips the camera's grey warm-up placeholder frames;
  * honours `--every N` frame skipping;
  * applies pending SIGHUP config hot-reloads at the frame boundary;
  * consumes the FIRST real frame as a model warm-up: kit runs
    `pre()` + one `infer()` on the primary model itself and does NOT
    yield the frame, so your loop body -- and any cross-frame state it
    carries -- starts on the SECOND real frame. This is exactly what
    the pre-migration loop did (it warmed the NPU, then `continue`d
    before the business-logic callback), which is what keeps a stateful
    app's tracker/dwell/window identical across the migration;
  * measures the complete loop-body budget and flushes the periodic
    `metrics` meta event (`loop` includes work after emit; pre/infer/
    emit are measured by kit, the remainder is `app`);
  * stops after `--n` processed frames.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/app.py#L1218)

### kit.app.App.pre

```python
def pre(self, frame: Frame) -> PreparedInput
```

Produce the model input for `frame` (letterbox to the manifest size).

Prefers what the frame source already did on RGA -- `frame.model_data`
(hw) or a `frame.model_info`-annotated `frame.data` (hw-direct) -- and
only falls back to the Python letterbox. Geometry is identical in every
case; `.info` always maps back to ORIGINAL camera pixels.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/app.py#L1386)

### kit.app.App.crop_roi_hw

```python
def crop_roi_hw(self, frame: Frame, box, out_size: int, pad: float=0.25)
```

Crop a padded square ROI around `box`, preferring hardware (RGA).

The cascade counterpart to `pre()`: where `pre()` gives stage-1 its
model image, this gives a stage-2 model its per-object ROI. Under
``model_frame = "hw-roi"`` the frame carries a `roi_cropper` bound to the
camera's NV12 dma-buf, so the ROI is cropped + resized on RGA WITHOUT
first converting a full-resolution RGB frame -- the saving the plain
"hw" mode could not realise. Every other mode (cpu/hw/hw-direct,
RTSP/snapshot, or after an RGA latch-off) has no cropper, so this simply
calls the numpy `crop_square_roi` on `frame.data`.

Returns ``(roi_uint8_HWC_RGB, roi_map)`` -- the SAME contract as
`kit.pipeline.crop_square_roi`, so the ROI and its coordinate mapping are
drop-in interchangeable. Timed into the app's `pre` budget.

★Correctness note★ the hardware and numpy crops share ONE geometry helper
(`kit.pipeline.square_roi_geometry`), so `roi_map` is byte-identical; the
pixels differ only by resampling (RGA 2-tap vs PIL antialiased) and by
the out-of-frame border fill (RGA gray 114 vs numpy edge-replicate), the
same family of differences documented for "hw" in
docs/guide/hw-preprocess.md. Perf gain is device-measured; see that doc.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/app.py#L1411)

### kit.app.App.request_recording

```python
def request_recording(self, event_kind: str, ts: Optional[float]=None) -> bool
```

Request one recording after the app's own business decision.

Only the authenticated managed gateway can carry this command. The
installed manifest must authorize ``event_kind``. True means queued
locally, not that Vigil accepted or completed a recording. This never
publishes a display event and ordinary ``emit`` never requests recording.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/app.py#L1480)

### kit.app.App.emit

```python
def emit(self, events=None, ts: Optional[float]=None, *, results=None, geometry=None, extra: Optional[Dict[str, Any]]=None) -> None
```

Publish one frame's output through the manifest-configured sinks.

`events` are the app-level events; `results` (optional) are the raw
per-frame detections/records that the /appcenter overlay and the
manifest `output` field mappings read as `results[]`. ``geometry`` is a
list (or :class:`kit.geometry.GeometryBuilder`) of validated canonical
drawing primitives.  Their coordinate space is not trusted from the
payload: managed Result Hub ingress injects it from the installed
manifest. `ts` defaults to the current frame's pts.

During the warm-up frame this is a no-op (same as pre-migration).

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/app.py#L1502)

### kit.app.App.tick

```python
def tick(self) -> None
```

Apply a pending SIGHUP config hot-reload.

`frames()` already ticks at every frame boundary; only an app that takes
over the loop entirely (audio chunks, multi-stream) needs to call this.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/app.py#L1568)

### kit.app.App.run

```python
def run(self) -> None
```

★The app's main loop★. Override with `owns_loop = True`.

The kit calls `start()` -> `run()` -> `finish()`; inside, the app does
its own `for frame in self.frames(): ...` and calls `self.pre()` /
`self.models.<id>.infer()` / `self.emit()`. There is no other shape:
the pre-migration callback loop (`run(model_path, ...)` driving
`run_postproc`/`process_frame`/`on_results`) was removed once all apps
migrated -- see `kit/tests/legacy_loop.py` for the frozen reference.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/app.py#L1577)

## kit.app.run_app

```python
def run_app(app: App, argv: Optional[List[str]]=None) -> None
```

Generic CLI entry an app's app.py calls from __main__.

Wires argparse -> config -> sink -> app.run(). Keeps app.py thin.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/app.py#L1612)
