"""
qrdecode.py -- app-bundled robust CPU QR decoder (qrcode-reader 0.2.2).

Based on kit/logic/qrcode.py (same wire protocol, same fork isolation), with
three reliability fixes that the firmware kit does not have. It is bundled
INSIDE the app package because kit itself is firmware-owned and is NOT shipped
per app (market/packaging/build.py excludes kit/).

Fixes over kit.logic.qrcode (0.2.0 defects):
  1. All crash / respawn / recovery diagnostics go to STDOUT (appmgr captures
     stdout into logs/app.log; stderr is dropped, which made 0.2.0 failures
     invisible).
  2. Crash respawn has exponential backoff when the child dies repeatedly.
  3. Wedged-worker health probe: the native WeChat detector can enter a bad
     state where it stays alive but silently returns [] for EVERY frame
     (verified on device 2026-11-26: same frame decodes fine in a fresh
     process). After EMPTY_PROBE_N consecutive empty results the wrapper
     decodes a bundled synthetic QR test image through the SAME worker.
     Probe decodes -> worker healthy (scene really is empty). Probe returns
     [] -> worker wedged -> kill + respawn + re-probe, all logged.

Output shape (one dict per successfully decoded, non-empty code):
    {"text": <decoded string>, "quad": [[x,y], [x,y], [x,y], [x,y]]}
"""
from __future__ import annotations

import json
import multiprocessing
import os
import signal
import struct
import sys
import time
import traceback
from typing import Any, Dict, List, Optional

import cv2
import numpy as np

_WECHAT_FILES = ("detect.prototxt", "detect.caffemodel",
                 "sr.prototxt", "sr.caffemodel")

_ISOLATE_DEFAULT = os.environ.get("QR_ISOLATE", "1") != "0"
_WORKER_TIMEOUT = float(os.environ.get("QR_WORKER_TIMEOUT", "10"))

# -- health probe tuning ---------------------------------------------------- #
EMPTY_PROBE_N = int(os.environ.get("QR_EMPTY_PROBE_N", "25"))
PROBE_COOLDOWN = float(os.environ.get("QR_PROBE_COOLDOWN", "5"))
PROBE_TIMEOUT = float(os.environ.get("QR_PROBE_TIMEOUT", "4"))
BACKOFF_WINDOW = 60.0     # count crashes inside this window for backoff
BACKOFF_MAX = 20.0        # cap respawn delay (s)


def _log(msg: str) -> None:
    # stdout, NOT stderr: appmgr only captures stdout into logs/app.log.
    print("[qrdecode] %s" % msg, file=sys.stdout, flush=True)


def _wechat_worker(conn, paths: List[str], det=None) -> None:
    """Child process: decode frames off `conn` until the parent goes away."""
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    except Exception:
        pass

    # fork() inherits kit's SIGTERM/SIGINT handlers, which RAISE _GracefulStop
    # (a BaseException) inside whatever syscall the child happens to be in --
    # observed on device as tracebacks through conn.recv_bytes() and
    # det.detectAndDecode() on every app restart. The child has no graceful
    # shutdown to perform; restore default disposition so SIGTERM just kills
    # it silently and SIGINT never surfaces as a fake decode crash.
    for _sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        try:
            signal.signal(_sig, signal.SIG_DFL)
        except (OSError, ValueError):
            pass

    keep = {0, 1, 2, conn.fileno()}
    try:
        for name in os.listdir("/proc/self/fd"):
            fd = int(name)
            if fd not in keep:
                try:
                    os.close(fd)
                except OSError:
                    pass
    except OSError:
        pass

    if det is None:
        try:
            det = cv2.wechat_qrcode.WeChatQRCode(*paths)
        except Exception:
            _log("worker: WeChatQRCode init FAILED: %s" % traceback.format_exc()
                 .strip().splitlines()[-1])
            os._exit(3)

    err_count = 0
    while True:
        try:
            blob = conn.recv_bytes()
        except (EOFError, OSError):
            break
        except BaseException:
            # Stray kit stop signals (_GracefulStop) inherited via fork():
            # die silently instead of printing a bogus traceback (0.2.2
            # field defect: worker "crashed" at conn.recv_bytes on restart).
            os._exit(0)
        if len(blob) < 12:
            break
        h, w, c = struct.unpack("<III", blob[:12])
        frame = np.frombuffer(blob, dtype=np.uint8, offset=12).reshape(h, w, c)
        out: List[Dict[str, Any]] = []
        try:
            texts, points = det.detectAndDecode(frame)
            for text, quad in zip(texts, points):
                if text:
                    out.append({"text": text, "quad": QrDecoder._quad(quad)})
        except cv2.error:
            out = []
        except BaseException as exc:
            if not isinstance(exc, Exception):
                os._exit(0)  # _GracefulStop / KeyboardInterrupt: silent exit
            err_count += 1
            if err_count <= 3:
                _log("worker: decode exception #%d: %s"
                     % (err_count, traceback.format_exc().strip()
                        .splitlines()[-1]))
            out = []
        try:
            conn.send_bytes(json.dumps(out).encode())
        except (BrokenPipeError, OSError):
            break
    os._exit(0)


