#!/usr/bin/env python3
"""
depth-estimation -- monocular relative depth on the RV1126B NPU.

Port of the SG2002 `solutions/depth-estimation` contract onto reCamera Pro, with
a better head: MiDaS v2.1 small at 256x256 FP16 (50.7 ms mean on device) in
place of FastDepth 224 BF16. The model choice, the conversion flags and the
device latency measurements are in
`apps/face-recognition/evaluation/depth-model-selection.md`.

`run()` owns the loop and reads top to bottom (internal/KIT_APP_SHAPE_SPEC.md
§1):

  frame -> self.pre()          RGA letterbox to 256 (manifest models[0].input)
        -> self.models.depth   MiDaS -> [1, 256, 256] relative INVERSE depth
        -> depth_map.*         ★business★ valid-ROI stats, proximity, grid
        -> planarity           ★business★ per-ROI plane fit, only if configured
        -> self.emit()         grid cells as results[] (OSD) + `extra`

★Sign★ MiDaS predicts inverse depth, so **larger = nearer** -- the OPPOSITE of
the FastDepth map the SG2002 solution reduces. The payload says so explicitly
(`depth.smaller_is_nearer: false`) rather than leaving a consumer to infer it
from the numbers; everything else in the `depth` block keeps the SG2002 field
names so an upstream integration can be reused.

★Normalisation★ the MiDaS ONNX carries its ImageNet Sub/Div *inside the graph*,
so the RKNN was built with `--mean 0,0,0 --std 255,255,255` and the model wants
RAW uint8 RGB. `self.pre()` hands over exactly that -- do not scale it here.

`model_frame = "hw-direct"`: the depth head only ever looks at the 256 letterbox
and nothing in this app reads original-resolution pixels, so the frame source
letterboxes on RGA straight into `frame.data` and skips the full-resolution
NV12->RGB convert entirely. Grid boxes are mapped back to original camera pixels
through the letterbox info, so the OSD lines up.

Run on device (inference requires root):

    python3 -m kit.run /userdata/local/apps/depth-estimation/app.py \
        --sink ws --port 8124
"""
import time

import numpy as np

import depth_map as dm
import planarity

from kit.app import App, run_app

MODEL_TAG = "rv1126b:midas_v21_small_256@fp16"


def depth_event(extra):
    """One flat `depth` event per frame -- mechanical restatement of `extra`.

    `extra` rides the WebSocket for the overlay, but the declarative output
    plumbing only sees `results` and `events`
    (`kit/adapters/output_sink.py:build_namespace`), and the Home Assistant
    state document unions the SCALAR fields of every event
    (`kit/adapters/mqtt_sink.py:_build_state`). So the same numbers are restated
    once as an event: that is what makes `(events.depth | last).nearest_value`
    mappable to MQTT and `value_json.summary.nearest_value` readable in HA,
    without an app writing any output code.

    Copies and renames only -- no threshold, no state (spec §5.3).
    """
    d = extra["depth"]
    near = extra.get("nearest") or {}
    return {
        "kind": "depth",
        "nearest_value": near.get("value"),
        "nearest_near": near.get("near"),
        "nearest_box": near.get("box"),
        "nearest_row": near.get("row"),
        "nearest_col": near.get("col"),
        "min": d["min"],
        "max": d["max"],
        "mean": d["mean"],
        "p5": d["p5"],
        "p95": d["p95"],
        "smaller_is_nearer": d["smaller_is_nearer"],
        "grid": extra["grid"],
        "grid_size": extra["grid_size"],
    }


