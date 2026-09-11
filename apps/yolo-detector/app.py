#!/usr/bin/env python3
"""
yolo-detector -- the first reCamera Pro self-hosted app (kit-design §6).

First app migrated to the new kit shape (internal/KIT_APP_SHAPE_SPEC.md §1):
the whole pipeline is an explicit `run()` loop -- pre / infer / post / emit are
four readable lines of ordinary Python. Frame grab, release, warm-up skipping,
config hot-reload, model loading and output fan-out stay with the kit.

Run on device (inference requires root):

    python3 -m kit.run /userdata/local/apps/yolo-detector \
        --model models/yolo8n_rawhead_int8.rknn --conf 0.35

Publishes per-frame JSON to a local WebSocket (default 0.0.0.0:8124) for the
/appcenter overlay to subscribe to; use --sink stdout for plain debug output.
"""

from kit.app import App, run_app
from kit.runtime.postprocess.detect import postprocess
from kit import events as E


class YoloDetectorApp(App):
    id = "yolo-detector"
    name = "YOLO Detector"
    owns_loop = True          # explicit new shape: run() drives self.frames()
    # Only boxes are consumed -- never frame.data pixels -- so the frame source
    # can letterbox on RGA into data itself (see App.model_frame).
    model_frame = "hw-direct"
    model_dma_input = True    # consume the borrowed frame within this loop

    # `conf` / `iou` are auto-bound from manifest config_schema (and re-bound on
    # SIGHUP, since both are apply:"live") -- no setup/on_config_reload needed.

    def run(self):
        for frame in self.frames():
            x = self.pre(frame)
            outs = self.models.det.infer(x)
            dets = postprocess(outs, x.info,
                               conf_thres=self.conf, iou_thres=self.iou)
            self.emit([E.detection(d) for d in dets], frame.pts, results=dets)


if __name__ == "__main__":
    run_app(YoloDetectorApp())
