# SDK Contracts

This reference summarizes the public Python SDK contracts that affect code
correctness. Verify unfamiliar method signatures against
`sdk/python/recamera_ext/__init__.py` in the current SDK checkout.

## Architecture boundary

RKIPC owns camera capture, ISP, media routing, encoding, RTSP, recording, and
official result distribution. An external Python app is a separate process
connected through these public Unix sockets:

```text
/run/recamera/frame.sock      FrameSource: camera frames
/run/recamera/result-in.sock  ResultSink: external inference results
/run/recamera/probe.sock      ProbeSource: built-in inference observation
```

Use `recamera_ext`, which manages the ABI, handshake, zero-copy mapping, and
IPC protocol. Do not recreate the socket protocol in application code.
These paths and the binding are supplied by the extension runtime; they are not
guaranteed by a base Python installation. Verify that the required runtime is
installed and the service is live before treating a route as runnable.

## FrameSource

`FrameSource` provides NV12 frame buffers. `frame.array` exposes a zero-copy
NumPy view of the Y plane; `frame.to_bgr()` returns a separately allocated BGR
image and requires OpenCV. Use `FrameConfig(fps_divisor=...)` to request
reduced sampling when full frame rate is unnecessary.

Rules:

- A frame and every zero-copy view derived from it are valid only in the
  current iterator step. The next frame release invalidates prior storage.
- Call `.copy()` before passing a frame array into a queue/thread or retaining
  it; `to_bgr()` already returns an independent image.
- `frame.pts_us` is the timestamp to pass into a corresponding result.
- Do not assume a requested width, height, or fourcc will be honored on every
  firmware. The effective geometry comes from the frame returned by the SDK.
- Use `frame.width` and `frame.height` for coordinate conversion rather than
  hard-coded model or stream dimensions.

Example shape:

```python
from recamera_ext import FrameConfig, FrameSource

with FrameSource(FrameConfig(fps_divisor=2)) as source:
    for frame in source:
        grayscale = frame.array
        persistent_image = grayscale.copy()
        # Process this frame before the next iterator step.
```

## ResultSink

`ResultSink` publishes external inference results to the official result
pipeline. The public result categories are detections, classification,
segmentation, tracking, and keypoints.

Rules:

- All bounding boxes, classification ROIs, tracking boxes, keypoint-instance
  boxes, and individual keypoint coordinates are normalized fractions in
  `[0, 1]`. Convert pixels using frame width and height.
- Use `pts_us=frame.pts_us` for a result computed from that frame. `pts_us=0`
  is for an event intentionally not tied to a video frame.
- `source_id="builtin"` is reserved. Choose a stable external source ID.
- Same-source updates replace prior same-source display state. An empty
  detection list can clear it.
- The current v1 baseline advertises at most 60 messages/sec per connection,
  120 messages/sec globally, 64 KiB per message, four result connections, and
  eight logical result sources. Treat these as upper bounds, not a target rate.
  Firmware capability values may vary; use the published capability response
  when available and make output rate controllable rather than hard-coding
  these values.
- Segmentation data is metadata/recording/push data; do not promise a rendered
  OSD segmentation overlay unless the current platform explicitly documents it.

## Kit output is a separate contract

`kit.app.App.emit(events=None, ts=None, *, results=None, extra=None)` produces
the Kit result envelope. Its `extra` mapping is merged into the envelope at the
top level, so `extra={"alarm": value}` is addressed as `alarm`, not
`extra.alarm`. Kit `manifest.output.fields[].from` paths describe this envelope
only. Direct ResultSink uses normalized coordinates and microsecond `pts_us`;
the current Kit contract uses seconds for `emit(ts=...)`.

For a Kit detector, `emit(results=...)` only publishes a result envelope; it
does not by itself select a frontend renderer. Use the strict manifest
contract with `output.contract_version: 2`, `sink: "ws"`, a direct
`results[].box` field with declared coordinate space, and `render.boxes`.
The official detector apps normally publish source-frame pixel `xyxy` boxes.
The browser canvas overlay and stream/recording OSD are separate consumers;
`render.stream_osd` is only for the latter. Do not use OpenCV drawing as a
substitute for either result contract.

Pixel-to-normalized conversion example:

```python
def normalized_box(x1, y1, x2, y2, frame):
    return (
        x1 / frame.width,
        y1 / frame.height,
        x2 / frame.width,
        y2 / frame.height,
    )
```

## ProbeSource

`ProbeSource` observes the installed built-in inference pipeline. Supported
stage identifiers include `metrics`, `preproc.out`, `npu.raw`, and
`postproc.out`, subject to the capabilities of the deployed firmware.

`sample.array` is zero-copy and has the same current-iteration lifetime rule
as a frame. Probe is suitable for diagnostics, monitoring, and inspection. It
does not give an app ownership of built-in models, a stable generic video
source, or a supported method to modify built-in pipeline behavior.

## GPIO, audio, and controls

- For reacting to published inference events, use the documented notification
  flow and the GPIO example. Do not poll or modify RKIPC internals.
- For the documented firmware topology, prefer the shared ALSA PCM name
  `ai_asr`; `ai_debug` is for diagnostics. Do not open `ai_main` or `ai_kws`.
  Access still requires root or membership in the audio group on the documented
  target. This is firmware integration guidance, not a `recamera_ext` binding.
  `kit.adapters.official.OfficialPcmSource` currently raises as a migration
  stub, so `/run/recamera/audio.sock` must not be treated as available.
- Audio capture and audio playback are different capabilities. The points above
  cover capture (`ai_asr` / `arecord`); the public binding exposes NO playback
  API. Speaker output is a firmware ALSA device-capability path (`aplay`, or
  `libasound.so.2` through `ctypes`), not a `recamera_ext` call. Assess and
  verify it per `audio-playback.md` and `capability-routing.md`; never fabricate
  a `recamera_ext` speaker method.
- Configuration/control requests must use a stable, documented public HTTP
  API. `/var/tmp/rkipc` is an internal service endpoint and must not appear in
  generated app code.
- `MaskControl` is a public binding for an optional firmware capability. Use
  that binding only after its symbols are available; do not reproduce or call
  its underlying native control transport in application code.

## Performance decisions

When a kit vision app only consumes model-space results and never needs
original camera pixels, `model_frame = "hw-direct"` can avoid a CPU copy. It
changes the meaning of frame data to a letterboxed model image, so it is wrong
for later original-image cropping or geometry. For detector-to-ROI cascades,
`model_frame = "hw-roi"` with `crop_roi_hw(...)` may be appropriate. Keep the
default CPU path whenever the app needs ordinary original-image pixel access.

Do not assume desktop ML runtimes are available or suitable. For a kit model
app, use the kit-provided `RknnModel` only through
`self.models.<model_id>.infer(...)`; do not instantiate `rknnlite` directly in
application code. In the current SDK, `RknnModel` defaults to a ctypes-backed
`librknnrt.so` implementation. `rknnlite` is retained as a compatibility
fallback, not the normal application-level integration surface. A compatible
RKNN model and platform runtime remain preconditions.
