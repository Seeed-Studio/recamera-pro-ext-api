"""
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
"""
from __future__ import annotations

import argparse
import inspect
import json
import os
import signal
import sys
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterator, List, Optional, Sequence

import numpy as np

from kit.adapters.frame_source import open_frame_source, DEFAULT_SUB_STREAM, Frame
from kit.adapters.result_sink import ResultSink, open_result_sink
from kit.diagnostics import get_logger
from kit.errors import AdapterError
from kit.runtime.preprocess import letterbox
from kit.runtime.postprocess.detect import postprocess, COCO80
# NOTE: RknnModel (kit.runtime.engine) is imported LAZILY inside _load_model(),
# not here.
# It does `from rknnlite.api import RKNNLite` at module top, so importing it
# eagerly would force rknnlite onto every interpreter that touches kit.app --
# including CPU-only / audio apps (e.g. voice-transcribe under the sherpa venv
# /userdata/rknnenv, which has no rknnlite). Model-backed vision apps still get
# it the moment start() constructs a model; behaviour there is unchanged.


log = get_logger("app")


class _GracefulStop(BaseException):
    """Internal TERM/INT control flow that broad user catches cannot swallow."""

    def __init__(self, signum: int) -> None:
        self.signum = int(signum)
        super().__init__(f"application stop requested by signal {self.signum}")


# --------------------------------------------------------------------------- #
# App shape (internal/KIT_APP_SHAPE_SPEC.md §1/§2): the app owns an explicit
# `run()` loop and calls back into these kit-owned primitives. This is the ONLY
# shape -- the pre-migration `run(model_path, ...)` callback loop is gone (frozen
# copy for the equivalence gates: kit/tests/legacy_loop.py).
# --------------------------------------------------------------------------- #

@dataclass
class PreparedInput:
    """What `App.pre(frame)` returns: model-space pixels + the letterbox map.

    `.data` is a uint8 HWC (or 1HWC) RGB array ready for `.infer()`; `.info` is
    a `LetterboxInfo`-compatible object post-processing uses to map coordinates
    back to ORIGINAL camera geometry.
    """
    data: Any
    info: Any

    def __iter__(self):            # allows `x, info = self.pre(frame)`
        return iter((self.data, self.info))


class _DeferredPreparedInput(PreparedInput):
    def __init__(self, image, info):
        self._image = image
        self.info = info

    @property
    def data(self):
        if hasattr(self, "_data_override"):
            return self._data_override
        return self._image.map()

    @data.setter
    def data(self, value):
        self._data_override = value

    def _prepare(self, descriptor):
        if hasattr(self, "_data_override"):
            return False
        return self._image.prepare(descriptor)


class _ModelHandle:
    """One preloaded model. `infer()` is timed into the owning app's frame budget."""

    def __init__(self, model_id: str, path: str, owner: "App", impl: Any):
        self.id = model_id
        self.path = path
        self._owner = owner
        self._impl = impl
        self._released = False

    def infer(self, x):
        """Run one forward pass. Accepts a raw array or a `PreparedInput`."""
        if isinstance(x, _DeferredPreparedInput):
            prepare_ms = 0.0

            def timed(call, *args):
                nonlocal prepare_ms
                started = time.monotonic()
                try:
                    return call(*args)
                finally:
                    elapsed = time.monotonic() - started
                    prepare_ms += elapsed
                    self._owner._t_pre += elapsed

            started = time.monotonic()
            try:
                infer_prepared = getattr(self._impl, "infer_prepared", None)
                if callable(infer_prepared):
                    return infer_prepared(
                        lambda target: timed(x._prepare, target),
                        lambda: timed(lambda: x.data))
                return self._impl.infer(timed(lambda: x.data))
            finally:
                self._owner._t_infer += max(0.0, time.monotonic() - started - prepare_ms)
        if isinstance(x, PreparedInput):
            x = x.data
        t0 = time.monotonic()
        try:
            return self._impl.infer(x)
        finally:
            self._owner._t_infer += time.monotonic() - t0

    def release(self) -> None:
        if self._released:
            return
        # Mark first so a failing vendor release is never invoked twice by a
        # repeated finish() during exception unwinding.
        self._released = True
        rel = getattr(self._impl, "release", None)
        if rel is not None:
            rel()

    def __repr__(self) -> str:      # pragma: no cover - debug aid
        return f"<model {self.id} {self.path}>"


# task -> extra short aliases, so `self.models.det` works for a manifest model
# declared as {"id": "yolo8n_rawhead_int8", "task": "detect"}.
_TASK_ALIASES = {
    "detect": ("det",),
    "detection": ("det",),
    "recognize": ("rec",),
    "recognition": ("rec",),
    "rec": ("rec",),
    "classify": ("cls",),
    "classification": ("cls",),
    "landmark": ("lmk",),
    "segment": ("seg",),
    "segmentation": ("seg",),
}


class ModelRegistry:
    """`self.models` -- attribute / index access to the manifest's `models[]`.

    Every model is reachable by its manifest `id`. Convenience aliases are added
    when unambiguous: the model's `task` (e.g. `.detect`), a short form of it
    (`.det`, `.rec`, ...), and -- for a single-model app -- `.model`/`.first`.
    An alias claimed by two models is dropped rather than resolved arbitrarily.
    """

    def __init__(self) -> None:
        object.__setattr__(self, "_by_id", {})
        object.__setattr__(self, "_alias", {})
        object.__setattr__(self, "_order", [])
        object.__setattr__(self, "_ambiguous", set())

    # -- construction (kit-internal) ------------------------------------- #
    def _register(self, model_id: str, handle: _ModelHandle, aliases=()) -> None:
        self._by_id[model_id] = handle
        self._order.append(handle)
        for a in aliases:
            if not a or a == model_id or a in self._by_id:
                continue
            if a in self._alias and self._alias[a] is not handle:
                self._ambiguous.add(a)
                self._alias.pop(a, None)
                continue
            if a in self._ambiguous:
                continue
            self._alias[a] = handle

    def _all(self) -> List[_ModelHandle]:
        return list(self._order)

    # -- access ----------------------------------------------------------- #
    def __getattr__(self, name):
        by_id = object.__getattribute__(self, "_by_id")
        if name in by_id:
            return by_id[name]
        alias = object.__getattribute__(self, "_alias")
        if name in alias:
            return alias[name]
        known = sorted(set(list(by_id) + list(alias)))
        if name in object.__getattribute__(self, "_ambiguous"):
            raise AttributeError(
                f"model alias {name!r} is ambiguous (several models claim it); "
                f"use the manifest id: {known}")
        raise AttributeError(
            f"no model {name!r} in manifest models[]; available: {known}")

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._order[key]
        return getattr(self, key)

    def __len__(self) -> int:
        return len(self._order)

    def __iter__(self):
        return iter(self._order)

    def __repr__(self) -> str:      # pragma: no cover - debug aid
        return f"<ModelRegistry {[h.id for h in self._order]}>"


def _schema_items(manifest: Optional[dict]) -> Dict[str, dict]:
    """Return {key: spec} from a manifest `config_schema` (grouped form).

    Thin alias for `kit.config.schema_items` -- the single place that knows the
    schema shape. Kept as a module-level name because the auto-bind code and its
    tests refer to it.
    """
    from kit.config import schema_items
    return schema_items(manifest)


def _coerce(spec_type: Optional[str], value):
    """Best-effort conversion of a config value to its declared schema type.

    Never raises: an unconvertible value is returned unchanged so a typo in
    config.json degrades to "wrong value" rather than "app will not start".
    """
    try:
        if spec_type == "number":
            return float(value)
        if spec_type == "integer":
            return int(value)
        if spec_type == "boolean":
            if isinstance(value, str):
                return value.strip().lower() in ("1", "true", "yes", "on")
            return bool(value)
        if spec_type == "string":
            return value if isinstance(value, str) else str(value)
    except (TypeError, ValueError):
        return value
    return value


_POSITIONAL_KINDS = (inspect.Parameter.POSITIONAL_ONLY,
                     inspect.Parameter.POSITIONAL_OR_KEYWORD,
                     inspect.Parameter.VAR_POSITIONAL)


