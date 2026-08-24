"""
CgiControl -- workaround control plane over the device's existing entry.cgi.

L0 adapter layer (docs/guide/adapter-bootstrap.md §2.4 R4). This is the reverse-engineered
counterpart to `OfficialControl`: instead of a future versioned control API it
drives the endpoints the shipped firmware already exposes through nginx +
`entry.cgi`, so the 9 kit apps get a working `ControlPlane` on TODAY's firmware
with zero application changes (registry.py swaps `OfficialControl` in later).

Two capabilities, two very different mechanisms
-----------------------------------------------
* set_inference(enable/model/fps)  -- a real device endpoint exists:
      POST http://127.0.0.1/cgi-bin/entry.cgi/model/inference?id=<model_id>
      body JSON, all three fields optional and co-sendable:
          {"iEnable":0|1, "sModel":"<file>", "iFPS":<int >=0>}
      (handler model_api.cpp:1052). Success -> {"code":0,"message":"success"}.
      `iFPS` is the NPU inference throttle, NOT the video encoder frame rate.
  Auth: entry.cgi behind nginx trusts 127.0.0.1 (rest_api.cpp:auth_verify top
      level pass-through for HTTP_X_INTERNAL_FROM_LOCALHOST=1), so a plain
      localhost HTTP request needs no JWT. We speak HTTP over TCP to nginx, not
      the gmgr unix socket.

* snapshot()  -- entry.cgi has NO frame-grab endpoint (confirmed). So snapshot
      is implemented as a FRAME PROXY (adapter-bootstrap decision "方案 A"): pull a
      single frame through the kit's own FrameSource (whichever the registry
      selects -- official dma-buf broker or the ffmpeg RTSP workaround), then
      JPEG-encode it with OpenCV. No new frame-connection logic is invented here.

Stdlib only for HTTP (http.client) -- no third-party dependency. cv2 + numpy are
already present for the vision apps and are imported lazily inside snapshot() so
an audio-only venv can still import this module.
"""
from __future__ import annotations

import http.client
import json
import math
import ssl
from typing import Optional

from .official import ControlPlane
from kit.errors import (
    AdapterError,
    CapabilityError,
    ConfigurationError,
    DeviceControlError,
    InputValidationError,
    TransportError,
)
from kit.diagnostics import get_logger, redact_url


log = get_logger("control.cgi")

# entry.cgi is mounted under this nginx location; PATH_INFO is appended.
CGI_BASE = "/cgi-bin/entry.cgi"


def _parse_loopback_redirect(location: str, fallback_target: str):
    """Map a nginx redirect Location to (tls, port, path?query) on loopback.

    Only scheme/port/path are honoured; the host stays self.host (127.0.0.1)
    because entry.cgi skips JWT only for localhost callers.
    """
    from urllib.parse import urlsplit
    u = urlsplit(location)
    tls = (u.scheme or "https") == "https"
    port = u.port or (443 if tls else 80)
    target = (u.path or fallback_target) + (("?" + u.query) if u.query else "")
    return tls, port, target

INFERENCE_PATH = "/model/inference"


