"""
builtin.py -- system adapter for the firmware's built-in inference pipeline.

The firmware's built-in detection pipeline is NOT an appmgr-supervised process: it runs
inside the shipped firmware (rkipc + entry.cgi). This module drives it through
the endpoints the firmware already exposes over nginx + entry.cgi.  Dedicated
system surfaces and the v1 App Center system card can activate/configure it and
consume its recording/results. It is neither an installable package nor an
appmgr-supervised application process; legacy package lists remain unchanged.

Endpoints (localhost 443, self-signed, no JWT -- mirrors kit/adapters/cgi_control.py):
  * GET/POST /cgi-bin/entry.cgi/model/inference
        GET  -> {iEnable,iFPS,iActualFPS,sModel,sStatus}
        POST body {iEnable?,sModel?,iFPS?} (each field independent/optional)
  * GET/POST /cgi-bin/entry.cgi/model/info?File-name=<model>
        GET  -> {algorithm,category,classes[],metrics:{confidence,iou,max_obj},...}
        POST wants the FULL info object back (validated field-by-field), so
        writes are read-modify-write: GET, overlay metrics, POST the whole thing.

Apply semantics: the firmware snapshots every threshold/model/fps at model LOAD
time and never re-reads per frame, so EVERY builtin config item is apply:"restart"
(verified 2026-08-13, DESIGN §6). A /model/info write alone does NOT reload the
model; set_config therefore uses the explicit /model/inference-restart endpoint
after threshold-only changes while enabled. A no-op /model/inference POST does
not reload the model on current firmware.

stdlib only (http.client) -- appmgr must not import the kit package.
"""
from __future__ import annotations

import http.client
import json
import math
import os
import ssl
import time
from typing import Any, Dict, Optional

from . import config as appconfig

# --------------------------------------------------------------------------- #
# constants
# --------------------------------------------------------------------------- #
BUILTIN_ID = "builtin"

_HOST = "127.0.0.1"
_PORT = 443
_TIMEOUT = 10.0
_CGI_BASE = "/cgi-bin/entry.cgi"
_INFERENCE = "/model/inference"
_INFERENCE_RESTART = "/model/inference-restart"
_MODEL_INFO = "/model/info"
_MODEL_ID = 0

# POST /model/inference only queues ``rc_model_infer_restart`` in rkipc.  The
# NPU thread releases the RKNN/RGA objects asynchronously, so a successful HTTP
# response is not a resource hand-off barrier.  External inference must wait for
# the observable terminal state before it is allowed to load its own RKNN
# context.  Environment overrides keep device tuning and deterministic tests
# possible without changing the public appmgr API.
STOP_TIMEOUT = float(os.environ.get("APPMGR_BUILTIN_STOP_TIMEOUT", "15"))
STOP_POLL_INTERVAL = float(os.environ.get("APPMGR_BUILTIN_STOP_POLL", "0.1"))

# The firmware default model (current shipped detector). Used as the fallback
# File-name for /model/info reads before /model/inference has reported sModel.
_DEFAULT_MODEL = "yolov5.rknn"


class BuiltinError(Exception):
    pass


