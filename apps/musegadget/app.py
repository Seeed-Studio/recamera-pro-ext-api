# Copyright (c) Meta Platforms, Inc. and affiliates.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Muse Gadget for the reCamera Pro App Center.

A thin, App-Center-managed wrapper around the pure-Python ``musegadget``
service.  The wrapper owns the things the device image cannot provide:

* a CA bundle with the four roots Muse refuses to trust removed;
* the environment (state/socket/commands paths, run-as account, proxy, CA);
* supervision of ``python -m musegadget run`` and of the short-lived
  ``pair`` / ``unpair`` processes;
* a small authenticated web UI for pairing, status and logs.

It deliberately contains no Muse protocol code: the venv wheel does that.
"""

from __future__ import annotations

import collections
import functools
import hashlib
import hmac
import json
import os
import pwd
import re
import secrets
import ssl
import subprocess
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from kit.app import App, run_app
from kit.config import appdata_root, app_dir_of

APP_ID = "musegadget"
WEBUI_PORT = 8099

# appmgr launches every app with this exact per-release interpreter.  The
# path is spelled out at each subprocess call (never through a variable): the
# App Center source validator rejects computed executables.
VENV_BIN = "/userdata/local/venvs/musegadget/current/bin"

CA_SOURCES = (
    Path("/etc/ssl/certs/ca-certificates.crt"),
    Path("/etc/pki/tls/certs/ca-bundle.crt"),
    Path("/etc/ssl/cert.pem"),
)

# The exact roots Muse refuses to trust.  Kept here as a fallback for the
# (unlikely) case where the wheel cannot be imported while the bundle is built.
DISTRUSTED_SHA256 = frozenset({
    # emSign ECC Root CA - G3 (compromised)
    "86a1ecba089c4a8d3bbe2734c612ba341d813e043cf9e8a862cd5c57a36bbe6b",
    # Comodo AAA Services root (compromised)
    "d7a7a0fb5d7e2731d771e9484ebcdef71d5f0c3e0a2948782bc83ee0ea699ef4",
    # emSign Root CA - C1 (retired)
    "125609aa301da0a249b97a8239cb6a34216f44dcac9f3954b14292f2e8c8608f",
    # emSign ECC Root CA - C3 (retired)
    "bc4d809b15189d78db3e1d8cf4f9726a795da1643ca5f1358e1ddb0edc0d7eb3",
})

_SDK_TOKEN_RE = re.compile(r"mgst_[A-Za-z0-9_-]{42}[AEIMQUYcgkosw048]")
_PEM_RE = re.compile(
    rb"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----", re.S
)
_LOGIN_LINE_RE = re.compile(r"choose (MuseGadget[0-9A-F]{6})", re.I)
_BLE_NAME_RE = re.compile(r"BLE name:\s*(MuseGadget[0-9A-F]{6})", re.I)
_NODE_ID_RE = re.compile(r"node id:\s*([a-z]+-[0-9a-f]{6})", re.I)
_MAC_RE = re.compile(r"[0-9a-f]{2}(?::[0-9a-f]{2}){5}")

MAX_BODY = 64 * 1024
MAX_LINES = 300
CHILD_HEALTHY_S = 30.0
STOP_GRACE_S = 3.5


def _atomic_write(path: Path, data: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        os.chmod(path, mode)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def _read_text(path: Path, limit: int = 4000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[-limit:]
    except OSError:
        return ""


class _WebUIHandler(BaseHTTPRequestHandler):
    """Small authenticated UI handler; a module-level class so the App Center
    source checker can resolve its methods statically."""

    server_version = "MuseGadgetUI/1"

    def __init__(self, app: "MuseGadgetApp", *args, **kwargs) -> None:
        self.app = app
        super().__init__(*args, **kwargs)

    def log_message(self, fmt, *args):
        return

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        super().send_response(status)
        super().send_header("Content-Type", "application/json")
        super().send_header("Content-Length", str(len(body)))
        super().send_header("Cache-Control", "no-store")
        super().end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        header = self.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            return False
        return hmac.compare_digest(header[7:].strip(), self.app._webui_token)

    def _static(self, name: str) -> bool:
        allowed = {
            "": "index.html",
            "index.html": "index.html",
            "app.js": "app.js",
            "style.css": "style.css",
            "icon.jpg": "icon.jpg",
            "fonts/Montserrat-Regular.woff2": "fonts/Montserrat-Regular.woff2",
            "fonts/Montserrat-Medium.woff2": "fonts/Montserrat-Medium.woff2",
            "fonts/Montserrat-SemiBold.woff2": "fonts/Montserrat-SemiBold.woff2",
            "fonts/Montserrat-Bold.woff2": "fonts/Montserrat-Bold.woff2",
        }
        target = allowed.get(name)
        if target is None:
            return False
        path = (self.app._app_dir / "icon.jpg" if target == "icon.jpg"
                else self.app._app_dir / "webui" / target)
        try:
            data = path.read_bytes()
        except OSError:
            return False
        ctype = {
            "index.html": "text/html; charset=utf-8",
            "app.js": "application/javascript; charset=utf-8",
            "style.css": "text/css; charset=utf-8",
            "icon.jpg": "image/jpeg",
            "fonts/Montserrat-Regular.woff2": "font/woff2",
            "fonts/Montserrat-Medium.woff2": "font/woff2",
            "fonts/Montserrat-SemiBold.woff2": "font/woff2",
            "fonts/Montserrat-Bold.woff2": "font/woff2",
        }[target]
        super().send_response(200)
        super().send_header("Content-Type", ctype)
        super().send_header("Content-Length", str(len(data)))
        super().send_header("Cache-Control", "no-store")
        super().end_headers()
        self.wfile.write(data)
        return True

    def do_GET(self) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        if (parsed.path in ("/", "/index.html", "/app.js", "/style.css", "/icon.jpg")
                or parsed.path.startswith("/fonts/")):
            if self._static(parsed.path.lstrip("/")):
                return
            self._json(404, {"ok": False, "error": "not found"})
            return
        if not self._authorized():
            self._json(401, {"ok": False, "error": "unauthorized"})
            return
        if parsed.path == "/api/status":
            self._json(200, {"ok": True, **self.app._status()})
        elif parsed.path == "/api/logs":
            self._json(200, {"ok": True, "lines": self.app._log_tail(parsed.query)})
        elif parsed.path == "/api/doctor":
            self._json(200, self.app._doctor())
        else:
            self._json(404, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:
        try:
            self._post()
        except Exception as exc:
            import traceback
            detail = traceback.format_exc()
            try:
                with (self.app._appdata / "webui-error.log").open("a") as f:
                    f.write(detail + "\n")
            except OSError:
                pass
            try:
                self.app._emit(f"[muse-app] POST {self.path} failed: {type(exc).__name__}: {exc}")
            except Exception:
                pass
            try:
                self._json(500, {"ok": False,
                                 "error": f"{type(exc).__name__}: {exc}"})
            except Exception:
                pass

    def _post(self) -> None:
        if not self._authorized():
            self._json(401, {"ok": False, "error": "unauthorized"})
            return
        parsed = urllib.parse.urlsplit(self.path)
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length < 0 or length > MAX_BODY:
            self._json(413, {"ok": False, "error": "body too large"})
            return
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            self._json(400, {"ok": False, "error": "invalid JSON"})
            return
        if not isinstance(body, dict):
            self._json(400, {"ok": False, "error": "JSON object required"})
            return

        if parsed.path == "/api/pair":
            force = bool(body.get("force", self.app._pair_force))
            try:
                timeout = int(body.get("timeout_s", self.app._pair_timeout_s))
            except (TypeError, ValueError):
                timeout = self.app._pair_timeout_s
            self.app._emit("[muse-app] pair request received")
            try:
                ok = self.app._start_pairing(force, timeout)
            except Exception as exc:
                self.app._emit(f"[muse-app] pair request failed: {type(exc).__name__}: {exc}")
                self._json(500, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})
                return
            self.app._emit(f"[muse-app] pair request -> {ok}")
            self._json(200 if ok else 409,
                       {"ok": ok, "state": self.app._pair_state,
                        "result": self.app._pair_result})
        elif parsed.path == "/api/pair/cancel":
            self.app._cancel_pairing()
            self._json(200, {"ok": True})
        elif parsed.path == "/api/unpair":
            ok = self.app._start_unpair()
            self._json(200 if ok else 409, {"ok": ok})
        elif parsed.path == "/api/service/restart":
            self.app._stop_child()
            self.app._next_child_at = 0.0
            self._json(200, {"ok": True})
        elif parsed.path == "/api/settings":
            ok, error = self.app._save_settings(body)
            self._json(200 if ok else 400,
                       {"ok": ok, "error": error, **self.app._status()})
        else:
            self._json(404, {"ok": False, "error": "not found"})


class MuseGadgetApp(App):
    id = APP_ID
    name = "Muse Gadget"
    owns_loop = True
    needs_model = False
    needs_frames = False

    def __init__(self) -> None:
        super().__init__()
        self._lock = threading.RLock()
        self._app_dir = Path(".")
        self._appdata = Path(appdata_root()) / APP_ID
        self._state_dir = self._appdata / "state"
        self._run_dir = self._appdata / "run"
        self._commands_dir = self._appdata / "commands.d"
        self._home = self._appdata / "home"
        self._ca_dir_root = self._appdata / "ca"
        self._ca_file = self._ca_dir_root / "ca-certificates.crt"
        self._ca_dir = self._ca_dir_root / "ssl-certs"
        self._webui_token_file = self._appdata / "webui_token"

        self._webui_token = ""
        self._sdk_token = ""
        self._sdk_token_error = ""
        self._proxy_url = ""
        self._no_proxy = ""
        self._run_as = "admin"
        self._pair_timeout_s = 600
        self._autopair = False
        self._pair_force = False
        self._wifi_provisioning = False
        self._autopair_done = False

        self._version = "0.0.0"
        self._ble_name = ""
        self._node_id = ""
        self._paired = False
        self._sdk_token_hint = ""

        self._ca_total = 0
        self._ca_dropped = 0
        self._core_ok = False
        self._ble_ok = False
        self._core_error = ""

        self._child: subprocess.Popen | None = None
        self._child_started = 0.0
        self._child_failures = 0
        self._child_last_exit: int | None = None
        self._next_child_at = 0.0
        self._child_log: collections.deque[str] = collections.deque(maxlen=MAX_LINES)

        self._hold_service = False
        self._pair_proc: subprocess.Popen | None = None
        self._pair_thread: threading.Thread | None = None
        self._pair_state = "idle"
        self._pair_lines: collections.deque[str] = collections.deque(maxlen=MAX_LINES)
        self._pair_deadline = 0.0
        self._pair_result = ""
        self._unpair_proc: subprocess.Popen | None = None
        self._unpair_thread: threading.Thread | None = None

        self._httpd: ThreadingHTTPServer | None = None
        self._http_thread: threading.Thread | None = None
        self._events: collections.deque[str] = collections.deque(maxlen=MAX_LINES)

    # -- kit lifecycle -------------------------------------------------------

    def _emit(self, message: str) -> None:
        """Record a line in-memory and in appdata, then try appmgr's stdout.

        appmgr's log pipe can close after READY (a print then raises
        BrokenPipeError in any thread); a diagnostic must never take down the
        pairing request or stop the child-output pump.
        """
        with self._lock:
            self._events.append(message)
            try:
                with (self._appdata / "musegadget.log").open("a", encoding="utf-8") as f:
                    f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + message + "\n")
                    f.flush()
            except OSError:
                pass
        try:
            print(message, flush=True)
        except (BrokenPipeError, OSError, ValueError):
            pass

    def setup(self, config: dict) -> None:
        super().setup(config)
        self._app_dir = Path(app_dir_of(self)).resolve()
        self._mkdirs()
        self._webui_token = self._ensure_webui_token()
        self._version = str(self._load_manifest().get("version") or "0.0.0")

        self._sdk_token = str(config.get("sdk_token") or "").strip()
        self._proxy_url = str(config.get("proxy_url") or "").strip()
        self._no_proxy = str(config.get("no_proxy") or "").strip()
        self._run_as = str(config.get("run_as") or "admin").strip() or "admin"
        try:
            self._pair_timeout_s = int(config.get("pair_timeout_s") or 600)
        except (TypeError, ValueError):
            self._pair_timeout_s = 600
        self._pair_timeout_s = max(60, min(1800, self._pair_timeout_s))
        self._autopair = bool(config.get("autopair"))
        self._pair_force = bool(config.get("pair_force"))
        self._wifi_provisioning = bool(config.get("wifi_provisioning"))

        self._store_sdk_token()
        self._refresh_identity()

    def prepare_runtime(self) -> None:
        self._filter_ca_bundle()
        self._probe_imports()
        self._run_info()
        self._start_http()

    def run(self) -> None:
        if self._autopair and not self._autopair_done and not self._paired:
            self._autopair_done = True
            self._start_pairing(self._pair_force, self._pair_timeout_s, source="startup")
        while not self._stop_flag:
            self.tick()
            self._reconcile()
            thread = self._http_thread
            if thread is not None and not thread.is_alive():
                raise RuntimeError("web UI thread exited")
            time.sleep(0.5)

    def finish(self) -> None:
        httpd, thread = self._httpd, self._http_thread
        self._httpd = None
        self._http_thread = None
        if httpd is not None:
            try:
                httpd.shutdown()
                httpd.server_close()
            except Exception:
                pass
        if thread is not None:
            thread.join(timeout=2.0)
        pair = self._pair_proc
        self._pair_proc = None
        if pair is not None and pair.poll() is None:
            try:
                pair.terminate()
                pair.wait(timeout=1.5)
            except Exception:
                try:
                    pair.kill()
                except Exception:
                    pass
        unpair = self._unpair_proc
        self._unpair_proc = None
        if unpair is not None and unpair.poll() is None:
            try:
                unpair.terminate()
                unpair.wait(timeout=1.5)
            except Exception:
                try:
                    unpair.kill()
                except Exception:
                    pass
        self._stop_child()
        super().finish()

    def on_config_reload(self, config: dict) -> None:
        super().on_config_reload(config)
        try:
            self._pair_timeout_s = max(
                60, min(1800, int(config.get("pair_timeout_s") or self._pair_timeout_s))
            )
        except (TypeError, ValueError):
            pass
        self._autopair = bool(config.get("autopair"))
        self._pair_force = bool(config.get("pair_force"))
        self._wifi_provisioning = bool(config.get("wifi_provisioning"))

    # -- paths and identity --------------------------------------------------

    def _mkdirs(self) -> None:
        for path, mode in (
            (self._appdata, 0o700),
            (self._state_dir, 0o700),
            (self._run_dir, 0o755),
            (self._commands_dir, 0o755),
            (self._home, 0o755),
            (self._ca_dir_root, 0o755),
            (self._ca_dir, 0o755),
        ):
            path.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(path, mode)
            except OSError:
                pass

    def _load_manifest(self) -> dict:
        try:
            data = json.loads((self._app_dir / "manifest.json").read_text())
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _ensure_webui_token(self) -> str:
        try:
            token = self._webui_token_file.read_text(encoding="utf-8").strip()
            if token:
                return token
        except OSError:
            pass
        token = secrets.token_urlsafe(24)
        _atomic_write(self._webui_token_file, (token + "\n").encode(), 0o600)
        return token

    def _store_sdk_token(self) -> None:
        path = self._state_dir / "sdk_token"
        token = self._sdk_token.strip()
        if not token:
            self._sdk_token_error = ""
            return
        if not _SDK_TOKEN_RE.fullmatch(token):
            self._sdk_token_error = "SDK token format is invalid"
            return
        _atomic_write(path, (token + "\n").encode(), 0o600)
        self._sdk_token_error = ""

    def _refresh_identity(self) -> None:
        identity = self._read_json(self._state_dir / "identity.json")
        pairing = self._read_json(self._state_dir / "pairing.json")
        mac = identity.get("mac") if isinstance(identity, dict) else None
        if isinstance(mac, str) and _MAC_RE.fullmatch(mac):
            suffix = mac.replace(":", "")[-6:]
            self._ble_name = "MuseGadget" + suffix.upper()
            stored = pairing.get("node_id") if isinstance(pairing, dict) else None
            if isinstance(stored, str) and re.fullmatch(r"[a-z]+-[0-9a-f]{6}", stored):
                self._node_id = stored
            else:
                self._node_id = "homelink-" + suffix.lower()
        self._paired = self._is_paired(pairing)
        token_path = self._state_dir / "sdk_token"
        try:
            token = token_path.read_text(encoding="utf-8").strip()
        except OSError:
            token = ""
        self._sdk_token_hint = token[:12] + "..." if token else ""

    @staticmethod
    def _read_json(path: Path) -> dict:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    @staticmethod
    def _is_paired(pairing: dict | None = None) -> bool:
        if pairing is None:
            try:
                pairing = json.loads(
                    (Path(appdata_root()) / APP_ID / "state" / "pairing.json")
                    .read_text(encoding="utf-8")
                )
            except (OSError, ValueError):
                return False
        if not isinstance(pairing, dict):
            return False
        return all(
            isinstance(pairing.get(key), str) and pairing[key].strip()
            for key in ("access_token", "refresh_token")
        )

    def _run_info(self) -> None:
        try:
            result = subprocess.run(
                ["/userdata/local/venvs/musegadget/current/bin/python", "-I",
                 "-m", "musegadget", "info"],
                env=self._child_env(), cwd=str(self._appdata),
                capture_output=True, text=True, errors="replace", timeout=20,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            self._emit(f"[muse-app] info failed: {exc}")
            return
        text = (result.stdout or "") + (result.stderr or "")
        match = _BLE_NAME_RE.search(text)
        if match:
            self._ble_name = match.group(1)
        match = _NODE_ID_RE.search(text)
        if match:
            self._node_id = match.group(1)
        if "paired:    yes" in text:
            self._paired = True
        self._refresh_identity()

    # -- CA bundle -----------------------------------------------------------

    def _filter_ca_bundle(self) -> None:
        source = next((path for path in CA_SOURCES if path.is_file()), None)
        if source is None:
            raise RuntimeError("no system CA bundle found (expected /etc/ssl/certs/ca-certificates.crt)")
        data = source.read_bytes()
        blocks = _PEM_RE.findall(data)
        if not blocks:
            raise RuntimeError(f"{source} contains no PEM certificates")

        def distrusted(der: bytes) -> bool:
            try:
                from musegadget import tls  # type: ignore
                return tls.distrusted_name(der) is not None
            except Exception:
                return hashlib.sha256(der).hexdigest() in DISTRUSTED_SHA256

        kept: list[bytes] = []
        dropped = 0
        for block in blocks:
            try:
                der = ssl.PEM_cert_to_DER_cert(block.decode("ascii"))
            except (ValueError, UnicodeDecodeError):
                kept.append(block)
                continue
            if distrusted(der):
                dropped += 1
                continue
            kept.append(block)
        if not kept:
            raise RuntimeError("CA filtering removed every certificate")
        _atomic_write(
            self._ca_file,
            b"\n".join(kept) + b"\n",
            0o644,
        )
        self._ca_dir.mkdir(parents=True, exist_ok=True)
        self._ca_total = len(blocks)
        self._ca_dropped = dropped
        self._emit(
            f"[muse-app] CA bundle: kept {len(kept)}/{len(blocks)}, "
            f"dropped {dropped} distrusted root(s) -> {self._ca_file}"
        )

    # -- environment and child process --------------------------------------

    def _child_env(self) -> dict:
        env = {
            "HOME": str(self._home),
            "PATH": f"{VENV_BIN}:/usr/bin:/bin:/sbin:/usr/sbin:/oem/usr/bin",
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "PYTHONUNBUFFERED": "1",
            "PYTHONNOUSERSITE": "1",
            "MUSEGADGET_STATE_DIR": str(self._state_dir),
            "MUSEGADGET_SOCKET": str(self._run_dir / "musegadget.sock"),
            "MUSEGADGET_COMMANDS_DIR": str(self._commands_dir),
            "MUSEGADGET_RUN_AS": self._run_as,
            # reCamera hardware identity and WiFi provisioning backend.  The
            # BLE name stays MuseGadgetXXXXXX (the Muse app matches it to the
            # node id), but Muse's device model/display name can say reCamera.
            "MUSEGADGET_DISPLAY_NAME": "reCamera Pro",
            "MUSEGADGET_MODEL_ID": "recamera-pro",
            "MUSEGADGET_MODEL_NAME": "reCamera Pro",
            "MUSEGADGET_GUIDE_FILE": str(self._app_dir / "guide.md"),
            "SSL_CERT_FILE": str(self._ca_file),
            "SSL_CERT_DIR": str(self._ca_dir),
        }
        if self._wifi_provisioning:
            env["MUSEGADGET_WIFI_BACKEND"] = "recamera"
        tz = os.environ.get("TZ")
        if tz:
            # Inherit the device's timezone when appmgr has one; otherwise let
            # Python read /etc/localtime.  Do not force UTC: Muse's log
            # timestamps should match the device clock.
            env["TZ"] = tz
        ld_path = os.environ.get("LD_LIBRARY_PATH")
        if ld_path:
            env["LD_LIBRARY_PATH"] = ld_path
        if self._proxy_url:
            env["HTTPS_PROXY"] = self._proxy_url
            env["HTTP_PROXY"] = self._proxy_url
            env["ALL_PROXY"] = self._proxy_url
        no_proxy = self._no_proxy.strip() or "127.0.0.1,localhost"
        env["NO_PROXY"] = no_proxy
        return env

    def _probe_imports(self) -> None:
        core = subprocess.run(
            ["/userdata/local/venvs/musegadget/current/bin/python", "-I", "-u", "-c",
             "import musegadget, cryptography, websockets, cffi; "
             "print(websockets.__version__)"],
            env=self._child_env(), cwd=str(self._appdata),
            capture_output=True, text=True, errors="replace", timeout=30,
            check=False,
        )
        self._core_ok = core.returncode == 0
        if not self._core_ok:
            self._core_error = (core.stderr or core.stdout or "unknown error")[-800:]
            raise RuntimeError(f"musegadget venv import probe failed: {self._core_error}")
        self._emit(f"[muse-app] import probe ok (websockets {(core.stdout or '').strip()})")

        ble = subprocess.run(
            ["/userdata/local/venvs/musegadget/current/bin/python", "-I", "-u", "-c",
             "import dbus, dbus.mainloop.glib; from gi.repository import GLib; print('ok')"],
            env=self._child_env(), cwd=str(self._appdata),
            capture_output=True, text=True, errors="replace", timeout=30,
            check=False,
        )
        self._ble_ok = ble.returncode == 0
        if not self._ble_ok:
            self._emit(
                "[muse-app] BLE bindings unavailable: "
                + ((ble.stderr or ble.stdout or "").strip()[-400:])
            )

    def _start_child(self) -> None:
        if self._hold_service or not self._is_paired():
            return
        now = time.monotonic()
        if now < self._next_child_at:
            return
        try:
            # The executable and its argv are literal: the App Center source
            # validator refuses computed command entry points.  Never pass -v:
            # Muse's DEBUG logging prints websocket headers, including the VM
            # bearer token, and the app log is viewable from its web UI.
            proc = subprocess.Popen(
                ["/userdata/local/venvs/musegadget/current/bin/python",
                 "-I", "-u",
                 "/userdata/local/apps/musegadget/recamera_runner.py"],
                cwd=str(self._appdata), env=self._child_env(),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                errors="replace", bufsize=1, start_new_session=False, close_fds=True,
            )
        except OSError as exc:
            self._child_failures += 1
            self._child_last_exit = None
            self._next_child_at = now + min(2 ** self._child_failures, 60)
            self._emit(f"[muse-app] could not start service: {exc}")
            return
        self._child = proc
        self._child_started = now
        self._child_last_exit = None
        threading.Thread(
            target=self._pump, args=(proc, "[musegadget]", self._child_log),
            name="musegadget-log", daemon=True,
        ).start()
        self._emit(f"[muse-app] started musegadget run (pid {proc.pid})")

    def _stop_child(self) -> None:
        with self._lock:
            proc, self._child = self._child, None
        if proc is None or proc.poll() is not None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=STOP_GRACE_S)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
                proc.wait(timeout=1.0)
            except Exception:
                pass
        except Exception:
            pass
        try:
            if proc.stdout is not None:
                proc.stdout.close()
        except Exception:
            pass

    def _record_child_exit(self, proc: subprocess.Popen) -> None:
        lasted = time.monotonic() - self._child_started
        if lasted >= CHILD_HEALTHY_S:
            self._child_failures = 0
        else:
            self._child_failures += 1
        self._child_last_exit = proc.returncode
        self._child = None
        delay = min(2 ** max(self._child_failures, 1), 60)
        self._next_child_at = time.monotonic() + delay
        self._emit(
            f"[muse-app] musegadget run exited {proc.returncode} after "
            f"{lasted:.1f}s; retry in {delay}s"
        )

    def _pump(self, proc: subprocess.Popen, prefix: str,
              sink: collections.deque[str]) -> None:
        try:
            if proc.stdout is None:
                return
            for raw in proc.stdout:
                line = raw.rstrip("\r\n")
                with self._lock:
                    sink.append(line)
                self._emit(f"{prefix} {line}")
        except Exception:
            pass
        finally:
            try:
                if proc.stdout is not None:
                    proc.stdout.close()
            except Exception:
                pass

    def _reconcile(self) -> None:
        with self._lock:
            pair = self._pair_proc
            unpair = self._unpair_proc
            child = self._child
        if pair is not None:
            self._poll_pair(pair)
        if unpair is not None:
            self._poll_unpair(unpair)
        if self._hold_service:
            self._stop_child()
            return

        # Re-read the pairing every tick: `pair` and `unpair` write it from a
        # child process, and the app's cached view must follow within a second.
        self._paired = self._is_paired()
        if not self._paired:
            self._stop_child()
            return
        if child is not None and child.poll() is None:
            return
        if child is not None:
            self._record_child_exit(child)
        self._start_child()

    # -- pairing -------------------------------------------------------------

    def _pair_precondition(self) -> str:
        if not Path("/sys/class/bluetooth/hci0").exists():
            return "no Bluetooth adapter (hci0); check S39bt-firmware / BlueZ"
        if not Path("/run/dbus/system_bus_socket").exists():
            return "system D-Bus is not running"
        if not self._ble_ok:
            return "dbus/gi Python bindings are unavailable"
        return ""

    def _start_pairing(self, force: bool, timeout_s: int | None = None,
                       source: str = "ui") -> bool:
        with self._lock:
            if self._pair_proc is not None and self._pair_proc.poll() is None:
                return False
            problem = self._pair_precondition()
            if problem:
                self._pair_state = "error"
                self._pair_result = problem
                self._pair_lines.append(problem)
                return False
            timeout_s = max(60, min(1800, int(timeout_s or self._pair_timeout_s)))
            self._hold_service = True
            self._stop_child()
            self._pair_lines.clear()
            self._pair_state = "pairing"
            self._pair_result = ""
            self._pair_deadline = time.monotonic() + timeout_s
            try:
                if force:
                    proc = subprocess.Popen(
                        ["/userdata/local/venvs/musegadget/current/bin/python",
                         "-I", "-u", "-m", "musegadget", "pair",
                         "--timeout", str(timeout_s), "--force"],
                        cwd=str(self._appdata), env=self._child_env(),
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                        errors="replace", bufsize=1, start_new_session=False,
                        close_fds=True,
                    )
                else:
                    proc = subprocess.Popen(
                        ["/userdata/local/venvs/musegadget/current/bin/python",
                         "-I", "-u", "-m", "musegadget", "pair",
                         "--timeout", str(timeout_s)],
                        cwd=str(self._appdata), env=self._child_env(),
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                        errors="replace", bufsize=1, start_new_session=False,
                        close_fds=True,
                    )
            except OSError as exc:
                self._pair_state = "error"
                self._pair_result = f"could not start pair: {exc}"
                self._hold_service = False
                return False
            self._pair_proc = proc
            self._emit(f"[muse-app] pairing window open ({source}, pid {proc.pid})")
            self._pair_thread = threading.Thread(
                target=self._pump, args=(proc, "[pair]", self._pair_lines),
                name="musegadget-pair-log", daemon=True,
            )
            self._pair_thread.start()
            return True

    def _poll_pair(self, proc: subprocess.Popen) -> None:
        now = time.monotonic()
        if proc.poll() is None:
            if now >= self._pair_deadline:
                try:
                    proc.terminate()
                    proc.wait(timeout=1.5)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                thread = self._pair_thread
                if thread is not None:
                    thread.join(timeout=0.5)
                with self._lock:
                    self._pair_thread = None
                    self._pair_state = "window_closed"
                    self._pair_result = "pairing window expired"
                    self._pair_proc = None
                    self._hold_service = False
                    self._refresh_identity()
                self._emit("[muse-app] pairing window expired")
            return
        thread = self._pair_thread
        if thread is not None:
            thread.join(timeout=0.5)
        text = "\n".join(list(self._pair_lines))
        if self._is_paired():
            state, result = "paired", "Paired."
        elif "Already paired" in text:
            state, result = "already_paired", "Already paired."
        elif "Can't pair" in text or "SDK token" in text and "invalid" in text.lower():
            state, result = "invalid_sdk_token", "Pairing rejected the SDK token."
        elif "Traceback" in text:
            state, result = "error", "pairing raised an exception; see the log"
        else:
            state, result = "window_closed", "Pairing window closed without pairing."
        with self._lock:
            self._pair_state = state
            self._pair_result = result
            self._pair_proc = None
            self._pair_thread = None
            self._hold_service = False
            self._next_child_at = 0.0
            self._refresh_identity()
        self._emit(f"[muse-app] pairing finished: {state} (exit {proc.returncode})")

    def _cancel_pairing(self) -> None:
        with self._lock:
            proc = self._pair_proc
            thread = self._pair_thread
            self._pair_proc = None
            self._pair_thread = None
            self._pair_state = "cancelled"
            self._pair_result = "Pairing cancelled."
            self._hold_service = False
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=1.5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        if thread is not None:
            thread.join(timeout=0.5)

    def _start_unpair(self) -> bool:
        with self._lock:
            if self._unpair_proc is not None and self._unpair_proc.poll() is None:
                return False
            self._hold_service = True
            self._stop_child()
            try:
                proc = subprocess.Popen(
                    ["/userdata/local/venvs/musegadget/current/bin/python",
                     "-I", "-u", "-m", "musegadget", "unpair"],
                    cwd=str(self._appdata), env=self._child_env(),
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                    errors="replace", bufsize=1, start_new_session=False,
                    close_fds=True,
                )
            except OSError:
                self._hold_service = False
                return False
            self._unpair_proc = proc
            self._unpair_thread = threading.Thread(
                target=self._pump, args=(proc, "[unpair]", self._pair_lines),
                name="musegadget-unpair-log", daemon=True,
            )
            self._unpair_thread.start()
            return True

    def _poll_unpair(self, proc: subprocess.Popen) -> None:
        if proc.poll() is None:
            return
        thread = self._unpair_thread
        if thread is not None:
            thread.join(timeout=0.5)
        with self._lock:
            self._unpair_proc = None
            self._unpair_thread = None
            self._hold_service = False
            self._autopair_done = True
            self._refresh_identity()
        self._emit(f"[muse-app] unpair finished (exit {proc.returncode})")

    # -- web UI --------------------------------------------------------------

    def _start_http(self) -> None:
        try:
            self._httpd = ThreadingHTTPServer(
                ("0.0.0.0", WEBUI_PORT), functools.partial(_WebUIHandler, self)
            )
        except OSError as exc:
            raise RuntimeError(f"cannot bind web UI on :{WEBUI_PORT}: {exc}") from exc
        self._httpd.daemon_threads = True
        self._http_thread = threading.Thread(
            target=self._httpd.serve_forever, name="musegadget-webui", daemon=True
        )
        self._http_thread.start()
        self._emit(f"[muse-app] web UI on http://0.0.0.0:{WEBUI_PORT}/")

    def _status(self) -> dict:
        self._refresh_identity()
        with self._lock:
            child = self._child
            pair = self._pair_proc
            service = {
                "running": child is not None and child.poll() is None,
                "pid": child.pid if child is not None and child.poll() is None else None,
                "failures": self._child_failures,
                "last_exit": self._child_last_exit,
            }
            pairing = {
                "active": pair is not None and pair.poll() is None,
                "state": self._pair_state,
                "result": self._pair_result,
                "remaining_s": max(0, int(self._pair_deadline - time.monotonic()))
                if pair is not None and pair.poll() is None else 0,
                "lines": list(self._pair_lines)[-40:],
            }
            log_lines = list(self._child_log)[-40:]
        return {
            "version": self._version,
            "paired": self._paired,
            "node_id": self._node_id,
            "ble_name": self._ble_name,
            "sdk_token_set": bool(self._sdk_token_hint),
            "sdk_token_hint": self._sdk_token_hint,
            "sdk_token_error": self._sdk_token_error,
            "hci0": Path("/sys/class/bluetooth/hci0").exists(),
            "dbus": Path("/run/dbus/system_bus_socket").exists(),
            "ble_bindings": self._ble_ok,
            "core_imports": self._core_ok,
            "core_error": self._core_error,
            "service": service,
            "pairing": pairing,
            "auto_pair": self._autopair,
            "pair_force": self._pair_force,
            "pair_timeout_s": self._pair_timeout_s,
            "run_as": self._run_as,
            "proxy_configured": bool(self._proxy_url),
            "wifi_provisioning": self._wifi_provisioning,
            "no_proxy": self._no_proxy,
            "ca": {"total": self._ca_total, "dropped": self._ca_dropped},
            "webui_port": WEBUI_PORT,
            "log_lines": log_lines,
        }

    def _log_tail(self, query: str) -> list[str]:
        try:
            params = urllib.parse.parse_qs(query)
            tail = max(1, min(400, int((params.get("tail") or ["120"])[0])))
        except (TypeError, ValueError):
            tail = 120
        event_log = self._appdata / "musegadget.log"
        path = event_log if event_log.is_file() and event_log.stat().st_size else (
            self._app_dir / "logs" / "app.log"
        )
        try:
            with path.open("rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(0, size - 128 * 1024))
                data = f.read()
            lines = data.decode("utf-8", errors="replace").splitlines()
            return lines[-tail:]
        except OSError:
            with self._lock:
                return list(self._child_log)[-tail:]

    def _doctor(self) -> dict:
        try:
            result = subprocess.run(
                ["/userdata/local/venvs/musegadget/current/bin/python", "-I",
                 "-m", "musegadget", "doctor"],
                env=self._child_env(), cwd=str(self._appdata),
                capture_output=True, text=True, errors="replace", timeout=40,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"ok": False, "output": f"doctor failed: {exc}"}
        output = ((result.stdout or "") + (result.stderr or ""))[-12000:]
        return {"ok": result.returncode == 0, "output": output}

    def _save_settings(self, body: dict) -> tuple[bool, str]:
        allowed = {"sdk_token", "proxy_url", "no_proxy", "run_as",
                   "pair_timeout_s", "autopair", "pair_force", "wifi_provisioning"}
        unknown = set(body) - allowed
        if unknown:
            return False, f"unknown setting(s): {', '.join(sorted(unknown))}"

        updates: dict = {}
        restart_needed = False

        if "sdk_token" in body:
            token = str(body.get("sdk_token") or "").strip()
            if token and not _SDK_TOKEN_RE.fullmatch(token):
                return False, "SDK token must look like mgst_... from gadgets.muse.ai"
            updates["sdk_token"] = token
            if token:
                _atomic_write(self._state_dir / "sdk_token", (token + "\n").encode(), 0o600)
            else:
                try:
                    (self._state_dir / "sdk_token").unlink()
                except FileNotFoundError:
                    pass
            self._sdk_token = token
            self._sdk_token_error = ""

        if "proxy_url" in body:
            proxy = str(body.get("proxy_url") or "").strip()
            if proxy:
                parsed = urllib.parse.urlsplit(
                    proxy if "://" in proxy else "http://" + proxy
                )
                if parsed.scheme not in ("http", "https") or not parsed.hostname:
                    return False, "proxy must be http://host:port or https://host:port"
            updates["proxy_url"] = proxy
            if proxy != self._proxy_url:
                restart_needed = True
            self._proxy_url = proxy

        if "no_proxy" in body:
            value = str(body.get("no_proxy") or "").strip()
            updates["no_proxy"] = value
            if value != self._no_proxy:
                restart_needed = True
            self._no_proxy = value

        if "run_as" in body:
            value = str(body.get("run_as") or "").strip()
            if value not in ("admin", "root"):
                return False, "run_as must be admin or root"
            try:
                pwd.getpwnam(value)
            except KeyError:
                return False, f"account {value!r} does not exist"
            updates["run_as"] = value
            if value != self._run_as:
                restart_needed = True
            self._run_as = value

        if "pair_timeout_s" in body:
            try:
                value = int(body.get("pair_timeout_s"))
            except (TypeError, ValueError):
                return False, "pair_timeout_s must be an integer"
            if value < 60 or value > 1800:
                return False, "pair_timeout_s must be 60..1800"
            updates["pair_timeout_s"] = value
            self._pair_timeout_s = value

        if "autopair" in body:
            value = bool(body.get("autopair"))
            updates["autopair"] = value
            self._autopair = value

        if "pair_force" in body:
            value = bool(body.get("pair_force"))
            updates["pair_force"] = value
            self._pair_force = value

        if "wifi_provisioning" in body:
            value = bool(body.get("wifi_provisioning"))
            updates["wifi_provisioning"] = value
            self._wifi_provisioning = value

        try:
            from appmgr import config as appmgr_config  # type: ignore
            appmgr_config.write_user_config(self.id, updates)
        except Exception as exc:
            self._emit(f"[muse-app] appmgr config write unavailable ({exc}); writing appdata")
            path = self._appdata / "config.json"
            merged = self._read_json(path)
            merged.update(updates)
            _atomic_write(path, json.dumps(merged, indent=2).encode(), 0o600)

        if restart_needed:
            self._stop_child()
            self._next_child_at = 0.0
        self._refresh_identity()
        return True, ""


APP = MuseGadgetApp()


if __name__ == "__main__":
    run_app(MuseGadgetApp())