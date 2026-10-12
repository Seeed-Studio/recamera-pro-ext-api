# reCamera Pro Extension API

<div align="center">

**High-Performance Modular Edge AI Camera Platform powered by Rockchip RV1126B (3 TOPS NPU)**

*Build, run, and scale on-device Vision AI applications as independent processes without firmware rebuilds.*

**English** | [简体中文](README.zh-CN.md)

[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![C ABI](https://img.shields.io/badge/C%20ABI-1.5.0-green.svg)](sdk/VERSION)
[![Python](https://img.shields.io/badge/Python-3.11-blue.svg)](sdk/python)
[![SoC](https://img.shields.io/badge/SoC-Rockchip%20RV1126B-orange.svg)](https://www.seeedstudio.com/reCamera-Pro-2GB.html)
[![NPU](https://img.shields.io/badge/NPU-3.0%20TOPS-red.svg)](https://www.seeedstudio.com/reCamera-Pro-2GB.html)
[![Docs](https://img.shields.io/badge/Docs-Wiki-brightgreen.svg)](https://wiki.seeedstudio.com/recamera_pro_getting_started/)
[![Community](https://img.shields.io/discord/862602258452578314?label=Discord&logo=discord&color=7289da)](https://discord.gg/agHdBMYGZK)

</div>

---

The extension API for **reCamera Pro** (Rockchip RV1126B / `recamera_v2`) empowers solution providers, developers, and system integrators to run customized AI applications as decoupled userland processes. By decoupling userland applications from system firmware, developers can iterate quickly without rebuilding the base OS image.

Through dedicated Unix domain sockets under `/run/recamera/`, client applications can acquire zero-copy camera frames, negotiate hardware NPU leases, inject detection and classification results back to on-screen displays (OSD), and observe pipeline tensors in real time. **The process boundary defines the API contract.**

---

## 🛒 Hardware & Official Accessories

This repository is dedicated specifically to **reCamera Pro**. Powered by the Rockchip RV1126B SoC, reCamera Pro delivers 3.0 TOPS on-device NPU compute, 4K starlight imaging, and industrial-grade connectivity for enterprise Edge AI vision systems.

### 🌟 Device: reCamera Pro 2GB

| Item | Details |
|------|---------|
| **Product Model** | **reCamera Pro 2GB** — Open Edge AI Camera |
| **Processor (SoC)** | Rockchip RV1126B (Quad-core ARM Cortex-A53 @ 1.5 GHz) |
| **NPU Acceleration** | **3.0 TOPS NPU** (INT8 / INT16 mixed precision), running CV, VLM, LLM, and speech AI |
| **Memory & Storage** | 2GB LPDDR4 RAM, 16GB eMMC 5.1 high-speed onboard flash |
| **Image Sensor** | 8MP Ultra-HD Starlight CMOS (up to 3840×2160 @ 30 FPS) |
| **Default Lens** | Interchangeable M12 mount, 123° ± 5° DFOV, F2.4 aperture |
| **Audio Subsystem** | Dual onboard microphone array + NS4150B Class-D speaker amplifier (supports 8Ω / 1W speaker) |
| **Display Interface** | MIPI DSI interface for external touchscreens and real-time live preview displays |
| **Industrial I/O & Sensors** | Gigabit Ethernet (with PoE), Wi-Fi/BT, CAN Bus, Debug UART, GPIO expansion headers, 6-axis IMU |
| **Power Flexibility** | DC 12V/2A Jack, Standard PoE (IEEE 802.3af/at), USB Type-C, and 7.4V battery interface |
| **Package Contents** | reCamera Pro unit × 1, Global multi-plug 12V power adapter × 1, USB Type-C data cable × 1 |
| **Official Price** | **$299.90** |
| **Purchase & Guide** | [🛒 **Buy on Seeed Studio Bazaar**](https://www.seeedstudio.com/reCamera-Pro-2GB.html) \| [📖 **Quick Start Guide**](https://wiki.seeedstudio.com/recamera_pro_getting_started/) |

### 🔍 Official Verified M12 Lenses & Optical Accessories

reCamera Pro features a standard **M12 interchangeable lens mount**, allowing you to customize the focal length and field of view for your specific deployment:

- **1/2.9" M12 Telephoto Lens (5MP, 20° FOV)**
  Designed for long-distance perimeter surveillance, license plate recognition (LPR), and hallway monitoring where narrow FOV and magnification are required.
  👉 [Buy on Seeed Studio Bazaar ($11.90)](https://www.seeedstudio.com/5-p-6645.html)

- **1/2.9" M12 Ultra Telephoto Lens (2MP, 15° FOV)**
  Provides extreme magnification for distant detail capture, instrument meter reading, and industrial macro inspection.
  👉 [Buy on Seeed Studio Bazaar ($13.90)](https://www.seeedstudio.com/22mm-B-p-6646.html)

---

## ⚡ Extension API Architecture

Six dedicated Unix domain sockets under `/run/recamera/` bridge customer applications with system services:

| Endpoint | Socket Path | Purpose |
|----------|-------------|---------|
| **Frame Broker** | `/run/recamera/frame.sock` | Receive full-resolution NV12 camera frames via shared memory with zero-copy overhead. |
| **Result Injection** | `/run/recamera/result-in.sock` | Feed inference results back into the platform OSD, notifications, and public result stream (does not trigger recording directly). |
| **Platform OSD** | `/run/recamera/osd-in.sock` | Reserved for `appmgr`; renders authorized application overlays directly onto the video pipeline. |
| **Recording Trigger** | `/run/recamera/record-in.sock` | Reserved for `appmgr`; routes manifest-declared application trigger signals to Vigil recording rules. |
| **Pipeline Observation** | `/run/recamera/probe.sock` | Sample tensors and performance metrics at intermediate stages of the built-in inference pipeline. |
| **NPU Arbitration** | `/run/recamera/inference-control.sock` | Safely pauses the built-in model to grant a single external process exclusive NPU lease tied to connection lifetime. |

---

## 🚀 Quick Start

Capture frames, run inference, and publish results back to the platform in Python.
For full instructions, see [docs/guide/README.md](docs/guide/README.md) §2.

```python
from recamera_ext import FrameSource, ResultSink

# Connect to frame source and result sink
with FrameSource() as src, ResultSink(source_id="my-app") as sink:
    for frame in src:
        # frame.array provides a zero-copy NumPy view of the Y plane (luminance)
        # Use frame.to_bgr() or frame.plane_array(i) if your model requires color
        boxes = my_model(frame.array)  # -> [(x1, y1, x2, y2, score, label), ...]

        # Coordinates must be normalized to [0, 1] relative to frame width/height
        sink.send_detections(pts_us=frame.pts_us, boxes=boxes)
```

> [!IMPORTANT]
> **Coordinate Contract**: All box coordinates `(x1, y1, x2, y2)` passed to `ResultSink` must be normalized to `[0, 1]` relative to frame width and height. Passing pixel coordinates will be clamped by the OSD to a 1-pixel invisible box. If your model outputs pixel coordinates, divide by frame width and height before injection.

More runnable examples are available in [`examples/`](examples/).

---

## 📂 Repository Layout

| Directory | Purpose |
|-----------|---------|
| [`docs/api/`](docs/api/) | Specifications and architecture: [`spec.md`](docs/api/spec.md) covers socket paths, protobuf schemas, and ABI versions; [`architecture.md`](docs/api/architecture.md) describes system layers and data flow. |
| [`docs/guide/`](docs/guide/) | [Developer guide](docs/guide/README.md) and topic guides covering control API, result publishing, GPIO, PCM audio, frontend extensions, FFmpeg / GStreamer integration, App Center publishing, deployment and operations, Kit design, adapter paths, and voice applications. See [`overlay-customization.md`](docs/guide/overlay-customization.md) for custom AI overlays and [`overlay-customization-handoff.md`](docs/guide/overlay-customization-handoff.md) for deployment handoff. |
| [`sdk/`](sdk/) | Device-side SDK: `librecamera_ext.so.1`, the `recamera_ext` Python package (ctypes wrapper), and C/C++ headers. |
| [`kit/`](kit/) | Reusable Python inference toolkit: L0 adapters, runtime preprocessing and postprocessing, logic modules, and `app.py`. |
| [`apps/`](apps/) | Sample applications (yolo-detector, face-analysis, fall-detection, voice-transcribe, etc.) and [Inference Workflows](apps/inference-rv1126b/README.md). |
| [`examples/`](examples/) | Minimal SDK examples: frame capture, result injection, frame → inference → OSD, GPIO triggers, C ABI usage, and browser overlay drawing (`11-overlay-geometry`). |
| [`market/`](market/) | App Center control plane, inference scheduler service, manifest v2 packaging, and catalog tools. |
| [`release/`](release/) | Historical delivery snapshots. Note: current `release/pkg` **cannot be deployed**; official artifacts must be generated from manifest-pinned source builds. |

---

## 🛠️ Installation and Environment

Firmware produced by the current top-level source build installs the native library in `/usr/lib` and the Python packages into the system Python 3.11 `site-packages` directory. Search paths normally require no manual configuration.

The following settings are only required when developing against legacy manual copies under `/userdata/sdk`:

```sh
export PYTHONPATH=/userdata/sdk/python:$PYTHONPATH
export LD_LIBRARY_PATH=/userdata/sdk/lib:$LD_LIBRARY_PATH
```

> [!WARNING]
> Firmware integration must use the top-level `project/app` source build for this SDK. The repository's current [`release/pkg`](release/pkg/) is a historical snapshot with inconsistent checksums and lacks the modern NPU broker. Its `install.sh` has been disabled to fail closed: **do not sideload it**. See [docs/guide/README.md](docs/guide/README.md) for socket prerequisites and handshakes, and [docs/guide/deploy-ops.md](docs/guide/deploy-ops.md) for deployment operations.

---

## 📦 Release Artifacts

Packages retained under `release/` are preserved for historical reference only:

| Package | Purpose | Status & Notes |
|---------|---------|----------------|
| `recamera-ext-api-v*.tar` / [`release/pkg`](release/pkg/) | Legacy sideload packages | **Disabled**: Binaries, manifests, and installation scripts are inconsistent, and `inference-control@1` is missing. Sideloading is prohibited. |
| `recamera-ext-kit-v*.tar.gz` | Historical Kit distribution packages | **Historical reference**: Does not constitute a consistent release set for Python 1.4 API, native client, and server. |

New releases must be built from the pinned source revisions of `recamera_ipc`, Vigil, this repository, and the Web backend. A unified bill of materials (BOM) and cryptographic hashes must be generated before installation is enabled. The existing [`release/build-release.sh`](release/build-release.sh) is a legacy artifact assembler and does not demonstrate a reproducible source build.

---

## 🏷️ Versioning

Keep the following version tracks distinct:

- **Product release train**: The current working tree has not yet produced a new deployable release train. Historical v1.x packages are archived and disabled.
- **Python distribution**: `recamera-ext 1.5.0`, including the broker-backed `InferenceLease` and appmgr-only `OsdSink` / `RecordSink`.
- **Native SDK / C ABI**: `sdk/VERSION` is **1.5.0**, while the SONAME remains `librecamera_ext.so.1`. Capabilities are `frame@1 / result@1 / osd@1 / record@1 / probe@1 / inference-control@1`. The Python package version and C ABI/SONAME are versioned independently.

---

## 🤝 Community & Support

- **Documentation**: [Seeed Studio Official Wiki](https://wiki.seeedstudio.com/recamera_pro_getting_started/)
- **Technical Support & Discussions**: [Seeed Studio Forum](https://forum.seeedstudio.com/)
- **Developer Community**: [Discord Server](https://discord.gg/agHdBMYGZK)
- **Issue Tracker**: Submit bug reports and feature requests via GitHub Issues

---

## 📄 License

This repository is licensed under the [Apache License 2.0](LICENSE), except for native client files explicitly listed in [`sdk/NOTICE`](sdk/NOTICE). Those files retain the [BSD-3-Clause](sdk/LICENSE) license. See [`NOTICE`](NOTICE) for the mixed-license overview.
