#!/usr/bin/env python3
"""
qrcode-reader -- CPU-only reCamera Pro app (no NPU model).

v0.2.0: selectable capture resolution (4K / 1080p / 720p) + moving-target
stability.

Frame paths
-----------
* 720p (legacy): the official R1 frame broker (`/run/recamera/frame.sock`),
  1280x720 NV12 -> RGB via RGA, ~27 fps, low CPU. Unchanged behaviour.
* 4k / 1080p: the firmware's frame broker clamps every subscription to
  1280x720 (verified by geometry sweep), so high resolutions go through the
  go2rtc RTSP main stream (rtsp://admin:admin@127.0.0.1:554/main, 3840x2160
  H.264 30 fps from rkipc). ffmpeg software-decodes to rgb24; 1080p adds
  `-vf scale`. Measured on RV1126B (4x A7): ~4.5 fps steady at 4K, ~7 fps at
  1080p, first-frame latency 8-10 s (GOP + go2rtc on-demand pull). The direct
  rkipc RTSP port :5554 is NOT usable while go2rtc holds the encoder.

Moving-target stability (two-stage decode + quad hold)
------------------------------------------------------
WeChat QR decode costs ~0.2-0.5 s on a full frame but MISSES small codes:
measured on device, one whole-frame pass finds only 2 of 6 chart codes at
every tier. The full scan therefore uses 2x2 overlapping tiles (~1.0 s,
recovers all 6); ROI decode costs ~0.05-0.2 s per crop. So:
  * no target held  -> tiled full-frame scan (all codes published);
  * target(s) held  -> decode only padded ROI crops around the last quads
    (coordinates mapped back to the full frame); after 2 consecutive ROI
    misses, fall back to a full scan;
  * every decoded text keeps its quad for HOLD_SEC (1.0 s) so the overlay
    box does not flicker between slow full-frame scans, and recording
    dedup still sees only fresh detections.

If the RTSP source yields zero frames (go2rtc down / stream dead) the app
falls back to the official 720p broker instead of crash-looping.

v0.2.3: frozen-box fix for 4k/1080p. Decode (~1-1.5 s at 4K) used to run
INSIDE the frame loop; while the main thread decoded, ffmpeg's stdout pipe
filled and ffmpeg stopped draining the RTSP socket. Two failure modes
followed (both measured on device, diag031/diag033):
  * ffmpeg's default vsync re-emitted the SAME stale frame at 30 fps to
    fill the CFR timeline -- 21 frames read across 12 s all had identical
    content hashes -- so the overlay kept re-drawing boxes from one old
    picture ("box frozen at the previous position", "new code never seen");
  * go2rtc eventually dropped the back-pressured consumer -> EOF -> retry
    -> official-broker fallback -> UNCAUGHT EINTERNAL(7) TransportError ->
    process crash loop.
Fix: (1) `-vsync 0` passthrough so every stdout frame is a genuinely new
picture; (2) a background pump thread drains frames continuously and keeps
only the NEWEST one (single slot), so decoding can never block the pipe and
the decoded image is always current; (3) a stall watchdog (no frame for
FRAME_GAP_SEC, or FIRST_FRAME_TIMEOUT for the cold start) clears the
overlay and rebuilds the source; (4) every stream error -- including the
official broker's EINTERNAL -- is caught, clears boxes, and backs off into
a reopen instead of killing the process.

Run on device (no root / no NPU needed):

    python3 -m kit.run /userdata/local/apps/qrcode-reader --sink stdout
"""
import os
import threading
import time

import numpy as np

from kit.app import App, run_app
from kit.logic.recording import request_configured_recording
# App-bundled robust decoder (kit is firmware-owned and NOT shipped per app;
# qrdecode.py adds stdout diagnostics, crash backoff and a wedged-worker
# health probe -- the two 0.2.0 field defects).
from qrdecode import QrDecoder
from kit.adapters.frame_source import FfmpegRtspSource, open_frame_source

# Bundled data files (the WeChat QR model dir) are resolved against the app's
# own install dir, not the cwd.
_here = os.path.dirname(os.path.abspath(__file__))

