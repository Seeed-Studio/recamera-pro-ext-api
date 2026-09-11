# Capability Routing

Use this reference to decide whether a request is genuinely implementable by
the public reCamera Pro extension SDK. The SDK connects an independent Python
process to RKIPC; it does not grant ownership of the camera/media pipeline.

## Runtime prerequisite

This table classifies the published SDK, not an arbitrary stock firmware. A
route using `recamera_ext` is directly runnable only after its extension
runtime is installed and enabled: `recamera_ext` imports, `librecamera_ext.so.1`
loads, and the relevant `/run/recamera/*.sock` endpoint is live. A socket path
alone is not sufficient evidence that its service is accepting connections.

For kit model apps, the kit runtime, manifest-declared model, compatible RKNN
runtime, and hardware permissions are additional prerequisites. Until these
are verified, report **Supported with preconditions**. Do not replace a missing
extension runtime by directly accessing RKIPC internals or media devices.

## Supported routes

| User outcome | Feasibility | Route | Notes |
| --- | --- | --- | --- |
| Consume camera frames for custom CPU vision | Supported with preconditions | `recamera_ext.FrameSource` | Requires the frame extension runtime and live frame socket. Frames are NV12 DMA-BUF backed; use the Y plane or `to_bgr()` when BGR is needed. |
| Run custom RKNN inference and publish detection, classification, tracking, keypoints, or segmentation | Supported with preconditions | `kit` or direct `FrameSource` + `ResultSink` | Requires a compatible supplied/provisioned model/runtime. Prefer kit for standard model pipelines. |
| Draw external results in official OSD and emit them to recording/push channels | Supported with preconditions | `recamera_ext.ResultSink` | Requires the result extension runtime and live result socket. Send normalized coordinates and the matching frame PTS. |
| Clear the app's OSD results | Supported with preconditions | `ResultSink.send_detections(..., boxes=[])` | Requires `ResultSink`; the same `source_id` updates a single logical slot. |
| Observe built-in NPU metrics, preprocessing output, raw NPU output, or postprocessing output | Supported with preconditions | `recamera_ext.ProbeSource` | The requested probe stage must be exposed by the installed SDK/firmware. Treat this as read-only observation. |
| Trigger GPIO from platform inference notifications | Supported with preconditions | Official notify path plus GPIO example | Use the documented event route; confirm GPIO identity and permissions. |
| Capture/process shared ALSA PCM for ASR or audio logic | Supported with preconditions | Firmware-documented ALSA `ai_asr` source | Confirm that ALSA topology and root/audio-group access exist. `OfficialPcmSource` is currently a stub; do not route to the unimplemented audio socket. |
| Play an alarm or arbitrary audio through a speaker from Python | Supported with preconditions (firmware ALSA path, not an SDK API) | `aplay` subprocess or `ctypes` + `libasound.so.2`; see `audio-playback.md` | The public Python binding exposes NO unified speaker/player API. Playback is a device-capability path: confirm `aplay`/`libasound.so.2`, a real playback card and device string (for example `hw:1,0`, not fixed across firmware), `/dev/snd` permissions, and WAV format on the target, then verify audible output with explicit user authorization. Never invent `recamera_ext` speaker calls. |
| Build a custom vision app with zones, tracking, temporal state, pose, OCR, QR code, or multiple models | Supported with preconditions | `kit` plus the closest app example | A compatible model and declared application configuration are still prerequisites. |
| Read RTSP as a side consumer | Supported with preconditions | RTSP consumer path | This is secondary to `FrameSource`; decoder availability depends on the installed environment. |
| Call stable public platform controls | Supported with preconditions | Documented versioned HTTP control API or its kit wrapper | Confirm the endpoint/capability is published by the installed firmware; use only the documented API. |
| Hardware privacy mask | Supported with preconditions | `MaskControl` | Confirm the installed extension library exports this optional capability; use the binding rather than its native transport directly. |

## Audio capability model

Audio is the capability most often mis-classified, so assess it in three
separate layers and never collapse them into one verdict:

1. **Public SDK speaker API: none.** The published `recamera_ext` Python binding
   exposes no unified speaker/player abstraction, and `docs/guide/audio-pcm.md`
   is capture-only (`ai_asr` / `arecord`). Do not invent `recamera_ext` playback
   calls.
2. **Firmware ALSA playback: conditional.** The target firmware may provide a
   working ALSA playback path (`aplay`, `libasound.so.2`, a real playback card).
   This is a device-capability route, not an SDK guarantee, and it is what an App
   uses to sound an alarm. See `audio-playback.md`.
