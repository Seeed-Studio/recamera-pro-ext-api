# reCamera Pro Extension API

English | [简体中文](README.zh-CN.md)

The extension API for reCamera Pro (RV1126B / recamera_v2) lets solution providers
run AI applications as independent processes without rebuilding the firmware for
each app. The device must first run firmware that includes the extension endpoints
and matches the SDK protocol. Applications then use Unix domain sockets under
`/run/recamera/` to receive frames, acquire NPU leases, publish results back to the
platform, and observe the pipeline. The process boundary defines the API contract.

Six socket endpoints are available:

| Endpoint | Socket | Purpose |
|------|--------|------|
| Frame broker | `/run/recamera/frame.sock` | Receive raw camera frames with zero-copy access for application-side inference |
| Result injection | `/run/recamera/result-in.sock` | Feed results into the platform OSD, notifications, and public result stream; does not directly trigger recording |
| Platform OSD | `/run/recamera/osd-in.sock` | Reserved for appmgr; draw only authorized application results onto the video stream |
| Platform recording triggers | `/run/recamera/record-in.sock` | Reserved for appmgr; pass manifest-declared signals from installed applications to Vigil recording rules |
| Pipeline observation | `/run/recamera/probe.sock` | Sample tensors at different stages of the built-in inference pipeline |
| NPU arbitration | `/run/recamera/inference-control.sock` | Grant a single external owner a lease tied to the connection lifetime after the built-in model has fully stopped |

## Repository Layout

| Directory | Purpose |
|------|------|
| [`docs/api/`](docs/api/) | Specifications and architecture: [`spec.md`](docs/api/spec.md) covers socket paths, protobuf schemas, and ABI versions; [`architecture.md`](docs/api/architecture.md) describes system layers and data flow. |
| [`docs/guide/`](docs/guide/) | The [developer guide](docs/guide/README.md) and topic guides cover the control API, result publishing, GPIO, PCM audio, frontend extensions, FFmpeg / GStreamer integration, App Center publishing, deployment and operations, Kit design, adapter paths, and voice application design. See [`overlay-customization.md`](docs/guide/overlay-customization.md) for custom AI result overlays and [`overlay-customization-handoff.md`](docs/guide/overlay-customization-handoff.md) for deployment handoff. |
| [`sdk/`](sdk/) | Device-side SDK: `librecamera_ext.so.1`, the `recamera_ext` Python package (a thin ctypes wrapper), and C headers. |
| [`kit/`](kit/) | Reusable Python inference toolkit: L0 adapters, runtime preprocessing and postprocessing, logic libraries, and `app.py`. |
| [`apps/`](apps/) | Sample applications such as yolo-detector, face-analysis, fall-detection, and voice-transcribe, plus [Inference Workflows](apps/inference-rv1126b/README.md). |
| [`examples/`](examples/) | Minimal SDK examples: frame capture, result injection, frame → inference → OSD, GPIO triggers, frame access in C, and freeform browser overlays (`11-overlay-geometry`). |
| [`release/`](release/) | Historical delivery snapshots. The current `release/pkg` **cannot be deployed**; official artifacts must be regenerated from source builds pinned to specific commits. |

## Quick Start

Capture frames, run inference, and publish results back to the platform in Python.
For full instructions, see [docs/guide/README.md](docs/guide/README.md), §2.

```python
from recamera_ext import FrameSource, ResultSink

with FrameSource() as src, ResultSink(source_id="my-app") as sink:
    for frame in src:                          # frame.array: zero-copy frame view
        boxes = my_model(frame.array)          # -> [(x1,y1,x2,y2,score,label), ...] normalized to [0,1]
        sink.send_detections(pts_us=frame.pts_us, boxes=boxes)
```

All coordinates passed to `ResultSink` must be normalized to `[0,1]`, relative to
the frame width and height. More examples are available in [`examples/`](examples/).

## Installation and Environment

Firmware produced by the current top-level source build installs the native
library in `/usr/lib` and the Python packages in the system Python 3.11
site-packages directory. Search paths normally require no manual configuration.
The following settings are only needed when debugging legacy manual copies under
`/userdata/sdk`:

```sh
export PYTHONPATH=/userdata/sdk/python:$PYTHONPATH
export LD_LIBRARY_PATH=/userdata/sdk/lib:$LD_LIBRARY_PATH
```

Firmware integration should use the top-level `project/app` source build for this
SDK. The repository's current [`release/pkg`](release/pkg/) is a historical snapshot
with inconsistent checksums and no new NPU broker. Its `install.sh` is disabled to
fail closed: **do not sideload it**. See
[docs/guide/README.md](docs/guide/README.md) for prerequisites, including socket
permissions and handshakes, and [docs/guide/deploy-ops.md](docs/guide/deploy-ops.md)
for release requirements.

## Release Artifacts

Packages retained under `release/` are for historical reference only:

| Package | Purpose | Notes |
|---|---|---|
| `recamera-ext-api-v*.tar` / [`release/pkg`](release/pkg/) | **Disabled legacy sideload packages** | Binaries, manifests, and installation scripts are inconsistent, and `inference-control@1` is missing. These packages cannot be installed. |
| `recamera-ext-kit-v*.tar.gz` | Historical Kit distribution packages | These do not constitute a consistent release set for the Python 1.4 API, native client, and server. |

New releases must be built from the source revisions of `recamera_ipc`, Vigil,
this repository, and the Web backend pinned in a manifest. A unified bill of
materials (BOM) and hashes must be generated before installation is enabled.
The existing [`release/build-release.sh`](release/build-release.sh) is still a
legacy artifact assembler and does not demonstrate a reproducible source build.

## Versioning

Keep the following version tracks distinct:

- **Product release train**: The current working tree has not yet produced a new
  deployable release train. Historical v1.x packages are archived and disabled.
- **Python distribution**: `recamera-ext 1.5.0`, including the broker-backed
  `InferenceLease` and the appmgr-only `OsdSink` / `RecordSink`.
- **Native SDK / C ABI**: `sdk/VERSION` is **1.5.0**, while the SONAME remains
  `librecamera_ext.so.1`. Capabilities are `frame@1 / result@1 / osd@1 /
  record@1 / probe@1 / inference-control@1`. The Python package version and the
  C ABI/SONAME are versioned independently.

## License

This repository is licensed under the [Apache License 2.0](LICENSE), except for
the native client files explicitly listed in [`sdk/NOTICE`](sdk/NOTICE). Those
files retain the [BSD-3-Clause](sdk/LICENSE) license. See [`NOTICE`](NOTICE) for
the mixed-license overview.