# go2rtc restream of the rkipc 4K main stream (digest auth admin:admin).
RTSP_MAIN_URL = "rtsp://admin:admin@127.0.0.1:554/main"
RTSP_RESOLUTIONS = {"4k": (3840, 2160), "1080p": (1920, 1080)}
HOLD_SEC = 1.0          # keep last quad this long after a fresh decode
ROI_PAD = 0.6           # ROI padding as a fraction of the quad bbox size
ROI_MIN = 256           # minimum ROI side (px) for the WeChat detector
ROI_MAX_TARGETS = 8     # decode ALL held ROIs per frame (0.2.2: was 2, which
                        # starved multi-code scenes -- only 2 of N codes got
                        # refreshed between full scans)
ROI_MISS_FULL_SCAN = 2  # consecutive ROI misses before a full-frame scan
# Anti-ghost: a hold must be re-confirmed by a FULL-FRAME scan periodically.
# ROI-only hits (possible detector false positives on small crops) can never
# keep a box alive longer than VERIFY_MAX_SEC without full-frame confirmation.
VERIFY_EVERY_SEC = 1.2  # force a full scan when a hold is this stale
VERIFY_MAX_SEC = 2.5    # drop holds unconfirmed by full scan for this long
MAX_HOLDS = 8           # track up to this many codes (even with same text)
# 0.2.3 pump/stall tuning. FIRST_FRAME_TIMEOUT covers the measured 8-10 s
# go2rtc cold-start GOP delay plus warmup; FRAME_GAP_SEC is the max inter-
# frame silence tolerated once the stream has produced frames (the pump
# drains at the DECODER's rate -- ~2.4 fps at 4K -- so 0.5 s is normal).
FIRST_FRAME_TIMEOUT = 25.0
FRAME_GAP_SEC = 6.0
RTSP_MAX_FAILS = 5      # consecutive dead RTSP cycles before broker demote
OFFICIAL_MAX_FAILS = 8  # dead broker cycles before retrying RTSP again


class ScaledRtspSource(FfmpegRtspSource):
    """FfmpegRtspSource + optional hardware-free downscale.

    The stock class decodes the stream at native size; for the 1080p tier we
    insert `-vf scale=W:H` so Python only ever reads W*H*3 bytes per frame.
    """

    def _build_cmd(self):
        cmd = super()._build_cmd()
        # Socket I/O timeout (ffmpeg 4.4 rtsp option, microseconds). Without
        # it a stalled RTSP stream blocks stdout.read() FOREVER: no EOF, no
        # emit, and the front-end overlay freezes with the last box while the
        # go2rtc preview keeps playing -- the "ghost box" defect of 0.2.0.
        # 15 s tolerates the 8-10 s first-frame GOP delay on go2rtc cold start.
        i = cmd.index("-i")
        cmd[i:i] = ["-stimeout", "15000000"]
        if (self.w, self.h) != (3840, 2160):
            j = cmd.index("-pix_fmt")
            cmd[j:j] = ["-vf", "scale=%d:%d" % (self.w, self.h)]
        # CRITICAL (0.2.3): passthrough frame timing. With the default vsync
        # (CFR @ 30 fps) a consumer slower than the stream is fed REPEATS of
        # the same stale frame to fill the timeline -- measured on device:
        # 21 frames read across 12 s all had identical content hashes, which
        # froze the overlay boxes at old positions. -vsync 0 emits every
        # decoded frame exactly once, so each read is a genuinely new image.
        k = cmd.index("-f")
        cmd[k:k] = ["-vsync", "0"]
        return cmd