3. **End-to-end audible output: requires an authorized hardware test.** Seeing a
   playback card is not hearing sound. Actual playback must be confirmed on the
   target, and because it makes noise it requires explicit user authorization.

When a request involves audio, search the local SDK checkout and guides for
existing evidence before concluding. Search at least:

```text
snd_pcm  aplay  arecord  rkipc_ao  RK_MPI_AO  speaker_test  libasound
```

Then judge in this priority order:

1. The current public SDK API (binding plus guide plus a working official
   example).
2. A runnable example in the local checkout (for example firmware `rkipc_ao_*`,
   `speaker_test`, or a Node-RED `aplay` flow).
3. Target-device tools, libraries, and ALSA cards (read-only inventory).
4. An actual hardware playback test (authorized, makes sound).

Split audio verification into two phases and label them separately in the
report:

- **Static / read-only:** tools, libraries, sound cards, ALSA config, the WAV
  file, permissions, and format. Safe to run automatically.
- **Active hardware:** actually playing a WAV to confirm the speaker sounds, the
  volume is correct, and there is no stutter. This makes noise and must be
  explicitly authorized by the user first.

A conclusion backed only by layer 3 must be reported as "device has ALSA
playback hardware; audible output not yet verified", never as "speaker works".

## Do not claim these are supported

| Requested behavior | Conclusion | Reason / response direction |
| --- | --- | --- |
| Direct ISP controls, sensor register tuning, VI/VPSS/VENC configuration, encoder ownership | Not supported through this SDK | RKIPC owns the media pipeline. Identify a documented platform control API if one exists; otherwise it requires platform/firmware work. |
| Open `/dev/video*` to acquire the camera independently | Not supported | It creates a competing camera path and bypasses the SDK contract. Use `FrameSource` or, where appropriate, RTSP. |
| Directly send commands to `/var/tmp/rkipc` | Not supported | This is an internal RPC surface, not a public app API. |
| Modify built-in inference internals from probe tensors | Not supported | `ProbeSource` is observation only. It can inform a separate app but is not a stable control/inference replacement. |
| Assume arbitrary OpenCV, GStreamer, FFmpeg, Python package, or decoder availability | Requires confirmation | Do not code it as a default dependency. Check the supplied target runtime or use the core SDK route. |
| Guarantee unrestricted simultaneous NPU applications | Requires product policy and runtime confirmation | Treat built-in and custom NPU workloads as resource-constrained; do not promise concurrency or performance. |
| Add firmware features, Buildroot packages, system services, deployment, or device-side installation | Out of this skill's scope | State that the requested work belongs to platform, release, or installation engineering. |
| Build an App Center v2 archive with target-compatible private dependencies | Supported by this skill | Read `app-packaging.md`; package only a supplied wheel closure as `wheels/*.whl` and delegate archive metadata to the SDK builder. |

## Evidence discipline

Keep these claims separate in the implementation report:

- **SDK contract:** a public binding, documented request, and current source/example establish that an API exists.
- **AppMgr/packaging contract:** the Kit lifecycle, manifest, artifact, dependency, and official-builder rules establish how an App is packaged and launched.
- **Skill policy:** offline validation, wheel closure checks, read-only probing, and publication gates are safeguards added by this Skill.
- **Device evidence:** imports, libraries, sockets, processes, ALSA cards, GPIO inventories, and logs describe one firmware instance only. They do not prove board wiring, pinmux, permissions, API support, or end-to-end success.

In particular, never promote the presence of playback hardware into an SDK capability. For GPIO, also verify the board mapping, pinmux, gmgr or documented event route, permissions, and electrical load before enabling an output. Likewise, successful archive creation proves only the packaging contract; if AppMgr records that an App started and exited, inspect its runtime logs and dependencies separately and never report archive success as device execution success.

## Assessment template

Use a compact conclusion before editing code:

```text
Conclusion: Supported / Supported with preconditions / Not supported through this SDK.
Route: <direct recamera_ext API, kit, public HTTP API, or none>.
Preconditions: <model, runtime, hardware, firmware capability, if any>.
Boundary: <what this SDK does not expose, if relevant>.
```

## Routing choices

Choose direct `recamera_ext` for a small, explicit loop such as frame motion
analysis followed by `send_detections`. Choose kit where its model loader,
preprocessing, postprocessing, output, tracking, zones, configuration reload,
or application loop materially removes work. Do not introduce kit solely to
wrap a few lines of direct SDK code.

For a capability not listed above, inspect the current local SDK source and
guides before concluding. An existing public C/Python API, a guide, and a
working official example together are strong evidence; a historical design
note or an internal RPC path is not.
