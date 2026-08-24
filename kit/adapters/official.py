"""
Official extension-API adapters for reCamera Pro (Rockchip RV1126B).

*** This module is the production-grade reference for wiring a self-hosted
    Python app onto the official reCamera Pro extension API. ***

It is the L0 "official" backend described in docs/guide/adapter-bootstrap.md §2 and
docs/guide/kit-design.md §L0. Where the workaround backends decode the go2rtc RTSP
sub-stream (FfmpegRtspSource) and publish results on our own WebSocket
(WsResultSink), these adapters use the shipped SDK `librecamera_ext`:

  * OfficialFrameSource  -- zero-copy frames from the M2 frame proxy
                            (`/run/recamera/frame.sock`, NV12 dma-buf), via the
                            SDK's `recamera_ext.FrameSource`.
  * OfficialResultSink   -- inject results into the M1 result sink
                            (`/run/recamera/result-in.sock`), via the SDK's
                            `recamera_ext.ResultSink`. Results then flow through
                            rkipc's *own* pipeline -> OSD burn-in + recording +
                            push -- which the WebSocket workaround could never do.

Both classes implement the exact kit ABCs (`FrameSource` / `ResultSink`) with
the same `Frame` contract as the workaround, so the capability registry
(registry.py) swaps them in with ZERO application changes (docs/guide/adapter-bootstrap.md §3:
"registry 一切换，9 个应用一行不改").

Interface source of truth (verified against the authoritative SDK, not guessed)
-------------------------------------------------------------------------------
* SDK Python:  sdk/python/recamera_ext/__init__.py  (authoritative; the old
               recamera_rk/m2_scratch/sdk_work copy is DEPRECATED). Provides:
                 FrameSource(config, timeout_ms, lib_path) iterable
                     -> Frame(.array / .to_bgr() / .pts_us / .planes / .fd)
                 ResultSink(source_id) with the FULL v1 result API:
                     .send_detections(pts_us, boxes)
                     .send_classification(pts_us, items)
                     .send_segmentation(pts_us, items)
                     .send_tracking(pts_us, items)
                     .send_keypoints(pts_us, instances)
* SDK C ABI:   sdk/include/recamera_ext.h -- rc_ext_frame_* (96-byte header +
               SCM_RIGHTS dma-buf fd) and rc_ext_result_send_*.
* API spec:    docs/api/spec.md §2/§3, docs/guide/README.md §3/§4.

RESULT ROUTING (which app output -> which SDK channel; see OfficialResultSink)
-----------------------------------------------------------------------------
    detection boxes  (yolo-detector, ppocr text, qrcode)  -> send_detections
    pose keypoints   (fall-detection, fitness, facemesh)  -> send_keypoints
    face/emotion     (face-analysis)                       -> send_classification
    tracked objects  (retail-vision)                       -> send_tracking
    segmentation masks (none shipped yet; mapping ready)   -> send_segmentation

Contract references
-------------------
* OfficialFrameSource  -> API spec §2 (M2 frame proxy), docs/guide/adapter-bootstrap.md §2.1
* OfficialResultSink    -> API spec §3 (M1 result injection), docs/guide/adapter-bootstrap.md §2.2
* OfficialPcmSource     -> docs/guide/adapter-bootstrap.md §2.3 (R8 clean 16k PCM broker; stub)
* OfficialControl       -> docs/guide/adapter-bootstrap.md §2.4 (R4 versioned control API; stub)
"""
from __future__ import annotations

from abc import ABC, abstractmethod
import os
from typing import Iterator, List, Optional

import numpy as np

from .frame_source import Frame, FrameSource
from .result_sink import ResultSink
# The audio contract (PcmFrame + AudioSource ABC) is defined canonically in
# audio_source.py and re-exported here so OfficialPcmSource and the workaround
# AlsaTakeoverSource share one identical interface.
from .audio_source import AudioSource, PcmFrame
from kit.diagnostics import WarningLimiter, get_logger
from kit.errors import CapabilityError, InputValidationError, KitError, TransportError


log = get_logger("adapters.official")

# Canonical official endpoint paths (spec §1: all extension IPC lives under
# /run/recamera/; /var/run is the usual symlink to /run). Overridable via env
# for testing -- see registry.py. Single source of truth for the adapters.
OFFICIAL_FRAME_SOCK = "/run/recamera/frame.sock"
OFFICIAL_RESULT_SOCK = "/run/recamera/result-in.sock"
OFFICIAL_AUDIO_SOCK = "/run/recamera/audio.sock"