class _FramePump:
    """Drain the kit frame iterator in a background thread; keep ONE frame.

    Single-slot latest-frame queue: when the producer (ffmpeg/ext broker)
    outruns the consumer (QR decode, 0.2-1.5 s/frame), intermediate frames
    are DROPPED, never queued. This decouples capture from decode so the
    pipe/socket never back-pressures the source -- the 0.2.2 stale-frame
    root cause -- and the decode thread always sees the newest picture.

    Any exception from the underlying iterator (TransportError, EINTERNAL,
    _GracefulStop, ...) is captured, not raised: the main loop reads it via
    `error_text()` and rebuilds the source instead of dying.
    """

    def __init__(self, app):
        self._app = app
        self._cond = threading.Condition()
        self._frame = None
        self._seq = 0
        self._err = None
        self._closed = False
        self._stop = False
        self.started = time.monotonic()
        self.last_arrival = None
        self.dropped = 0
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="qr-frame-pump")
        self._thread.start()

    def _run(self):
        try:
            for frame in self._app.frames():
                with self._cond:
                    if self._stop:
                        break
                    if self._frame is not None:
                        self.dropped += 1
                    self._frame = frame
                    self._seq += 1
                    self.last_arrival = time.monotonic()
                    self._cond.notify_all()
        except BaseException as exc:   # TransportError / _GracefulStop / ...
            with self._cond:
                self._err = exc
        finally:
            with self._cond:
                self._closed = True
                self._cond.notify_all()

    def next(self, last_seq, timeout=0.5):
        """Block until a frame newer than `last_seq` exists.

        Returns (frame, seq), or None on timeout/closure. Because only the
        newest frame is kept, a slow consumer skips the backlog automatically.
        """
        deadline = time.monotonic() + timeout
        with self._cond:
            while self._seq == last_seq:
                if self._closed or self._stop:
                    return None
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._cond.wait(remaining)
            if self._stop:
                return None
            return self._frame, self._seq

    @property
    def closed(self):
        with self._cond:
            return self._closed

    def idle_sec(self):
        """Seconds since the last frame arrived (or since pump start)."""
        with self._cond:
            ref = self.last_arrival if self.last_arrival else self.started
            return time.monotonic() - ref

    def error_text(self):
        with self._cond:
            if self._err is None:
                return "EOF/stall"
            return "%s: %s" % (type(self._err).__name__, self._err)

    def stop(self):
        """Flag shutdown, unblock the producer, and reap the thread."""
        with self._cond:
            self._stop = True
            self._cond.notify_all()
        try:
            self._app._rt.get("src").close()   # EOF the blocking read
        except Exception:
            pass
        self._thread.join(timeout=3.0)


