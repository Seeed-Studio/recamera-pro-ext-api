#!/usr/bin/env python3
"""
qrcode-reader -- CPU-only reCamera Pro app (no NPU model).

The first-gen `qrcode-reader` C++ solution decoded QR codes on the CPU with
quirc (manifest pipeline `{"name":"quirc","path":"cpu","task":"qrcode-decode"}`,
no model). This port keeps the same shape: no model, pure OpenCV decode on the
ARM cores.

Migrated to the new kit shape (internal/KIT_APP_SHAPE_SPEC.md §1/§3). This is
the NO-MODEL variant of the shape, and the loop body is correspondingly short:

  frame -> QrDecoder.decode(frame.data)   CPU, ARM cores, stateless per frame
        -> self.emit()                    one "qrcode" event per decoded code

There is deliberately **no `self.pre()` and no infer step**: `needs_model` is
False, so the kit loads no RKNN model, `self.models` stays empty and the kit's
first-frame NPU warm-up is a no-op (`App._warm_up` returns immediately when
there is no model). Calling `self.pre()` here would letterbox the frame to 640
for nobody -- the decoder wants the ORIGINAL camera pixels, because `quad` is
published in original-frame coordinates.

Frame grab / grey warm-up skip / `--every` / hot-reload / FPS + latency stats
all still come from `self.frames()`, exactly as for the model-backed apps.

Run on device (no root / no NPU needed):

    python3 -m kit.run /userdata/local/apps/qrcode-reader --sink stdout
"""
import os

from kit.app import App, run_app
from kit.logic.recording import request_configured_recording
from kit.logic.qrcode import QrDecoder

# Bundled data files (the WeChat QR model dir) are resolved against the app's
# own install dir, not the cwd.
_here = os.path.dirname(os.path.abspath(__file__))


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

    def run(self):
        for frame in self.frames():
            # CPU decode over the ORIGINAL camera pixels; `quad` comes back in
            # original-frame coordinates, which is what the overlay draws.
            codes = self._decoder.decode(frame.data)
            # Flat, overlay-friendly events -- the published contract the
            # /appcenter overlay reads is {kind, text, quad}.
            events = [
                {
                    "kind": "qrcode",
                    "text": r["text"],
                    "quad": r["quad"],   # [[x,y]*4] corners in original px
                }
                for r in codes
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