def _run_takes_positional(fn) -> bool:
    """True when `fn` (an unbound `run`) accepts any positional arg besides self.

    A loop-owning `run()` takes NONE (kit supplies frames via `self.frames()`);
    positional args are the removed pre-migration signature
    (`run(self, model_path, *, source=..., ...)`). Unintrospectable callables are
    reported as positional -- the conservative answer, since it raises rather
    than letting an unknown signature through the shape guard.
    """
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):     # pragma: no cover - exotic callables
        return True
    params = [p for n, p in sig.parameters.items() if n != "self"]
    return any(p.kind in _POSITIONAL_KINDS for p in params)


# The pre-migration callback hooks. Nothing dispatches to them any more; they are
# listed only so a class that still defines one fails loudly instead of having
# its business logic silently never called.
_LEGACY_HOOKS = ("on_results", "run_postproc", "process_frame")


def _check_loop_shape(app: "App") -> None:
    """★Anti-footgun guard★. Raise unless `app` is a well-formed loop-owning app.

    There is exactly ONE app shape (KIT_APP_SHAPE_SPEC §1/§3): `owns_loop = True`
    plus `def run(self)` taking no positional argument. Kit then calls
    `start()` + `run()` + `finish()`.

    `owns_loop` stays an EXPLICIT declaration rather than something sniffed from
    the signature: sniffing used to misread `def run(self, *, debug=False)` and
    silently route the app down a camera loop it never asked for. Every way of
    getting the declaration wrong is a hard startup error:

      * no `owns_loop = True` (whatever `run()` looks like);
      * `owns_loop = True` but `run()` not overridden;
      * `owns_loop = True` on a `run()` that still takes positional args
        (i.e. the removed pre-migration signature);
      * any leftover `on_results` / `run_postproc` / `process_frame` hook --
        nothing calls those, so keeping one means dead business logic.
    """
    cls = type(app)
    fn = getattr(cls, "run", None)
    overridden = fn is not None and fn is not App.run

    if not bool(getattr(cls, "owns_loop", False)):
        raise RuntimeError(
            f"{cls.__name__}: every app must declare `owns_loop = True` and "
            f"define `def run(self): for frame in self.frames(): ...` "
            f"(kit then calls start()/run()/finish()). The pre-migration "
            f"callback shape -- run(self, model_path, ...) driving "
            f"on_results()/run_postproc()/process_frame() -- was removed "
            f"(spec §1/§3).")
    if not overridden:
        raise RuntimeError(
            f"{cls.__name__}: owns_loop = True but run() is not overridden; "
            f"a loop-owning app must define `def run(self): "
            f"for frame in self.frames(): ...` (spec §1)")
    if _run_takes_positional(fn):
        raise RuntimeError(
            f"{cls.__name__}: owns_loop = True but run{inspect.signature(fn)} "
            f"takes positional arguments; a loop-owning run() takes no "
            f"arguments (kit supplies frames via self.frames()) (spec §1)")

    stale = [h for h in _LEGACY_HOOKS if getattr(cls, h, None) is not None]
    if stale:
        raise RuntimeError(
            f"{cls.__name__}: run() cannot be combined with the removed "
            f"callback hook(s) {stale}; nothing dispatches to them any more -- "
            f"move that logic into run() (spec §6)")


# --------------------------------------------------------------------------- #
# manifest `models[].classes` -> class_names  (RENDER_DECLARATION_SPEC §5 P0-3)
# --------------------------------------------------------------------------- #
# Built-in label tables addressable by name from a manifest. Keep the keys
# lowercase; lookup lowercases + strips the declared string.
BUILTIN_CLASS_TABLES: Dict[str, List[str]] = {
    "coco80": list(COCO80),
    "coco": list(COCO80),
}

# A `classes` string is treated as a FILE reference (rather than a built-in
# table name) when it carries one of these suffixes.
_CLASS_FILE_SUFFIXES = (".txt", ".names", ".labels", ".json")


def resolve_class_names(spec: Any, app_dir: Optional[str] = None,
                        *, who: str = "app") -> Optional[List[str]]:
    """Resolve a manifest `models[].classes` declaration into a label list.

    Three accepted shapes (RENDER_DECLARATION_SPEC §5 P0-3):

      1. built-in table name -- ``"coco80"``
      2. literal array       -- ``["cat", "dog"]``
      3. in-package file     -- ``"models/labels.txt"`` (one label per line,
                                ``#`` comments and blank lines ignored) or a
                                ``.json`` file holding an array of strings.

    Returns ``None`` when there is nothing to resolve (``spec`` absent) or when
    resolution FAILS -- failures are logged and the caller keeps its previous
    value, so a typo in a manifest can never stop an app from starting.
    """
    if spec is None:
        return None

    # (2) literal array
    if isinstance(spec, (list, tuple)):
        names = [str(x) for x in spec]
        if not names:
            print(f"[app:{who}] manifest classes: empty array, ignored", flush=True)
            return None
        return names

    if not isinstance(spec, str):
        print(f"[app:{who}] manifest classes: unsupported type "
              f"{type(spec).__name__}, ignored", flush=True)
        return None

    s = spec.strip()
    if not s:
        return None

    # (3) in-package file -- only when it looks like a path/file name.
    if s.lower().endswith(_CLASS_FILE_SUFFIXES) or "/" in s:
        path = _class_file_path(s, app_dir, who)
        if path is None:
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = f.read()
        except OSError as e:
            print(f"[app:{who}] manifest classes: cannot read {s!r} ({e}); "
                  f"keeping default labels", flush=True)
            return None
        if s.lower().endswith(".json"):
            try:
                data = json.loads(raw)
            except ValueError as e:
                print(f"[app:{who}] manifest classes: {s!r} is not valid JSON "
                      f"({e}); keeping default labels", flush=True)
                return None
            if not isinstance(data, list) or not data:
                print(f"[app:{who}] manifest classes: {s!r} must hold a non-empty "
                      f"JSON array; keeping default labels", flush=True)
                return None
            return [str(x) for x in data]
        names = [ln.strip() for ln in raw.splitlines()]
        names = [n for n in names if n and not n.startswith("#")]
        if not names:
            print(f"[app:{who}] manifest classes: {s!r} has no labels; "
                  f"keeping default labels", flush=True)
            return None
        return names

    # (1) built-in table name
    table = BUILTIN_CLASS_TABLES.get(s.lower())
    if table is None:
        print(f"[app:{who}] manifest classes: unknown built-in table {s!r} "
              f"(known: {sorted(BUILTIN_CLASS_TABLES)}); keeping default labels",
              flush=True)
        return None
    return list(table)


def _class_file_path(rel: str, app_dir: Optional[str], who: str) -> Optional[str]:
    """Resolve an in-package labels file, refusing anything outside app_dir."""
    if os.path.isabs(rel):
        print(f"[app:{who}] manifest classes: absolute path {rel!r} refused",
              flush=True)
        return None
    if not app_dir:
        print(f"[app:{who}] manifest classes: {rel!r} needs an app dir to "
              f"resolve against; keeping default labels", flush=True)
        return None
    root = os.path.realpath(app_dir)
    path = os.path.realpath(os.path.join(root, rel))
    if path != root and not path.startswith(root + os.sep):
        print(f"[app:{who}] manifest classes: {rel!r} escapes the app dir; refused",
              flush=True)
        return None
    if not os.path.isfile(path):
        print(f"[app:{who}] manifest classes: {rel!r} not found in package; "
              f"keeping default labels", flush=True)
        return None
    return path


# --------------------------------------------------------------------------- #
# manifest `render` -> envelope.render   (RENDER_DECLARATION_SPEC §1-§3)
# --------------------------------------------------------------------------- #
# Keys inside a render section that are SEMANTIC (bound to the model / topology)
# rather than visual. They describe *what* the points mean, so a user twiddling
# a config knob must never be able to change them -- only `apply:"restart"`-grade
# metadata (a model swap) legitimately changes a layout, and that goes through a
# new manifest, not config.json.
_RENDER_FROZEN_KEYS = frozenset({"layout", "skeleton", "as"})


def _render_overlay(spec: Any, cfg: Dict[str, Any],
                    prefix: Optional[str] = None) -> Any:
    """Overlay runtime config onto ONE declared render section.

    Only keys the manifest already declares are overridable -- the declaration
    is the contract of "what this app lets you tune"; an unrelated config key
    (`conf`, `max_faces`, ...) can never leak into the render block just by
    being named alike. Precedence, per §2:

        `<prefix>_<key>` in config  >  `<key>` in config  >  declared default

    The prefixed form exists because bare names collide across sections/events
    (three declarations can each carry `duration_sec`); e.g. an app can expose
    `line_cross_duration_sec` to tune just that toast.
    """
    if not isinstance(spec, dict):
        return spec
    merged = dict(spec)
    for key in spec:
        if key in _RENDER_FROZEN_KEYS:
            continue
        pk = f"{prefix}_{key}" if prefix else None
        if pk is not None and pk in cfg:
            merged[key] = cfg[pk]
        elif key in cfg:
            merged[key] = cfg[key]
    return merged


