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
WeChat QR decode costs ~0.8 s on a full 4K frame but only ~0.2-0.3 s on a
1280x720 ROI. So:
  * no target held  -> full-frame scan;
  * target(s) held  -> decode only padded ROI crops around the last quads
    (coordinates mapped back to the full frame); after 2 consecutive ROI
    misses, fall back to a full scan;
  * every decoded text keeps its quad for HOLD_SEC (1.0 s) so the overlay
    box does not flicker between slow full-frame scans, and recording
    dedup still sees only fresh detections.

If the RTSP source yields zero frames (go2rtc down / stream dead) the app
falls back to the official 720p broker instead of crash-looping.

Run on device (no root / no NPU needed):

    python3 -m kit.run /userdata/local/apps/qrcode-reader --sink stdout
"""
import os
import time

import numpy as np

from kit.app import App, run_app
from kit.logic.recording import request_configured_recording
from kit.logic.qrcode import QrDecoder
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
ROI_MAX_TARGETS = 2     # decode at most this many held ROIs per frame
ROI_MISS_FULL_SCAN = 2  # consecutive ROI misses before a full-frame scan


class ScaledRtspSource(FfmpegRtspSource):
    """FfmpegRtspSource + optional hardware-free downscale.

    The stock class decodes the stream at native size; for the 1080p tier we
    insert `-vf scale=W:H` so Python only ever reads W*H*3 bytes per frame.
    """

    def _build_cmd(self):
        cmd = super()._build_cmd()
        if (self.w, self.h) != (3840, 2160):
            i = cmd.index("-pix_fmt")
            cmd[i:i] = ["-vf", "scale=%d:%d" % (self.w, self.h)]
        return cmd


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
        self._decoder = QrDecoder(model_dir=model_dir)
        self._held = {}          # text -> {"quad": [[x,y]*4], "seen": monotonic}
        self._roi_miss = 0

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

    def _decode_frame(self, frame):
        now = time.monotonic()
        arr = frame.data
        # expire stale holds
        self._held = {t: v for t, v in self._held.items()
                      if now - v["seen"] <= HOLD_SEC}
        results = []
        targets = list(self._held.values())[:ROI_MAX_TARGETS]
        if targets:
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
        if not targets or self._roi_miss >= ROI_MISS_FULL_SCAN:
            # Full-frame scan: original camera pixels, quad in full-frame px.
            results = self._decoder.decode(arr)
            self._roi_miss = 0
        for r in results:
            t = str(r.get("text", ""))
            if t:
                self._held[t] = {"quad": r["quad"], "seen": now}
        return results

    # -- main loop ---------------------------------------------------------- #
    def run(self):
        self._reopen_source()
        fell_back = False
        while True:
            n = 0
            for frame in self.frames():
                n += 1
                self._process(frame)
            # RTSP EOF with zero frames -> go2rtc/stream unavailable.
            # Fall back to the official 720p broker once, then stop normally.
            if (n == 0 and not fell_back and not self._stop_flag
                    and self._resolution() in RTSP_RESOLUTIONS):
                fell_back = True
                self._fallback_official()
                continue
            break

    def _process(self, frame):
        codes = self._decode_frame(frame)
        # Flat, overlay-friendly events -- the published contract the
        # /appcenter overlay reads is {kind, text, quad}. Held quads are
        # re-emitted so the box stays steady between slow 4K scans.
        events = [
            {
                "kind": "qrcode",
                "text": t,
                "quad": v["quad"],   # [[x,y]*4] corners in original px
            }
            for t, v in self._held.items()
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


if __name__ == "__main__":
    run_app(QrcodeReaderApp())
