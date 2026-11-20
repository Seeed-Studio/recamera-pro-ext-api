# reCamera Pro (Muse gadget)

This device is a Seeed reCamera Pro: RV1126B (4× Cortex-A53, 3 TOPS NPU),
2 GB RAM, 8 MP MIPI camera, speaker, microphone, eMMC, Ethernet and Wi-Fi.
There is no display. Muse runs here as a headless Linux gadget.

The camera/audio/app/GPIO commands below call the on-device
reCamera-Intellisense SDK over the local reCamera API. They are read-only
inspection commands; the SDK's destructive operations (storage format, reboot,
file delete, GPIO writes, rule changes) are intentionally not exposed.

- `camera.info` — serial, firmware, sensor and base-board model.
- `camera.resources` — CPU, NPU, memory and storage utilisation.
- `camera.models` — installed AI detection models and their labels.
- `camera.events` — recent detection events with timestamps and file paths
  (optional `start_unix_ms`, `end_unix_ms`).
- `camera.rules` — active detection rules (labels, confidence, regions).
- `camera.records` — recorded images/clips (optional `path`, `limit`, `offset`).
- `camera.video` — main/sub stream encoder settings (optional `stream`).
- `camera.snapshot` — capture one JPEG on the device. Returns its on-device
  path and size; the image bytes are not sent to Muse. Optional `output`
  (absolute on-device directory) and `timeout`.
- `audio.models` — AcousticsLab sound-detection heads; active one marked.
- `audio.sources` — recording sources and currently producible sound classes.
- `apps.list` — App Center applications and their status.
- `gpio.list` — GPIO pins and their configuration (read-only).
- `recamera.time` — clock, timezone and NTP configuration.

Use `camera.snapshot` when the owner asks for a fresh photo; report the saved
path. Do not claim to show or stream the image. The speaker and microphone are
used by the device's own voice/AcousticsLab apps; these commands do not play
audio or open a live microphone. Ask before changing camera, storage, network
or recording configuration.