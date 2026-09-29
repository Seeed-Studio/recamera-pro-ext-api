#!/usr/bin/env python3
"""
overlay-plugin-demo -- minimal no-inference app that declares a ui.overlay plugin.

Companion to ``web/overlay.html``: the manifest's ``ui.overlay`` block points
the App Center front end at that self-contained document, which the host
fetches and mounts in a sandboxed iframe to draw this app's own results.  The
Python side itself stays boring on purpose -- it emits two declarative
geometry primitives per frame (the same contract example 11 uses) so the
declarative canvas remains the fallback whenever the plugin is absent,
unsupported, or torn down:

  * a drifting inspection zone (polygon);
  * one labeled anchor point inside it.

No model, no NPU.  Camera frames are used only for the frame size and stream
association, exactly like example 11.
"""
import math
import time

from kit.app import App, run_app
from kit.geometry import GeometryBuilder


class OverlayPluginDemoApp(App):
    id = "overlay-plugin-demo"
    name = "Overlay Plugin Demo"
    owns_loop = True
    needs_model = False          # no RKNN model, no letterbox, no NPU

    def setup(self, config):
        super().setup(config or {})
        self._frame_no = 0

    def run(self):
        for frame in self.frames():
            self._frame_no += 1
            w, h = int(frame.w), int(frame.h)
            t = time.monotonic()
            g = GeometryBuilder()
            self._draw_zone(g, w, h, t)
            self._draw_anchor(g, w, h, t)
            self.emit([], frame.pts, geometry=g)

    # -- drawing ----------------------------------------------------------- #
    def _draw_zone(self, g, w, h, t):
        """A rounded-feeling inspection zone drifting sideways."""
        cw = 0.34 * w
        ch = 0.30 * h
        x0 = w * (0.5 - 0.22 * math.sin(t / 3.0)) - cw / 2
        y0 = 0.16 * h
        g.polygon(((x0, y0), (x0 + cw, y0), (x0 + cw, y0 + ch), (x0, y0 + ch)),
                  id="zone", color="#64ffda", line_width=2.5,
                  fill=True, fill_color="#64ffda40", opacity=0.5)

    def _draw_anchor(self, g, w, h, t):
        """One labeled point breathing inside the zone."""
        cx = 0.5 * w + 0.22 * w * math.sin(t / 3.0)
        cy = 0.31 * h + 0.03 * h * math.sin(t / 1.7)
        g.point(cx, cy, id="anchor", label=f"frame #{self._frame_no}",
                color="#ffb4a2", point_radius=5)


if __name__ == "__main__":
    run_app(OverlayPluginDemoApp())
