# kit.events

[API 索引](../index.md) · [接口特性与边界](../features.md)

源码基线：[kit/events.py](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/events.py)；签名由 AST 提取，不导入硬件依赖。

生成 legacy 检测、文本、跟踪、属性与指标字典；构造字典不负责发送、叠加或录像。

源码说明中的历史验证记录、迁移目标和旧示例不代表当前固件可用；应用入口、资源准入和接口可用性以本页补充及接口特性文档为准。

## 模块契约（源码）

kit.events -- mechanical result->event converters (KIT_APP_SHAPE_SPEC §5.3).

Imported as ``from kit import events as E`` and used inside an app's ``run()``:

    self.emit([E.detection(d) for d in dets], frame.pts, results=dets)

**Only mechanical transforms live here**: renaming/copying fields, ``round``,
normalised<->pixel conversion, filling in ``kind``. Business semantics -- what
counts as an event, which threshold fires it, cross-frame state -- stay in the
app. A helper here must never decide a threshold, hold state, or gate whether
an event is produced.

This module is deliberately grown one converter at a time, as each app is
migrated to the new shape. Today: ``detection`` (yolo-detector), ``track`` and
``metrics`` (retail-vision), ``text`` (ppocr-reader), ``face_attributes``
(face-analysis), ``drowsiness_metrics`` (facemesh-reader).

## kit.events.detection

```python
def detection(d: Dict[str, Any]) -> Dict[str, Any]
```

One detect() result dict -> one flat, overlay-friendly ``detection`` event.

Field-for-field identical to the hand-written mapping yolo-detector carried
in ``on_results()`` before the migration:

    {"kind": "detection", "label": <cls_name>, "cls": <cls>,
     "score": <score>, "box": <box>}

``box`` is passed through untouched -- post-processing has already
un-letterboxed it into ORIGINAL-frame xyxy pixels.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/events.py#L28)

## kit.events.text

```python
def text(box: Dict[str, Any], *, text: str, rec_conf: float) -> Dict[str, Any]
```

One recognized text box -> one flat, overlay-friendly ``text`` event.

Field-for-field identical to the hand-written mapping ppocr-reader carried
in ``on_results()`` before the migration:

    {"kind": "text", "box": ..., "quad": ..., "text": ...,
     "score": <detection score>, "rec_conf": <round(conf, 4)>}

What this function does -- all mechanical (spec §5.3):

  * copies ``box`` / ``quad`` / ``score`` off the stage-1 result dict
    (already un-letterboxed into ORIGINAL-frame pixels by db_ocr.decode),
  * rounds the recognition confidence to 4 dp,
  * fills in ``kind``.

What it does NOT do -- the caller passes these in, because they are the
app's business decisions:

  * the perspective crop and which recognizer ran,
  * the reading order the boxes arrive in,
  * ``text`` itself -- in particular, whether a low-confidence reading is
    blanked out. This helper never compares ``rec_conf`` against anything;
    the app decides that and hands over the string it wants published.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/events.py#L49)

## kit.events.track

```python
def track(tr, frame, *, state, in_zone: bool) -> Dict[str, Any]
```

One ``kit.logic.tracker.Track`` -> one flat, overlay-friendly ``track`` event.

Purely mechanical (spec §5.3). What this function does:

  * de-normalises the track's centre/size (``cx``/``cy``/``w``/``h`` are in
    [0,1]) into ORIGINAL-frame xyxy pixels using ``frame.w``/``frame.h``,
  * rounds -- box to 0.1 px, score to 3 dp, speed to 0.1 px/s,
  * copies ``track_id`` off the Track and fills in ``kind``.

What it does NOT do -- the caller passes these in, because they are business
decisions the app owns:

  * ``state``    -- the dwell state machine's verdict for this track,
  * ``in_zone``  -- whether the counting-zone polygon contains this track.

It holds no state, applies no threshold, and never decides whether an event
is emitted -- the app calls it once per track it has already decided to
report.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/events.py#L84)

## kit.events.face_attributes

```python
def face_attributes(r: Dict[str, Any], *, blur: bool) -> Dict[str, Any]
```

One annotated face result dict -> one flat ``face`` attribute event.

Purely mechanical (spec §5.3): copies ``box`` / ``score``, the identity and
evidence-quality fields, and the six FairFace + two emotion attribute fields
off the result dict with ``.get`` (a missing attribute stays ``None``),
coerces ``blur`` to bool, and fills in ``kind``.

``track_id`` / ``stable`` / ``gated`` / ``evidence_frames`` describe how much
the attribute fields are worth on this face:

  * ``track_id`` -- stable identity from ``kit.logic.tracker``; ``None`` when
    the face was not associated to a track this frame. It is what lets a
    consumer tell "the same person, still being measured" from "a second
    person", which a per-frame face event otherwise cannot express.
  * ``gated`` -- the face was too small (or scored too low) to classify, so
    every attribute field is ``None`` by construction rather than a guess.
  * ``stable`` / ``evidence_frames`` -- whether enough frames have voted
    (``kit.logic.attributes``). An unstable verdict is an early read, not a
    wrong one; consumers that cannot tolerate a label changing under them
    should wait for ``stable``.

What it does NOT do -- the app owns all of it:

  * WHICH faces get an event (the top-K ``max_faces`` slice),
  * the gate, the tracking and the accumulation the fields above report on,
  * the value of ``blur`` -- the caller passes the privacy setting in.

It holds no state, applies no threshold, and never decides whether an event
is produced.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/events.py#L119)

## kit.events.drowsiness_metrics

```python
def drowsiness_metrics(m, yawn, drowsy) -> Dict[str, Any]
```

The three drowsiness dataclasses -> one flat ``metrics`` event.

Field-for-field identical to the hand-written mapping facemesh-reader
carried in ``on_results()`` before the migration. Purely mechanical
(spec §5.3): copies named fields off ``FaceMetrics`` / ``YawnState`` /
``DrowsinessState``, rounds (EAR/MAR/level to 3 dp, PERCLOS to 1 dp,
closure seconds to 2 dp), coerces the flags to bool/int and fills in
``kind``.

What it does NOT do -- the app (and ``kit.logic.drowsiness``) owns it:

  * every threshold and temporal decision that PRODUCED these values
    (EAR/MAR thresholds, PERCLOS window, yawn debounce, alert cooldown),
  * the blink / yawn / drowsiness edge events, which depend on cross-frame
    state and stay in the app,
  * when to publish -- the app calls this once per frame it has already
    decided to report.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/events.py#L170)

## kit.events.metrics

```python
def metrics(snapshot, **extra) -> Dict[str, Any]
```

A metrics dataclass (e.g. ``zones.WindowSnapshot``) -> a ``metrics`` event.

``dataclasses.asdict`` + ``kind``, nothing else: every field of the snapshot
is copied out under its own name. `extra` merges in additional flat fields
the app wants alongside them.

The app decides WHAT the snapshot contains and WHEN to publish it (which
frames feed the rolling window, what counts as occupancy); this helper only
flattens the object it is handed.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/635ffc3c51d596dd2e8139f297798162e6be62b9/kit/events.py#L208)
