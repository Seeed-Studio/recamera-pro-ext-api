# SDK Documentation Router

Use this index to read only the SDK material needed for the current request.
Do not preload or copy the complete `docs/` tree into context.

## Source resolution

1. Prefer a compatible local SDK checkout supplied by the user.
2. If the user names a tag or commit, use exactly that revision.
3. Otherwise, when no checkout is available, resolve the compatible official
   public SDK upstream HEAD once to a full commit SHA and record it as
   `sdk_source_commit`.
4. For the rest of the task, read only compatible upstream content pinned to
   that SHA. The caller or the project's public SDK documentation determines
   the repository; this Skill does not require a specific private checkout.
5. Do not mix floating branch content with pinned content, clone the whole
   repository by default, or fetch every document.
6. If GitHub is unavailable, do not guess unfamiliar APIs. Use the bundled
   stable contracts only and classify unverified behavior as **Supported with
   preconditions**.

Packaging does not require source resolution: the Skill already contains the
fixed official builder from its pinned public SDK revision
`525addec801680f6aabbbb7605d6de3bb390348a`. Use a local checkout or pinned
public-upstream content when comparing newer API docs, Kit code, examples, or
manifests.

A public SDK revision is an SDK source reference, not proof that the target
device has the same runtime. Unless runtime alignment is independently known,
retain the device precondition.

## `docs/api`

| Document | Read when | Status | Also inspect |
| --- | --- | --- | --- |
| `docs/api/architecture.md` | Deciding process boundaries, RKIPC integration, sockets, ownership, or data flow | Public architecture contract | Current binding and service implementation named by the document |
| `docs/api/spec.md` | Using public request/response types, limits, frame/result/probe semantics, errors, or capabilities | Public API contract | `sdk/python/recamera_ext/__init__.py` and relevant tests |

## `docs/guide`

| Document | Read when | Status | Also inspect |
| --- | --- | --- | --- |
| `docs/guide/README.md` | Locating the right guide or checking the guide set's intended scope | Guide index | The routed guide and current code |
| `docs/guide/adapter-bootstrap.md` | Understanding adapter startup, binding discovery, or extension bootstrap | Migration/design guide mixed with implementation | Adapter code, binding loader, runtime paths; verify every proposed adapter exists |
| `docs/guide/ai-result-overlay.md` | Emitting AI results or drawing boxes, labels, keypoints, and overlays | Implementation guide | Result types, coordinate/PTS contract, closest example |
| `docs/guide/app-center-publishing.md` | Preparing metadata or a public App Center release | App Center/AppMgr publishing-chain guide | Current manifest tests and release tooling; not a base binding contract |
| `docs/guide/app-package-v2.md` | Preparing a v2 package manifest, the platform/compatibility contract, icon, release lock/BOM, or per-release environment fields | App package v2 contract; mirrors `market/appmgr/manifest.py` and `market/appmgr/schema/manifest-v2.schema.json` | `market/appmgr/manifest.py`, `market/appmgr/schema/manifest-v2.schema.json`, `market/packaging/build.py` |
| `docs/guide/audio-pcm.md` | Reading PCM from the firmware ALSA topology (capture only; speaker playback is not covered here, see `audio-playback.md`) | Device/firmware integration record | Target ALSA inventory and permissions; `OfficialPcmSource` remains a stub |
| `docs/guide/control-api.md` | Changing supported camera/runtime controls | Mixed implemented/proposed platform HTTP control guide | Verify each endpoint against current implementation and capability response |
| `docs/guide/deploy-ops.md` | Diagnosing runtime layout, service startup, logs, sockets, or deployment operations | Operations | Target firmware/runtime version and service files |
| `docs/guide/ffmpeg-integration.md` | Integrating FFmpeg with the supported extension/media boundary | Integration guide | Architecture, exact example, firmware FFmpeg features |
| `docs/guide/frontend-extension.md` | Adding an App frontend or understanding frontend/backend communication | Implementation guide | App manifest, frontend assets, current sample App |
| `docs/guide/gpio-result-trigger.md` | Triggering GPIO from inference/results | Implementation guide | GPIO example, event/probe contract, permissions |
| `docs/guide/gstreamer-integration.md` | Integrating a software GStreamer pipeline without violating media ownership | Integration guide | Architecture and firmware plugin availability |
| `docs/guide/hw-codec-gstreamer.md` | Using hardware codec elements through GStreamer | Hardware integration guide | Target plugin inventory and media ownership boundary |
| `docs/guide/hw-mask-api.md` | Configuring hardware privacy/mask regions | Public integration guide | Control API, coordinate contract, capability response |
| `docs/guide/hw-preprocess.md` | Using hardware ROI/resize/preprocess for model inputs | Hardware implementation guide | Closest official App, preprocess code, geometry mapping |
| `docs/guide/inference-as-app.md` | Structuring model inference as an installable Kit App | Implementation guide | `kit/app.py`, manifest contract, closest current App |
| `docs/guide/kit-design.md` | Understanding Kit abstractions, responsibilities, or extension points | Design and implementation mixed; may contain stale descriptions | Current `kit/app.py`; code wins over prose |
| `docs/guide/model-onboarding.md` | Adding an RKNN model, labels, preprocessing, and manifest metadata | Implementation guide | Closest App, model loader, target RKNN compatibility |
| `docs/guide/output-sink.md` | Declaring output fields, mappings, channels, and sinks | Kit component/manifest implementation guide | Manifest/output tests and output implementation; not the base ResultSink API |
| `docs/guide/per-app-dependencies.md` | Understanding dependency-isolation design background | Design/background document | `market/appmgr/pythonenv.py` and `market/packaging/build.py`; do not infer current behavior from this document alone |
| `docs/guide/python-ai-api.md` | Checking which Python AI interfaces currently exist and their per-layer lifecycle/availability (`recamera_ext`, `kit`, RGA, RKNN, NPU broker, managed launch) | Current Python AI API/lifecycle overview with target-verification status (Chinese) | `sdk/python/recamera_ext`, `kit/`, `market/appmgr`, and the named per-layer source |
| `docs/guide/result-hub-v2.md` | Understanding how App and builtin AI results reach the frontend Result Hub, the WebSocket endpoints (`8123`/`8124`/`8125`), or the canonical result envelope | Result Hub v2 implementation/integration guide (Chinese) | `kit` output-sink code, `docs/guide/ai-result-overlay.md`, `docs/guide/output-sink.md`, and `managed-runtime.md` |
| `docs/guide/result-push.md` | Pushing external results and binding them to frames | Implementation guide | ResultSink API, limits, normalized geometry, PTS rules |
| `docs/guide/rkipc-rpc-status.md` | Understanding internal RKIPC RPC coverage or migration status | Internal boundary/status | Public API first; never generate private RPC calls from this document |
| `docs/guide/voice-app.md` | Building the repository's voice/audio application | Business App design/implementation guide | `apps/voice-transcribe/`, audio guide, target audio capability |

## Reading priority

For runtime behavior, use this order:

1. Current public source and tests at the pinned revision.
2. Closest current official example or App.
3. `docs/api/spec.md` and the relevant implementation guide.
4. Design, proposal, operations, or internal-status documents.

When prose conflicts with `kit/app.py` or a current official App, follow the
current code and record the discrepancy.
