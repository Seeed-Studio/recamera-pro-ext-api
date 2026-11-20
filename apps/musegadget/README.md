# Muse Gadget (reCamera Pro App Center)

This directory packages the Muse Linux client (`musegadget`) as a managed
reCamera Pro App Center application. The app is a headless Muse gadget: it
pairs with the Muse phone app over Bluetooth LE, keeps the device registered
over the Noise/WSS session, and answers `device.health`, `system.run`,
`file.read`, `file.write` and `device.guide`.

The Python service itself comes from the Muse repository and is installed as a
pure-Python wheel. This app is the device adapter: CA filtering, environment,
Bluetooth/pairing sequencing, supervision and a small web UI. It also bundles
the upstream `recamera-local` skill under `skills/recamera-local/`.

## Prerequisites (firmware)

The image must be reCamera Pro firmware **1.3.3 or newer**. The manifest
declares `compatibility.firmware_version = ">=1.3.3"`; AppMgr reads
`/etc/ota_version` and refuses to install on older firmware. Older AppMgrs
that do not know the field also reject it as an unknown manifest field, so
firmware 1.3.3 is both the feature and the packaging baseline.

The image must provide:

- Python 3.11, pip/venv, `dbus-python`, PyGObject, cffi/libffi, bash;
- BlueZ 5.65 with a working `hci0`;
- `/etc/bluetooth/main.conf` with `[GATT] ExchangeMTU = 256`;
- a boot-time script that powers the BCM4345C5 and runs `brcm_patchram_plus`
  before `bluetoothd` (reCamera overlay `S39bt-firmware`).

See the Muse repository `linux/tools/recamera/README.md` and the deployment
plan for the exact Buildroot overlay/defconfig lines.

## Build

The app needs five wheels in `wheels/`:

```text
musegadget-0.1.0-py3-none-any.whl
cryptography-45.0.7-cp311-abi3-manylinux2014_aarch64.manylinux_2_17_aarch64.whl
websockets-17.1-cp311-cp311-manylinux2014_aarch64.manylinux_2_17_aarch64.manylinux_2_28_aarch64.whl
cffi-1.15.1-cp311-cp311-manylinux_2_17_aarch64.manylinux2014_aarch64.whl
pycparser-2.22-py3-none-any.whl
```

These wheels are intentionally **tracked in Git** (the `apps/musegadget/wheels/`
exception in `.gitignore`) so a fresh clone has the exact offline payload and
CI can build the package without downloading dependencies.

Use `linux/tools/recamera/package_app.py` from the Muse checkout; it builds the
local wheel, downloads the target wheels, fills `python.wheels` with hashes and
sizes, and runs the App Center packager.

Do not use the App Center skill's `package_app.py` as the primary path: its
platform import allowlist does not know `dbus`/`gi`/`cffi`, which this app gets
from the system site-packages.

## reCamera-Intellisense skill

