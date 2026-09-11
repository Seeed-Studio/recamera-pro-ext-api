# Result Overlay and Frontend Boxes

A Kit detection App does not draw boxes on the official video preview just
because it calls `self.emit(results=...)` or logs `dets=N`. The browser overlay
and the RTSP/recording burn-in are two separate rendering paths, and each needs
an explicit manifest contract. This reference is the authority for both.

## Two rendering paths

- **Browser canvas overlay (Result Hub).** The device frontend reads published
  results from the Result Hub and draws boxes itself. This is the default way a
  detection App becomes visible and needs the `output` + `render.boxes`
  contract below. It does not modify the encoded stream.
- **Stream burn-in OSD.** Boxes are composited into the RTSP/recording video
  itself. This is an additional opt-in declared with `render.stream_osd` and
  enabled per app by the user; it is never on by default.

Drawing with `cv2.rectangle()` on an app-owned image produces a frame the App
owns; it does not reach the official preview unless that image is itself
published through a supported sink. Do not claim frontend boxes from OpenCV
drawing alone.

## Browser overlay contract

For a Kit App that declares a detector model and emits `results`, the manifest
must declare all of the following. The validator reports
`missing_detection_render_contract` when any part is absent.

- `output.contract_version: 2`
- `output.sink: "ws"`
- a direct `output.fields[]` entry named exactly `box`, sourced from
  `results[].box`, with `coord` set to either `pixel_xyxy` or
  `normalized_xyxy`. The coordinate space must be consistent with what the App
  actually emits; an undeclared or mismatched space is treated as `unknown` and
  the frontend draws nothing.
- `render.schema_version: 1` with a `render.boxes` object. Any
  `render.boxes.label`/`color_by` reference must name a declared `output.fields[]`
  entry, otherwise the validator reports `invalid_detection_render_reference`.

The current official Kit detector convention uses original-frame pixel `xyxy`
boxes (`coord: "pixel_xyxy"`). Use `normalized_xyxy` only when the runtime
results genuinely use normalized `[0,1]` coordinates. Direct
`recamera_ext.ResultSink` Apps keep their own separate normalized-coordinate
contract and are not governed by this Kit `render` block.

## Stream burn-in OSD contract

To also burn boxes into the RTSP/recording stream, declare:

```json
"render": {
  "schema_version": 1,
  "boxes": {"label": "cls_name", "color_by": "cls_name", "line_width": 2},
  "stream_osd": {"supported": ["boxes"], "default": false}
}
```

`stream_osd.default` must be `false`; the user opts in per app. The opt-in is a
device App Center call, conceptually
`PUT /api/app-center/v1/apps/<id>/visualization` with body
`{"stream_burn_in": {"enabled": true}}`. This skill never performs that call; it
only ensures the manifest declares the capability so the option exists. The
validator reports `invalid_stream_osd`, `invalid_stream_osd_supported`, or
`invalid_stream_osd_default` when `stream_osd` is malformed, `supported` is not
exactly `["boxes"]`, or `default` is not `false`, and it warns
`missing_stream_osd` when a box field exists but burn-in is not declared.

## Publishing path reminder

Boxes only reach the frontend through the managed result endpoint. A
model/output App must use `instances.endpoint_mode: "allocated"` and exactly one
`result.publish` claim with `mode: "brokered"`, and publish through
`kit.App.emit()`. With `shared`, AppMgr does not inject
`RECAMERA_RESULT_GATEWAY_SOCK`; Kit falls back to its child-owned `8124` sink
and the Result Hub never receives the results, so no overlay appears regardless
of how correct the `render` block is. See
[managed-runtime.md](managed-runtime.md) and
[manifest-contract.md](manifest-contract.md).

## Debugging "no boxes on the frontend"

Check in order:

1. `result.publish` is `brokered` and `endpoint_mode` is `allocated` (otherwise
   results never reach the hub).
2. `output.contract_version` is `2`, `output.sink` is `"ws"`, and a field named
   `box` maps from `results[].box`.
3. The declared `coord` matches the coordinate space the App actually emits.
4. `render.schema_version` is `1` and `render.boxes` references real fields.
5. For stream burn-in specifically, `render.stream_osd.default` is `false` and
   the user has enabled the visualization opt-in on the device.

A generated archive or a passing static validator never proves the overlay is
visible; confirm against the Result Hub and the actual device frontend when a
device is available.
