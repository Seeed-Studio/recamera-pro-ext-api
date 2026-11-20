"""Local endpoint resolution + keep-alive HTTP session for the on-camera API.

The agent runs *on the reCamera itself*, so there is no device registry:

* host   — ``$RECAMERA_HOST`` (default ``127.0.0.1``)
* port   — ``$RECAMERA_PORT`` / protocol — ``$RECAMERA_PROTOCOL``; when unset,
  auto-detected by probing ``http://host:80`` then ``https://host:443``
  (the device's HTTPS cert is self-signed; loopback traffic never leaves the
  device, so verification is disabled for the 443 probe)
* token  — ``$RECAMERA_TOKEN``, else the on-device daemon key file
  (``/userdata/config/system/http_key.json``), else empty

All requests share one keep-alive connection **per thread** (``http.client``
connections are not thread-safe); a dropped idle connection is transparently
re-established once per request.
"""

from __future__ import annotations

import http.client
import json
import os
import socket
import ssl
import threading
import urllib.parse
from typing import Any, Mapping, Optional, Tuple

from ._errors import RecameraError

DEFAULT_TIMEOUT = 10.0
PROBE_TIMEOUT = 2.0
PROBE_PATH = "/api/v1/recamera-generate-204"

ENV_HOST = "RECAMERA_HOST"
ENV_PORT = "RECAMERA_PORT"
ENV_PROTOCOL = "RECAMERA_PROTOCOL"
ENV_TOKEN = "RECAMERA_TOKEN"

LOCAL_TOKEN_FILES = ("/userdata/config/system/http_key.json",)

_DEFAULT_HOST = "127.0.0.1"
_CANDIDATES = (("http", 80), ("https", 443))

# Loopback HTTPS uses self-signed certs; traffic never leaves the device.
_INSECURE_SSL = ssl.create_default_context()
_INSECURE_SSL.check_hostname = False
_INSECURE_SSL.verify_mode = ssl.CERT_NONE


def _read_local_token() -> str:
    """Best-effort read of the on-device daemon token (agent runs as root)."""
    for path in LOCAL_TOKEN_FILES:
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            continue
        key = data.get("key") if isinstance(data, dict) else None
        if isinstance(key, str) and key.strip():
            return key.strip()
    return ""


class Endpoint:
    """Resolved local API endpoint (one per process)."""

    __slots__ = ("host", "port", "protocol", "token")

    def __init__(self, protocol: str, host: str, port: int, token: str) -> None:
        self.protocol = protocol
        self.host = host
        self.port = port
        self.token = token

    @property
    def origin(self) -> str:
        return f"{self.protocol}://{self.host}:{self.port}"

    def __repr__(self) -> str:  # never includes the token
        return f"Endpoint({self.origin})"


def _probe(
    protocol: str, host: str, port: int, token: str, timeout: float
) -> Optional[Tuple[str, int]]:
    """Return the effective (protocol, port) when a reCamera API answers on
    (protocol, host, port), following same-host redirects (the device nginx
    307s :80 -> :443). ``None`` when unreachable.

    401/403 also count as reachable — the point is finding the service, not
    authenticating (auth problems surface with a clear error on real calls).
    """
    cur = (protocol, host, port)
    for _ in range(3):  # redirect hops
        scheme, h, p = cur
        conn: http.client.HTTPConnection
        try:
            if scheme == "https":
                conn = http.client.HTTPSConnection(
                    h, p, timeout=timeout, context=_INSECURE_SSL
                )
            else:
                conn = http.client.HTTPConnection(h, p, timeout=timeout)
            headers = {"Authorization": f"Bearer {token}"} if token else {}
            try:
                conn.request("GET", PROBE_PATH, headers=headers)
                resp = conn.getresponse()
                status = resp.status
                location = resp.getheader("Location")
                resp.read()
            finally:
                conn.close()
        except (OSError, http.client.HTTPException):
            return None
        if status in (301, 302, 303, 307, 308) and location:
            scheme2, host2, port2, _ = _split(urllib.parse.urljoin(
                f"{scheme}://{h}:{p}{PROBE_PATH}", location))
            if host2.lower() != host.lower():
                return None  # never follow a redirect off the probe host
            cur = (scheme2, host2, port2)
            continue
        if status < 500:  # 2xx reachable; 401/403 reachable-but-auth
            return (scheme, p)
        return None
    return None