class CgiControl(ControlPlane):
    """Control plane backed by the device's existing `entry.cgi` endpoints.

    Signature mirrors the other adapters (accepts and ignores extra kw) so the
    registry can construct it with the same `**kw` it passes everywhere.
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 443,
                 use_tls: bool = True, model_id: int = 0, timeout: float = 10.0,
                 frame_url: Optional[str] = None, verbose: bool = True,
                 **_ignored):
        self.host = str(host).strip()
        if not self.host:
            raise ConfigurationError(
                "entry.cgi host must not be empty",
                operation="control.open",
            )
        try:
            self.port = int(port)
            self.model_id = int(model_id)
            self.timeout = float(timeout)
        except (TypeError, ValueError) as exc:
            raise ConfigurationError(
                "entry.cgi port, model_id, and timeout must be numeric",
                operation="control.open",
                details={"port": repr(port), "model_id": repr(model_id),
                         "timeout": repr(timeout)},
            ) from exc
        if not 1 <= self.port <= 65535:
            raise ConfigurationError(
                "entry.cgi port must be between 1 and 65535",
                operation="control.open",
                details={"port": self.port},
            )
        if self.model_id < 0:
            raise ConfigurationError(
                "entry.cgi model_id must be non-negative",
                operation="control.open",
                details={"model_id": self.model_id},
            )
        if not math.isfinite(self.timeout) or self.timeout <= 0:
            raise ConfigurationError(
                "entry.cgi timeout must be finite and positive",
                operation="control.open",
                details={"timeout": self.timeout},
            )
        # nginx redirects plain HTTP (port 80) to HTTPS with a 307, so entry.cgi
        # is reachable only over TLS (self-signed cert). Default to HTTPS on 443.
        self.use_tls = bool(use_tls)
        # Optional RTSP url override forwarded to the workaround FrameSource;
        # None lets the registry/FrameSource use its own default sub-stream.
        self.frame_url = frame_url
        self.verbose = verbose

    # -- low-level HTTP to entry.cgi (localhost, no JWT) -------------------- #
    def _do_http(self, tls: bool, port: int, method: str, target: str,
                 body: Optional[bytes], headers: dict):
        """One raw request; returns (status, body_bytes, Location-or-None)."""
        if tls:
            # Cert is self-signed and this is a loopback request; skip verify.
            ctx = ssl._create_unverified_context()
            conn = http.client.HTTPSConnection(self.host, port,
                                               timeout=self.timeout,
                                               context=ctx)
        else:
            conn = http.client.HTTPConnection(self.host, port,
                                              timeout=self.timeout)
        try:
            conn.request(method, target, body=body, headers=headers)
            resp = conn.getresponse()
            get_header = getattr(resp, "getheader", None)
            location = get_header("Location") if callable(get_header) else None
            return resp.status, resp.read(), location
        finally:
            conn.close()

    def _request(self, method: str, path: str,
                 body: Optional[bytes] = None) -> dict:
        """Send one HTTP request to entry.cgi and return the parsed JSON dict.

        Raises a typed :class:`TransportError` or
        :class:`DeviceControlError` on transport, protocol, HTTP, or device
        failures.  ``error.operation``, ``error.code``, and ``error.details``
        are safe to include in application diagnostics.
        """
        headers = {"Host": "localhost", "Connection": "close"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        target = CGI_BASE + path
        tls, port = self.use_tls, self.port
        try:
            status, raw, location = self._do_http(tls, port, method, target,
                                                  body, headers)
        except (OSError, ssl.SSLError, http.client.HTTPException) as first_error:
            # TLS side unreachable (cert missing / 443 not listening / handshake
            # failure): fall back to plain :80 once, otherwise re-raise.
            if not tls:
                raise TransportError(
                    f"entry.cgi {method} {path} transport failed: {first_error}",
                    operation="control.http",
                    retryable=True,
                    details={"method": method, "path": path,
                             "host": self.host, "port": port},
                ) from first_error
            try:
                status, raw, location = self._do_http(False, 80, method, target,
                                                      body, headers)
            except (OSError, ssl.SSLError,
                    http.client.HTTPException) as fallback_error:
                log.error("entry.cgi transport failed method=%s path=%s: %s",
                          method, path, fallback_error)
                raise TransportError(
                    f"entry.cgi {method} {path} was unreachable over HTTPS and HTTP",
                    operation="control.http",
                    retryable=True,
                    details={"method": method, "path": path,
                             "host": self.host, "https_port": port,
                             "http_port": 80},
                ) from fallback_error
            tls, port = False, 80
        # Firmware HTTPS toggle (entry.cgi /system/secure sEnable=false): nginx
        # then answers 443 with `307 http://$host$request_uri` and serves
        # entry.cgi on plain :80 (and vice versa when enabled: 80 -> 307 https).
        # http.client never follows redirects, so follow exactly one hop and
        # remember where we landed so later calls go straight there.
        if status in (301, 302, 307, 308) and location:
            tls, port, target = _parse_loopback_redirect(location, target)
            try:
                status, raw, _ = self._do_http(
                    tls, port, method, target, body, headers)
            except (OSError, ssl.SSLError,
                    http.client.HTTPException) as redirect_error:
                log.error(
                    "entry.cgi redirect failed method=%s path=%s: %s",
                    method, path, redirect_error,
                )
                raise TransportError(
                    f"entry.cgi {method} {path} redirect was unreachable",
                    operation="control.http.redirect",
                    retryable=True,
                    details={"method": method, "path": path,
                             "port": port, "tls": tls},
                ) from redirect_error
        if 200 <= status < 300:
            self.use_tls, self.port = tls, port

        if not (200 <= status < 300):
            excerpt = raw[:200].decode("utf-8", "replace")
            log.error("entry.cgi rejected method=%s path=%s status=%d response=%s",
                      method, path, status, excerpt)
            raise TransportError(
                "entry.cgi %s %s -> HTTP %d: %s"
                % (method, path, status, excerpt),
                operation="control.http",
                code="http_error",
                retryable=status >= 500,
                details={"method": method, "path": path, "status": status},
            )

        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception as e:
            excerpt = raw[:200].decode("utf-8", "replace")
            log.error("entry.cgi invalid JSON method=%s path=%s response=%s",
                      method, path, excerpt)
            raise TransportError(
                "entry.cgi %s %s -> non-JSON response (%s): %s"
                % (method, path, e, excerpt),
                operation="control.decode",
                code="invalid_response",
                details={"method": method, "path": path},
            ) from e

        # entry.cgi envelope: {"code":0,"message":"success", ...payload...}.
        if isinstance(data, dict) and data.get("code", 0) != 0:
            log.error("entry.cgi API error method=%s path=%s code=%s message=%s",
                      method, path, data.get("code"), data.get("message"))
            raise DeviceControlError(
                "entry.cgi %s %s -> code=%s message=%s"
                % (method, path, data.get("code"), data.get("message")),
                operation="control.request",
                details={"method": method, "path": path,
                         "native_code": data.get("code"),
                         "message": data.get("message")},
            )
        log.debug("entry.cgi request succeeded method=%s path=%s", method, path)
        return data if isinstance(data, dict) else {"data": data}

    def _inference_query(self) -> str:
        return "%s?id=%d" % (INFERENCE_PATH, self.model_id)

    # -- ControlPlane ABC --------------------------------------------------- #
    def set_inference(self, *, enable: bool, model: Optional[str] = None,
                      fps: Optional[int] = None) -> dict:
        """Enable/disable inference and optionally switch model / set NPU fps.

        Maps to POST /model/inference?id=<model_id> with a JSON body carrying
        only the fields actually supplied (the handler treats each key as an
        independent optional update).
        """
        if not isinstance(enable, bool):
            raise InputValidationError(
                "enable must be bool",
                operation="control.set_inference",
                details={"enable": repr(enable)},
            )
        payload: dict = {"iEnable": 1 if enable else 0}
        if model is not None:
            if not isinstance(model, str) or not model.strip():
                raise InputValidationError(
                    "model must be a non-empty path string",
                    operation="control.set_inference",
                    details={"model": repr(model)},
                )
            payload["sModel"] = model
        if fps is not None:
            if isinstance(fps, bool):
                raise InputValidationError(
                    "fps must be a non-negative integer",
                    operation="control.set_inference",
                    details={"fps": repr(fps)},
                )
            try:
                parsed_fps = int(fps)
            except (TypeError, ValueError) as exc:
                raise InputValidationError(
                    "fps must be a non-negative integer",
                    operation="control.set_inference",
                    details={"fps": repr(fps)},
                ) from exc
            if parsed_fps < 0 or parsed_fps != fps:
                raise InputValidationError(
                    "fps must be a non-negative integer",
                    operation="control.set_inference",
                    details={"fps": repr(fps)},
                )
            payload["iFPS"] = parsed_fps
        body = json.dumps(payload).encode("utf-8")
        return self._request("POST", self._inference_query(), body=body)

    def get_inference(self) -> dict:
        """Read current inference state (helper for verification / callers).

        Returns the handler payload, e.g.
        {"iEnable","sModel","iFPS","iActualFPS","sStatus", ...}.
        """
        return self._request("GET", self._inference_query())

    def snapshot(self) -> bytes:
        """Grab one frame via the kit FrameSource and return JPEG bytes.

        entry.cgi has no frame-grab endpoint, so this proxies a single frame
        through whichever FrameSource the capability registry selects (official
        broker or ffmpeg RTSP workaround), then JPEG-encodes it with OpenCV.
        """
        try:
            import cv2
        except Exception as e:  # pragma: no cover - device has cv2
            raise CapabilityError(
                f"snapshot requires OpenCV (cv2): {e}",
                operation="control.snapshot",
                code="opencv_unavailable",
            ) from e
        try:
            from .registry import select_frame_source
        except Exception as e:
            raise CapabilityError(
                f"snapshot frame-source registry is unavailable: {e}",
                operation="control.snapshot",
                code="frame_source_unavailable",
            ) from e

        url = self.frame_url or _DEFAULT_URL()
        # prefer_rga=False: a one-shot snapshot has no throughput need, and the
        # RGA hardware NV12->RGB path in OfficialFrameSource can fault on some
        # librga builds; the OpenCV convert is correct and safe. The kwarg is
        # ignored by the ffmpeg/snapshot workaround sources (they take **_ignored).
        try:
            src = select_frame_source(url=url, prefer_rga=False)
        except (OSError, ConnectionError, TimeoutError) as exc:
            log.error("snapshot frame source unavailable url=%s: %s",
                      redact_url(url), exc)
            raise TransportError(
                f"snapshot frame source is unavailable: {exc}",
                operation="control.snapshot.open",
                retryable=True,
                details={"url": redact_url(url)},
            ) from exc
        except Exception as exc:
            log.error("snapshot frame source setup failed url=%s: %s",
                      redact_url(url), exc)
            raise AdapterError(
                f"snapshot frame source setup failed: {exc}",
                operation="control.snapshot.open",
                details={"url": redact_url(url)},
            ) from exc

        iterator = None
        frame = None
        try:
            iterator = iter(src.frames())
            try:
                frame = next(iterator)
            except StopIteration as exc:
                raise TransportError(
                    "snapshot frame source ended before producing a frame",
                    operation="control.snapshot.acquire",
                    retryable=True,
                    details={"url": redact_url(url)},
                ) from exc

            # Encode while the source and any borrowed frame lease are still
            # alive.  Closing the source first was harmless for owned ffmpeg
            # frames but invalid for a future zero-copy/broker-backed frame.
            arr = frame.data
            fmt = str(frame.fmt).upper()
            if fmt == "RGB":
                bgr = arr[:, :, ::-1]
            elif fmt == "BGR":
                bgr = arr
            elif fmt in ("GRAY", "GREY", "Y8"):
                bgr = arr
            else:
                raise AdapterError(
                    f"snapshot does not support frame format {frame.fmt!r}",
                    operation="control.snapshot.encode",
                    code="unsupported_format",
                    details={"format": frame.fmt},
                )
            try:
                ok, buf = cv2.imencode(".jpg", bgr)
            except Exception as exc:
                raise AdapterError(
                    f"JPEG encoding failed: {exc}",
                    operation="control.snapshot.encode",
                ) from exc
            if not ok:
                raise AdapterError(
                    "JPEG encoder rejected the frame",
                    operation="control.snapshot.encode",
                    code="encode_failed",
                )
            return bytes(buf.tobytes())
        finally:
            if frame is not None:
                release = getattr(frame, "release", None)
                if callable(release):
                    try:
                        release()
                    except Exception as exc:
                        log.warning("snapshot frame release failed: %s", exc)
            close_iterator = getattr(iterator, "close", None)
            if callable(close_iterator):
                try:
                    close_iterator()
                except Exception as exc:
                    log.warning("snapshot iterator close failed: %s", exc)
            try:
                src.close()
            except Exception as exc:
                log.warning("snapshot frame source close failed: %s", exc)


def _DEFAULT_URL() -> str:
    """Default RTSP sub-stream for the workaround FrameSource (imported lazily
    so this module stays importable without numpy/cv2)."""
    from .frame_source import DEFAULT_SUB_STREAM
    return DEFAULT_SUB_STREAM
