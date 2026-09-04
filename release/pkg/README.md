# reCamera Pro Extension API — v1.6.5 (sideload package)

Persistent sideload that adds the extension API (frame proxy / result injection / probe) to a
reCamera Pro (RV1126B). It overwrites `/oem/usr/bin/rkipc` (ext4, read-write) so it **survives reboot**.
It is not a firmware image and does not touch partitions.

## What's inside
See `MANIFEST.txt` for md5s and install targets. In short: a patched `rkipc`, an optional `entry.cgi`
(M4 control plane), the SDK (`librecamera_ext.so` + Python `recamera_ext` + C header), and the
**Python inference runtime** (`rknnlite` 2.3.2 wheels under `wheels/`).

## Python inference runtime (rknnlite)

`install.sh` now provisions the Python inference runtime so vision apps run out of the box on a
device with no network. Step `[6/7]` of the install:

1. symlinks `/usr/lib/librknnrt.so -> /oem/usr/lib/librknnrt.so` (stock `rknnlite` hardcodes that path);
2. creates a venv at `/userdata/rknnenv` with `--system-site-packages` (numpy comes from the system);
3. offline-installs `rknn-toolkit-lite2 psutil ruamel.yaml ruamel.yaml.clib` from the bundled `wheels/`;
4. self-checks `from rknnlite.api import RKNNLite; RKNNLite()`.

This step is **best-effort**: any failure only warns and never blocks the main `rkipc` install.
Run vision apps with:

```sh
PYTHONPATH=/userdata/local:/userdata/sdk/python \
LD_LIBRARY_PATH=/oem/usr/lib \
/userdata/rknnenv/bin/python3 <app>.py
```

## Flash a new device

Prereqs: device reachable over `adb` as root (`adb connect <ip>:5555`).

```sh
# from the machine holding this package (Mac/Linux):
adb shell "mkdir -p /userdata/ext-pkg"
adb push ./ /userdata/ext-pkg/            # push the whole package dir
adb shell "sh /userdata/ext-pkg/install.sh"   # backs up factory, md5-verifies, overwrites /oem
adb reboot                                    # or: install.sh --reboot
# wait ~1-2 min, then self-check:
adb shell "ls -l /run/recamera/"          # expect frame.sock result-in.sock probe.sock apps.d/
adb shell "md5sum /oem/usr/bin/rkipc"     # expect f683352a9d062a05a3df1f8df22d7d53
```

`install.sh` is idempotent and md5-checked: it refuses to run with a mismatched artifact, backs up the
factory `rkipc`/`entry.cgi` to `/userdata/*.factory.bak` exactly once, and aborts before overwriting if
no valid rollback target exists.

## Verify the SDK (anyone can connect)

```sh
export LD_LIBRARY_PATH=/oem/usr/lib:$LD_LIBRARY_PATH
export PYTHONPATH=/userdata/sdk/python:$PYTHONPATH
python3 - <<'PY'
import recamera_ext as re
s = re.ResultSink("selftest")                 # opens /run/recamera/result-in.sock (handshake)
print("rc =", s.send_detections(123456, [(0.05,0.07,0.62,0.94,0.9,"person",0)]))  # normalized [0,1]; 0 = accepted
PY
```

## Rollback

```sh
adb shell "sh /userdata/ext-pkg/rollback.sh --reboot"   # restores factory rkipc + entry.cgi, reboots
```

## Boundaries
- **OTA reverts this.** A firmware OTA / `update.img` flash rewrites `/oem` and restores factory `rkipc`.
  Re-run `install.sh` after any OTA.
- **Firmware compat:** V1.0.4 and V1.0.10 (cross-build compatible).
- **Resolved:** keypoints *decode over WebSocket* now works in this `rkipc` (de5b3aa4), alongside
  classification box OSD rendering. frame/result/probe/keypoints paths are all active.