_resolved: Optional[Endpoint] = None


def resolve() -> Endpoint:
    """Resolve (once per process) the local endpoint; raise if unreachable."""
    global _resolved
    if _resolved is not None:
        return _resolved

    host = (os.environ.get(ENV_HOST) or _DEFAULT_HOST).strip()
    token = (os.environ.get(ENV_TOKEN) or "").strip() or _read_local_token()

    protocol_env = (os.environ.get(ENV_PROTOCOL) or "").strip().lower()
    port_env = (os.environ.get(ENV_PORT) or "").strip()
    if protocol_env or port_env:
        protocol = protocol_env or ("https" if port_env == "443" else "http")
        if protocol not in ("http", "https"):
            raise RecameraError(
                f"Invalid {ENV_PROTOCOL}: {protocol_env!r} (expected 'http' or 'https')."
            )
        try:
            port = int(port_env) if port_env else (443 if protocol == "https" else 80)
        except ValueError:
            raise RecameraError(f"Invalid {ENV_PORT}: {port_env!r}.") from None
        candidates = ((protocol, port),)
    else:
        candidates = tuple((p, port) for p, port in _CANDIDATES)

    for protocol, port in candidates:
        effective = _probe(protocol, host, port, token, PROBE_TIMEOUT)
        if effective is not None:
            eff_protocol, eff_port = effective
            _resolved = Endpoint(eff_protocol, host, eff_port, token)
            return _resolved

    tried = ", ".join(f"{p}://{host}:{port}" for p, port in candidates)
    raise RecameraError(
        f"Local reCamera API not reachable (tried {tried}). "
        "This skill must run on the reCamera itself with the intellisense "
        f"firmware. Override with {ENV_HOST}/{ENV_PORT}/{ENV_PROTOCOL}/"
        f"{ENV_TOKEN} if the setup is unusual."
    )


def reset() -> None:
    """Forget the resolved endpoint and drop this thread's connection (tests)."""
    global _resolved
    _resolved = None
    _drop_session()


# http.client connections are not thread-safe — pool per thread.
_tls = threading.local()


def _new_connection(ep: Endpoint) -> http.client.HTTPConnection:
    if ep.protocol == "https":
        return http.client.HTTPSConnection(
            ep.host, ep.port, timeout=DEFAULT_TIMEOUT, context=_INSECURE_SSL
        )
    return http.client.HTTPConnection(ep.host, ep.port, timeout=DEFAULT_TIMEOUT)


def _apply_timeout(conn: http.client.HTTPConnection, timeout: float) -> None:
    """Apply a per-request timeout to a connection.

    ``conn.timeout`` is only consulted when (re)connecting; an already-open
    pooled socket keeps the timeout from its creation, so retime the live
    socket explicitly.
    """
    conn.timeout = timeout
    if conn.sock is not None:
        conn.sock.settimeout(timeout)


def _session(ep: Endpoint) -> http.client.HTTPConnection:
    conn = getattr(_tls, "conn", None)
    if conn is None or getattr(_tls, "origin", None) != ep.origin:
        _drop_session()
        conn = _new_connection(ep)
        _tls.conn = conn
        _tls.origin = ep.origin
    return conn


def _drop_session() -> None:
    conn = getattr(_tls, "conn", None)
    if conn is not None:
        try:
            conn.close()
        finally:
            _tls.conn = None
            _tls.origin = None


def _auth_headers(ep: Endpoint) -> dict:
    # Explicit Bearer: CGI strips the prefix; appmgr's origin guard requires it.
    return {"Authorization": f"Bearer {ep.token}"} if ep.token else {}