# --------------------------------------------------------------------------- #
# low-level HTTP to entry.cgi (localhost, no JWT)
# --------------------------------------------------------------------------- #
def _do_http(tls: bool, host: str, port: int, method: str, target: str,
             body: Optional[bytes], headers: dict, timeout: float = _TIMEOUT):
    """One raw request; returns (status, body_bytes, Location-or-None)."""
    if tls:
        ctx = ssl._create_unverified_context()   # self-signed loopback cert
        conn = http.client.HTTPSConnection(host, port, timeout=timeout, context=ctx)
    else:
        conn = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        conn.request(method, target, body=body, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        return resp.status, raw, resp.getheader("Location")
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _parse_loopback_redirect(location: str, fallback_target: str):
    """Map a nginx redirect Location to (tls, port, path?query) on loopback.

    Only scheme/port/path are honoured; the host is always 127.0.0.1 because
    entry.cgi is trusted (no JWT) only from localhost.
    """
    from urllib.parse import urlsplit
    u = urlsplit(location)
    tls = (u.scheme or "https") == "https"
    port = u.port or (443 if tls else 80)
    target = (u.path or fallback_target) + (("?" + u.query) if u.query else "")
    return tls, port, target


# Endpoint actually serving entry.cgi, learned at runtime: (tls, port).
# Starts at HTTPS 443 (firmware default). The firmware's HTTPS toggle
# (entry.cgi /system/secure, sEnable=false) makes nginx answer 443 with
# `307 http://$host$request_uri` and serve entry.cgi on plain :80 (and the
# reverse when re-enabled: 80 -> 307 https). We follow one hop and remember
# where we landed so subsequent calls go straight there.
_endpoint = [True, _PORT]


def _request(method: str, path: str, body: Optional[bytes] = None, *,
             timeout: Optional[float] = None) -> dict:
    """One HTTP request to entry.cgi -> parsed JSON dict.

    Raises BuiltinError on transport failure, non-2xx, non-JSON, or a JSON
    envelope with a non-zero `code`.
    """
    headers = {"Host": "localhost", "Connection": "close"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    target = _CGI_BASE + path
    tls, port = _endpoint
    request_options = {} if timeout is None else {"timeout": timeout}
    try:
        try:
            status, raw, location = _do_http(tls, _HOST, port, method, target,
                                             body, headers, **request_options)
        except (OSError, ssl.SSLError) as e:
            # TLS side unreachable (cert missing / 443 not listening / handshake
            # failure): fall back to plain :80 once, otherwise re-raise.
            if not tls:
                raise
            status, raw, location = _do_http(False, _HOST, 80, method, target,
                                             body, headers, **request_options)
            tls, port = False, 80
        # http.client never follows redirects; follow exactly one hop (loopback
        # only, so the scheme carries no trust semantics).
        if status in (301, 302, 307, 308) and location:
            tls, port, target = _parse_loopback_redirect(location, target)
            status, raw, _ = _do_http(tls, _HOST, port, method, target,
                                      body, headers, **request_options)
        if 200 <= status < 300:
            _endpoint[0], _endpoint[1] = tls, port
    except (OSError, ssl.SSLError, http.client.HTTPException) as e:
        raise BuiltinError("entry.cgi %s %s -> transport error: %s"
                           % (method, path, e))

    if not (200 <= status < 300):
        raise BuiltinError("entry.cgi %s %s -> HTTP %d: %s"
                           % (method, path, status,
                              raw[:200].decode("utf-8", "replace")))
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception as e:
        raise BuiltinError("entry.cgi %s %s -> non-JSON (%s): %s"
                           % (method, path, e,
                              raw[:200].decode("utf-8", "replace")))
    if isinstance(data, dict) and data.get("code", 0) != 0:
        raise BuiltinError("entry.cgi %s %s -> code=%s message=%s"
                           % (method, path, data.get("code"), data.get("message")))
    return data if isinstance(data, dict) else {"data": data}


def _inference_q() -> str:
    return "%s?id=%d" % (_INFERENCE, _MODEL_ID)


def _info_q(model: str) -> str:
    # File-name must be a query parameter (both GET and POST reject a body-only
    # File-name with HTTP 400 "Missing File-name parameter").
    from urllib.parse import quote
    return "%s?File-name=%s" % (_MODEL_INFO, quote(str(model)))


# --------------------------------------------------------------------------- #
# inference endpoint
# --------------------------------------------------------------------------- #
def get_inference(*, timeout: Optional[float] = None) -> dict:
    if timeout is None:
        return _request("GET", _inference_q())
    return _request("GET", _inference_q(), timeout=timeout)


def inference_status() -> dict:
    """Observe the firmware engine, independently of its persisted enable bit.

    The list's read timeout is deliberately shorter than mutation/barrier calls.
    Old firmware does not report the external lease hold; absence is unknown,
    never evidence that the broker is blocking the engine.
    """
    def number(value):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        return value if math.isfinite(value) and value >= 0 else None

    result = {"available": False, "enabled": None, "state": "unknown",
              "model": None, "fps": None, "actual_fps": None,
              "external_hold": None, "reason": None}
    try:
        data = get_inference(timeout=1.0)
        enabled = data.get("iEnable")
        if enabled not in (0, 1) or isinstance(enabled, str):
            raise BuiltinError("inference status has no valid iEnable")
        engine_state = data.get("sStatus")
        if engine_state not in ("starting", "running", "stopping", "stopped", "error"):
            raise BuiltinError("inference engine state is unavailable")
        hold = data.get("bExternalHold")
        hold = hold if isinstance(hold, bool) else None
        result.update({
            "available": True, "enabled": bool(enabled), "state": engine_state,
            "model": data.get("sModel") if isinstance(data.get("sModel"), str) else None,
            "fps": number(data.get("iFPS")), "actual_fps": number(data.get("iActualFPS")),
            "external_hold": hold,
        })
        if enabled and hold and engine_state == "stopped":
            result["reason"] = "external_npu_hold"
        elif enabled and engine_state == "stopped":
            result["reason"] = "enabled_but_stopped"
        elif engine_state == "error":
            result["reason"] = "inference_error"
    except BuiltinError as exc:
        result["reason"] = str(exc)
    return result


def set_inference(enable: Optional[bool] = None, model: Optional[str] = None,
                  fps: Optional[int] = None) -> dict:
    """POST /model/inference with only the supplied fields (each independent)."""
    payload: Dict[str, Any] = {}
    if enable is not None:
        payload["iEnable"] = 1 if enable else 0
    if model is not None:
        payload["sModel"] = str(model)
    if fps is not None:
        payload["iFPS"] = int(fps)
    body = json.dumps(payload).encode("utf-8")
    return _request("POST", _inference_q(), body=body)


def is_running() -> bool:
    try:
        return int(get_inference().get("iEnable", 0)) == 1
    except BuiltinError:
        return False


def current_model() -> str:
    try:
        return get_inference().get("sModel") or _DEFAULT_MODEL
    except BuiltinError:
        return _DEFAULT_MODEL


def start() -> dict:
    """Enable built-in inference, keeping the firmware's persisted model/fps."""
    return set_inference(enable=True)


def restart_inference() -> dict:
    """Reload the configured engine without changing enable or broker hold."""
    return _request("POST", "%s?id=%d" % (_INFERENCE_RESTART, _MODEL_ID))


def _stop_state(data: dict) -> tuple:
    """Return ``(confirmed, detail)`` for one inference-status response.

    The current firmware exposes no RKNN-handle count or completion token.  The
    strongest available, conservative observation is therefore all three of:

      * persisted enable is zero;
      * rkipc's model state is exactly ``stopped`` (set only after deinit);
      * measured inference FPS is zero.

    Missing/malformed fields are deliberately *not* treated as stopped.  That is
    fail-closed: older/incomplete firmware may prevent an external app starting,
    but it cannot make appmgr overlap two RKNN owners on an assumption.
    """
    missing = [k for k in ("iEnable", "sStatus", "iActualFPS") if k not in data]
    if missing:
        return False, "missing fields: %s" % ",".join(missing)
    try:
        enabled = int(data["iEnable"])
        actual_fps = int(data["iActualFPS"])
    except (TypeError, ValueError):
        return False, "non-integer iEnable/iActualFPS"
    status = str(data.get("sStatus", "")).strip().lower()
    confirmed = enabled == 0 and status == "stopped" and actual_fps == 0
    return confirmed, "iEnable=%d sStatus=%s iActualFPS=%d" % (
        enabled, status or "<empty>", actual_fps)


def wait_stopped(timeout: Optional[float] = None,
                 poll_interval: Optional[float] = None) -> dict:
    """Wait until rkipc reports that built-in RKNN teardown completed.

    Raises :class:`BuiltinError` on timeout, including the last complete status
    (or transport error) in the message.  Callers must propagate that failure
    and must not start an external RKNN process.
    """
    timeout = STOP_TIMEOUT if timeout is None else max(0.0, float(timeout))
    poll_interval = (STOP_POLL_INTERVAL if poll_interval is None
                     else max(0.0, float(poll_interval)))
    started = time.monotonic()
    deadline = started + timeout
    attempts = 0
    last_status = None
    last_detail = "no status response"
    last_logged = None

    while True:
        attempts += 1
        try:
            status = get_inference()
            last_status = status
            confirmed, last_detail = _stop_state(status)
            if confirmed:
                elapsed = time.monotonic() - started
                print("[appmgr] builtin stop confirmed after %.3fs (%d probes): %s"
                      % (elapsed, attempts, last_detail), flush=True)
                return status
        except BuiltinError as e:
            last_detail = "status probe failed: %s" % e

        # Log state transitions, not every 100 ms poll, so the failure is
        # diagnosable without flooding /var/log/appmgr.log.
        if last_detail != last_logged:
            print("[appmgr] waiting for builtin teardown: %s" % last_detail,
                  flush=True)
            last_logged = last_detail

        now = time.monotonic()
        if now >= deadline:
            elapsed = now - started
            rendered = (json.dumps(last_status, sort_keys=True)
                        if last_status is not None else "<unavailable>")
            raise BuiltinError(
                "built-in inference stop was not confirmed within %.3fs "
                "(%d probes); fail-closed, external RKNN was not started; "
                "last=%s; detail=%s" %
                (elapsed, attempts, rendered, last_detail))
        time.sleep(min(poll_interval, max(0.0, deadline - now)))


def stop(timeout: Optional[float] = None,
         poll_interval: Optional[float] = None) -> dict:
    """Disable built-in inference and wait for its RKNN teardown barrier.

    The POST acknowledgement alone is insufficient: it only queues a restart in
    rkipc.  A lost POST response is tolerated if the subsequent readback proves
    the pipeline is stopped; otherwise this method raises and callers fail
    closed.
    """
    print("[appmgr] requesting builtin inference stop", flush=True)
    response = None
    post_error = None
    try:
        response = set_inference(enable=False)
    except BuiltinError as e:
        # The request may have reached entry.cgi even when its response was lost.
        # Readback is authoritative for this transition.
        post_error = e
        print("[appmgr] builtin stop POST failed; verifying readback: %s" % e,
              flush=True)

    try:
        status = wait_stopped(timeout=timeout, poll_interval=poll_interval)
    except BuiltinError as e:
        if post_error is not None:
            raise BuiltinError("builtin disable request failed (%s); %s" %
                               (post_error, e)) from e
        raise

    result = dict(response) if isinstance(response, dict) else {}
    result["stop_confirmed"] = True
    result["confirmed_status"] = status
    if post_error is not None:
        result["warning"] = "disable response lost; stopped state confirmed by readback"
    return result


# --------------------------------------------------------------------------- #
# model_info endpoint (metrics: confidence / iou / max_obj)
# --------------------------------------------------------------------------- #
def get_model_info(model: str) -> dict:
    return _request("GET", _info_q(model))


def set_model_metrics(model: str, updates: Dict[str, Any]) -> dict:
    """Read-modify-write /model/info metrics for `model`.

    The handler validates the WHOLE info object (category/algorithm/... all
    required), so we GET the current object, overlay only the changed metrics,
    and POST it back. Returns the POST envelope.
    """
    info = get_model_info(model)
    metrics = dict(info.get("metrics") or {})
    for k, v in updates.items():
        metrics[k] = v
    info["metrics"] = metrics
    body = json.dumps(info).encode("utf-8")
    return _request("POST", _info_q(model), body=body)


# --------------------------------------------------------------------------- #
# internal compatibility descriptor (bundled with appmgr, never downloaded)
# --------------------------------------------------------------------------- #
def manifest() -> dict:
    """Describe builtin config and system-source presentation metadata.

    This is not an installable application manifest. ``type: builtin`` preserves
    the existing internal
    config/recording compatibility contract; every config item carries a
    bind{endpoint,field} + apply:"restart" (DESIGN §1.2, verified §6).
    """
    return {
        "id": BUILTIN_ID,
        "name": "AI Model Inference",
        "name_zh": "AI模型推理",
        "type": "builtin",
        "scene": "system",
        "scene_zh": "系统",
        "version": "firmware",
        "image": "/appcenter/apps/builtin.png",
        "author": "reCamera (firmware)",
        "description": "The firmware's built-in object detection (rkipc + NPU). "
                       "Managed through entry.cgi -- enable/disable, switch model, "
                       "set NPU fps and detection thresholds.",
        "description_zh": "固件内建目标检测（rkipc + NPU）。经 entry.cgi 管理："
                          "开关、切换模型、设置 NPU 帧率与检测阈值。",
        "config_schema": {
            "groups": [
                {
                    "key": "inference",
                    "title": "Inference",
                    "title_zh": "推理",
                    "items": [
                        {
                            "key": "model", "type": "string", "apply": "restart",
                            "title": "Model file", "title_zh": "模型文件",
                            "default": _DEFAULT_MODEL,
                            "bind": {"endpoint": "inference", "field": "sModel"},
                        },
                        {
                            "key": "fps", "type": "number", "apply": "restart",
                            "title": "NPU inference FPS", "title_zh": "NPU 推理帧率",
                            "min": 0, "max": 30, "step": 1, "default": 20,
                            "bind": {"endpoint": "inference", "field": "iFPS"},
                        },
                    ],
                },
                {
                    "key": "detection",
                    "title": "Detection",
                    "title_zh": "检测",
                    "items": [
                        {
                            "key": "confidence", "type": "number", "apply": "restart",
                            "title": "Confidence threshold", "title_zh": "置信度阈值",
                            "min": 0.05, "max": 0.95, "step": 0.05, "default": 0.25,
                            "bind": {"endpoint": "model_info", "field": "confidence"},
                        },
                        {
                            "key": "iou", "type": "number", "apply": "restart",
                            "title": "NMS IoU threshold", "title_zh": "NMS IoU 阈值",
                            "min": 0.1, "max": 0.9, "step": 0.05, "default": 0.45,
                            "bind": {"endpoint": "model_info", "field": "iou"},
                        },
                        {
                            "key": "max_obj", "type": "number", "apply": "restart",
                            "title": "Max objects", "title_zh": "最大目标数",
                            "min": 1, "max": 200, "step": 1, "default": 100,
                            "bind": {"endpoint": "model_info", "field": "max_obj"},
                        },
                    ],
                },
            ]
        },
    }


# --------------------------------------------------------------------------- #
# config get/set (reverse-assembled from the endpoints; app-isomorphic shape)
# --------------------------------------------------------------------------- #
def _binds() -> Dict[str, dict]:
    """{config key -> bind dict} from the synthesized schema."""
    out = {}
    for k, spec in appconfig.schema_specs(manifest()).items():
        if isinstance(spec.get("bind"), dict):
            out[k] = spec["bind"]
    return out


def get_config() -> dict:
    """Return {id, config_schema, values, defaults} -- IDENTICAL shape to a
    self-hosted app's GET /api/appMgr/config, so the frontend needs zero
    branching. `values` are read live off the endpoints via each item's bind.
    """
    man = manifest()
    defaults = appconfig.schema_defaults(man)
    values = dict(defaults)   # start from defaults, overlay live reads

    inf = {}
    try:
        inf = get_inference()
    except BuiltinError:
        inf = {}
    model = inf.get("sModel") or defaults.get("model") or _DEFAULT_MODEL

    info_metrics = {}
    try:
        info_metrics = (get_model_info(model).get("metrics") or {})
    except BuiltinError:
        info_metrics = {}

    for key, bind in _binds().items():
        ep = bind.get("endpoint")
        field = bind.get("field")
        if ep == "inference":
            if field in inf:
                values[key] = inf[field]
        elif ep == "model_info":
            if field in info_metrics:
                values[key] = info_metrics[field]

    return {
        "id": BUILTIN_ID,
        "config_schema": man.get("config_schema") or {},
        "values": values,
        "defaults": defaults,
    }


def set_config(incoming: dict) -> dict:
    """Validate + apply a config change by dispatching each item to its bound
    endpoint. Every builtin item is apply:"restart"; a model_info change does not
    reload the model on its own. Threshold-only changes use the explicit restart
    endpoint, since the inference endpoint deliberately ignores no-op writes.
    Neither endpoint releases the NPU broker's external hold."""
    man = manifest()
    clean, errors = appconfig.validate_config(man, incoming)
    if errors:
        raise ValueError("; ".join(errors))
    if not clean:
        return {"id": BUILTIN_ID, "saved": True, "applied": "restart",
                "restarted": False, "config": {}}

    binds = _binds()
    inf_updates: Dict[str, Any] = {}   # {firmware field -> value} for /model/inference
    metric_updates: Dict[str, Any] = {}   # {metric field -> value} for /model/info

    for key, val in clean.items():
        bind = binds.get(key) or {}
        ep = bind.get("endpoint")
        field = bind.get("field")
        if ep == "inference":
            inf_updates[field] = val
        elif ep == "model_info":
            metric_updates[field] = val

    # Read once and fail before any write if the current intent is unavailable.
    # is_running() is a legacy best-effort rollback hint and must not be used to
    # infer a disabled state here after a transport failure.
    current = get_inference()
    if current.get("iEnable") not in (0, 1):
        raise BuiltinError("inference status has no valid iEnable")
    enabled = bool(current["iEnable"])
    # Target model: an incoming model change wins, else the currently loaded one.
    target_model = inf_updates.get("sModel") or current.get("sModel")
    if not target_model:
        raise BuiltinError("inference status has no configured model")
    changed_inference = {key: value for key, value in inf_updates.items()
                         if value != current.get(key)}

    # 1) metrics: read-modify-write /model/info for the target model.
    if metric_updates:
        set_model_metrics(target_model, metric_updates)

    # A changed model/fps already reloads in firmware. Do not double-reload or
    # write iEnable back from our snapshot (another system client may change it).
    if changed_inference:
        set_inference(
            model=changed_inference.get("sModel"),
            fps=changed_inference.get("iFPS"),
        )
    elif metric_updates and enabled:
        restart_inference()

    return {"id": BUILTIN_ID, "saved": True, "applied": "restart",
            "restarted": bool((changed_inference or metric_updates) and enabled),
            "config": clean}