class OfficialFrameSource(FrameSource):
    """Zero-copy frame source over the M2 frame proxy (`/run/recamera/frame.sock`).

    Wraps the SDK's `recamera_ext.FrameSource`, which hands back borrowed NV12
    dma-buf frames. For each frame we produce a standalone RGB ``Frame``.  The
    default is full-resolution (the historical contract); an explicit
    ``direct_preprocess`` opt-in instead performs model-aspect NV12 resize and
    RGB conversion in RGA, then fills only the small square border in Python.
    In that mode ``Frame.w``/``h`` remain the original camera geometry and
    ``Frame.model_info`` carries the LetterboxInfo-compatible mapping.

    Design decisions a vendor copying this MUST understand
    ------------------------------------------------------
    1. FULL-RESOLUTION BY DEFAULT; MODEL-SIZED ON EXPLICIT OPT-IN.
       Existing apps and callers continue to receive a full-resolution RGB
       frame.  The direct path is selected only for apps that never inspect
       source pixels.  Original dimensions are retained for result routing and
       post-processing receives the exact letterbox transform.

    2. PREPROCESS PATH: RGA (fast) with an OpenCV FALLBACK -- ONE switch point.
       NV12->RGB is the per-frame hot spot. On the RV1126B the RGA 2D engine
       does it in hardware, reading the dma-buf fd directly (near-zero CPU). If
       librga is missing / unusable, we fall back to the SDK's `frame.to_bgr()`
       (cv2 NV12->BGR) + a numpy channel flip. The selection is latched on the
       first frame (see `_convert`) so the branch is decided once, not per frame.

    3. dma-buf RELEASE. The SDK's Frame is *borrowed*: valid only for the
       current loop step and released when the loop advances. Every conversion
       returns a COPY (RGA writes a fresh RGB buffer; the OpenCV path copies via
       to_bgr), so `Frame.data` is safe to hold after release. We never keep a
       zero-copy view alive across iterations.

    4. PTS ALIGNMENT. `frame.pts_us` (CLOCK_MONOTONIC microseconds, the VI PTS)
       is carried as `Frame.pts = pts_us / 1e6` seconds. OfficialResultSink
       converts it back with `round(pts * 1e6)` -- an exact integer round-trip
       -- so injected results align to the frame the OSD burns onto.

    Signature mirrors `FfmpegRtspSource.__init__` (accepts `url` + misc kw and
    ignores them) so the registry constructs it identically. Extra optional
    knobs (`width`/`height`/`fps_divisor`/`input_size`/`direct_preprocess`/
    `prefer_rga`/`lib_path`) are honoured when supplied but never required.
    """

    def __init__(self, url: Optional[str] = None,
                 sock: str = OFFICIAL_FRAME_SOCK,
                 width: int = 0, height: int = 0, fps_divisor: int = 0,
                 input_size: int = 0, direct_preprocess: bool = False,
                 hw_letterbox: bool = False, hw_roi: bool = False,
                 timeout_ms: int = 1000, prefer_rga: bool = True,
                 lib_path: Optional[str] = None, verbose: bool = True,
                 **_ignored):
        if os.path.normpath(sock) != OFFICIAL_FRAME_SOCK:
            raise CapabilityError(
                "the current native frame ABI uses a fixed endpoint; "
                "RECAMERA_FRAME_SOCK overrides are unsupported",
                operation="frame.open",
                details={
                    "requested_endpoint": str(sock),
                    "native_endpoint": OFFICIAL_FRAME_SOCK,
                },
            )
        self.url = url
        self.sock = sock
        self.width = int(width)
        self.height = int(height)
        self.fps_divisor = int(fps_divisor)
        self.input_size = int(input_size)
        self.direct_preprocess = bool(direct_preprocess and self.input_size > 0)
        # ROI mode: like `direct` for the model image (RGA letterbox IS `data`,
        # NO full-resolution RGB convert), but the source ALSO keeps the NV12
        # dma-buf reachable so a cascade app can crop per-object ROIs from it on
        # RGA on demand (App.crop_roi_hw). This is the fast path for detect->crop
        # ->second-model apps that previously had to stay on "cpu" to keep the
        # full-resolution pixels their numpy crop needs. `direct_preprocess`
        # (pure model image, no cropper) wins if both are somehow requested.
        self.hw_roi = bool(hw_roi and self.input_size > 0
                           and not self.direct_preprocess)
        # Aux mode: letterbox on RGA into a SEPARATE model image while `data`
        # keeps original-resolution pixels.  `direct_preprocess`/`hw_roi` (which
        # reuse the letterbox AS `data`) win when more than one is requested.
        self.hw_letterbox = bool(hw_letterbox and self.input_size > 0
                                 and not self.direct_preprocess
                                 and not self.hw_roi)
        self.timeout_ms = int(timeout_ms)
        self.prefer_rga = bool(prefer_rga)
        self.lib_path = lib_path
        self.verbose = verbose
        self._src = None            # recamera_ext.FrameSource (opened in frames())
        self._rga = None            # RgaNV12ToRGB instance, or None once latched off
        self._rga_decided = False   # first-frame latch flag
        self._direct_logged = False  # one diagnostic line per source lifetime
        self._roi_logged = False    # one diagnostic line for the ROI crop path
        # Persistent per-output-size RGB scratch buffers reused across every ROI
        # crop of a run (keyed by out_size), so cropping N faces per frame for
        # thousands of frames allocates the canvas ONCE per size, not per crop.
        self._roi_scratch: dict = {}
        # NOTE: no connection at construction -- the registry only builds this
        # once frame.sock is probed present; the real open() happens in frames().

    # -- NV12 -> RGB conversion (the one RGA-vs-OpenCV switch point) --------- #
    def _decide_backend(self, frame) -> None:
        """Latch the converter on the first frame: try RGA once, else OpenCV."""
        self._rga_decided = True
        if not self.prefer_rga:
            self._log("preprocess backend: OpenCV (prefer_rga=False)")
            return
        try:
            from . import _rga
            self._rga = _rga.try_open()
        except Exception:
            self._rga = None
        self._log("preprocess backend: %s"
                  % ("RGA (hardware)" if self._rga else "OpenCV (librga unavailable)"))
        # ROI mode additionally needs improcess_t (the crop op). Older librga
        # builds export the letterbox helpers but not improcess_t; drop back to
        # the full-resolution "hw" path (data keeps original pixels, crop_roi_hw
        # then falls to numpy) rather than a broken cropper.
        if self.hw_roi:
            can_crop = bool(self._rga is not None
                            and getattr(self._rga, "can_crop", lambda: False)())
            if not can_crop:
                self.hw_roi = False
                self.hw_letterbox = True   # keep the model letterbox on RGA
                self._log("hw-roi unavailable (librga improcess_t missing); "
                          "falling back to hw (full-res + numpy ROI crop)")

    def _letterbox_geometry(self, width: int, height: int):
        """Return RGA resize geometry and a LetterboxInfo-compatible object."""
        from kit.runtime.preprocess import LetterboxInfo

        target = int(self.input_size)
        net_w = net_h = target
        if min(width, height, net_w, net_h) <= 0:
            raise ValueError("invalid direct-preprocess geometry")
        scale = min(net_w / float(width), net_h / float(height))
        small_w = int(round(width * scale))
        small_h = int(round(height * scale))
        # NV12 resize requires even source and destination dimensions.  Camera
        # and model sizes are normally even; fail closed for odd geometry so a
        # caller gets the established full-resolution fallback.
        if (small_w | small_h | net_w | net_h) & 1:
            raise ValueError("direct-preprocess requires even NV12 geometry")
        left = int(round((net_w - small_w) / 2.0 - 0.1))
        top = int(round((net_h - small_h) / 2.0 - 0.1))
        info = LetterboxInfo(scale=scale, pad_w=left, pad_h=top,
                             orig_w=int(width), orig_h=int(height))
        return small_w, small_h, left, top, info, net_w, net_h

    def _rga_letterbox(self, frame):
        """RGA NV12 -> model-sized letterboxed RGB. Returns ``(padded, info)``.

        Shared by both hardware modes; raises on any geometry/ABI problem so the
        caller can latch the optimization off and continue on the safe path.
        """
        sw, sh, left, top, info, net_w, net_h = self._letterbox_geometry(
            int(frame.width), int(frame.height))
        off0, stride0, vstride0 = frame.planes[0]
        if off0 != 0:
            raise RuntimeError("Y-plane offset %d unsupported by fd-wrap" % off0)
        small = self._rga.resize_nv12_to_rgb(
            fd=frame.fd, width=int(frame.width), height=int(frame.height),
            y_stride=int(stride0), y_vstride=int(vstride0),
            dst_width=sw, dst_height=sh)
        padded = np.full((net_h, net_w, 3), 114, dtype=np.uint8)
        padded[top:top + sh, left:left + sw] = small
        return np.ascontiguousarray(padded), info

    def _convert(self, frame):
        """Return ``(rgb, model_data, model_info)`` as fresh contiguous arrays.

        Three shapes, by mode:
          * direct  -> ``(letterbox, letterbox, info)``  -- no full-res convert
            at all (cheapest; caller loses original-resolution pixels).
          * hw      -> ``(full_rgb, letterbox, info)``   -- keeps source pixels
            AND skips the Python letterbox (for apps that crop ROIs).
          * neither -> ``(full_rgb, None, None)``        -- legacy path.

        Tries the latched RGA path; on ANY RGA error it permanently falls back
        to OpenCV for the rest of the run (a mis-versioned librga must never take
        the whole app down -- correctness over speed).
        """
        if not self._rga_decided:
            self._decide_backend(frame)

        model_data = model_info = None
        if self._rga is not None and (self.direct_preprocess or self.hw_roi
                                      or self.hw_letterbox):
            # Both `direct` and `hw-roi` reuse the letterbox AS `data` and skip
            # the full-resolution NV12->RGB convert; `hw-roi` additionally lets
            # the app crop ROIs off the dma-buf (the cropper is attached in
            # frames(), not here). Only `aux` (hw_letterbox) keeps full-res data.
            skip_fullres = self.direct_preprocess or self.hw_roi
            mode = ("direct" if self.direct_preprocess
                    else ("roi" if self.hw_roi else "aux"))
            try:
                model_data, model_info = self._rga_letterbox(frame)
                if not self._direct_logged:
                    self._direct_logged = True
                    self._log("preprocess path: RGA %s NV12 resize %dx%d -> RGB %dx%d + gray pad"
                              % (mode, int(frame.width), int(frame.height),
                                 model_data.shape[1], model_data.shape[0]))
                if skip_fullres:
                    # The letterbox IS the frame: skip the full-res conversion.
                    return model_data, model_data, model_info
            except Exception as e:
                # A resize ABI/driver mismatch must not take the app down.
                # Disable only the optimization and continue through the
                # known-good full-resolution RGA/OpenCV path below.
                self.direct_preprocess = False
                self.hw_roi = False
                self.hw_letterbox = False
                model_data = model_info = None
                self._log("RGA %s preprocess failed (%s); latching to full RGB"
                          % (mode, e))

        if self._rga is not None:
            try:
                off0, stride0, vstride0 = frame.planes[0]
                if off0 != 0:
                    # RGA fd-wrap assumes plane[0] starts at the buffer origin.
                    raise RuntimeError("Y-plane offset %d unsupported by fd-wrap" % off0)
                return self._rga.convert(
                    fd=frame.fd, width=int(frame.width), height=int(frame.height),
                    y_stride=int(stride0), y_vstride=int(vstride0),
                ), model_data, model_info
            except Exception as e:
                self._rga = None  # latch OFF -- never retry RGA this run
                self._log("RGA convert failed (%s); latching to OpenCV" % e)

        # OpenCV fallback: SDK cv2 NV12->BGR (a copy), then BGR->RGB via numpy
        # (no cv2 import needed in this module -- keeps the adapter cv2-free).
        bgr = frame.to_bgr()
        return np.ascontiguousarray(bgr[:, :, ::-1]), model_data, model_info

    def _log(self, msg: str) -> None:
        if self.verbose:
            log.info("OfficialFrameSource: %s", msg)

    # -- hw-roi: on-demand dma-buf ROI crop --------------------------------- #
    def _roi_out(self, out_size: int):
        """A persistent [out_size, out_size, 3] uint8 scratch canvas, reused
        across every ROI crop of this run (allocated once per distinct size)."""
        buf = self._roi_scratch.get(out_size)
        if buf is None:
            buf = np.empty((out_size, out_size, 3), dtype=np.uint8)
            self._roi_scratch[out_size] = buf
        return buf

    def _crop_roi(self, ext_frame, box, out_size: int, pad: float):
        """RGA crop of a padded square ROI straight from the frame's NV12 dma-buf.

        Returns ``(roi_uint8_HWC_RGB copy, roi_map)`` with the SAME `roi_map`
        contract as `kit.pipeline.crop_square_roi` (shared geometry helper), so
        downstream coordinate mapping is identical. The out-of-frame margin is
        gray-filled (114) rather than edge-replicated -- a small border-only
        difference from the numpy crop, see docs/guide/hw-preprocess.md.
        """
        from kit.pipeline import square_roi_geometry

        out_size = int(out_size)
        off0, stride0, vstride0 = ext_frame.planes[0]
        if off0 != 0:
            raise RuntimeError("Y-plane offset %d unsupported by fd-wrap" % off0)
        roi_map, src_valid, dst_window, _sq = square_roi_geometry(
            int(ext_frame.height), int(ext_frame.width), box, out_size, pad)
        buf = self._roi_out(out_size)
        if src_valid is None:
            # Box entirely outside the frame -- match crop_square_roi's zeros.
            buf[:] = 0
            return buf.copy(), roi_map
        self._rga.crop_nv12_to_rgb(
            fd=ext_frame.fd, width=int(ext_frame.width),
            height=int(ext_frame.height),
            y_stride=int(stride0), y_vstride=int(vstride0),
            src_rect=src_valid, dst_size=out_size, dst_window=dst_window,
            out=buf, pad_value=114)
        if not self._roi_logged:
            self._roi_logged = True
            self._log("roi path: RGA crop %s -> RGB %dx%d (dma-buf, no full-res)"
                      % (tuple(src_valid), out_size, out_size))
        # Hand back a COPY: `buf` is reused by the next crop this frame.
        return buf.copy(), roi_map

    # -- FrameSource ABC ---------------------------------------------------- #
    def _make_cropper(self, ext_frame):
        """Bind a `_FrameRoiCropper` to THIS borrowed frame (valid this step)."""
        return _FrameRoiCropper(self, ext_frame)

    def frames(self) -> Iterator[Frame]:
        # Lazy import: recamera_ext (+ librecamera_ext.so.1) only exists on the
        # device with the extension-API firmware. Importing here keeps this
        # module importable off-device (packaging, unit tests, the registry).
        from recamera_ext import FrameSource as ExtFrameSource, FrameConfig
        try:
            from recamera_ext import AcquireTimeoutError, RecameraError
        except ImportError:  # compatibility with old host-side test doubles
            AcquireTimeoutError = TimeoutError
            RecameraError = RuntimeError

        cfg = None
        if self.width or self.height or self.fps_divisor:
            cfg = FrameConfig(width=self.width, height=self.height,
                              fourcc=0, fps_divisor=self.fps_divisor)  # 0 => NV12

        self._src = ExtFrameSource(config=cfg, timeout_ms=self.timeout_ms,
                                   lib_path=self.lib_path)
        self._log("subscribed: %dx%d fourcc=0x%08x pool_depth=%d max_outstanding=%d"
                  % (self._src.width, self._src.height, self._src.fourcc,
                     self._src.pool_depth, self._src.max_outstanding))
        primary_error: BaseException | None = None
        try:
            acquire = getattr(self._src, "acquire", None)

            def strict_frames():
                if callable(acquire):
                    while True:
                        try:
                            yield acquire()
                        except AcquireTimeoutError:
                            continue
                        except StopIteration:
                            return
                        except RecameraError as exc:
                            raise TransportError(
                                f"native frame acquisition failed: {exc}",
                                operation="frame.acquire",
                                retryable=bool(getattr(exc, "retryable", False)),
                                details={
                                    "native_code": getattr(exc, "code", None),
                                    "source": "official",
                                },
                            ) from exc
                else:
                    # Compatibility for pre-1.3 wrappers and host fakes.  The
                    # iterator stores its typed terminal cause in last_error;
                    # never silently translate that cause into normal EOF.
                    yield from self._src
                    terminal = getattr(self._src, "last_error", None)
                    if terminal is not None:
                        raise TransportError(
                            f"native frame stream ended with an error: {terminal}",
                            operation="frame.acquire",
                            retryable=bool(getattr(terminal, "retryable", False)),
                            details={
                                "native_code": getattr(terminal, "code", None),
                                "source": "official-compat",
                            },
                        ) from terminal

            for ext_frame in strict_frames():
                rgb, model_data, model_info = self._convert(ext_frame)  # standalone copies
                # hw-roi: attach a cropper bound to THIS borrowed dma-buf so the
                # app can crop ROIs from it during its loop body. Only when the
                # RGA letterbox path was actually taken this frame (model_info
                # set) and hw_roi survived the first-frame probe/latch; otherwise
                # `roi_cropper` stays None and crop_roi_hw uses the numpy crop.
                cropper = None
                if self.hw_roi and self._rga is not None and model_info is not None:
                    cropper = self._make_cropper(ext_frame)
                yield Frame(
                    data=rgb,
                    w=int(ext_frame.width),
                    h=int(ext_frame.height),
                    fmt="RGB",
                    pts=ext_frame.pts_us / 1e6,    # us -> s; round-trips in the sink
                    model_info=model_info,
                    model_data=model_data,
                    roi_cropper=cropper,
                )
                # The SDK releases `ext_frame`'s dma-buf when the loop advances;
                # `rgb` is a copy, so nothing here holds the borrowed buffer.
        except BaseException as exc:
            primary_error = exc
            raise
        finally:
            try:
                self.close()
            except BaseException as cleanup_error:
                if primary_error is None:
                    raise
                log.error(
                    "frame source cleanup failed while preserving stream error",
                    exc_info=(
                        type(cleanup_error),
                        cleanup_error,
                        cleanup_error.__traceback__,
                    ),
                )

    def close(self) -> None:
        src, self._src = self._src, None
        if src is not None:
            try:
                src.close()
            except KitError:
                raise
            except Exception as exc:
                raise TransportError(
                    f"could not close native frame source: {exc}",
                    operation="frame.close",
                    details={"source": "official"},
                ) from exc