def _raise_for_http(status: int, reason: str, body: bytes, what: str) -> RecameraError:
    body_text = body.decode("utf-8", errors="replace") if body else ""
    detail = f": {body_text.strip()}" if body_text.strip() else ""
    hint = ""
    if status in (401, 403):
        hint = (
            " Check the bearer token ($RECAMERA_TOKEN or "
            f"{LOCAL_TOKEN_FILES[0]})."
        )
    return RecameraError(
        f"{what} failed: HTTP {status} {reason}{detail}{hint}",
        status=status,
        body=body_text,
    )


def _split(url: str) -> Tuple[str, str, int, str]:
    u = urllib.parse.urlsplit(url)
    scheme = u.scheme or "http"
    host = u.hostname or ""
    port = u.port or (443 if scheme == "https" else 80)
    path = u.path or "/"
    if u.query:
        path = f"{path}?{u.query}"
    return scheme, host, port, path


def _request(
    endpoint: str,
    *,
    method: str,
    params: Optional[Mapping[str, Any]] = None,
    body: Optional[bytes] = None,
    content_type: Optional[str] = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> Tuple[bytes, str]:
    """One HTTP round-trip against the local endpoint (keep-alive).

    Follows redirects only when the target stays on the resolved host —
    including the device's ``:80 -> :443`` upgrade, which is served over the
    same unverified loopback channel. One transparent reconnect+retry when the
    pooled connection went stale.
    """
    ep = resolve()
    path = endpoint
    if params:
        cleaned = [(k, str(v)) for k, v in params.items() if v is not None]
        if cleaned:
            path = f"{path}?{urllib.parse.urlencode(cleaned)}"

    headers = _auth_headers(ep)
    if content_type is not None:
        headers["Content-Type"] = content_type

    cur_method, cur_body, cur_path = method, body, path
    cur_ep = ep
    conn: Optional[http.client.HTTPConnection] = None
    max_hops = 3
    for hop in range(max_hops + 1):
        own_conn = False
        if cur_ep is ep:
            conn = _session(ep)
            try:
                _apply_timeout(conn, timeout)
            except OSError:
                _drop_session()  # dead pooled socket: rebuild once
                conn = _session(ep)
                _apply_timeout(conn, timeout)
        else:
            # Redirect to same host but new scheme/port (e.g. 80 -> 443): unpooled.
            conn = _new_connection(cur_ep)
            _apply_timeout(conn, timeout)
            own_conn = True
        try:
            send_headers = dict(headers)
            if cur_body is not None:
                send_headers.setdefault("Content-Length", str(len(cur_body)))
            conn.request(cur_method, cur_path, body=cur_body, headers=send_headers)
            resp = conn.getresponse()
            status, reason = resp.status, resp.reason
            if status in (301, 302, 303, 307, 308) and hop < max_hops:
                location = resp.getheader("Location")
                resp.read()
                if not location:
                    raise RecameraError(f"{method} {endpoint}: redirect without Location")
                scheme, host, port, next_path = _split(urllib.parse.urljoin(cur_ep.origin + cur_path, location))
                if host.lower() != ep.host.lower():
                    raise RecameraError(
                        f"{method} {endpoint}: refused redirect to foreign host "
                        f"{host!r} (local skill only talks to {ep.host})."
                    )
                cur_ep = Endpoint(scheme, host, port, ep.token)
                cur_path = next_path
                if status == 303:
                    cur_method, cur_body = "GET", None
                    headers.pop("Content-Type", None)
                continue
            data = resp.read()
            ct = resp.getheader("Content-Type", "") or ""
            if 200 <= status < 300:
                return data, ct
            raise _raise_for_http(status, reason, data, f"{method} {endpoint}")
        except (http.client.BadStatusLine, http.client.RemoteDisconnected,
                BrokenPipeError, ConnectionResetError) as exc:
            if own_conn:
                raise RecameraError(f"{method} {endpoint} failed: {exc}") from exc
            _drop_session()  # stale pooled connection: rebuild and retry once
            try:
                conn = _session(ep)
                _apply_timeout(conn, timeout)
                conn.request(cur_method, cur_path, body=cur_body, headers=send_headers)
                resp = conn.getresponse()
                data = resp.read()
                ct = resp.getheader("Content-Type", "") or ""
                if 200 <= resp.status < 300:
                    return data, ct
                raise _raise_for_http(resp.status, resp.reason, data, f"{method} {endpoint}")
            except RecameraError:
                _drop_session()
                raise
            except (OSError, http.client.HTTPException) as exc2:
                _drop_session()
                raise RecameraError(f"{method} {endpoint} failed: {exc2}") from exc2
        except (socket.timeout, TimeoutError) as exc:
            # A timed-out request leaves the conn state undefined; never repool it.
            if not own_conn:
                _drop_session()
            raise RecameraError(
                f"{method} {endpoint} timed out after {timeout:.0f}s"
            ) from exc
        except OSError as exc:
            if not own_conn:
                _drop_session()
            raise RecameraError(f"{method} {endpoint} failed: {exc}") from exc
        finally:
            if own_conn and conn is not None:
                conn.close()
    raise RecameraError(f"{method} {endpoint} exceeded redirect limit ({max_hops})")


def get_json(
    endpoint: str,
    params: Optional[Mapping[str, Any]] = None,
    *,
    timeout: float = DEFAULT_TIMEOUT,
) -> Any:
    data, _ = _request(endpoint, method="GET", params=params, timeout=timeout)
    parsed = _parse_json(data, f"GET {endpoint}")
    expect_ok(parsed, f"GET {endpoint}")
    return parsed


def get_bytes(
    endpoint: str,
    params: Optional[Mapping[str, Any]] = None,
    *,
    timeout: float = DEFAULT_TIMEOUT,
) -> Tuple[bytes, str]:
    """Binary GET; returns ``(body, content_type)``."""
    return _request(endpoint, method="GET", params=params, timeout=timeout)


def _send_json(
    endpoint: str,
    *,
    method: str,
    params: Optional[Mapping[str, Any]] = None,
    payload: Any = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> Any:
    body = None
    ct = None
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        ct = "application/json"
    data, _ = _request(
        endpoint, method=method, params=params, body=body, content_type=ct,
        timeout=timeout,
    )
    if not data:
        return {}
    return _parse_json(data, f"{method} {endpoint}")


def post_json(
    endpoint: str,
    params: Optional[Mapping[str, Any]] = None,
    payload: Any = None,
    *,
    timeout: float = DEFAULT_TIMEOUT,
) -> Any:
    return _send_json(endpoint, method="POST", params=params, payload=payload, timeout=timeout)


def put_json(endpoint: str, payload: Any = None, *, timeout: float = DEFAULT_TIMEOUT) -> Any:
    return _send_json(endpoint, method="PUT", payload=payload, timeout=timeout)


def post_text(endpoint: str, body: str, *, timeout: float = DEFAULT_TIMEOUT) -> None:
    _request(
        endpoint, method="POST", body=body.encode("utf-8"),
        content_type="text/plain", timeout=timeout,
    )


def delete(
    endpoint: str,
    params: Optional[Mapping[str, Any]] = None,
    *,
    timeout: float = DEFAULT_TIMEOUT,
) -> None:
    _request(endpoint, method="DELETE", params=params, timeout=timeout)


def expect_ok(resp: Any, context: str) -> None:
    """Enforce the `code == 0` contract for Record API POSTs."""
    if not isinstance(resp, dict):
        return
    code = resp.get("code")
    if code is None or code == 0:
        return
    msg = resp.get("message") or "Unknown error"
    raise RecameraError(f"{context} failed (code={code}): {msg}", code=int(code))


def _parse_json(data: bytes, ctx: str) -> Any:
    if not data:
        return None
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RecameraError(f"{ctx}: invalid JSON response") from exc
