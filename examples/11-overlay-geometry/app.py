#!/usr/bin/env python3
"""
overlay-geometry-demo -- draws free-form overlay geometry, no AI model.

Shows what the canonical Result Hub v2 ``geometry[]`` contract can draw on the
browser overlay without any detection result:

  * a COCO-17 stick figure (lines + points, per-limb colours) that sways
    slightly, in the left third of the picture;
  * a semi-transparent text panel (polygon + point labels) in the top-right
    corner, updated every frame with the frame number and wall-clock time;
  * a scrolling sine-wave polyline chart along the bottom edge.

The app claims ``camera.frames`` only so appmgr launches it on the official
frame.sock (the Hub then associates ``stream.id=main`` and the frame size).
Frame pixels are never read beyond kit's warm-up check; there is no NPU use.
Coordinates are frame pixels (manifest ``coord: pixel_points``).
"""
import math
import time
from collections import deque

from kit.app import App, run_app
from kit.geometry import GeometryBuilder

# COCO-17 keypoint layout in a unit box (x: -0.5..0.5 around the centre,
# y: 0 head .. 1 feet).
_BASE_POSE = [
    (0.00, 0.06),                       # 0 nose
    (-0.03, 0.04), (0.03, 0.04),        # 1-2 eyes
    (-0.07, 0.06), (0.07, 0.06),        # 3-4 ears
    (-0.20, 0.22), (0.20, 0.22),        # 5-6 shoulders
    (-0.30, 0.42), (0.30, 0.42),        # 7-8 elbows
    (-0.34, 0.60), (0.34, 0.60),        # 9-10 wrists
    (-0.12, 0.55), (0.12, 0.55),        # 11-12 hips
    (-0.14, 0.77), (0.14, 0.77),        # 13-14 knees
    (-0.15, 0.98), (0.15, 0.98),        # 15-16 ankles
]
# (edge, colour) -- one colour per limb group.
_LIMBS = [
    ((0, 1), "#ffffff"), ((0, 2), "#ffffff"), ((1, 3), "#ffffff"), ((2, 4), "#ffffff"),
    ((5, 6), "#ffd400"), ((5, 11), "#ffd400"), ((6, 12), "#ffd400"), ((11, 12), "#ffd400"),
    ((5, 7), "#00c8ff"), ((7, 9), "#00c8ff"),       # left arm
    ((6, 8), "#ff4fd8"), ((8, 10), "#ff4fd8"),      # right arm
    ((11, 13), "#3cff6e"), ((13, 15), "#3cff6e"),   # left leg
    ((12, 14), "#ff8a00"), ((14, 16), "#ff8a00"),   # right leg
]
_CHART_SAMPLES = 90


def _clamp(value, upper):
    return min(max(0.0, float(value)), float(upper))


class OverlayGeometryDemoApp(App):
    id = "overlay-geometry-demo"
    name = "Overlay Geometry Demo"
    owns_loop = True
    needs_model = False          # no RKNN model, no letterbox, no NPU

    def setup(self, config):
        super().setup(config or {})
        self._frame_no = 0
        self._wave = deque(maxlen=_CHART_SAMPLES)

    def run(self):
        for frame in self.frames():
            self._frame_no += 1
            w, h = int(frame.w), int(frame.h)
            phase = self._frame_no / 15.0
            self._wave.append(math.sin(phase) * 0.7 + 0.3 * math.sin(phase * 3.1))
            drawing = GeometryBuilder()
            self._draw_skeleton(drawing, w, h, phase)
            self._draw_panel(drawing, w, h)
            self._draw_chart(drawing, w, h)
            self.emit([], frame.pts, geometry=drawing)

    # -- drawing ----------------------------------------------------------- #
    def _draw_skeleton(self, g, w, h, phase):
        cx, top, height = 0.17 * w, 0.12 * h, 0.66 * h
        sway = math.sin(phase) * 0.06            # radians, arms/legs swing
        pts = []
        for index, (ux, uy) in enumerate(_BASE_POSE):
            if index in (7, 8, 9, 10):           # arms swing around shoulders
                sx, sy = _BASE_POSE[5 if index % 2 else 6]
                s = sway if index % 2 else -sway
                dx, dy = ux - sx, uy - sy
                ux = sx + dx * math.cos(s) - dy * math.sin(s)
                uy = sy + dx * math.sin(s) + dy * math.cos(s)
            elif index in (13, 14, 15, 16):
                ux += (sway if index % 2 else -sway) * (uy - 0.55)
            pts.append((_clamp(cx + ux * height, w), _clamp(top + uy * height, h)))
        for edge_index, ((a, b), color) in enumerate(_LIMBS):
            g.line(pts[a], pts[b], id=f"limb-{edge_index}", color=color, line_width=4)
        for index, (x, y) in enumerate(pts):
            g.point(x, y, id=f"kpt-{index}", color="#ff3040", point_radius=4)

    def _draw_panel(self, g, w, h):
        x1, y1, x2 = 0.62 * w, 0.05 * h, 0.97 * w
        lines = [
            ("Overlay geometry demo", "#ffffff"),
            (f"frame #{self._frame_no}", "#ffd400"),
            ("time " + time.strftime("%H:%M:%S"), "#00c8ff"),
            (f"skeleton: 17 kpts / {len(_LIMBS)} limbs", "#3cff6e"),
            (f"chart: {len(self._wave)} samples", "#ff8a00"),
            ("custom text here", "#ff4fd8"),
        ]
        step = 0.06 * h
        y2 = y1 + step * (len(lines) + 0.6)
        g.polygon(((x1, y1), (x2, y1), (x2, y2), (x1, y2)), id="panel-bg",
                  color="#ffffff", line_width=1.5, fill=True,
                  fill_color="#10203080", opacity=0.6)
        for index, (text, color) in enumerate(lines):
            g.point(x1 + 0.015 * w, y1 + step * (index + 1.2), id=f"panel-line-{index}",
                    label=text, color=color, point_radius=3)

    def _draw_chart(self, g, w, h):
        left, right = 0.05 * w, 0.95 * w
        mid, amp = 0.88 * h, 0.07 * h
        g.polyline(((left, mid - amp - 4), (left, mid + amp + 4), (right, mid + amp + 4)),
                   id="chart-axes", color="#c0c0c0", line_width=1.5)
        g.line((left, mid), (right, mid), id="chart-zero", color="#808080",
               line_width=1, opacity=0.6)
        if len(self._wave) >= 2:
            dx = (right - left) / (_CHART_SAMPLES - 1)
            pts = [(_clamp(left + i * dx, w), _clamp(mid - v * amp, h))
                   for i, v in enumerate(self._wave)]
            g.polyline(pts, id="chart-sine", color="#00ffa0", line_width=2.5)
            g.point(*pts[-1], id="chart-head", label=f"{self._wave[-1]:+.2f}",
                    color="#00ffa0", point_radius=5)


if __name__ == "__main__":
    run_app(OverlayGeometryDemoApp())