class _FrameRoiCropper:
    """Per-frame hardware ROI cropper attached to a Frame in ``hw-roi`` mode.

    Holds the owning `OfficialFrameSource` (for the RGA instance + reused scratch
    buffers) and the borrowed `recamera_ext` frame (for its dma-buf fd + NV12
    plane geometry). Valid ONLY for the loop step the frame is yielded on -- the
    SDK releases the underlying dma-buf when the loop advances. `App.crop_roi_hw`
    is the app-facing entry; it catches failures and degrades, so this raises
    freely on any geometry/ABI problem.
    """

    __slots__ = ("_src", "_frame")

    def __init__(self, source: "OfficialFrameSource", ext_frame):
        self._src = source
        self._frame = ext_frame

    def crop_square(self, box, out_size: int, pad: float = 0.25):
        """Crop a padded square ROI -> ``(roi_uint8_HWC_RGB, roi_map)``.

        Mirrors `kit.pipeline.crop_square_roi`'s signature and `roi_map`
        contract, backed by RGA reading the dma-buf directly.
        """
        return self._src._crop_roi(self._frame, box, out_size, pad)


def _bbox_from_quad(quad) -> Optional[list]:
    """Axis-aligned bounding box [x1,y1,x2,y2] of a 4-point quad (qrcode/ppocr)."""
    try:
        xs = [float(p[0]) for p in quad]
        ys = [float(p[1]) for p in quad]
        return [min(xs), min(ys), max(xs), max(ys)]
    except Exception:
        return None