class QrcodeReaderApp(App):
    id = "qrcode-reader"
    name = "QR Code Reader"
    owns_loop = True             # explicit new shape: run() drives self.frames()
    needs_model = False          # CPU-only: no RKNN model, no letterbox, no infer

    def setup(self, config):
        super().setup(config or {})
        # Bundled CPU model files for the WeChatQRCode backend (the firmware's
        # slim cv2 lacks QRCodeDetector). Ignored if a QRCodeDetector build is
        # present. models/ ships inside the app package (build.py includes it).
        # These are Caffe files for the ARM cores -- NOT NPU models, which is
        # why they are not declared in the manifest `models[]`.
        model_dir = os.path.join(_here, "models", "wechat")
        self._decoder = QrDecoder(
            model_dir=model_dir,
            health_path=os.path.join(_here, "models", "qr_health.png"))
        # Holds are a LIST, not a text-keyed dict: the A4 size chart carries
        # several codes with identical text, and a dict collapsed them into
        # one jumping box (0.2.0 ghost defect #2).
        self._held = []          # [{text, quad, seen, verified}]
        self._roi_miss = 0
        self._last_pts = None

    # -- resolution handling ---------------------------------------------- #
    def _resolution(self):
        return str(self.config.get("resolution", "4k")).strip().lower()

    def _reopen_source(self):
        """Replace the kit-opened frame source with the configured one.

        kit's _start_impl always opens the default source (official broker,
        1280x720). frames() reads rt["src"] lazily at iteration time and
        finish() closes whatever rt["src"] holds, so swapping here is safe.
        """
        res = self._resolution()
        if res not in RTSP_RESOLUTIONS:
            return  # 720p: keep the official broker source untouched
        rt = self._rt
        w, h = RTSP_RESOLUTIONS[res]
        old = rt.get("src")
        rt["src"] = ScaledRtspSource(url=RTSP_MAIN_URL, width=w, height=h)
        if old is not None:
            try:
                old.close()
            except Exception:
                pass

    def _fallback_official(self):
        rt = self._rt
        old = rt.get("src")
        rt["src"] = open_frame_source(url=rt["url"], prefer=rt["source"],
                                      input_size=0)
        if old is not None:
            try:
                old.close()
            except Exception:
                pass

    # -- two-stage decode -------------------------------------------------- #
    @staticmethod
    def _roi_crop(arr, quad):
        """Padded, clamped ROI around a quad; returns (crop, ox, oy) or None."""
        try:
            xs = [float(p[0]) for p in quad]
            ys = [float(p[1]) for p in quad]
        except (TypeError, ValueError, IndexError):
            return None
        h, w = arr.shape[:2]
        x0, x1 = min(xs), max(xs)
        y0, y1 = min(ys), max(ys)
        pad = ROI_PAD * max(x1 - x0, y1 - y0, 1.0)
        x0 = int(max(0, x0 - pad)); x1 = int(min(w, x1 + pad))
        y0 = int(max(0, y0 - pad)); y1 = int(min(h, y1 + pad))
        # enforce a minimum side so the detector still has enough pixels
        if x1 - x0 < ROI_MIN:
            grow = (ROI_MIN - (x1 - x0)) // 2 + 1
            x0 = max(0, x0 - grow); x1 = min(w, x1 + grow)
        if y1 - y0 < ROI_MIN:
            grow = (ROI_MIN - (y1 - y0)) // 2 + 1
            y0 = max(0, y0 - grow); y1 = min(h, y1 + grow)
        if x1 - x0 < 32 or y1 - y0 < 32:
            return None
        return np.ascontiguousarray(arr[y0:y1, x0:x1]), x0, y0

    @staticmethod
    def _center_size(quad):
        xs = [float(p[0]) for p in quad]
        ys = [float(p[1]) for p in quad]
        cx = (min(xs) + max(xs)) / 2.0
        cy = (min(ys) + max(ys)) / 2.0
        size = max(max(xs) - min(xs), max(ys) - min(ys), 1.0)
        return cx, cy, size

    def _match_hold(self, text, quad):
        """Nearest hold with same text within 0.75x its size (IoU-lite)."""
        cx, cy, size = self._center_size(quad)
        best, best_d = None, float("inf")
        for h in self._held:
            if h["text"] != text:
                continue
            hx, hy, hs = self._center_size(h["quad"])
            d = ((cx - hx) ** 2 + (cy - hy) ** 2) ** 0.5
            if d < 0.75 * max(size, hs) and d < best_d:
                best, best_d = h, d
        return best

    def _absorb(self, r, now, verified):
        t = str(r.get("text", ""))
        if not t:
            return
        h = self._match_hold(t, r["quad"])
        if h is None:
            if len(self._held) < MAX_HOLDS:
                # ROI-only newcomers start unverified: the next frame forces
                # a full scan, so a hallucinated ROI hit cannot settle in.
                self._held.append({"text": t, "quad": r["quad"],
                                   "seen": now,
                                   "verified": now if verified else 0.0})
        else:
            h["quad"] = r["quad"]
            h["seen"] = now
            if verified:
                h["verified"] = now

    def _decode_frame(self, frame):
        now = time.monotonic()
        arr = frame.data
        # Expire stale holds: no decode for HOLD_SEC, or no FULL-FRAME
        # confirmation for VERIFY_MAX_SEC (anti-hallucination ceiling).
        self._held = [h for h in self._held
                      if now - h["seen"] <= HOLD_SEC
                      and now - h["verified"] <= VERIFY_MAX_SEC]
        results = []
        targets = self._held[:ROI_MAX_TARGETS]
        need_full = (not targets
                     or self._roi_miss >= ROI_MISS_FULL_SCAN
                     or any(now - h["verified"] > VERIFY_EVERY_SEC
                            for h in self._held))
        if targets and not need_full:
            for v in targets:
                crop = self._roi_crop(arr, v["quad"])
                if crop is None:
                    continue
                roi, ox, oy = crop
                for r in self._decoder.decode(roi):
                    r = dict(r)
                    r["quad"] = [[float(x) + ox, float(y) + oy]
                                 for x, y in r["quad"]]
                    results.append(r)
            if results:
                self._roi_miss = 0
            else:
                self._roi_miss += 1
            for r in results:
                self._absorb(r, now, verified=False)
        else:
            # Full-frame scan: TILED 2x2 (0.2.2 multi-code fix). A single
            # WeChat pass over the whole frame finds only 2 of 6 chart codes
            # at EVERY resolution (measured on device); overlapping tiles
            # normalized to ~1920px recover all 6. Quads come back in
            # full-frame px, one result per physical code.
            results = self._decoder.decode_tiled(arr)
            self._roi_miss = 0
            for r in results:
                self._absorb(r, now, verified=True)
        return results

    # -- main loop ---------------------------------------------------------- #
    def _clear_overlay(self):
        """Drop all holds and publish an empty snapshot.

        Called whenever the frame stream ends (EOF / socket timeout): without
        this the front-end keeps the LAST payload's boxes forever while the
        go2rtc preview keeps playing -- the 0.2.0 ghost box.
        """
        self._held = []
        self._roi_miss = 0
        try:
            pts = self._last_pts if self._last_pts is not None \
                else time.monotonic()
            self.emit([], pts, results=[])
        except Exception:
            pass

    def run(self):
        """Pump-driven main loop (0.2.3).

        A background _FramePump drains self.frames() continuously and keeps
        only the newest frame, so decode latency can never back-pressure the
        source (the 0.2.2 stale/frozen-frame root cause). EOF, stall and ANY
        transport exception (incl. the official broker's EINTERNAL that used
        to kill the process) clear the overlay and rebuild the source with
        bounded backoff; the process itself never dies from stream faults.
        """
        use_rtsp = self._resolution() in RTSP_RESOLUTIONS
        self._reopen_source()          # no-op for 720p: keep official broker
        on_official = not use_rtsp
        fails = 0
        while not self._stop_flag:
            pump = _FramePump(self)
            self._pump = pump   # for stats logging in _process
            n, last_seq = 0, 0
            try:
                while not self._stop_flag:
                    got = pump.next(last_seq, timeout=0.5)
                    if got is not None:
                        frame, last_seq = got
                        n += 1
                        self._process(frame)
                        continue
                    if pump.closed:
                        break
                    # No new frame yet. Cold start (GOP + go2rtc on-demand
                    # pull) legitimately takes 8-10 s; once frames flowed, a
                    # FRAME_GAP_SEC silence means the source is wedged.
                    limit = FIRST_FRAME_TIMEOUT if n == 0 else FRAME_GAP_SEC
                    if pump.idle_sec() > limit:
                        break
            finally:
                pump.stop()
            if self._stop_flag:
                break
            # Stream ended (EOF / stall / transport error). Clear the overlay
            # FIRST so no ghost box survives the recovery window.
            print("[app:%s] stream cycle ended after %d frames (%s, dropped "
                  "%d); rebuilding source" % (self.id, n, pump.error_text(),
                                               pump.dropped), flush=True)
            self._clear_overlay()
            fails += 1
            if on_official:
                time.sleep(min(2.0 * fails, 10.0))
                if use_rtsp and fails >= OFFICIAL_MAX_FAILS:
                    on_official = False   # give the RTSP tier another chance
                    fails = 0
                    self._reopen_source()
                else:
                    self._fallback_official()
                continue
            if fails <= RTSP_MAX_FAILS:
                time.sleep(min(2.0 * fails, 8.0))
                self._reopen_source()
                continue
            # RTSP persistently dead: demote to the official 720p broker
            # (degraded but alive) and keep retrying both from there.
            on_official = True
            fails = 0
            self._fallback_official()

    def _process(self, frame):
        self._last_pts = frame.pts
        codes = self._decode_frame(frame)
        # Flat, overlay-friendly events -- the published contract the
        # /appcenter overlay reads is {kind, text, quad}. Held quads are
        # re-emitted so the box stays steady between slow 4K scans. One box
        # per tracked code, even when several codes share the same text.
        events = [
            {
                "kind": "qrcode",
                "text": h["text"],
                "quad": h["quad"],   # [[x,y]*4] corners in original px
            }
            for h in self._held
        ]
        # A code remaining in view is state, not another recording event.
        current_codes = {str(item.get("text", "")) for item in codes[:64]
                         if item.get("text")}
        previous_codes = getattr(self, "_recording_codes", set())
        pending = (getattr(self, "_recording_pending_codes", set()) & current_codes)
        pending |= current_codes - previous_codes
        if pending and request_configured_recording(self, "qrcode", frame.pts):
            pending.clear()
        self._recording_pending_codes = pending
        self._recording_codes = current_codes
        self.emit(events, frame.pts, results=codes)
        # Own stats line: kit's verbose `frame#NNN dets=...` reads
        # _last_payload right when the pump thread resumes the generator,
        # which races the (slow) decode thread's emit and misleadingly shows
        # dets=0 forever. This line reflects what was actually published.
        self._proc_n = getattr(self, "_proc_n", 0) + 1
        if self._proc_n % 16 == 1:
            pump = getattr(self, "_pump", None)
            print("[app:%s] proc#%d codes=%d events=%d held=%d dropped=%s"
                  % (self.id, self._proc_n, len(codes), len(events),
                     len(self._held),
                     pump.dropped if pump is not None else "?"), flush=True)


if __name__ == "__main__":
    run_app(QrcodeReaderApp())
