# reCamera Local — Reference

Detailed schemas for the `recamera-local` skill. Read this when constructing
structured arguments (rules, triggers, ISP values); the command list lives in
[SKILL.md](SKILL.md) and per-command usage in `rcl <command> --help`.
For high-level watch/alert requests, first read [OBSERVATION.md](OBSERVATION.md): these primitives do not by themselves provide durable task or transition semantics.

## Contents

- [Detection rule schema](#detection-rule-schema)
- [Recording sources](#recording-sources)
- [Schedule ranges](#schedule-ranges)
- [Record trigger schema](#record-trigger-schema)
- [Detection events and wait_event](#detection-events-and-wait_event)
- [Capture](#capture)
- [Records and files](#records-and-files)
- [Storage slots](#storage-slots)
- [GPIO](#gpio)
- [ISP image settings](#isp-image-settings)
- [Acoustic heads](#acoustic-heads)
- [App Center](#app-center)
- [Video encode](#video-encode)
- [Result push (notify)](#result-push-notify)
- [Battery and backup](#battery-and-backup)
- [Serve-mode protocol](#serve-mode-protocol)

## Detection rule schema

For `set_detection_rules rules=[...]` (and the `inference_set` trigger):

```json
{
  "name": "front-door-person",
  "debounce_times": 3,
  "confidence_range_filter": [0.25, 1.0],
  "label_filter": ["person"],
  "source_filter": ["builtin"],
  "region_filter": [[[0.1, 0.1], [0.9, 0.1], [0.9, 0.9], [0.1, 0.9]]]
}
```

- `label_filter` holds label **names** from `get_detection_models_info` (vision)
  or `get_record_sources` / `get_active_acoustic_model` (sound) — never indexes.
  Empty matches any label.
- `source_filter` holds source ids from `get_record_sources` (`"builtin"`,
  `"acousticslab"`, app ids). **Empty matches EVERY source** — including
  acoustic classifications — so `set_detection_rules` defaults omitted filters
  to `["builtin"]`. Pass an explicit `[]` only for a genuine all-sources rule.
- `region_filter` is a list of polygons of normalized `[x, y]` in `[0, 1]`;
  omit/null = full frame. Only sources with `supports_roi` honor it.
- `confidence_range_filter` is `[min, max]`, both in `[0.0, 1.0]`, `min <= max`
  (default `[0.25, 1.0]`). `debounce_times` defaults to 3 consecutive matching
  frames (AcousticsLab hops are ~960 ms apart, so N hops ≈ N seconds).
- `set_detection_rules` validates source ids and labels against
  `get_record_sources` and fails loudly on mismatches; it also enables the
  rule pipeline with a JPG writer and auto-enables internal storage
  (disable via `ensure_writer=false` / `ensure_storage=false`).

## Recording sources

`get_record_sources` → the recording-rule sources this firmware exposes:

```json
[{"id": "builtin", "kind": "builtin", "name": "Built-in Vision",
  "running": true, "frame_capable": true, "event_capable": true,
  "supports_roi": true, "classes": []},
 {"id": "acousticslab", "kind": "system", "name": "AcousticsLab",
  "running": true, "frame_capable": false, "event_capable": true,
  "supports_roi": false, "classes": ["Cat", "Dog"]}]
```

`classes` is the union of labels the source can currently produce (empty when
unknowable — the `builtin` vision source's labels follow the selected model,
see `get_detection_models_info`). A source with `running: false` belongs to a
stopped app (see [App Center](#app-center)) and produces nothing.

## Schedule ranges

```json
[{"start": "Mon 08:00:00", "end": "Mon 18:00:00"}]
```

Three-letter day; `Day 24:00:00` is valid. `null`, `[]`, or omitting the
argument disables the schedule (rule active 24/7). Applies to
`set_detection_schedule` / `set_schedule_rule` (aliases).

## Record trigger schema

Tagged union on `kind` for `set_record_trigger trigger={...}`. Only one kind
is active at a time; switching preserves the other kinds' remembered settings
(read-modify-write).

```json
{"kind": "inference_set", "rules": [{"name": "person", "label_filter": ["person"]}]}
{"kind": "inference_set", "rules": [{"name": "alarm", "source_filter": ["acousticslab"], "label_filter": ["Cat"], "debounce_times": 3}]}
{"kind": "timer", "interval_seconds": 60}
{"kind": "gpio", "num": 106, "state": "PULL_UP", "signal": "FALLING", "debounce_ms": 50}
{"kind": "tty", "name": "ttyS4", "command": "SHOOT"}
{"kind": "http"}
{"kind": "always_on"}
```

- `gpio`: one of `name`/`num`; `state` ∈ `DISABLED|FLOATING|PULL_UP|PULL_DOWN`;
  `signal` ∈ `HIGH|LOW|RISING|FALLING`. Available pins: `get_status` /
  `list_gpios`.
- Sound-event recording (the retired `sed` kind): the second `inference_set`
  example above; firmware migrates legacy `dSED` sections to this shape at boot
  (window → debounce hops, ~960 ms each).
- `http`: after selecting it, fire one-shot events with `activate_http_trigger`.
- `set_record_config` enables the pipeline and picks the writer:
  `rule_enabled=true writer_format=JPG|MP4|RAW` (`writer_interval_ms` = MP4
  clip length in ms).

## Detection events and wait_event

Normalized event (from `get_detection_events` / `wait_event`):

```json
{"timestamp": "2026-04-20T12:34:56Z", "timestamp_unix_ms": 1745152496000,
 "rule_name": "front-door-person", "snapshot_path": "/mnt/rc_mmcblk0p8/reCamera/....jpg"}
```

`snapshot_path` is present when the writer produced a file; it is a local
path — open it directly (or `read_file`), no fetch needed.

`wait_event timeout_s=60 since_unix_ms=1745152496000` blocks until an event
**newer than** the watermark exists, then returns:

```json
{"events": ["..."], "watermark_unix_ms": 1745152501000, "timed_out": false}
```

Loop contract: pass the returned `watermark_unix_ms` back as `since_unix_ms`.
Omitting it starts the watermark at "now" (only future events). On timeout the
watermark is unchanged and `timed_out` is true — safe to retry. `timeout_s`
clamps to [0.5, 300]; default poll interval 0.25 s (`poll_interval_s`).

## Capture

- `capture_image [output] [timeout] [inline] [max_inline_bytes]` — one-shot
  JPG; waits for completion; returns `{event, path, size}` (+
  `content_base64` when `inline=true` and within budget).
- `start_capture [output] format=JPG|RAW|MP4 [video_length_seconds]` → capture
  event `{id, output_directory, format, status, timestamp_unix_ms, file_name}`;
  poll `get_capture_status` until `last_capture.status` is `COMPLETED` /
  `FAILED` / `INTERRUPTED` / `CANCELED`; the file is
  `output_directory + "/" + file_name`.
- `output` must be an absolute on-device directory under a mounted slot
  (`get_storage_status` → `mount_path`); omit it to use the selected slot.
  Device error code 30022 means the path is outside a mount.
- `stop_capture` ends a running MP4/RAW capture (no-op for JPG).

## Records and files

- `list_records [path] [dev_path] [limit] [offset]` — walks the active slot's
  record data directory; `path` is relative to it. Returns
  `{entries: [{name, is_dir, size?, mtime}], offset, limit, total, has_more, root}`.
  Directories sort first, then by name; `limit` defaults to 100 (max 500);
  `mtime` is Unix seconds.
- `read_file path=<abs> [max_inline_bytes]` — images and payloads ≤ 5 MiB
  return `{path, content_type, size, content_base64}`; larger payloads return
  metadata plus a note. Paths must be absolute, under `/mnt`, without `..`
  (symlinks are resolved before the check).
- `delete_file path=<abs> confirm=true` — destructive, gated. Recordings are
  ordinary files; deleting them is how a local agent frees space (the fleet
  skill's FORMAT/FREE_UP storage tasks are intentionally not exposed here).

## Storage slots

`get_storage_status` → list of slots, key fields per slot:

```json
{"dev_path": "/dev/mmcblk0p8", "mount_path": "/mnt/rc_mmcblk0p8",
 "enabled": true, "selected": true, "state": "IDLE", "removable": false,
 "size_bytes": 0, "free_bytes": 0, "quota_limit_bytes": -1, "quota_rotate": true,
 "data_dir": "reCamera"}
```

- The record data root is `mount_path + "/" + data_dir`.
- `set_storage_slot by_dev_path=…` enables a slot (both selectors empty =
  disable all). `configure_storage_quota dev_path=… quota_limit_bytes=-1
  quota_rotate=true` sets rotation; `-1` = unlimited.

## GPIO

- `list_gpios` / `get_gpio_info pin_id=…` — pins with `info` (name/chip/line/
  capabilities) and `settings` (state/edge/debounce_ms).
- `set_gpio_value pin_id=… value=0|1` — reconfigures the pin as **push-pull
  output**, then drives it.
- `get_gpio_value pin_id=… [debounce_ms=100]` — reconfigures the pin as
  **floating input** (enabling both-edge detection when debouncing), then reads.

Both value calls change pin direction; they are not passive probes.

## ISP image settings

`get_image_settings` returns `{video_adjustment, night_to_day, profiles[3]}`;
the profile the camera is currently using is `night_to_day.profile_current`.
`set_image_settings section=… values={…} [scene_id=…]` merges a partial
object into one section (read-modify-write), PUTs it, and returns
`{section, changed, state}` (+ `scene_id` for profile sections) with the
section re-read after the write. For profile sections `scene_id` defaults
to the active profile, so writes take visible effect immediately; pass
0/1/2 explicitly to pre-configure an inactive profile. Writes to an inactive
profile are stored but have no visible effect until that profile goes live.

| Section | `scene_id` | Fields (values) |
|---|---|---|
| `video_adjustment` | — | `rotation` (0/90/180/270), `flip` (close/mirror/flip/centrosymmetric), `power_line_frequency` (PAL(50HZ)/NTSC(60HZ)) |
| `night_to_day` | — | `mode` (0=auto/1=scheduled/2=fixed), `filter_level` (0–2), `filter_time` (1–60 s), `dawn_time`/`dusk_time` (0–86400 s, dusk > dawn), `profile_select` (0–2) |
| `adjustment` | opt 0/1/2 | `brightness`, `contrast`, `hue`, `saturation`, `sharpness` (0–100) |
| `exposure` | opt 0/1/2 | `exposure_mode`, `gain_mode` (auto/manual), `exposure_time` (fraction string like `1/60`), `exposure_gain` (1–128) |
| `backlight` | opt 0/1/2 | `blc_region`/`hdr`/`hlc` (open/close — **mutually exclusive**), `blc_strength`, `dark_boost_level` (0–100), `hdr_level` (=1), `hlc_level` (1–100) |
| `white_balance` | opt 0/1/2 | `style` (auto/manual/daylight/streetlamp/outdoor), `color_temperature` (2800–7500 K) |
| `enhancement` | opt 0/1/2 | `noise_reduce_mode` (0/1), `spatial_denoise_level`, `temporal_denoise_level` (0–100) |

Example: `rcl set_image_settings section=adjustment 'values={"saturation":80}'`

## Acoustic heads

- `get_active_acoustic_model` → `{runtime_head_id, labels, n_classes?, sha256?}`
  or `null` when the AcousticsLab app is stopped. `labels` feeds the
  `label_filter` of `source_filter=["acousticslab"]` rules.
- `list_acoustic_models` → every trained head across workspaces
  (`{workspace_id, head_id, n_classes, status, active}`); [] when the app is
  stopped.
- `set_acoustic_model workspace_id=… head_id=…` switches the live head (unknown
  ids fail loudly listing the available ones); `default=true` restores the
  factory head. Requires the AcousticsLab app running (`start_app`).

## App Center

- `list_apps` → installed + firmware system apps (`builtin`, `acousticslab`)
  with `{id, name, version, status, system, installed}`. A stopped app
  produces no frames for recording rules (see `get_record_sources`).
- `get_app_logs app_id=… [tail=200]` → `{id, lines, text}` (tail clamps to
  [1, 2000], spans rotation).
- `start_app` / `stop_app` / `restart_app` queue the action (HTTP 202 — poll
  `list_apps` for the new status). Stopping or restarting a **system** app
  interrupts a firmware-managed result source (recording rules fed by it go
  silent), so those calls require `confirm=true`.

## Video encode

`get_video_encode [stream=main|sub]` → `{stream, stream_type, enabled, codec,
resolution, frame_rate, gop, rc_mode, rc_quality, max_rate(kbps)}`.

`set_video_encode` changes only the passed fields — `codec` (H.264/H.265),
`resolution` (`WxH` within 384*384~3840*2160), `frame_rate`/`gop` (1~120),
`rc_mode` (CBR/VBR), `rc_quality` (highest/high/medium/low), `max_rate`
(3~65536 kbps), `enabled` — then re-reads the device and fails loudly if a
value did not apply. Applying briefly re-inits the encoder.

## Result push (notify)

The shared pipeline that fans results (built-in vision AND AcousticsLab
classifications) out to MQTT / HTTP / UART.

`get_notify_config` → `{mode, mode_name, mqtt, http, uart, templates}`;
`mode`: 0=off, 1=MQTT, 2=HTTP, 3=UART. **`password`/`token` are redacted as
`"***"`** — the device stores them in cleartext and they must not enter agent
contexts. `templates` are the shared per-task payload templates; empty =
built-in default.

`set_notify_config [mode=0..3] [mqtt={url, port, client_id, username,
password, topic}] [http={url, token}] [templates={classification, detection,
keypoint, segmentation, tracking}]` — only the passed fields change; omitted
fields keep their stored values, so secrets survive a redacted read followed
by a write. Passing the literal `"***"` placeholder is rejected; pass `""` to
clear a secret. NOTE: applying restarts the notify service and recameraipc —
a brief pipeline gap.

## Battery and backup

- `get_battery_status` → `{attached, charging, display_steps, total_steps}`;
  `attached=false` on base plates without a battery.
- `export_device_config output=./backup.tar` — downloads the full device
  configuration tarball to a local path (refuses to overwrite). Snapshot
  before making configuration changes so a human can restore a known-good
  state from the Web Console.

## Clock and timezone

- `get_system_time` → `{method, timestamp, timezone, tz, ntp: {address, port}}`.
- `set_system_time method=ntp|manual [timezone=IANA] [timestamp=unix] [ntp_server=…] [ntp_port=…]`
  — `ntp` applies server config (kept unless overridden) and triggers an
  immediate device-side sync; `manual` sets the clock to `timestamp`
  (default: now). The firmware silently accepts unknown timezones with
  success, so the command re-reads the device and raises a loud verification
  error instead of a false success.
- `probe_timezone [dhcp_probe=false] [geoip_url=…]` →
  `{perspective: "device", suggested_timezone, needs_user_confirmation,
  candidates: [{timezone, via}], sources, notes}`. Sources (one bounded
  attempt each, all broken-clock safe): plain-HTTP geo-IP (IANA name direct);
  WiFi 802.11d country IEs majority-voted (votes reported; single-zone
  countries map to one IANA name, multi-zone to candidates); DHCP RFC 4833
  options 100/101 (`tcode`/`pcode`, opt-in one-shot probe, applies nothing,
  skipped on static addressing).
- `sync_system_time [timezone=auto|IANA|null] [method=auto|ntp|manual]
  [tolerance_s=120] [wait_s=15] [dry_run=false]` — the one-call "fix the
  device time". Reference time: explicit `reference_unix` → one SNTP query →
  plain-HTTP `Date` headers; never the camera's own suspect clock. Returns
  `{changed, verified, method_used, drift_before_s, drift_after_s,
  timezone: {…}, ntp, warnings, follow_up}`; on total reference failure
  returns `needs_user_help` + `ask_user` and changes nothing. A manual
  stopgap re-arms NTP afterwards (auto mode only) so the device self-heals; explicit `method=manual` is respected as-is.

## Serve-mode protocol

`rcl serve` reads one JSON object per line from stdin and writes one JSON
object per line to stdout (always flushed):

```json
> {"id": 1, "cmd": "capture_image", "args": {"timeout": 5}}
< {"id": 1, "ok": true, "result": {"event": {"status": "COMPLETED", ...}, "path": "/mnt/...", "size": 12345}}
> {"id": 2, "cmd": "set_gpio_value", "args": {"pin_id": 106}}
< {"id": 2, "ok": false, "error": "set_gpio_value: missing required argument(s): ['value']"}
```

- `cmd` (required) is any CLI command name, plus `list-commands` and `exit`.
- `args` is a JSON object (optional); `id` is echoed back when present.
- The process keeps its endpoint resolution and HTTP connection alive across
  commands; an error response never terminates the session.
- Long-blocking commands (`wait_event timeout_s=300`) block the loop — one
  outstanding command at a time per `serve` process.