class OfficialResultSink(ResultSink):
    """Inject results into the M1 result sink (`/run/recamera/result-in.sock`).

    Wraps the SDK's `recamera_ext.ResultSink` and ROUTES each frame's payload to
    the correct SDK channel by inspecting the result/event fields our apps
    already produce -- so rkipc runs it through the SAME three-way dispatch as
    its built-in inference -> **OSD burn-in + recording + push** (API spec §3),
    the capability the WebSocket workaround (WsResultSink) fundamentally lacks.

    Result-type routing (the mapping a vendor copies)
    -------------------------------------------------
    The base loop hands us `payload = {"results": [...], "events": [...]}`.
    `results` carries the per-object output each app leaves after on_results();
    tracking is the exception -- retail-vision keeps its tracked boxes in
    `events` (kind="track"), so we look there for track_id. Per-frame routing:

      | our field(s) on the item          | app(s)                    | SDK call            |
      |-----------------------------------|---------------------------|---------------------|
      | event has `track_id` + `box`      | retail-vision             | send_tracking       |
      | result has non-empty `keypoints`  | fall / fitness / facemesh | send_keypoints      |
      | result has `mask`/`mask_bytes`    | (none shipped; ready)     | send_segmentation   |
      | result has face attrs (gender/    | face-analysis             | send_classification |
      |   age/emotion) OR label w/o box   |                           |                     |
      | result has `box` (or `quad`)      | yolo / ppocr / qrcode     | send_detections     |

    Coordinate normalization (★ the critical contract ★)
    ----------------------------------------------------
    recamera_ext.h v1.2.0: EVERY box coordinate (detection / classification ROI
    / segmentation ROI / tracking / keypoint object box) AND every keypoint
    point x/y is a NORMALIZED [0,1] fraction of frame width/height. The OSD
    renderer clamps to [0,1] then multiplies by frame size, so PIXEL values
    collapse to an invisible 1px box. Our postprocess emits ORIGINAL full-res
    PIXELS, so this sink divides x by frame width and y by frame height (clamped)
    for every coordinate before sending -- see set_frame_size() + _norm_box().
    Non-coordinate fields (score, class_id, label, track_id, keypoint_id,
    keypoint score, segmentation mask bytes) are passed through unchanged.

    Field mapping to the SDK tuples/dicts (verified vs sdk/python/recamera_ext;
    all coordinates below are the NORMALIZED [0,1] values, not pixels)
    --------------------------------------------------------------------------
    * detections   : (x1,y1,x2,y2, score, label, class_id)
                       label = cls_name | text | label ; class_id = cls|0 ;
                       box from `box`, else derived from `quad` (qrcode/ppocr).
    * keypoints    : instance dict {"points":[(x,y,score,keypoint_id)...],
                       "box":(x1,y1,x2,y2), "score", "class_id", "label"}.
                       Our keypoints are [[x,y,conf]...] (pose, 17) or [[x,y]...]
                       (facemesh landmarks, 468) -> conf defaults to 1.0 when
                       absent; keypoint_id is the list index (COCO order for pose).
    * classification: (score, class_id, label[, (x1,y1,x2,y2)]). face-analysis's
                       per-face attributes become a composite label
                       ("Male,30-39,Happiness"); since SDK v1.1.0 the entry
                       carries an optional normalized ROI box (4th element) so we
                       attach the face box -> the OSD can localize the label.
    * tracking     : (x1,y1,x2,y2, score, class_id, label, track_id).
    * segmentation : (x1,y1,x2,y2, score, class_id, label, mask_bytes, mask_w,
                       mask_h). ROI box normalized; mask bytes untouched. No
                       shipped app emits masks yet; mapping is ready.

    source_id + pts_us (vendor gotchas)
    -----------------------------------
    * `source_id` identifies this app's result stream to rkipc (advisory: the
      server may override it from the connection's peer-credential identity, spec
      §1.1). We default it to the app id, so multiple extension apps stay
      distinguishable in the OSD/registry.
    * `pts_us` associates the result with a specific camera frame so the OSD
      overlays it on the right image. We reconstruct it from the frame's
      `pts` (seconds) that the base loop threads through `emit(payload, pts)`:
      `pts_us = round(pts * 1e6)`, the exact inverse of OfficialFrameSource's
      `pts_us / 1e6`. Passing 0 means "no frame association".

    Signature mirrors `WsResultSink.__init__` (host/port/app_id) so the registry
    builds it identically; `source_id`/`lib_path` are optional extras.
    """

    # Face-attribute keys that mark a result as a classification (face-analysis).
    _CLASS_ATTR_KEYS = ("gender", "age", "emotion", "race")

    def __init__(self, host: Optional[str] = None, port: Optional[int] = None,
                 app_id: str = "app", source_id: Optional[str] = None,
                 sock: str = OFFICIAL_RESULT_SOCK,
                 lib_path: Optional[str] = None, verbose: bool = True,
                 **_ignored):
        if os.path.normpath(sock) != OFFICIAL_RESULT_SOCK:
            raise CapabilityError(
                "the current native result ABI uses a fixed endpoint; "
                "RECAMERA_RESULT_SOCK overrides are unsupported",
                operation="result.open",
                details={
                    "requested_endpoint": str(sock),
                    "native_endpoint": OFFICIAL_RESULT_SOCK,
                },
            )
        self.host = host
        self.port = port
        self.app_id = app_id
        self.source_id = source_id or app_id
        self.sock = sock
        self.lib_path = lib_path
        self.verbose = verbose
        self._sink = None            # recamera_ext.ResultSink (opened lazily)
        self._warnings = WarningLimiter(log, limit=3)
        # Local diagnostics (surfaced via stats()): frames handed to emit(),
        # per-channel SDK send attempts, and how many of those raised. The SDK
        # ResultSink keeps the authoritative wire counters (sent / oversize /
        # send_error); stats() merges both so an app can see delivery health.
        self._frames = 0
        self._send_calls = 0
        self._send_fail = 0
        self._oversize = 0
        self._fw = None              # current frame width  (px) for normalization
        self._fh = None              # current frame height (px) for normalization
        # No connection at construction (cheap + side-effect free); the socket is
        # opened on the first emit() so the registry can build this freely.

    def set_frame_size(self, w: int, h: int) -> None:
        """Record the current frame's pixel size (base loop calls this per frame).

        ★THE FIX★ The extension-API OSD renderer treats every box/keypoint
        coordinate as a NORMALIZED [0,1] fraction of frame width/height (header
        recamera_ext.h v1.2.0: it clamps to [0,1] then multiplies by frame size,
        so a pixel value like 240 collapses to a 1px box). Our postprocess emits
        ORIGINAL full-res-frame PIXELS, so we must divide by this frame size
        before sending. We store it here and apply it in the per-item mappers.
        """
        if w and h and w > 0 and h > 0:
            self._fw = float(w)
            self._fh = float(h)

    def _ensure_open(self, *, strict: bool = False) -> bool:
        """Open the SDK ResultSink on first use. Returns False if unavailable
        (so legacy emit() degrades to a no-op rather than crashing the inference
        loop). ``emit_checked`` requests strict mode and receives the original
        exception instead of a false success."""
        if self._sink is not None:
            return True
        try:
            from recamera_ext import ResultSink as ExtResultSink
            self._sink = ExtResultSink(source_id=self.source_id,
                                       lib_path=self.lib_path)
            return True
        except Exception as e:
            self._warn_once("open failed: %s" % e)
            if strict:
                raise
            return False

    # -- coordinate normalization (pixels -> [0,1] fraction of frame size) --- #
    @staticmethod
    def _clamp01(v: float) -> float:
        return 0.0 if v < 0.0 else (1.0 if v > 1.0 else v)

    def _nx(self, x) -> float:
        """Normalize an x pixel to [0,1] by frame width, clamped."""
        return self._clamp01(float(x) / self._fw)

    def _ny(self, y) -> float:
        """Normalize a y pixel to [0,1] by frame height, clamped."""
        return self._clamp01(float(y) / self._fh)

    def _norm_box(self, box) -> tuple:
        """[x1,y1,x2,y2] pixels -> ([0,1]) fractions (x by width, y by height)."""
        return (self._nx(box[0]), self._ny(box[1]),
                self._nx(box[2]), self._ny(box[3]))

    # -- per-item mappers (our result dict -> SDK tuple/dict, NORMALIZED) ---- #
    # NB: only the x/y COORDINATES are normalized; score, class_id, label,
    # track_id, keypoint_id, keypoint score and the segmentation mask bytes are
    # passed through unchanged (they are not coordinates -- header v1.2.0).
    def _to_detection(self, d: dict) -> Optional[tuple]:
        box = d.get("box") or _bbox_from_quad(d.get("quad") or [])
        if not box or len(box) < 4:
            return None
        nx1, ny1, nx2, ny2 = self._norm_box(box)
        label = d.get("cls_name") or d.get("text") or d.get("label") or ""
        return (nx1, ny1, nx2, ny2,
                float(d.get("score", 0.0) or 0.0), str(label),
                int(d.get("cls", 0) or 0))

    def _to_keypoints(self, d: dict) -> Optional[dict]:
        kps = d.get("keypoints") or []
        if not kps:
            return None
        points = []
        for j, p in enumerate(kps):
            # pose: [x,y,conf]; facemesh landmark: [x,y] (no conf -> 1.0 visible)
            score = float(p[2]) if len(p) > 2 else 1.0
            points.append((self._nx(p[0]), self._ny(p[1]), score, j))  # id = index
        inst = {"points": points}
        box = d.get("box")
        if box and len(box) >= 4:
            inst["box"] = self._norm_box(box)   # object box normalized too
            inst["score"] = float(d.get("score", 0.0) or 0.0)
            inst["class_id"] = int(d.get("cls", 0) or 0)
            inst["label"] = str(d.get("cls_name") or d.get("kind") or "person")
        return inst

    def _to_classification(self, d: dict) -> Optional[tuple]:
        # Prefer the face-analysis attribute composite; fall back to a plain
        # label/score classification (boxless image classifier).
        parts, confs = [], []
        for k in ("gender", "age", "emotion"):   # race omitted from the OSD label
            v = d.get(k)
            if v:
                parts.append(str(v))
                c = d.get(k + "_conf")
                if c is not None:
                    confs.append(float(c))
        if parts:
            label = ",".join(parts)
            score = float(min(confs)) if confs else float(d.get("score", 0.0) or 0.0)
            item = (score, int(d.get("cls", 0) or 0), label)
            box = d.get("box")
            if box and len(box) >= 4:
                # SDK v1.1.0+ classification carries an optional ROI box (4th
                # tuple element, normalized) -- attach the face box so the OSD can
                # localize the attribute label (previously dropped for lack of a
                # box channel).
                item = item + (self._norm_box(box),)
            return item
        # generic boxless classification result
        label = d.get("label") or d.get("cls_name")
        if label is not None and not d.get("box"):
            return (float(d.get("score", 0.0) or 0.0), int(d.get("cls", 0) or 0),
                    str(label))
        return None

    def _to_segmentation(self, d: dict) -> Optional[tuple]:
        mask = d.get("mask") or d.get("mask_bytes")   # raw bytes: NOT normalized
        box = d.get("box") or [0.0, 0.0, 0.0, 0.0]
        nx1, ny1, nx2, ny2 = self._norm_box(box)      # ROI box normalized
        return (nx1, ny1, nx2, ny2,
                float(d.get("score", 0.0) or 0.0), int(d.get("cls", 0) or 0),
                str(d.get("cls_name") or d.get("label") or ""),
                mask, int(d.get("mask_w", 0) or 0), int(d.get("mask_h", 0) or 0))

    def _to_tracking(self, e: dict) -> Optional[tuple]:
        box = e.get("box")
        if not box or len(box) < 4:
            return None
        nx1, ny1, nx2, ny2 = self._norm_box(box)
        label = str(e.get("label") or e.get("state") or e.get("kind") or "object")
        return (nx1, ny1, nx2, ny2,
                float(e.get("score", 0.0) or 0.0), int(e.get("cls", 0) or 0),
                label, int(e.get("track_id")))

    def _has_class_attrs(self, d: dict) -> bool:
        return any(d.get(k) for k in self._CLASS_ATTR_KEYS)

    # -- ResultSink ABC ----------------------------------------------------- #
    def emit(self, payload: dict, pts: float) -> None:
        """Legacy best-effort publish; failures are logged and counted."""

        self._emit(payload, pts, strict=False)

    def emit_checked(self, payload: dict, pts: float) -> None:
        """Publish while surfacing native open/send failures to typed callers."""

        self._emit(payload, pts, strict=True)

    def _emit(self, payload: dict, pts: float, *, strict: bool) -> None:
        if not self._ensure_open(strict=strict):
            return
        self._frames += 1
        if not self._fw or not self._fh:
            # Coordinates are pixels; without the frame size we cannot normalize,
            # and sending pixels would render as invisible 1px boxes. Skip + warn
            # rather than emit garbage. The base loop always calls set_frame_size
            # before emit, so this only trips for misuse (a sink driven directly
            # without set_frame_size).
            self._warn_once("frame size unknown (call set_frame_size before "
                            "emit); skipping frame to avoid 1px OSD boxes")
            if strict:
                raise InputValidationError(
                    "frame size is required before checked result publication",
                    operation="result.emit",
                    details={"source": self.source_id},
                )
            return
        pts_us = int(round((pts or 0.0) * 1e6))  # s -> us; inverse of the source
        results = payload.get("results") or []
        events = payload.get("events") or []

        # 1. Tracking is carried in events (retail-vision). When present it
        #    supersedes the raw (untracked) detection results for the same
        #    objects -- send it and we are done for this frame.
        track_items = [t for t in
                       (self._to_tracking(e) for e in events
                        if isinstance(e, dict) and e.get("track_id") is not None)
                       if t is not None]
        if track_items:
            self._safe_send("send_tracking", pts_us, track_items, strict=strict)
            return

        # 2. Partition results by task type (first matching rule wins).
        dets, kpts, cls_items, seg_items = [], [], [], []
        for d in results:
            if not isinstance(d, dict):
                continue
            if d.get("keypoints"):
                inst = self._to_keypoints(d)
                if inst:
                    kpts.append(inst)
            elif d.get("mask") or d.get("mask_bytes"):
                seg_items.append(self._to_segmentation(d))
            elif self._has_class_attrs(d):
                item = self._to_classification(d)
                if item:
                    cls_items.append(item)
            else:
                det = self._to_detection(d)
                if det:
                    dets.append(det)
                else:
                    # boxless label-only classification (generic image classifier)
                    item = self._to_classification(d)
                    if item:
                        cls_items.append(item)

        # 3. Emit each non-empty channel.
        if kpts:
            self._safe_send("send_keypoints", pts_us, kpts, strict=strict)
        if cls_items:
            self._safe_send("send_classification", pts_us, cls_items, strict=strict)
        if seg_items:
            self._safe_send("send_segmentation", pts_us, seg_items, strict=strict)
        # Detections: send when we have boxes, OR when the frame produced nothing
        # at all -- an empty detection list clears the OSD for this frame, the
        # same way built-in inference signals "no objects".
        # NOTE(端侧验证 TODO): whether an empty send_detections also clears a
        # prior keypoint/track overlay is a device-behaviour question to confirm
        # on firmware; today apps are single-task so this is not exercised.
        if dets or not (kpts or cls_items or seg_items):
            self._safe_send("send_detections", pts_us, dets, strict=strict)

    def _safe_send(self, method: str, pts_us: int, items, *, strict: bool = False) -> None:
        """Call one SDK send_* method, swallowing errors so a transient sink
        problem never takes the inference loop down (warns on the first few).
        An oversize rejection is a distinct, actionable failure -- count it
        separately so stats() can flag "results are being dropped for being too
        big" rather than hiding it in a generic error tally."""
        self._send_calls += 1
        try:
            getattr(self._sink, method)(pts_us, items)
        except Exception as e:
            self._send_fail += 1
            if type(e).__name__ == "ResultTooLarge":
                self._oversize += 1
            self._warn_once("%s failed: %s" % (method, e))
            if strict:
                if isinstance(e, KitError):
                    raise
                raise TransportError(
                    f"native result send {method} failed: {e}",
                    operation="result.emit",
                    retryable=bool(getattr(e, "retryable", False)),
                    details={"method": method, "source": self.source_id},
                ) from e

    def stats(self) -> dict:
        """Delivery diagnostics: this adapter's local tallies (frames handed in,
        SDK send attempts, failures, oversize rejections) merged with the SDK
        ResultSink's authoritative wire counters under `wire`. Local only --
        a frame accepted by the socket that the server later drops is not
        reflected until the server-ACK protocol lands (docs/guide/result-push.md)."""
        st = {"frames": self._frames,
              "send_calls": self._send_calls,
              "send_fail": self._send_fail,
              "oversize_rejected": self._oversize}
        wire = getattr(self._sink, "stats", None)
        if callable(wire):
            try:
                st["wire"] = self._sink.stats()
            except Exception:
                pass
        return st

    def emit_meta(self, payload: dict) -> None:
        # Metrics/telemetry are debug-panel only; the official OSD path (and ABI
        # v1) has no meta channel, so this is intentionally a no-op.
        pass

    def _warn_once(self, msg: str) -> None:
        # Never spam the log or block the loop: warn on the first few failures.
        if self.verbose:
            self._warnings.warning("result-sink", "OfficialResultSink: %s", msg)

    def close(self) -> None:
        sink, self._sink = self._sink, None
        if sink is not None:
            try:
                sink.close()
            except KitError:
                raise
            except Exception as exc:
                raise TransportError(
                    f"could not close native result sink: {exc}",
                    operation="result.close",
                    details={"source": self.source_id},
                ) from exc


