"""Read-only, bounded readiness checks for the resources a launch actually uses.

Extension probes exchange only Hello/HelloAck from sdk/proto/ext_api.proto.
They never subscribe to frames/probes, acquire the NPU, or load a model.  Keep
this stdlib-only: appmgr does not require a native SDK or protobuf Python wheel
merely to diagnose a missing/incompatible firmware endpoint.
"""
from __future__ import annotations

import errno
import http.client
import io
import json
import math
import socket
import ssl
import time
from urllib.parse import urlsplit

from . import paths


class ProtocolError(ValueError):
    pass


def _timeout(value: float) -> float:
    value = float(value)
    if not math.isfinite(value) or value <= 0:
        raise ValueError("dependency probe timeout must be positive and finite")
    return min(value, 2.0)


def _varint(data: bytes, offset: int) -> tuple[int, int]:
    value = 0
    for shift in range(0, 70, 7):
        if offset >= len(data):
            raise ProtocolError("truncated protobuf varint")
        byte = data[offset]
        offset += 1
        if shift == 63 and byte > 1:
            raise ProtocolError("protobuf varint overflow")
        value |= (byte & 127) << shift
        if byte < 128:
            return value, offset
    raise ProtocolError("protobuf varint overflow")


def _fields(data: bytes):
    """Read the bounded proto3 handshake, skipping unknown scalar fields."""
    offset = 0
    while offset < len(data):
        tag, offset = _varint(data, offset)
        number, wire = tag >> 3, tag & 7
        if not 0 < number < (1 << 29):
            raise ProtocolError("invalid protobuf field number")
        if wire == 0:
            value, offset = _varint(data, offset)
        else:
            if wire == 2:
                size, offset = _varint(data, offset)
            elif wire in (1, 5):
                size = 8 if wire == 1 else 4
            else:
                raise ProtocolError("invalid protobuf wire type")
            end = offset + size
            if end > len(data):
                raise ProtocolError("truncated protobuf field")
            value, offset = data[offset:end], end
        yield number, wire, value


def _message(data: bytes, expected: dict) -> dict:
    result = {}
    for number, wire, value in _fields(data):
        if number in expected:
            if wire != expected[number]:
                raise ProtocolError("invalid handshake field type")
            result.setdefault(number, []).append(value)
    return result


def _last(fields: dict, number: int, default=None):
    return fields.get(number, [default])[-1]


def _failure(path: str, error, *, retryable: bool, **extra) -> dict:
    return {"available": False, "socket": path, "error": str(error)[:512],
            "retryable": retryable, **extra}


def probe_extension(path: str, capability: str, timeout: float = 0.5) -> dict:
    """Validate one native endpoint without entering its data/ownership plane."""
    deadline = time.monotonic() + _timeout(timeout)
    conn = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    try:
        def remaining():
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError("extension handshake timed out")
            conn.settimeout(left)

        remaining()
        conn.connect(path)
        # Hello { version_min: 1, version_max: 1, client_name: ... }.
        name = b"appmgr-readiness"
        hello = b"\x08\x01\x10\x01\x1a" + bytes([len(name)]) + name
        remaining()
        if conn.send(hello) != len(hello):
            raise ConnectionError("incomplete extension Hello")
        remaining()
        raw, _ancillary, flags, _address = conn.recvmsg(4096, 0)
        if not raw:
            raise ConnectionError("extension closed during HelloAck")
        if flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC):
            raise ProtocolError("oversized HelloAck or unexpected descriptors")
        ack = _message(raw, {1: 0, 2: 0, 3: 2, 4: 2, 5: 0, 6: 2})
        error = _last(ack, 5, 0)
        if error:
            detail = _last(ack, 6, b"handshake rejected").decode("utf-8")
            return _failure(path, detail, retryable=error in (3, 5, 6, 7),
                            capability=capability, code=error)
        if _last(ack, 1) != 1:
            raise ProtocolError("unsupported extension API version")
        if _last(ack, 3) != b"peercred":
            raise ProtocolError("unsupported extension authentication mode")
        supported = False
        for raw_cap in ack.get(4, []):
            cap = _message(raw_cap, {1: 2, 2: 0})
            if _last(cap, 1, b"").decode("utf-8") == capability:
                supported = supported or _last(cap, 2) == 1
        if not supported:
            raise ProtocolError("missing capability %s@1" % capability)
        return {"available": True, "socket": path, "capability": capability}
    except (ValueError, PermissionError) as exc:
        return _failure(path, exc, retryable=False, capability=capability)
    except OSError as exc:
        transient = (exc.errno is None or exc.errno in {
            errno.ENOENT, errno.ECONNREFUSED, errno.ECONNRESET, errno.EPIPE,
            errno.EAGAIN, errno.EINTR, errno.ETIMEDOUT, errno.ENOBUFS,
        })
        return _failure(path, exc, retryable=transient, capability=capability,
                        errno=exc.errno)
    finally:
        conn.close()


_CGI_PATH = "/cgi-bin/entry.cgi/model/inference?id=0"


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("CGI readiness deadline exceeded")
    return remaining


class _DeadlineReader(io.RawIOBase):
    def __init__(self, sock, deadline):
        super().__init__()
        self.sock, self.deadline = sock, deadline
        self.reader = sock.makefile("rb", buffering=0)

    def readable(self):
        return True

    def readinto(self, buffer):
        self.sock.settimeout(_remaining(self.deadline))
        return self.reader.readinto(buffer)

    def close(self):
        try:
            self.reader.close()
        finally:
            super().close()