def effective_render(manifest: Optional[dict],
                     config: Optional[Dict[str, Any]]) -> Optional[dict]:
    """Merge `manifest.render` with the running config -> the EFFECTIVE block.

    Returns None when the app declares nothing, so `emit()` leaves the key out
    entirely and the front end keeps its shape-driven fallback (§3, backward
    compatible). A None-valued config item is ignored, same rule as everywhere
    else in the kit: a cleared field must not wipe a declared default.
    """
    decl = (manifest or {}).get("render")
    if not isinstance(decl, dict) or not decl:
        return None
    cfg = {k: v for k, v in (config or {}).items() if v is not None}
    out: Dict[str, Any] = {}
    for section, body in decl.items():
        if section == "events" and isinstance(body, dict):
            out["events"] = {kind: _render_overlay(spec, cfg, prefix=kind)
                             for kind, spec in body.items()}
        else:
            out[section] = _render_overlay(body, cfg, prefix=section)
    return out


class App:
    """Base application. Subclass with `owns_loop = True` and define `run()`."""

    # Subclasses set these (usually mirrored from manifest.json).
    id: str = "app"
    name: str = "App"
    postproc: str = "detect"          # which post-processor the base loop runs
    input_size: int = 640             # stage-1 model input side (letterbox target);
                                      # ppocr-reader overrides to 480 for the DB detector
    # ★Detector label table★. Kit default = COCO80. A subclass that DECLARES its
    # own `class_names` (class attribute, or an instance assignment before
    # start()) wins over the manifest -- the manifest's `models[].classes` only
    # fills in when the app left this at the kit default. See start().
    class_names: Sequence[str] = COCO80
    # ★Loop shape★ (KIT_APP_SHAPE_SPEC §1/§3). MUST be True: the app owns the
    # loop -- it defines `def run(self):`, iterates `self.frames()`, and kit
    # calls start()/run()/finish() around it. Left False here (rather than
    # dropped) so a class that forgets the declaration hits the explicit
    # _check_loop_shape error instead of a confusing AttributeError. This is an
    # EXPLICIT declaration -- kit never guesses it from the run() signature.
    owns_loop: bool = False

    needs_model: bool = True          # CPU-only apps (e.g. qrcode-reader) set this
                                      # False: start() loads no RknnModel and
                                      # `pre()` is never used; run() does the work.

    # ★Frame appetite★. True (default) = this app consumes camera frames, so
    # `start()` opens the frame source and `self.frames()` yields from it. Set it
    # False on a loop-owning app whose input is NOT the camera (voice-transcribe
    # reads audio chunks; a file/batch app reads its own input): kit then opens
    # NO frame source at all -- no /dev/video, no RTSP client, no VPSS buffers --
    # while `emit` / `tick` / `models` / config auto-binding all keep working
    # exactly as they do for a frame-driven app (spec §2 hard constraint: an
    # escape hatch must never cost you the rest of the infrastructure).
    # `self.frames()` then raises with the reason instead of silently returning
    # nothing.
    needs_frames: bool = True

    # Where the model-input letterbox is produced.  The Python resize costs
    # ~40 ms/frame at 1280x720 -> 640x640; moving it onto RGA measured +49% e2e
    # throughput.  Set it on your App subclass:
    #
    #     class MyApp(App):
    #         model_frame = "hw"        # or "hw-direct"
    #
    #   "cpu"       (default) letterbox in this loop.  Always correct.
    #   "hw"        the source letterboxes on RGA into ``frame.model_data`` and
    #               keeps ORIGINAL-resolution pixels in ``frame.data``.  Safe for
    #               any model-backed app, including ones that crop source pixels
    #               (ROI / perspective) after inference.
    #   "hw-direct" the RGA letterbox IS ``frame.data`` -- also skips the
    #               full-resolution NV12->RGB conversion (the cheapest path), so
    #               NO original-resolution pixels are available.  Only for apps
    #               that consume detections/keypoints and never read
    #               ``frame.data``.
    #   "hw-roi"    like "hw-direct" for the stage-1 model image (RGA letterbox
    #               IS ``frame.data``, no full-res convert), but the source ALSO
    #               keeps the NV12 dma-buf reachable so a cascade app can crop
    #               per-object ROIs off it on RGA via ``self.crop_roi_hw(frame,
    #               box, out_size, pad)`` instead of numpy-cropping a full-res
    #               RGB frame.  For detect->crop->second-model apps
    #               (face-analysis / facemesh / ppocr) that were forced onto
    #               "cpu" only to keep the pixels their crop needed.  Such an app
    #               MUST take every ROI through ``crop_roi_hw`` and never read
    #               ``frame.data`` (it is the letterbox, not the camera frame).
    #
    # ``frame.w``/``frame.h`` and post-processed coordinates stay in original
    # camera geometry in every mode.  Ignored when ``needs_model`` is False, when
    # the backend exposes no dma-buf fd (RTSP/snapshot), or when RGA/librga is
    # unavailable -- each case falls back to the CPU letterbox with identical
    # geometry, never an error.  For "hw-roi", a librga without ``improcess_t``
    # degrades to "hw" (full-res data + numpy ROI crop), still correct.
    model_frame: str = "cpu"
    # Opt in only when the application consumes each frame inside its loop.
    # Historical apps retain eager, independent RGB frame storage by default.
    model_dma_input: bool = False

    def __init__(self) -> None:
        # config_schema-backed knobs (defaults; overridden in setup())
        self.conf: float = 0.25
        self.iou: float = 0.45
        # NOTE: `class_names` is deliberately NOT assigned here. Writing it as an
        # instance attribute would shadow a subclass's class-attribute
        # declaration and make "did the app declare its own labels?" impossible
        # to answer in start(). The class attribute above supplies the default.
        self.config: Dict[str, Any] = {}
        # Hot-reload (SIGHUP) flag. appmgr set_config sends SIGHUP after writing
        # config.json when ALL changed items are apply:"live" (see DESIGN
        # §3.2/§4). The signal handler only FLIPS this flag; the main loop does
        # the real re-read on the next frame -- signal handlers must stay tiny
        # and must not touch the model / pipeline.
        self._reload_flag: bool = False
        self._stop_flag: bool = False
        self._previous_signal_handlers: Dict[int, Any] = {}

        # -- loop runtime state (populated by start()) --------------------- #
        # `self.models` is populated by start(); an empty registry until then so
        # attribute access fails with a clear message instead of AttributeError
        # on `self.models` itself.
        self.models = ModelRegistry()
        self._rt: Optional[Dict[str, Any]] = None   # runtime options from start()
        # run_app and start() use this bit to avoid double-finishing a failed
        # transaction while still covering subclasses that override start().
        self._lifecycle_cleaned: bool = True
        self._manifest: Optional[dict] = None
        self._params_bound: bool = False
        self._pre_size: int = 0
        self._cur_frame: Optional[Frame] = None
        self._t_frame0: float = 0.0
        self._t_pre: float = 0.0
        self._t_infer: float = 0.0
        self._t_emit: float = 0.0
        self._warmed: bool = False
        self._processed: int = 0
        self._last_payload: Optional[Dict[str, Any]] = None
        # Render declaration (§3): merging manifest+config every frame would be
        # pure waste -- the inputs only move on SIGHUP. `_config_version` is
        # bumped by each successful reload; `_render_cache` is (version, block).
        self._config_version: int = 0
        self._render_cache: Optional[tuple] = None

    # -- application hooks (override these) -------------------------------- #
    def setup(self, config: Dict[str, Any]) -> None:
        """Read config_schema parameters. Override + call super().setup(config)."""
        self.config = config or {}
        self.conf = float(self.config.get("conf", self.conf))
        self.iou = float(self.config.get("iou", self.iou))

    def prepare_runtime(self) -> None:
        """Prepare app-owned runtime resources before appmgr receives READY.

        ``setup()`` runs before the kit publishes ``self._rt``.  Applications
        that need the resolved source URL, sink or other runtime options can
        override this later hook instead.  It is still part of ``start()``'s
        rollback transaction: if it raises, :meth:`finish` runs and appmgr never
        observes a false-ready process.
        """

        return None

    # -- live-reload value-replace helpers (shared by every app override) --- #
    @staticmethod
    def _reload_params(config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """Drop keys whose value is None so a missing/cleared config item never
        overwrites a live attribute (the None-filter every app applied by hand)."""
        return {k: v for k, v in (config or {}).items() if v is not None}

    @staticmethod
    def _reload_float(params: Dict[str, Any], key: str, cur: float) -> float:
        """Value-replace a float knob from `params`; keep `cur` on absence/garbage."""
        try:
            return float(params.get(key, cur))
        except (TypeError, ValueError):
            return cur

    @staticmethod
    def _reload_int(params: Dict[str, Any], key: str, cur: int) -> int:
        """Value-replace an int knob from `params`; keep `cur` on absence/garbage."""
        try:
            return int(params.get(key, cur))
        except (TypeError, ValueError):
            return cur

    def on_config_reload(self, config: Dict[str, Any]) -> None:
        """★Live config hot-reload hook★ (SIGHUP -> re-read config.json).

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
        """
        self.config = config or {}
        params = self._reload_params(config)
        self.conf = self._reload_float(params, "conf", self.conf)
        self.iou = self._reload_float(params, "iou", self.iou)

    # -- hot-reload plumbing (base; apps do not touch) -------------------- #
    def _install_reload_handler(self) -> None:
        """Install the SIGHUP live-reload handler, best-effort."""
        try:
            self._previous_signal_handlers.setdefault(
                signal.SIGHUP, signal.getsignal(signal.SIGHUP))
            signal.signal(signal.SIGHUP, self._on_sighup)
        except (ValueError, OSError):
            pass

    def _install_stop_handlers(self) -> None:
        """Install TERM/INT cooperative-stop handlers, best-effort.

        The handlers raise an internal ``BaseException`` so arbitrary user
        loops unwind through ``run_app`` cleanup. This improves normal shutdown
        only: crash correctness still depends on kernel fd cleanup, appmgr
        containment, and the planned rkipc connection lease.
        """

        for signum in (signal.SIGTERM, signal.SIGINT):
            try:
                self._previous_signal_handlers.setdefault(
                    signum, signal.getsignal(signum))
                signal.signal(signum, self._on_stop_signal)
            except (ValueError, OSError):
                pass

    def _on_sighup(self, signum, frame) -> None:
        # Tiny by design: only flip the flag; the loop does the work.
        self._reload_flag = True

    def _on_stop_signal(self, signum, frame) -> None:
        """Mark the runtime stopping and immediately unwind the current loop."""

        self._stop_flag = True
        raise _GracefulStop(signum)

    def _restore_signal_handlers(self) -> None:
        """Restore handlers that were active before :meth:`start`."""

        previous, self._previous_signal_handlers = self._previous_signal_handlers, {}
        for signum, handler in previous.items():
            try:
                signal.signal(signum, handler)
            except (ValueError, OSError):
                pass

    def _maybe_reload(self) -> None:
        """If a SIGHUP arrived, re-read the effective config and hand it to
        on_config_reload. Never raises into the loop."""
        if not self._reload_flag:
            return
        self._reload_flag = False
        from kit import config as _cfg
        try:
            cfg = _cfg.effective_config(_cfg.app_dir_of(self))
        except Exception as e:                 # config unreadable -> keep running
            print(f"[app:{self.id}] config reload skipped (read failed: {e})",
                  flush=True)
            return
        # The render block is derived from the effective config, so it must see
        # the fresh values whether or not the app's on_config_reload override
        # remembers to call super(). Assign here (the base hook re-assigns the
        # same dict below) and invalidate the cache in one place.
        self.config = cfg
        self._config_version += 1
        # New-shape apps: re-bind the apply:"live" config_schema keys onto self
        # BEFORE the (usually absent) on_config_reload hook, so an override can
        # still see/adjust the freshly bound values. Legacy apps never reach
        # this branch -- _params_bound is only set by start().
        if self._params_bound:
            try:
                changed = self._bind_params(cfg, live_only=True)
                if changed:
                    self.on_params_changed(changed)
            except Exception as e:             # binding bug must not kill loop
                print(f"[app:{self.id}] param rebind failed: {e}", flush=True)
        try:
            self.on_config_reload(cfg)
            print(f"[app:{self.id}] config hot-reloaded ({len(cfg)} keys)",
                  flush=True)
        except Exception as e:                 # app hook bug must not kill loop
            print(f"[app:{self.id}] config reload failed: {e}", flush=True)
        # The sink is kit-owned infrastructure, so kit -- not the app -- routes
        # the live change into it. A ConfigurableSink assembled from the manifest
        # `output` block re-applies its apply:"live" filters/template here; every
        # other sink no-ops. This is what makes `emit` fully kit-managed for a
        # loop-owning app: it never has to go looking for its own sink.
        rt = self._rt
        if rt is not None:
            try:
                rt["sink"].on_config_reload(cfg)
            except Exception as e:
                print(f"[app:{self.id}] sink config reload failed: {e}",
                      flush=True)

    # ------------------------------------------------------------------ #
    # New app shape: start / frames / pre / models / emit / tick
    # (internal/KIT_APP_SHAPE_SPEC.md §2). A migrated app overrides `run(self)`
    # and drives these; `run_app` wires them up.
    # ------------------------------------------------------------------ #
    def _load_model(self, path: str):
        """Construct the NPU model for `path`. Overridable seam (tests stub it)."""
        from kit.runtime.remote import configured_inference_socket

        service_socket = configured_inference_socket()
        if service_socket:
            # appmgr only injects this endpoint after manifest/resource
            # admission.  All models in the application then live in the one
            # platform daemon, allowing compatible applications to keep
            # multiple RKNN contexts resident while driver calls are scheduled.
            from kit.runtime.remote import RemoteRknnModel

            return RemoteRknnModel(path, socket_path=service_socket)
        from kit.runtime.engine import RknnModel  # lazy: only vision apps need rknnlite
        return RknnModel(path)

    def on_params_changed(self, changed: set) -> None:
        """Called after SIGHUP re-bound the apply:"live" params onto `self`.

        `changed` is the set of keys whose value actually differs. Override only
        when a derived object must be rebuilt (state machine, cached geometry).
        Plain scalar knobs need nothing -- they are already re-bound.
        """
        pass

    def _bind_params(self, config: Dict[str, Any], *, live_only: bool = False) -> set:
        """Bind `config_schema` keys onto `self` as plain attributes.

        Returns the set of keys whose value changed. `live_only` restricts the
        pass to `apply:"live"` items (the SIGHUP re-bind); the initial pass binds
        everything. A None value is skipped so a cleared config item never wipes
        a live attribute (same rule as `_reload_params`). A key that would shadow
        a method/property on the class is skipped with a warning.
        """
        schema = _schema_items(self._manifest)
        changed = set()
        for key, spec in schema.items():
            if live_only and (spec.get("apply") or "live") != "live":
                continue
            if key not in config:
                continue
            value = config[key]
            if value is None:
                continue
            attr = getattr(type(self), key, None)
            if callable(attr) or isinstance(attr, property):
                print(f"[app:{self.id}] config key {key!r} not auto-bound "
                      f"(would shadow a method/property)", flush=True)
                continue
            value = _coerce(spec.get("type"), value)
            if getattr(self, key, object()) != value:
                changed.add(key)
            setattr(self, key, value)
        self._params_bound = True
        return changed

    def _bind_class_names(self, decls: List[dict], app_dir: Optional[str]) -> None:
        """Fill `self.class_names` from the primary model's `classes` decl.

        Precedence:  app declaration  >  manifest `models[0].classes`  >  COCO80.

        "App declaration" = the subclass overrode the `class_names` class
        attribute, or assigned `self.class_names` before start() ran. In that
        case the manifest is not consulted at all (the app knows better than the
        packaging metadata). Otherwise the manifest value is resolved via
        `resolve_class_names`; anything unresolvable logs and leaves COCO80 in
        place so the app still starts.
        """
        declared = ("class_names" in self.__dict__ or
                    type(self).class_names is not App.class_names)
        spec = decls[0].get("classes") if decls else None
        if declared:
            if spec is not None:
                print(f"[app:{self.id}] class_names: app declaration wins over "
                      f"manifest models[0].classes", flush=True)
            return
        names = resolve_class_names(spec, app_dir, who=self.id)
        if names is not None:
            self.class_names = names

    def start(
        self,
        model_path: Optional[str] = None,
        *,
        source: str = "ffmpeg",
        url: str = DEFAULT_SUB_STREAM,
        sink: Optional[ResultSink] = None,
        n: int = 0,
        every: int = 1,
        skip_gray_std: float = 8.0,
        max_gray_skip: int = 120,
        verbose: bool = True,
        app_dir: Optional[str] = None,
        manifest: Optional[dict] = None,
        config: Optional[Dict[str, Any]] = None,
    ) -> "App":
        """Prepare the kit-owned runtime for a new-shape `run()`.

        Loads the manifest's `models[]` (paths made absolute against the install
        dir), binds `config_schema` params onto `self`, opens the frame source and
        installs the SIGHUP handler. `run_app` calls this; `finish()` tears it down.
        `model_path` (the `--model` CLI flag) overrides the FIRST manifest model.

        Startup is a transaction: a failure at any stage invokes ``finish``
        before the original exception is re-raised.  This includes BaseException
        control flow so SIGTERM/KeyboardInterrupt cannot strand an earlier model
        or source acquired by this same attempt.
        """
        self._lifecycle_cleaned = False
        try:
            return self._start_impl(
                model_path,
                source=source,
                url=url,
                sink=sink,
                n=n,
                every=every,
                skip_gray_std=skip_gray_std,
                max_gray_skip=max_gray_skip,
                verbose=verbose,
                app_dir=app_dir,
                manifest=manifest,
                config=config,
            )
        except BaseException:
            try:
                self.finish()
            except BaseException as cleanup_error:
                # Cleanup is best-effort, but it must never replace the model,
                # setup, source, or signal exception that aborted startup.
                log.error(
                    "startup cleanup failed app=%s error=%s",
                    self.id, cleanup_error, exc_info=True,
                )
                print(f"[app:{self.id}] startup cleanup failed: "
                      f"{cleanup_error}", file=sys.stderr, flush=True)
            finally:
                self._lifecycle_cleaned = True
            raise

    def _start_impl(
        self,
        model_path: Optional[str] = None,
        *,
        source: str = "ffmpeg",
        url: str = DEFAULT_SUB_STREAM,
        sink: Optional[ResultSink] = None,
        n: int = 0,
        every: int = 1,
        skip_gray_std: float = 8.0,
        max_gray_skip: int = 120,
        verbose: bool = True,
        app_dir: Optional[str] = None,
        manifest: Optional[dict] = None,
        config: Optional[Dict[str, Any]] = None,
    ) -> "App":
        """Implementation of :meth:`start`; the public method owns rollback."""
        from kit import config as _cfg
        if app_dir is None:
            app_dir = _cfg.app_dir_of(self)
        if manifest is None:
            manifest = _cfg.load_manifest(app_dir)
        self._manifest = manifest or {}

        # Shape guard: the class must declare `owns_loop = True` with a
        # no-argument run() and carry no leftover pre-migration callback hook.
        # Raises with the fix spelled out (see _check_loop_shape).
        _check_loop_shape(self)

        cfg = config if config is not None else (self.config or {})
        self._bind_params(cfg)

        # -- models: preload + absolutise paths --------------------------- #
        self.models = ModelRegistry()
        decls = list(self._manifest.get("models") or [])
        if self.needs_model:
            for i, m in enumerate(decls):
                mid = m.get("id") or f"model{i}"
                rel = m.get("file") or ""
                path = rel if os.path.isabs(rel) else os.path.join(app_dir, rel)
                if i == 0 and model_path:
                    # --model overrides the primary model. supervisor passes it
                    # RELATIVE (`models/x.rknn`), so absolutise it the same way.
                    path = (model_path if os.path.isabs(model_path)
                            else os.path.join(app_dir, model_path))
                aliases = []
                task = (m.get("task") or "").strip().lower()
                if task:
                    aliases.append(task)
                    aliases.extend(_TASK_ALIASES.get(task, ()))
                if len(decls) == 1:
                    aliases.extend(("model", "first"))
                self.models._register(mid, _ModelHandle(mid, path, self,
                                                        self._load_model(path)),
                                      aliases)
            if not decls and model_path:
                # No manifest models[] but a --model was supplied: expose it as
                # `.model` so a hand-run app still works.
                p = (model_path if os.path.isabs(model_path)
                     else os.path.join(app_dir, model_path))
                self.models._register("model",
                                      _ModelHandle("model", p, self,
                                                   self._load_model(p)),
                                      ("first",))

        # -- model input geometry: manifest first, class attribute as fallback #
        size = None
        if decls:
            inp = decls[0].get("input")
            if isinstance(inp, (list, tuple)) and len(inp) >= 3:
                try:
                    size = int(inp[1])
                except (TypeError, ValueError):
                    size = None
        self._pre_size = size or self.input_size

        # -- detector labels: manifest `models[].classes`, app declaration wins #
        # Same precedence shape as `input` above, with one addition: an app that
        # DECLARED its own `class_names` (subclass attribute or a pre-start()
        # instance assignment) is authoritative -- the manifest only fills the
        # kit default (COCO80). Unresolvable declarations log and fall back.
        self._bind_class_names(decls, app_dir)

        # setup() runs AFTER the auto-bind AND after the models are registered,
        # so an app that still needs one (to build derived objects: trackers,
        # zone geometry, state machines, a stage-2 cascade) can read the
        # already-bound `self.<param>` attributes AND adopt `self.models.<id>`
        # instead of digging through the raw config dict or loading a second
        # copy of a model the kit already holds (facemesh-reader's
        # CascadePipeline does exactly that). `run_app` therefore does NOT call
        # setup() for a loop-owning app -- start() owns that call.
        self.setup(cfg)

        mode = self.model_frame if self.needs_model else "cpu"
        if mode not in ("cpu", "hw", "hw-direct", "hw-roi"):
            raise ValueError(
                "%s: model_frame must be 'cpu', 'hw', 'hw-direct' or 'hw-roi' "
                "(got %r)" % (self.id, self.model_frame))

        # Publish a provisional runtime before opening native endpoints.  If a
        # later sink/handler step fails, finish() can see and close everything
        # that was successfully returned so far.
        rt = {
            "src": None, "sink": sink, "own_sink": False, "n": n,
            "every": every, "skip_gray_std": skip_gray_std,
            "max_gray_skip": max_gray_skip, "verbose": verbose,
            "app_dir": app_dir, "source": source, "url": url,
            "grays_skipped": 0, "loop_start": None,
        }
        self._rt = rt
        if self.needs_frames:
            rt["src"] = open_frame_source(
                url=url,
                prefer=source,
                input_size=self._pre_size if mode != "cpu" else 0,
                direct_preprocess=(mode == "hw-direct"),
                hw_letterbox=(mode == "hw"),
                hw_roi=(mode == "hw-roi"),
                deferred_preprocess=(self.model_dma_input is True
                    and mode == "hw-direct" and len(self.models) > 0
                    and getattr(self.models[0]._impl, "io_transport", None) == "rknn-dma-v1"),
            )
        else:
            # No camera at all (needs_frames = False). Everything else below --
            # sink, SIGHUP handler, models, bound params -- is unchanged.
            rt["src"] = None

        if sink is None:
            sink = open_result_sink("stdout")
            rt["sink"] = sink
            rt["own_sink"] = True
        # A frameless app has no warm-up frame to discard, so emit() must publish
        # from its very first call (otherwise the first voice event -- the initial
        # idle state -- would be silently dropped).
        self._warmed = not self.needs_frames
        self._stop_flag = False
        self._processed = 0
        self._install_reload_handler()
        self._install_stop_handlers()
        # App-owned resources that depend on the published runtime (for
        # example Voice's audio source URL) must also be ready before start()
        # returns and run_app emits APPMGR_READY.
        self.prepare_runtime()
        if verbose:
            actual_source = (type(rt["src"]).__name__
                             if self.needs_frames else "none")
            print(f"[app:{self.id}] models={[h.path for h in self.models]} "
                  f"source={actual_source} "
                  f"requested_source={source if self.needs_frames else 'none'} "
                  f"url={url if self.needs_frames else '-'} "
                  f"input={self._pre_size} "
                  f"sink={type(sink).__name__}", flush=True)
        return self

    # -- kit-owned runtime options a loop-owning run() may need ------------- #
    @property
    def verbose(self) -> bool:
        """The `--quiet`-derived verbosity `start()` was given (True by default).

        Available to any loop-owning `run()`; the removed callback loop took it as an
        argument instead.
        """
        rt = self._rt
        return True if rt is None else bool(rt.get("verbose", True))

    @property
    def source_url(self) -> Optional[str]:
        """The `--url` value `start()` was given, or None before start().

        Frame-driven apps never need it (kit already opened the source with it);
        an app that owns its own input (voice-transcribe's RTSP audio-track
        demux) reads the same CLI knob from here instead of re-parsing argv.
        """
        rt = self._rt
        return None if rt is None else rt.get("url")

    def finish(self) -> None:
        """Release every acquired resource exactly once.

        Models are detached even when ``_rt`` was never established, which is
        the normal shape of a second-model or setup failure.  Runtime state is
        detached before invoking user/vendor cleanup so repeated calls remain
        idempotent even if one cleanup callback raises.
        """
        rt, self._rt = self._rt, None
        models, self.models = self.models, ModelRegistry()
        self._lifecycle_cleaned = True
        control_error = None
        failures: list[tuple[str, Exception]] = []

        def clean(label: str, callback) -> None:
            nonlocal control_error
            try:
                callback()
            except Exception as exc:
                failures.append((label, exc))
                log.error(
                    "application cleanup failed app=%s resource=%s error=%s",
                    self.id, label, exc, exc_info=True,
                )
            except BaseException as exc:
                log.critical(
                    "application cleanup interrupted app=%s resource=%s "
                    "error=%s", self.id, label, exc, exc_info=True,
                )
                if control_error is None:
                    control_error = (exc, exc.__traceback__)

        clean("signal_handlers", self._restore_signal_handlers)

        if rt is not None and rt.get("src") is not None:
            clean("frame_source", rt["src"].close)
        for h in models:
            clean(f"model:{h.id}", h.release)
        if rt is not None and rt.get("own_sink") and rt.get("sink") is not None:
            clean("result_sink", rt["sink"].close)

        if rt is not None:
            loop_start = rt.get("loop_start")
            if self._processed and loop_start:
                wall = time.monotonic() - loop_start
                print(f"\n[app:{self.id}] === {self._processed} frames "
                      f"(grey-skipped {rt['grays_skipped']}) ===", flush=True)
                print(f"[app:{self.id}] end-to-end "
                      f"{self._processed/wall:4.1f} fps", flush=True)
            elif rt.get("verbose") and self.needs_frames:
                print(f"[app:{self.id}] no frames processed", file=sys.stderr)

        if control_error is not None:
            error, traceback = control_error
            raise error.with_traceback(traceback)
        if failures:
            details = {
                "app_id": self.id,
                "failures": [
                    {"resource": label, "type": type(exc).__name__,
                     "message": str(exc)}
                    for label, exc in failures
                ],
            }
            raise AdapterError(
                f"{len(failures)} resource cleanup operation(s) failed for "
                f"app {self.id!r}",
                operation="app.finish",
                code="cleanup_failed",
                details=details,
            ) from failures[0][1]

    def frames(self) -> Iterator[Frame]:
        """Yield frames to the app's `run()` loop, kit-managed.

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
        """
        rt = self._rt
        if rt is None:
            raise RuntimeError(
                f"{type(self).__name__}.frames() called before start(); use "
                f"run_app(app) (or app.start(...)) to drive a new-shape app")
        if rt["src"] is None:
            raise RuntimeError(
                f"{type(self).__name__}.frames() called but the class declares "
                f"`needs_frames = False`, so kit opened no camera frame source. "
                f"Either drop that declaration, or drive your own input and use "
                f"self.emit()/self.tick() (spec §3)")
        verbose = rt["verbose"]
        every = max(1, int(rt["every"] or 1))
        n = int(rt["n"] or 0)

        METRICS_PERIOD = 1.0
        m_t0 = time.monotonic()
        m_frames = 0
        m_pre = m_inf = m_emit = m_app = m_loop = 0.0

        got_real = False
        fidx = 0

        for frame in rt["src"].frames():
            if self._stop_flag:
                break
            self._maybe_reload()

            if not got_real:
                std = float(np.asarray(frame.data).std())
                if (std < rt["skip_gray_std"]
                        and rt["grays_skipped"] < rt["max_gray_skip"]):
                    rt["grays_skipped"] += 1
                    continue
                got_real = True
                if verbose:
                    print(f"[app:{self.id}] skipped {rt['grays_skipped']} grey "
                          f"warm-up frames; first real frame std={std:.1f}",
                          flush=True)

            fidx += 1
            if every > 1 and (fidx % every) != 0:
                continue

            if not self._warmed:
                self._warmed = True
                self._warm_up(frame)
                rt["loop_start"] = time.monotonic()
                if verbose:
                    print(f"[app:{self.id}] warmup frame {frame.w}x{frame.h} "
                          f"fmt={frame.fmt} (not handed to run())", flush=True)
                continue

            # -- frame boundary: reset the per-frame timing buckets --------- #
            self._cur_frame = frame
            self._t_pre = self._t_infer = self._t_emit = 0.0
            self._t_frame0 = time.monotonic()
            self._last_payload = None

            yield frame                                   # <-- app's loop body

            total = time.monotonic() - self._t_frame0
            self._cur_frame = None

            self._processed += 1
            app_ms = total - self._t_pre - self._t_infer - self._t_emit
            m_frames += 1
            m_pre += self._t_pre
            m_inf += self._t_infer
            m_emit += self._t_emit
            m_app += max(0.0, app_ms)
            m_loop += total

            m_now = time.monotonic()
            m_dt = m_now - m_t0
            if m_dt >= METRICS_PERIOD and m_frames:
                try:
                    rt["sink"].emit_meta({
                        "type": "metrics",
                        "kind": "metrics",
                        "app": self.id,
                        "fps": round(m_frames / m_dt, 1),
                        "latency_ms": {
                            # Complete application loop body, including all
                            # emit calls and work after them. Frame acquisition
                            # and this metrics publication are outside `total`.
                            "loop": round(m_loop / m_frames * 1000, 1),
                            # `pre`/`infer`/`post` keep the existing appmgr /
                            # debug-panel contract; in the new shape the app owns
                            # post-processing, so `post` IS the app bucket. `app`
                            # and `emit` are additive detail (spec §4).
                            "pre": round(m_pre / m_frames * 1000, 1),
                            "infer": round(m_inf / m_frames * 1000, 1),
                            "post": round(m_app / m_frames * 1000, 1),
                            "app": round(m_app / m_frames * 1000, 1),
                            "emit": round(m_emit / m_frames * 1000, 1),
                        },
                        "frames": self._processed,
                        "pts": frame.pts,
                    })
                except Exception:
                    pass    # telemetry must never break the inference loop
                m_t0 = m_now
                m_frames = 0
                m_pre = m_inf = m_emit = m_app = m_loop = 0.0

            if verbose:
                p = self._last_payload or {}
                res = p.get("results") or []
                evs = p.get("events") or []

                def _label(d):
                    name = (d.get("cls_name") or d.get("text")
                            or d.get("label") or "?")
                    return (f"{name}:{d['score']:.2f}" if "score" in d
                            else f"{name}")
                names = ", ".join(_label(d) for d in res[:6])
                print(f"[app:{self.id}] frame#{self._processed:03d} "
                      f"dets={len(res):2d} events={len(evs)} "
                      f"clients={getattr(rt['sink'], 'client_count', lambda: 0)()} "
                      f"{names}", flush=True)

            if n and self._processed >= n:
                break

    def _warm_up(self, frame: Frame) -> None:
        """Kit-side NPU warm-up on the first real frame (see `frames()`).

        Runs `pre()` + one `infer()` on the PRIMARY model, exactly what the
        pre-migration loop did on the frame it then discarded. The app's `run()` body
        never sees this frame, so cross-frame state (trackers, dwell timers,
        rolling windows) starts on the same frame it did before the migration.

        Best-effort: a warm-up failure is logged, never raised -- it costs
        latency on the first real frame, nothing else. Cascade stages beyond the
        primary model are not warmed (their input geometry is app-specific).
        """
        if not self.needs_model or not len(self.models):
            return
        try:
            x = self.pre(frame)
            self.models[0].infer(x.data)
        except Exception as e:                 # warm-up is an optimisation only
            print(f"[app:{self.id}] warm-up inference skipped ({e})", flush=True)
        finally:
            self._t_pre = self._t_infer = 0.0

    def pre(self, frame: Frame) -> PreparedInput:
        """Produce the model input for `frame` (letterbox to the manifest size).

        Prefers what the frame source already did on RGA -- `frame.model_data`
        (hw) or a `frame.model_info`-annotated `frame.data` (hw-direct) -- and
        only falls back to the Python letterbox. Geometry is identical in every
        case; `.info` always maps back to ORIGINAL camera pixels.
        """
        t0 = time.monotonic()
        try:
            deferred = getattr(frame, "_deferred_model_image", None)
            if deferred is not None:
                return _DeferredPreparedInput(deferred, frame.model_info)
            info = getattr(frame, "model_info", None)
            padded = getattr(frame, "model_data", None)
            if padded is None:
                if info is not None:
                    padded = frame.data
                else:
                    padded, info = letterbox(frame.data, self._pre_size
                                             or self.input_size)
            return PreparedInput(padded, info)
        finally:
            self._t_pre += time.monotonic() - t0

    def crop_roi_hw(self, frame: Frame, box, out_size: int, pad: float = 0.25):
        """Crop a padded square ROI around `box`, preferring hardware (RGA).

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
        """
        from kit.pipeline import crop_square_roi
        t0 = time.monotonic()
        try:
            cropper = getattr(frame, "roi_cropper", None)
            if cropper is not None:
                try:
                    return cropper.crop_square(box, out_size, pad)
                except Exception as e:
                    # A hardware ROI crop failed. `frame.data` is the letterbox
                    # here (hw-roi), so a numpy crop of it would be the WRONG
                    # region -- return a gray ROI (no crash, no wrong pixels) and
                    # warn once. A stable ABI problem is caught by the source's
                    # first-frame probe, so this is a rare per-crop edge case.
                    self._warn_roi_hw(e)
                    roi = np.full((int(out_size), int(out_size), 3), 114,
                                  dtype=np.uint8)
                    return roi, (float(box[0]), float(box[1]), 1.0, 1.0)
            return crop_square_roi(frame.data, box, out_size, pad)
        finally:
            self._t_pre += time.monotonic() - t0

    def _warn_roi_hw(self, e) -> None:
        n = getattr(self, "_roi_hw_warns", 0) + 1
        self._roi_hw_warns = n
        if n <= 3:
            print(f"[app:{self.id}] hw ROI crop failed ({e}); gray ROI this "
                  f"object", flush=True)

    def _render_block(self) -> Optional[dict]:
        """The effective render declaration for the CURRENT config, cached.

        Recomputed only when `_config_version` moves (i.e. after a SIGHUP
        reload), so the per-frame cost is one integer compare. A malformed
        declaration degrades to "no render key" rather than killing the loop.
        """
        cached = self._render_cache
        if cached is not None and cached[0] == self._config_version:
            return cached[1]
        try:
            block = effective_render(self._manifest, self.config)
        except Exception as e:                  # a bad manifest must not stop emit
            print(f"[app:{self.id}] render declaration ignored ({e})", flush=True)
            block = None
        self._render_cache = (self._config_version, block)
        return block

    def emit(self, events=None, ts: Optional[float] = None, *,
             results=None, geometry=None,
             extra: Optional[Dict[str, Any]] = None) -> None:
        """Publish one frame's output through the manifest-configured sinks.

        `events` are the app-level events; `results` (optional) are the raw
        per-frame detections/records that the /appcenter overlay and the
        manifest `output` field mappings read as `results[]`. ``geometry`` is a
        list (or :class:`kit.geometry.GeometryBuilder`) of validated canonical
        drawing primitives.  Their coordinate space is not trusted from the
        payload: managed Result Hub ingress injects it from the installed
        manifest. `ts` defaults to the current frame's pts.

        During the warm-up frame this is a no-op (same as pre-migration).
        """
        rt = self._rt
        if rt is None:
            raise RuntimeError("emit() called outside a started run()")
        t0 = time.monotonic()
        try:
            frame = self._cur_frame
            geometry_items = None
            if geometry is not None:
                from kit.geometry import GeometryBuilder
                if isinstance(geometry, GeometryBuilder):
                    geometry_items = geometry.build()
                else:
                    geometry_items = GeometryBuilder().extend(geometry).build()
            payload = {
                "results": list(results) if results is not None else [],
                "events": list(events) if events is not None else [],
                "inference_time_ms": round(self._t_infer * 1000.0, 3),
                # `_t_frame0` is set at each frame boundary by frames(); a
                # frameless app (needs_frames=False) has no such boundary, so
                # report 0 rather than "seconds since the monotonic epoch".
                "pipeline_ms": (round((t0 - self._t_frame0) * 1000.0, 3)
                                if self._t_frame0 else 0.0),
                "stream_id": "camera-0",
            }
            # Preserve the legacy wire shape for every existing application.
            # Only an explicit use of the new API opts the envelope into the
            # top-level geometry contract; ``geometry=[]`` remains available
            # when a producer deliberately wants to publish an empty set.
            if geometry_items is not None:
                payload["geometry"] = geometry_items
            # ★Self-describing stream★ (§3): the EFFECTIVE render declaration
            # rides along every frame, so a third-party consumer on :8124 draws
            # the overlay correctly without ever fetching the manifest. Absent
            # for an app that declares nothing -- the front end then falls back.
            render = self._render_block()
            if render is not None:
                payload["render"] = render
            if extra:
                payload.update(extra)
            self._last_payload = payload
            if not self._warmed:
                return                      # warm-up frame: output discarded
            sink = rt["sink"]
            if frame is not None:
                sink.set_frame_size(frame.w, frame.h)
            if ts is None:
                ts = frame.pts if frame is not None else time.monotonic()
            sink.emit(payload, ts)
        finally:
            self._t_emit += time.monotonic() - t0

    def tick(self) -> None:
        """Apply a pending SIGHUP config hot-reload.

        `frames()` already ticks at every frame boundary; only an app that takes
        over the loop entirely (audio chunks, multi-stream) needs to call this.
        """
        self._maybe_reload()

    # -- loop entry point (the app overrides this) ------------------------ #
    def run(self) -> None:
        """★The app's main loop★. Override with `owns_loop = True`.

        The kit calls `start()` -> `run()` -> `finish()`; inside, the app does
        its own `for frame in self.frames(): ...` and calls `self.pre()` /
        `self.models.<id>.infer()` / `self.emit()`. There is no other shape:
        the pre-migration callback loop (`run(model_path, ...)` driving
        `run_postproc`/`process_frame`/`on_results`) was removed once all apps
        migrated -- see `kit/tests/legacy_loop.py` for the frozen reference.
        """
        raise NotImplementedError(
            f"{type(self).__name__}: define `owns_loop = True` and "
            f"`def run(self): for frame in self.frames(): ...`")



def _signal_ready() -> None:
    """Tell appmgr this app reached its main loop (readiness handshake §core1).

    appmgr's supervisor.start() blocks until this file appears before it commits
    the app as running/active, so an interpreter/import failure, a missing model,
    or a sink it could not bind is caught as a failed start (the process dies
    before this point) instead of the UI showing a dead app as "running". A no-op
    when APPMGR_READY_FILE is unset (hand-run / demo), so nothing changes off
    appmgr. Best-effort: a write failure must never take the app down."""
    path = os.environ.get("APPMGR_READY_FILE")
    if not path:
        return
    try:
        with open(path, "w") as f:
            f.write(str(os.getpid()))
    except OSError:
        pass


def run_app(app: App, argv: Optional[List[str]] = None) -> None:
    """Generic CLI entry an app's app.py calls from __main__.

    Wires argparse -> config -> sink -> app.run(). Keeps app.py thin.
    """
    ap = argparse.ArgumentParser(description=f"reCamera Pro app: {app.id}")
    ap.add_argument("--model", default=None,
                    help="path to .rknn model (omitted for CPU-only apps)")
    # conf/iou default to None: the effective config (manifest defaults overlaid
    # by config.json) supplies them. A value here is a MANUAL override that wins
    # over config.json -- used for on-device debugging, not by appmgr.
    ap.add_argument("--conf", type=float, default=None)
    ap.add_argument("--iou", type=float, default=None)
    ap.add_argument("--source", default="ffmpeg", choices=["ffmpeg", "snapshot"])
    ap.add_argument("--url", default=DEFAULT_SUB_STREAM)
    ap.add_argument("--sink", default="ws", choices=["ws", "stdout"])
    ap.add_argument("--port", type=int, default=8124)
    # Default LOOPBACK (C9): the overlay reaches this WS through nginx
    # (proxy_pass -> 127.0.0.1:<port>), so binding loopback keeps the raw result
    # stream off the LAN behind the JWT edge. Pass --host 0.0.0.0 to deliberately
    # expose an UNAUTHENTICATED LAN-direct stream.
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--n", type=int, default=0, help="stop after N frames (0=forever)")
    ap.add_argument("--every", type=int, default=1)
    ap.add_argument("--quiet", action="store_true")
    # MQTT / Home Assistant fan-out (optional; default OFF). Normally driven by
    # appmgr via RECAMERA_MQTT_* env; these flags are for on-device manual test.
    ap.add_argument("--mqtt-host", default=None,
                    help="enable MQTT/HA output to this broker (adds to WS)")
    ap.add_argument("--mqtt-port", type=int, default=None)
    ap.add_argument("--mqtt-user", default=None)
    ap.add_argument("--mqtt-pass", default=None)
    ap.add_argument("--mqtt-base-topic", default=None)
    ap.add_argument("--mqtt-discovery-prefix", default=None)
    args = ap.parse_args(argv)

    # Library imports are deliberately silent.  The CLI is the ownership
    # boundary where a default handler is appropriate, so typed adapter/RGA/
    # inference diagnostics are visible without requiring every user app to
    # understand Python logging configuration.  ``--quiet`` suppresses normal
    # progress while retaining warnings and actionable errors.
    from kit.diagnostics import configure_logging
    configure_logging("WARNING" if args.quiet else "INFO")

    # Unified config load (kit-design / parameter hot-tuning): manifest
    # config_schema defaults overlaid by <app_dir>/config.json. Explicit CLI
    # --conf/--iou win over that (manual debugging override).
    from kit import config as _cfg
    app_dir = _cfg.app_dir_of(app)
    # Read the manifest ONCE and thread it through both consumers (effective
    # config overlay + output-sink assembly) instead of loading it twice.
    manifest = _cfg.load_manifest(app_dir)
    eff = _cfg.effective_config(app_dir, manifest=manifest)
    if args.conf is not None:
        eff["conf"] = args.conf
    if args.iou is not None:
        eff["iou"] = args.iou
    # Check the loop shape FIRST: a mis-declared shape must fail before we open
    # sinks or a camera. setup() is NOT called here -- start() owns that call,
    # so it runs after the config_schema auto-bind (see App.start).
    _check_loop_shape(app)

    # Keep the first lifecycle failure and perform every cleanup stage before
    # re-raising that exact object.  Sink construction is part of the same
    # transaction: a configurable-output failure after the primary WS/stdout
    # sink opened must close that primary rather than leaking a server/socket.
    primary_error = None
    sink: Optional[ResultSink] = None
    pending_sinks: List[ResultSink] = []
    start_attempted = False
    app._lifecycle_cleaned = False
    try:
        if args.sink == "stdout":
            primary: ResultSink = open_result_sink("stdout")
        else:
            primary = open_result_sink("ws", host=args.host, port=args.port,
                                       app_id=app.id)
        pending_sinks.append(primary)

        # Unified configurable output (internal/OUTPUT_SINK_SPEC.md §3). Apps
        # declaring capabilities:["output"] get the manifest-driven pipeline;
        # other apps retain the optional legacy MQTT fan-out.
        from kit.adapters.result_sink import MultiSink
        from kit.adapters.output_sink import assemble_output_sink
        out_sink, opted_in = assemble_output_sink(
            app, app_dir, manifest, eff, verbose=not args.quiet)
        if opted_in:
            if out_sink is not None:
                pending_sinks.append(out_sink)
        else:
            mqtt = _maybe_open_mqtt_sink(
                app, app_dir, args, verbose=not args.quiet)
            if mqtt is not None:
                pending_sinks.append(mqtt)
        sink = (MultiSink(pending_sinks)
                if len(pending_sinks) > 1 else pending_sinks[0])

        # KIT_APP_SHAPE_SPEC §1: the app owns the loop; kit supplies
        # frames/pre/models/emit/tick via start().
        start_attempted = True
        app.start(args.model, source=args.source, url=args.url, sink=sink,
                  n=args.n, every=args.every, verbose=not args.quiet,
                  app_dir=app_dir, manifest=manifest, config=eff)
        # start() returned: models, kit endpoints and app-owned runtime resources
        # all passed their readiness transaction. Signal READY before entering
        # the long-running loop.
        _signal_ready()
        try:
            app.run()
        except _GracefulStop as stop:
            if not args.quiet:
                print(f"[app:{app.id}] graceful stop requested by signal "
                      f"{stop.signum}", flush=True)
    except BaseException as exc:
        primary_error = (exc, exc.__traceback__)
    finally:
        if start_attempted and not getattr(app, "_lifecycle_cleaned", False):
            try:
                app.finish()
            except BaseException as exc:
                if primary_error is None:
                    primary_error = (exc, exc.__traceback__)
                else:
                    log.error(
                        "app finish failed while preserving earlier error "
                        "app=%s error=%s", app.id, exc, exc_info=True,
                    )
                    print(f"[app:{app.id}] finish failed while preserving "
                          f"earlier error: {exc}", file=sys.stderr, flush=True)

        # Once MultiSink exists it owns all children.  Before that point close
        # every successfully constructed child individually in reverse order.
        close_targets = ([sink] if sink is not None
                         else list(reversed(pending_sinks)))
        seen = set()
        for target in close_targets:
            if target is None or id(target) in seen:
                continue
            seen.add(id(target))
            try:
                target.close()
            except BaseException as exc:
                if primary_error is None:
                    primary_error = (exc, exc.__traceback__)
                else:
                    log.error(
                        "sink close failed while preserving earlier error "
                        "app=%s sink=%s error=%s", app.id,
                        type(target).__name__, exc, exc_info=True,
                    )
                    print(f"[app:{app.id}] sink close failed while preserving "
                          f"earlier error: {exc}", file=sys.stderr, flush=True)

    if primary_error is not None:
        error, traceback = primary_error
        raise error.with_traceback(traceback)


def _env_flag(name: str) -> bool:
    return str(os.environ.get(name, "")).strip().lower() in ("1", "true", "yes", "on")


def _maybe_open_mqtt_sink(app: "App", app_dir: str, args, *, verbose: bool):
    """Build an MqttSink from CLI flags (win) or RECAMERA_MQTT_* env, or None.

    Precedence: --mqtt-host flag > RECAMERA_MQTT_ENABLED+RECAMERA_MQTT_HOST env >
    disabled. HA entities come from the app manifest's `ha_entities` array.
    """
    env = os.environ
    host = args.mqtt_host or (env.get("RECAMERA_MQTT_HOST")
                              if _env_flag("RECAMERA_MQTT_ENABLED") else None)
    if not host:
        return None
    port = args.mqtt_port or int(env.get("RECAMERA_MQTT_PORT", "1883") or 1883)
    user = args.mqtt_user if args.mqtt_user is not None else env.get("RECAMERA_MQTT_USERNAME", "")
    pw = args.mqtt_pass if args.mqtt_pass is not None else env.get("RECAMERA_MQTT_PASSWORD", "")
    base = args.mqtt_base_topic or env.get("RECAMERA_MQTT_BASE_TOPIC", "recamera")
    prefix = args.mqtt_discovery_prefix or env.get("RECAMERA_MQTT_DISCOVERY_PREFIX", "homeassistant")

    from kit import config as _cfg
    manifest = _cfg.load_manifest(app_dir)
    entities = manifest.get("ha_entities") or []
    device_name = manifest.get("name") or app.name or "reCamera Pro"
    try:
        from kit.adapters.mqtt_sink import MqttSink
        return MqttSink(host=host, port=port, app_id=app.id, base_topic=base,
                        discovery_prefix=prefix, username=user or "", password=pw or "",
                        entities=entities, device_name=device_name, verbose=verbose)
    except Exception as e:  # never let MQTT setup break WS output
        if verbose:
            print(f"[app:{app.id}] MQTT sink disabled (setup error: {e})", flush=True)
        return None