class _IsolatedWeChat:
    """Fork-per-life wrapper around `cv2.wechat_qrcode.WeChatQRCode`.

    `decode()` never raises and never lets a native SIGSEGV reach the caller.
    Adds: stdout diagnostics, crash backoff, and a synthetic-QR health probe
    that detects and rebuilds a wedged (alive-but-always-empty) worker.
    """

    def __init__(self, paths: List[str], timeout: float = _WORKER_TIMEOUT,
                 health_img: Optional[np.ndarray] = None):
        self._paths = list(paths)
        self._timeout = timeout
        self._ctx = multiprocessing.get_context("fork")
        self._proc = None
        self._conn = None
        self.crashes = 0
        self.rebuilds = 0
        self._crash_times: List[float] = []
        self._empty_streak = 0
        self._last_probe = 0.0
        self._health_img = health_img
        # NOTE: never construct the detector in the parent and let fork()
        # inherit it -- OpenCV dnn state does not survive fork (kit comment).

    # -- process lifecycle -------------------------------------------------- #
    def _reap(self) -> None:
        conn, proc = self._conn, self._proc
        self._conn = self._proc = None
        if conn is not None:
            try:
                conn.close()
            except OSError:
                pass
        if proc is not None:
            if proc.is_alive():
                try:
                    proc.terminate()
                except OSError:
                    pass
            proc.join(2)
            try:
                proc.close()
            except Exception:
                pass

    def _spawn(self) -> None:
        parent, child = self._ctx.Pipe(duplex=True)
        proc = self._ctx.Process(target=_wechat_worker,
                                 args=(child, self._paths), daemon=True)
        proc.start()
        child.close()
        self._proc, self._conn = proc, parent

    def _backoff_sleep(self) -> None:
        now = time.monotonic()
        self._crash_times = [t for t in self._crash_times
                             if now - t <= BACKOFF_WINDOW]
        self._crash_times.append(now)
        n = len(self._crash_times)
        if n >= 4:
            delay = min(2.0 ** (n - 3), BACKOFF_MAX)
            _log("crash #%d within %.0fs -- backoff %.1fs before respawn"
                 % (n, BACKOFF_WINDOW, delay))
            time.sleep(delay)

    def _send_recv(self, frame: np.ndarray, timeout: float):
        frame = np.ascontiguousarray(frame)
        h, w = frame.shape[:2]
        c = frame.shape[2] if frame.ndim == 3 else 1
        self._conn.send_bytes(struct.pack("<III", h, w, c) + frame.tobytes())
        if not self._conn.poll(timeout):
            raise TimeoutError("qr worker wedged (poll timeout)")
        return json.loads(self._conn.recv_bytes().decode())

    # -- health probe -------------------------------------------------------- #
    def _probe_worker(self) -> bool:
        """Decode the synthetic QR through the CURRENT worker. True=healthy."""
        if self._health_img is None:
            return True  # no probe asset: cannot judge, assume healthy
        try:
            out = self._send_recv(self._health_img, PROBE_TIMEOUT)
            return bool(out)
        except Exception:
            return False

    def _health_check(self) -> None:
        now = time.monotonic()
        self._last_probe = now
        if self._proc is None or not self._proc.is_alive():
            self._reap()
            self._spawn()
        if self._probe_worker():
            _log("health probe OK after %d empty frames (worker healthy, "
                 "scene empty)" % self._empty_streak)
            self._empty_streak = 0
            return
        # Wedged: alive but returns [] even for a known-good synthetic QR.
        self.rebuilds += 1
        _log("health probe FAILED -- worker wedged, rebuilding (#%d)"
             % self.rebuilds)
        self._reap()
        self._spawn()
        if self._probe_worker():
            _log("worker rebuilt OK -- decode recovered")
        else:
            _log("worker rebuilt but probe STILL fails (models/scene issue?)")
        self._empty_streak = 0

    # -- public --------------------------------------------------------------- #
    def decode(self, frame: np.ndarray) -> List[Dict[str, Any]]:
        if self._proc is None or not self._proc.is_alive():
            self._reap()
            self._backoff_sleep()
            self._spawn()
        try:
            out = self._send_recv(frame, self._timeout)
        except Exception:
            # Child segfaulted mid-decode (or hung): drop this frame, respawn
            # on the next one. This is the whole point of the isolation.
            self.crashes += 1
            _log("native decoder died (absorbed crash #%d), respawning worker"
                 % self.crashes)
            self._reap()
            return []
        if out:
            self._empty_streak = 0
            return out
        self._empty_streak += 1
        if (self._health_img is not None
                and self._empty_streak >= EMPTY_PROBE_N
                and time.monotonic() - self._last_probe >= PROBE_COOLDOWN):
            self._health_check()
        return []

    def close(self) -> None:
        self._reap()