The app bundles the MIT-licensed, stdlib-only `recamera-local` skill at
`skills/recamera-local/`, copied from the upstream
[reCamera-Intellisense](https://github.com/iChizer0/reCamera-Intellisense)
`dev` branch (`skills/recamera-local`) at commit
`14636ee927cb2369764627f854bb757be50aed80`. The pinned revision is recorded in
`skills/recamera-local/upstream.json`; tests and the lint-only `ruff.toml` are
excluded. Everything installs under `/userdata/local/apps/musegadget/`;
nothing is copied to a separate `/userdata/musegadget` directory.

`recamera_runner.py` imports `recamera_local` from the skill's `scripts/`
directory (there is no second `vendor/` copy) and adds these **read-only**
commands as built-ins:

`camera.info`, `camera.resources`, `camera.models`, `camera.events`,
`camera.rules`, `camera.records`, `camera.video`, `camera.snapshot`,
`audio.models`, `audio.sources`, `apps.list`, `gpio.list`, `recamera.time`.

It also publishes `skills.list` and `skills.read`, so Muse AI can discover the
bundled skill and load `SKILL.md`, `REFERENCE.md` and `OBSERVATION.md` into its
session without relying on filesystem access to the root-only app directory.

The handlers run in the app service process (root) and only call the local
reCamera API; Muse's `system.run` / `file.*` commands still use the normal
`run_as` account (default `admin`). The skill is stdlib-only and needs no
device registry or copied state.

`camera.snapshot` captures a JPEG on the device and returns its path/size; the
base64 image is stripped from the Muse result. Deliberately **not** exposed:
storage format/delete, reboot, GPIO writes, detection-rule changes, app
lifecycle, and any other mutating operation. Live microphone/speaker streaming
is also not part of this command surface.

## WiFi provisioning during pairing

**Default: off (bind-only).** The app does not set
`MUSEGADGET_WIFI_BACKEND`, so `musegadget` keeps the stock behavior: it offers
a "Use current connection" entry (or the current WiFi SSID), ignores the WiFi
fields in `provision_v2`, and verifies the Muse tokens over whatever network
the device already has. This is the right choice when the camera is on
Ethernet or its WiFi was configured in the reCamera Web UI. The device must be
online: pairing verifies tokens with the Muse API, so a fully offline camera
cannot complete binding this way.

Enable **"Provision WiFi during pairing"** in the app settings
(`wifi_provisioning=true`) when the camera has no network yet. The app then sets
`MUSEGADGET_WIFI_BACKEND=recamera` and:

- answers `wifi_scan` from `GET https://127.0.0.1/cgi-bin/entry.cgi/network/wifi-list`
  (powering wlan0 on first with `POST /network/wifi-status?power=on`);
- turns the reCamera list into Muse's `{ssid, rssi, secure}` entries, deduped by
  SSID and preferring the connected/strongest BSSID;
- joins the network the user selected with
  `POST /network/wifi {"sSsid":..., "sPassword":...}`;
- keeps a "Use current connection" entry for Ethernet-only setups.

The local API needs no token and uses the device's self-signed HTTPS
certificate; the client only talks to `127.0.0.1`. The selected Wi-Fi is stored
by the reCamera network service, so it survives restarts independently of the
Muse pairing.

## BLE name

The advertised BLE name stays `MuseGadgetXXXXXX`. Muse's app matches the text
after `MuseGadget` with the six hex digits of the node id, so adding
`reCameraPro` there would break pairing. The BlueZ adapter name and the Muse
device model/display name are set to `reCamera Pro` instead
(`MUSEGADGET_DISPLAY_NAME`, `MUSEGADGET_MODEL_ID=recamera-pro`,
`MUSEGADGET_MODEL_NAME`).

## Runtime layout

Everything belongs to the app, in the App Center's per-app locations:

```text
/userdata/local/apps/musegadget/          # code, installed from the package
  app.py                                  # App Center wrapper
  recamera_runner.py                      # Muse service + camera/skill commands
  skills/recamera-local/                  # bundled upstream skill
    SKILL.md REFERENCE.md OBSERVATION.md
    upstream.json                         # pinned repository + commit
    scripts/recamera_local/               # stdlib-only runtime SDK
  guide.md                                # device.guide text
  icon.png / icon.jpg / webui/            # App Center icon and UI assets
/userdata/local/appdata/musegadget/       # persistent app data (survives upgrades)
  config.json  state/  run/  ca/
  commands.d/                             # owner-added Muse commands
/userdata/local/venvs/musegadget/         # per-release Python environment
```

There is no `/userdata/musegadget` or other shared tool directory. The skill is
part of the app package, so AppMgr installs/upgrades/removes it with the app;
the persisted appdata (including pairing) is kept across upgrades by the
platform.

To update the skill, replace `skills/recamera-local/` with the same upstream
`skills/recamera-local` path, copy everything except `tests/` and
`ruff.toml`, and update the commit fields in `upstream.json`.

- service: `python -I -u /userdata/local/apps/musegadget/recamera_runner.py`
- web UI: `http://<device>:8099/` (token from App Center's Open link)

## Security

- The app runs as root because App Center managed apps inherit appmgr's uid.
  Muse commands are dropped to `run_as` (default `admin`) by the Muse SDK,
  not by this wrapper.
- The web UI is HTTP on the LAN, protected by a bearer token delivered in the
  URL fragment. Use it only on a trusted network.
- Install only signed packages; register the owner public key under
  `/userdata/local/appmgr/keys/owners/`.