class DepthEstimationApp(App):
    id = "depth-estimation"
    name = "Depth Estimation"
    owns_loop = True
    # Nothing here reads original-resolution pixels -- the depth map, the grid
    # and every ROI live in letterbox space -- so take the cheapest frame path.
    model_frame = "hw-direct"
    model_dma_input = True
    input_size = 256
    # No detector, so the kit's COCO80 default would be meaningless; the grid
    # cells carry their own near/mid/far label.
    class_names = ("far", "mid", "near")

    # config_schema keys (grid_cols / grid_rows / near_percentile /
    # emit_interval / publish_map / depth_roi) are auto-bound onto self by
    # start(), and re-bound on SIGHUP since all six are apply:"live". The
    # defaults below only matter to a hand-constructed instance in a test.
    grid_cols = 4
    grid_rows = 3
    near_percentile = 95.0
    emit_interval = 1
    publish_map = False
    depth_roi = ""

    _rois = ()
    _fidx = 0

    def setup(self, config):
        super().setup(config)
        self._rois = dm.parse_rois(self.depth_roi)
        self._fidx = 0

    def on_params_changed(self, changed):
        # `depth_roi` is the one knob with a derived object behind it (the
        # parsed ROI list); everything else is a plain scalar the auto-bind
        # already replaced.
        if "depth_roi" in changed:
            self._rois = dm.parse_rois(self.depth_roi)

    # -- one frame --------------------------------------------------------- #
    def analyse(self, depth, info):
        """Reduce one depth map to (results, extra). Pure -- no I/O, no frame.

        Split out of `run()` so the whole per-frame contract is testable off a
        synthetic depth map, with no camera and no NPU.
        """
        depth = np.squeeze(np.asarray(depth, dtype=np.float32))
        if depth.ndim != 2:
            raise ValueError(f"depth model must return one HxW map, got "
                             f"{depth.shape}")
        l, t, r, b = dm.valid_region(depth.shape, info)
        view = depth[t:b, l:r]

        stats = dm.frame_stats(view)
        prox = dm.proximity(view, stats["p5"], stats["p95"])
        cells = dm.grid_cells(prox, self.grid_rows, self.grid_cols,
                              self.near_percentile)

        results = []
        grid = []
        best = None
        for r_i, row in enumerate(cells):
            grid_row = []
            for c_i, cell in enumerate(row):
                x0, y0 = dm.to_original(l + cell["x0"], t + cell["y0"], info)
                x1, y1 = dm.to_original(l + cell["x1"], t + cell["y1"], info)
                box = [round(x0, 1), round(y0, 1), round(x1, 1), round(y1, 1)]
                label = dm.label_of(cell["mean"])
                results.append({
                    "box": box,
                    "label": label,
                    # `cls_name` mirrors `label` so an overlay written against
                    # the detector shape (results[].cls_name) draws these too.
                    "cls_name": label,
                    "cls": ("far", "mid", "near").index(label),
                    "score": round(cell["mean"], 4),
                })
                grid_row.append(round(cell["mean"], 4))
                # ★Which zone wins★ the `near_percentile` value, not the mean:
                # a person standing in one corner of a zone must beat a zone
                # that is uniformly middle-distance. ★What gets REPORTED★ is
                # that zone's MEAN. The percentile itself saturates at 1.0 for
                # any zone holding content at the near end of the stabilised
                # range -- measured on device, the winner sat at exactly 1.000
                # on all 20 consecutive frames, which is useless as a sensor.
                if best is None or cell["near"] > best[0]:
                    best = (cell["near"], box, cell["mean"], r_i, c_i)
            grid.append(grid_row)

        ox0, oy0 = dm.to_original(l, t, info)
        ox1, oy1 = dm.to_original(r, b, info)
        extra = {
            "model_tag": MODEL_TAG,
            "depth": {
                "unit": "relative",
                # MiDaS = inverse depth. Stated, never inferred.
                "smaller_is_nearer": False,
                "source_size": [int(getattr(info, "orig_w", 0) or 0),
                                int(getattr(info, "orig_h", 0) or 0)],
                "valid_roi": [round(ox0, 1), round(oy0, 1),
                              round(ox1 - ox0, 1), round(oy1 - oy0, 1)],
                "min": round(stats["min"], 4),
                "max": round(stats["max"], 4),
                "mean": round(stats["mean"], 4),
                "p5": round(stats["p5"], 4),
                "p95": round(stats["p95"], 4),
            },
            "grid": grid,
            "grid_size": [int(self.grid_cols), int(self.grid_rows)],
            "nearest": ({"box": best[1],
                         "value": round(float(best[2]), 4),
                         "near": round(float(best[0]), 4),
                         "row": best[3], "col": best[4]}
                        if best is not None else None),
        }

        if self._rois:
            extra["rois"] = self._roi_rows(view, info)
        if self.publish_map:
            extra["depth_map"] = dm.depth_map_payload(prox)
        return results, extra

    def _roi_rows(self, view, info):
        """Plane fit per configured ROI -- how flat that part of the scene is.

        The ROI is normalised (0..1 of the ORIGINAL frame); `planarity_from_depth`
        wants an image-space xywh box plus the image size it belongs to, and does
        the mapping onto depth indices itself.
        """
        w = float(getattr(info, "orig_w", 0) or 0)
        h = float(getattr(info, "orig_h", 0) or 0)
        vh, vw = view.shape[:2]
        if w <= 0 or h <= 0:
            w, h = float(vw), float(vh)
        rows = []
        for rx, ry, rw, rh in self._rois:
            box = (rx * w, ry * h, rw * w, rh * h)
            try:
                res = planarity.planarity_from_depth(view, box,
                                                     image_size=(w, h))
            except ValueError as e:          # a bad ROI costs this ROI only
                rows.append({"roi": [rx, ry, rw, rh], "error": str(e)})
                continue
            rows.append({
                "roi": [rx, ry, rw, rh],
                "planarity": round(res["planarity"], 4),
                "relief": round(res["relief"], 4),
                "score": round(res["score"], 4),
                "n_samples": res["n_samples"],
            })
        return rows

    def run(self):
        for frame in self.frames():
            self._fidx += 1
            every = max(1, int(self.emit_interval))
            if self._fidx % every:
                continue                     # skip the inference too, not just
                                             # the publish -- that is the point
            x = self.pre(frame)
            t0 = time.monotonic()
            outs = self.models.depth.infer(x)
            infer_ms = (time.monotonic() - t0) * 1000.0
            depth = outs[0] if isinstance(outs, (list, tuple)) else outs
            results, extra = self.analyse(depth, x.info)
            extra["inference_time_ms"] = round(infer_ms, 3)
            self.emit([depth_event(extra)], frame.pts, results=results,
                      extra=extra)


if __name__ == "__main__":
    run_app(DepthEstimationApp())