class QrDecoder:
    """Stateless multi-QR decoder over RGB/BGR uint8 frames.

    `model_dir` is only consulted for the WeChatQRCode backend.
    `health_path` (optional) points at a small synthetic QR image used to
    probe a wedged isolated worker (see module docstring).
    """

    def __init__(self, model_dir: Optional[str] = None,
                 isolate: Optional[bool] = None,
                 health_path: Optional[str] = None) -> None:
        health_img = None
        if health_path and os.path.isfile(health_path):
            try:
                health_img = cv2.imread(health_path)
            except Exception:
                health_img = None
        if hasattr(cv2, "QRCodeDetector"):
            self.backend = "builtin"
            self._det = cv2.QRCodeDetector()
        elif hasattr(cv2, "wechat_qrcode") and hasattr(cv2.wechat_qrcode,
                                                       "WeChatQRCode"):
            paths = self._wechat_paths(model_dir)
            if isolate is None:
                isolate = _ISOLATE_DEFAULT
            if isolate:
                self.backend = "wechat-isolated"
                self._det = _IsolatedWeChat(paths, health_img=health_img)
            else:
                self.backend = "wechat"
                self._det = cv2.wechat_qrcode.WeChatQRCode(*paths)
        else:
            raise RuntimeError(
                "cv2 has no QR backend (need QRCodeDetector or wechat_qrcode)")
        if health_img is None and health_path:
            _log("health probe asset missing (%s) -- probe disabled"
                 % health_path)

    @staticmethod
    def _wechat_paths(model_dir: Optional[str]) -> List[str]:
        if not model_dir:
            raise RuntimeError(
                "wechat_qrcode backend requires model_dir with "
                + ", ".join(_WECHAT_FILES))
        paths = [os.path.join(model_dir, n) for n in _WECHAT_FILES]
        missing = [p for p in paths if not os.path.isfile(p)]
        if missing:
            raise RuntimeError(f"missing WeChat QR model files: {missing}")
        return paths

    # -- tiled full-frame scan (0.2.2 multi-code fix) ---------------------- #
    # Measured on device 2026-11-26 (A4 chart, 6 codes, same text): a single
    # WeChat detectAndDecode over the WHOLE frame finds only 2 of 6 codes at
    # EVERY tier (720p/1080p/4K) -- the CNN detector internally downscales and
    # small codes vanish. A 2x2 overlapping tile scan (each tile normalized to
    # ~1920px long side) finds ALL 6 at every tier (~1.0 s total on RV1126B).
    TILE_LONG = 1920

    def decode_tiled(self, frame: np.ndarray) -> List[Dict[str, Any]]:
        """Full-frame multi-code scan: 2x2 overlapping tiles, merged quads.

        Quad coordinates are mapped back to FULL-FRAME pixels. Duplicates
        (same physical code seen by two overlapping tiles) are merged by
        center proximity, independent of text -- the chart carries several
        codes with identical text.
        """
        if frame is None or getattr(frame, "size", 0) == 0:
            return []
        h, w = frame.shape[:2]
        if max(w, h) <= 640:
            return self.decode(frame)   # tiny frame: tiling adds nothing
        tw, th = w // 2, h // 2
        ovx, ovy = tw // 8, th // 8
        merged: List[Dict[str, Any]] = []
        centers: List[tuple] = []
        for oy in (0, h - th):
            for ox in (0, w - tw):
                x0, y0 = max(0, ox - ovx), max(0, oy - ovy)
                x1, y1 = min(w, ox + tw + ovx), min(h, oy + th + ovy)
                tile = frame[y0:y1, x0:x1]
                sc = self.TILE_LONG / float(tile.shape[1])
                if abs(sc - 1.0) > 0.05:
                    tile = cv2.resize(
                        tile, None, fx=sc, fy=sc,
                        interpolation=cv2.INTER_LINEAR if sc > 1
                        else cv2.INTER_AREA)
                else:
                    sc = 1.0
                for r in self.decode(tile):
                    quad = [[round(x / sc + x0, 1), round(y / sc + y0, 1)]
                            for x, y in r["quad"]]
                    xs = [p[0] for p in quad]
                    ys = [p[1] for p in quad]
                    cx = (min(xs) + max(xs)) / 2.0
                    cy = (min(ys) + max(ys)) / 2.0
                    size = max(max(xs) - min(xs), max(ys) - min(ys), 1.0)
                    dup = False
                    for mcx, mcy, ms in centers:
                        d = ((cx - mcx) ** 2 + (cy - mcy) ** 2) ** 0.5
                        if d < 0.75 * max(size, ms):
                            dup = True
                            break
                    if not dup:
                        centers.append((cx, cy, size))
                        merged.append({"text": r["text"], "quad": quad})
        return merged

    # -- decode ----------------------------------------------------------- #
    def decode(self, frame: np.ndarray) -> List[Dict[str, Any]]:
        if frame is None or getattr(frame, "size", 0) == 0:
            return []
        if self.backend == "builtin":
            return self._decode_builtin(frame)
        if self.backend == "wechat-isolated":
            return self._det.decode(frame)
        return self._decode_wechat(frame)

    @property
    def crashes(self) -> int:
        return getattr(self._det, "crashes", 0)

    @property
    def rebuilds(self) -> int:
        return getattr(self._det, "rebuilds", 0)

    def close(self) -> None:
        closer = getattr(self._det, "close", None)
        if closer is not None:
            closer()

    def _decode_builtin(self, frame: np.ndarray) -> List[Dict[str, Any]]:
        try:
            ok, infos, points, _straight = self._det.detectAndDecodeMulti(frame)
        except cv2.error:
            return []
        if not ok or points is None:
            return []
        out: List[Dict[str, Any]] = []
        for text, quad in zip(infos, points):
            if not text:
                continue
            out.append({"text": text, "quad": self._quad(quad)})
        return out

    def _decode_wechat(self, frame: np.ndarray) -> List[Dict[str, Any]]:
        try:
            texts, points = self._det.detectAndDecode(frame)
        except cv2.error:
            return []
        out: List[Dict[str, Any]] = []
        for text, quad in zip(texts, points):
            if not text:
                continue
            out.append({"text": text, "quad": self._quad(quad)})
        return out

    @staticmethod
    def _quad(quad: Any) -> List[List[int]]:
        pts = np.asarray(quad, dtype=float).reshape(-1, 2)
        return [[int(round(x)), int(round(y))] for x, y in pts]
