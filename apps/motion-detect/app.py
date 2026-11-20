"""CPU motion detection with the existing Home Assistant MQTT contract."""
import math

from kit.app import App, run_app

from config_schema import derive_tables, load_schema
from ha_mqtt import HaMqttBridge
from motion_core import MotionCore, retention_seconds


class MotionDetectApp(App):
    id = "motion-detect"
    name = "Motion Detection"
    owns_loop = True
    needs_model = False
    # Own the raw SDK source: the detector needs NV12 Y, without RGB conversion.
    # camera.frames is still explicitly declared in the manifest.
    needs_frames = False
    owns_mqtt = True

    def setup(self, config):
        super().setup(config)
        _, defaults, _ = derive_tables(load_schema())
        for key, value in defaults.items():
            setattr(self, key, config.get(key, value))
        self._core = MotionCore(analysis_width=int(self.analysis_width))
        self._mqtt = None
        self._source = None
        self._active = False
        self._last_motion_pts = 0.0
        self._last_sample_pts = -math.inf
        self._last_notify = 0.0
        self._last_hub_notify = -math.inf
        self._last_result = {"score": 0.0, "changed_ratio": 0.0,
                             "changed_pixels": 0}

    def prepare_runtime(self):
        from recamera_ext import FrameConfig, FrameSource

        divisor = max(1, round(30.0 / float(self.sample_fps)))
        self._source = FrameSource(FrameConfig(fps_divisor=divisor),
                                   timeout_ms=5000)
        self._mqtt = HaMqttBridge(self.config)
        self._publish_hub(False, self._last_result, 0.0)

    def on_params_changed(self, changed):
        if "analysis_width" in changed:
            self._core = MotionCore(analysis_width=int(self.analysis_width))
        if changed & {"mqtt_host", "mqtt_port", "mqtt_user", "mqtt_pass"}:
            if self._mqtt is not None:
                self._mqtt.close()
            self._mqtt = HaMqttBridge(self.config)

    def run(self):
        for frame in self._source:
            self.tick()
            if self._stop_flag:
                break
            now = float(frame.pts_us) / 1e6
            interval = 1.0 / float(self.sample_fps)
            if now - self._last_sample_pts < interval * 0.9:
                continue
            self._last_sample_pts = now
            # downscale() makes a small owned array before the SDK borrow expires.
            self._core.push(now, self._core.downscale(frame.array))
            self._core.trim(now, retention_seconds(
                interval * 1000, self.baseline_gap_ms,
                self.baseline_frames, self.current_frames))
            result = self._core.evaluate(
                now, baseline_gap_ms=self.baseline_gap_ms,
                baseline_frames=self.baseline_frames,
                current_frames=self.current_frames,
                sq_diff_threshold=self.sq_diff_threshold,
                min_changed_ratio=self.min_changed_ratio)
            if result is not None:
                self._step(result, now)

    def _step(self, result, now):
        self._last_result = result
        active = bool(result["active"])
        if active:
            self._last_motion_pts = now
        elif self._active and (now - self._last_motion_pts) * 1000 < self.motion_hold_ms:
            active = True
        if active != self._active:
            self._active = active
            self._report(active, result, now, edge=active)
            self._last_notify = now
        elif now - self._last_notify >= self.heartbeat_sec:
            self._report(active, result, now, edge=False)
            self._last_notify = now
        # Preserve the old platform bridge's independent ten-second heartbeat.
        if now - self._last_hub_notify >= 10.0:
            self._publish_hub(active, result, now)

    def _report(self, active, result, now, *, edge):
        scalars = {key: result[key] for key in
                   ("score", "changed_ratio", "changed_pixels")}
        events = ([{"kind": "motion", **scalars}] if edge and active else [])
        events.append({"kind": "motion_state", "motion": bool(active), **scalars})
        # HA keeps the original MqttSink formatter, topics, discovery and IDs.
        # Hub uses its own authenticated app envelope; it never reformats HA.
        if self._mqtt is not None:
            self._mqtt.emit(events, now)
        self._publish_hub(active, result, now, events=events)

    def _publish_hub(self, active, result, now, *, events=None):
        scalars = {key: result[key] for key in
                   ("score", "changed_ratio", "changed_pixels")}
        if events is None:
            events = [{"kind": "motion_state", "motion": bool(active), **scalars}]
        self.emit_status({"motion": bool(active), **scalars,
                          "daemon_running": True}, events=events, ts=now)
        self._last_hub_notify = now

    def finish(self):
        try:
            if self._rt is not None and getattr(self, "_source", None) is not None:
                self.emit_status({"motion": False, "daemon_running": False,
                                  "score": 0.0, "changed_ratio": 0.0,
                                  "changed_pixels": 0})
        finally:
            try:
                source, self._source = getattr(self, "_source", None), None
                if source is not None:
                    source.close()
            finally:
                try:
                    mqtt, self._mqtt = getattr(self, "_mqtt", None), None
                    if mqtt is not None:
                        mqtt.close()
                finally:
                    super().finish()


if __name__ == "__main__":
    run_app(MotionDetectApp())