# Back-compat alias: the registry/tests historically referenced the sink by its
# role name. Both names are the same class.
OsdInjectResultSink = OfficialResultSink


# --- Audio (R8) --------------------------------------------------------------
# PcmFrame + AudioSource are imported from audio_source.py (canonical contract).
class OfficialPcmSource(AudioSource):
    """R8 clean-PCM broker source.

    Connects to `/var/run/recamera/audio.sock` and reads VQE-clean 16k mono PCM
    directly -- no need to take over/close rkipc audio, AEC/denoise handled by
    the firmware (docs/guide/adapter-bootstrap.md §2.3, §5). Upper STT/VAD logic is unchanged
    vs the (future) `AlsaTakeoverSource` workaround.
    """

    def __init__(self, sock: str = OFFICIAL_AUDIO_SOCK, **_ignored):
        self.sock = sock

    def read(self) -> Optional[PcmFrame]:
        # TODO(official-R8): recv a PCM chunk from the audio broker socket.
        raise NotImplementedError(
            "OfficialPcmSource is a migration stub; official audio broker "
            f"({self.sock}) not implemented yet")

    def close(self) -> None:
        pass


# --- Control plane (R4) ------------------------------------------------------
class ControlPlane(ABC):
    """Abstract device control (R4). Applications call these abstract methods;
    they never know whether the backend is the reverse-engineered CGI or the
    official versioned API."""

    @abstractmethod
    def set_inference(self, *, enable: bool, model: Optional[str] = None,
                      fps: Optional[int] = None) -> None:
        raise NotImplementedError

    @abstractmethod
    def snapshot(self) -> bytes:
        raise NotImplementedError


class OfficialControl(ControlPlane):
    """R4 versioned control API (docs/guide/adapter-bootstrap.md §2.4). Migration target for
    the reverse-engineered `CgiControl` workaround."""

    def __init__(self, **_ignored):
        pass

    def set_inference(self, *, enable: bool, model: Optional[str] = None,
                      fps: Optional[int] = None) -> None:
        # TODO(official-R4): call the official versioned control API.
        raise NotImplementedError(
            "OfficialControl is a migration stub; official versioned API not "
            "implemented yet")

    def snapshot(self) -> bytes:
        raise NotImplementedError(
            "OfficialControl is a migration stub; official versioned API not "
            "implemented yet")
