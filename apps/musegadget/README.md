# Muse Gadget (reCamera Pro App Center)

This directory packages the Muse Linux client (`musegadget`) as a managed
reCamera Pro App Center application. The app is a headless Muse gadget: it
pairs with the Muse phone app over Bluetooth LE, keeps the device registered
over the Noise/WSS session, and answers `device.health`, `system.run`,
`file.read`, `file.write` and `device.guide`.

The Python service itself comes from the Muse repository and is installed as a
pure-Python wheel. This app is the device adapter: CA filtering, environment,
Bluetooth/pairing sequencing, supervision and a small web UI.

## Prerequisites (firmware)

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
websockets-17.1-py3-none-any.whl
cffi-1.15.1-cp311-cp311-manylinux_2_17_aarch64.manylinux2014_aarch64.whl
pycparser-2.22-py3-none-any.whl
```

Use `linux/tools/recamera/package_app.py` from the Muse checkout; it builds the
local wheel, downloads the target wheels, fills `python.wheels` with hashes and
sizes, and runs the App Center packager.

Do not use the App Center skill's `package_app.py` as the primary path: its
platform import allowlist does not know `dbus`/`gi`/`cffi`, which this app gets
from the system site-packages.

## reCamera-Intellisense integration

The app vendors the MIT-licensed, stdlib-only
[reCamera-Intellisense](https://github.com/iChizer0/reCamera-Intellisense) SDK
under `vendor/recamera_intellisense/`. It stays **inside the app install
directory** (`/userdata/local/apps/musegadget/`); nothing is copied to a
separate `/userdata/musegadget` directory.

`recamera_runner.py` runs the Muse service in the app's own process and adds
these **read-only** commands as built-ins:

`camera.info`, `camera.resources`, `camera.models`, `camera.events`,
`camera.rules`, `camera.records`, `camera.video`, `camera.snapshot`,
`audio.models`, `audio.sources`, `apps.list`, `gpio.list`, `recamera.time`.

The handlers run in the app service process (root) and only call the local
reCamera API; Muse's `system.run` / `file.*` commands still use the normal
`run_as` account (default `admin`). The skill's device registry lives under the
app's appdata home (`/userdata/local/appdata/musegadget/home/.recamera/`), so
all state stays in the app's unified data directory.

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
  recamera_runner.py                      # Muse service + camera commands
  vendor/recamera_intellisense/           # vendored MIT SDK
  guide.md                                # device.guide text
  icon.png / icon.jpg / webui/            # App Center icon and UI assets
/userdata/local/appdata/musegadget/       # persistent app data (survives upgrades)
  config.json  state/  run/  ca/
  home/.recamera/                         # skill device registry (root, 0600)
  commands.d/                             # owner-added Muse commands
/userdata/local/venvs/musegadget/         # per-release Python environment
```

There is no `/userdata/musegadget` or other shared tool directory. The skill is
part of the app package, so AppMgr installs/upgrades/removes it with the app;
the persisted appdata (including pairing and the skill's device record) is kept
across upgrades by the platform.

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