class _DeadlineSocket:
    """Apply the same deadline to every header/body read, including trickles."""
    def __init__(self, sock, deadline):
        self.sock, self.deadline = sock, deadline

    def __getattr__(self, name):
        return getattr(self.sock, name)

    def sendall(self, data):
        self.sock.settimeout(_remaining(self.deadline))
        self.sock.sendall(data)

    def makefile(self, mode):
        if mode != "rb":
            raise ValueError("CGI probe only reads HTTP responses")
        return io.BufferedReader(_DeadlineReader(self.sock, self.deadline))


class _DeadlineConnection(http.client.HTTPConnection):
    def __init__(self, port, tls, timeout):
        super().__init__("127.0.0.1", port, timeout=timeout)
        self.deadline = time.monotonic() + timeout
        self.tls = tls

    def connect(self):
        sock = socket.create_connection(
            (self.host, self.port), timeout=_remaining(self.deadline))
        try:
            if self.tls:
                # Match builtin.py's self-signed, strictly loopback transport.
                context = ssl._create_unverified_context()
                sock = context.wrap_socket(
                    sock, server_hostname=self.host, do_handshake_on_connect=False)
                sock.settimeout(_remaining(self.deadline))
                sock.do_handshake()
            self.sock = _DeadlineSocket(sock, self.deadline)
        except BaseException:
            sock.close()
            raise


def _cgi_get(tls: bool, port: int, target: str, timeout: float):
    """GET only; redirects are interpreted by the caller and stay on loopback."""
    conn = _DeadlineConnection(port, tls, timeout)
    try:
        conn.request("GET", target, headers={"Host": "localhost",
                                            "Connection": "close"})
        with conn.getresponse() as response:
            raw = response.read(65537)
            if len(raw) > 65536:
                raise ProtocolError("oversized CGI readiness response")
            return response.status, raw, response.getheader("Location")
    finally:
        conn.close()


def probe_legacy_cgi(timeout: float = 0.5) -> dict:
    """Prove old firmware can answer a read before its existing stop barrier.

    This is not a stopped-state proof. The launch callback must still perform
    builtin.stop and its strict readback before creating any direct RKNN owner.
    """
    deadline = time.monotonic() + _timeout(timeout)
    endpoint = "loopback:" + _CGI_PATH
    tls, port, target = True, 443, _CGI_PATH
    redirected = False
    for _ in range(3):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return _failure(endpoint, "CGI readiness timed out", retryable=True)
        try:
            status, raw, location = _cgi_get(
                tls, port, target, min(remaining, _timeout(timeout) / 3))
        except OSError as exc:
            if tls and not redirected:
                tls, port = False, 80
                continue
            return _failure(endpoint, exc, retryable=not isinstance(exc, PermissionError))
        except http.client.HTTPException as exc:
            return _failure(endpoint, exc,
                            retryable=isinstance(exc, http.client.IncompleteRead))
        except ValueError as exc:
            return _failure(endpoint, exc, retryable=False)
        if status in (301, 302, 307, 308) and location and not redirected:
            try:
                url = urlsplit(location)
                if url.scheme not in ("", "http", "https"):
                    raise ProtocolError("unsupported CGI redirect scheme")
                tls = (url.scheme == "https") if url.scheme else tls
                port = url.port or (443 if tls else 80)
                target = (url.path or _CGI_PATH.split("?")[0])
                if url.query:
                    target += "?" + url.query
            except ValueError as exc:
                return _failure(endpoint, exc, retryable=False)
            redirected = True
            continue
        if not 200 <= status < 300:
            return _failure(endpoint, "CGI readiness HTTP %d" % status,
                            retryable=status in (408, 429, 500, 502, 503, 504))
        try:
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ProtocolError("CGI readiness response is not an object")
            if data.get("code", 0) != 0:
                code = data.get("code")
                return _failure(endpoint, "CGI readiness code=%s" % code,
                                retryable=code not in (401, 403))
            # Same mandatory status fields as builtin._stop_state. Merely
            # accepting HTML, a generic code=0 envelope, or a login page would
            # turn nginx readiness into a false claim about rkipc readiness.
            if (type(data.get("iEnable")) is not int
                    or data["iEnable"] not in (0, 1)
                    or type(data.get("iActualFPS")) not in (int, float)
                    or not math.isfinite(data["iActualFPS"])
                    or data["iActualFPS"] < 0
                    or not isinstance(data.get("sStatus"), str)
                    or not data["sStatus"]):
                raise ProtocolError("CGI lacks valid inference status fields")
        except (ValueError, TypeError) as exc:
            return _failure(endpoint, exc, retryable=False)
        return {"available": True, "socket": endpoint,
                "legacy_compatibility": True}
    return _failure(endpoint, "CGI redirect limit exceeded", retryable=False)


def probe_plan(plan, timeout: float = 0.5) -> dict:
    """Probe only IPC-backed resources from the effective launch plan.

    audio.capture alone is not an IPC declaration: the deployed default uses
    ALSA ai_asr, and CPU-only audio must not wait for an optional audio.sock.
    The canonical camera-0 claim selects frame.sock in supervisor.py; other
    legacy stream names retain their existing adapter selection semantics.
    """
    names = {request.resource for request in plan.requests}
    endpoints = []
    if "camera.frame:camera-0" in names:
        endpoints.append(("/run/recamera/frame.sock", "frame"))
    if "probe.read" in names:
        endpoints.append(("/run/recamera/probe.sock", "probe"))
    if "result.ingress" in names:
        endpoints.append(("/run/recamera/result-in.sock", "result"))
    if plan.npu_mode == "legacy-direct":
        endpoints.append((paths.INFERENCE_CONTROL_SOCK, "inference-control"))
    for path, capability in endpoints:
        result = probe_extension(path, capability, timeout)
        if (capability == "inference-control"
                and result.get("errno") == errno.ENOENT):
            result = probe_legacy_cgi(timeout)
        if not result.get("available"):
            return result
    return {"available": True}
