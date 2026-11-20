"""System endpoints + the composite `get_status` snapshot (`/system/...`)."""

from __future__ import annotations

import time
from typing import Any, Dict, Optional

from . import _local as http
from ._coerce import require_confirm
from ._errors import RecameraError

__all__ = [
    "get_device_info",
    "get_resource_info",
    "get_system_time",
    "get_battery_status",
    "reboot_device",
    "get_status",
]

PATH_DEVICE_INFO = "/cgi-bin/entry.cgi/system/device-info"
PATH_RESOURCE_INFO = "/cgi-bin/entry.cgi/system/resource-info"
PATH_TIME = "/cgi-bin/entry.cgi/system/time"
PATH_BATTERY = "/cgi-bin/entry.cgi/system/battery"
PATH_REBOOT = "/cgi-bin/entry.cgi/system/reboot"


def get_device_info() -> Dict[str, Any]:
    """Firmware/hardware identity of the device."""
    d = http.get_json(PATH_DEVICE_INFO) or {}
    return {
        "serial_number": d.get("sSerialNumber"),
        "firmware_version": d.get("sFirmwareVersion"),
        "sensor_model": d.get("sSensorModel"),
        "base_plate_model": d.get("sBasePlateModel"),
    }


def _usage_block(d: Dict[str, Any], total: str, used: str, pct: str) -> Dict[str, Any]:
    return {"total_gb": d.get(total), "used_gb": d.get(used), "usage_percent": d.get(pct)}


def get_resource_info() -> Dict[str, Any]:
    """CPU/NPU/memory/storage utilisation (percentages 0-100)."""
    d = http.get_json(PATH_RESOURCE_INFO) or {}
    return {
        "cpu_usage": d.get("iCpuUsage"),
        "npu_usage": d.get("iNpuUsage"),
        "memory": _usage_block(d.get("sMem") or {}, "iMemTotal", "iMemUsed", "iMemUsage"),
        "storage": _usage_block(
            d.get("sStorage") or {}, "iStorageTotal", "iStorageUsed", "iStorageUsage"
        ),
    }


def _raw_time() -> Dict[str, Any]:
    return http.get_json(PATH_TIME) or {}


def get_system_time() -> Dict[str, Any]:
    """Device clock, timezone, and NTP configuration."""
    d = _raw_time()
    ntp = d.get("dNtpConfig") or {}
    return {
        "method": d.get("sMethod"),
        "timestamp": d.get("iTimestamp"),
        "timezone": d.get("sTimezone"),
        "tz": d.get("sTz"),
        "ntp": {"address": ntp.get("sAddress"), "port": ntp.get("sPort")},
    }


_METHODS = ("ntp", "manual")


def _validate_timezone_name(tz: str) -> str:
    tz = tz.strip()
    if (
        not tz
        or len(tz) > 64
        or not tz.isascii()
        or any(c.isspace() for c in tz)
        or ".." in tz
        or not tz.replace("_", "").replace("-", "").replace("/", "").isalnum()
    ):
        raise RecameraError(
            f"Invalid timezone {tz!r}: expected an IANA name like 'Asia/Shanghai'."
        )
    return tz


def set_system_time(
    *,
    method: str,
    timezone: Optional[str] = None,
    timestamp: Optional[int] = None,
    ntp_server: Optional[str] = None,
    ntp_port: Optional[int] = None,
) -> Dict[str, Any]:
    """Set the clock / timezone / NTP config, then read back and verify.

    ``method="ntp"`` applies the NTP server config (keeping the current one
    unless ``ntp_server``/``ntp_port`` are given) and triggers an immediate
    sync. ``method="manual"`` sets the clock to ``timestamp`` (defaults to
    the current time). ``timezone`` takes an IANA name (e.g.
    ``Asia/Shanghai``); omitted = keep current.

    The firmware reports success even when it silently rejects an unknown
    timezone, so the device is re-read afterwards and a mismatch raises a
    loud error instead of returning a false success.
    """
    method = (method or "").strip().lower()
    if method not in _METHODS:
        raise RecameraError(f"Invalid method {method!r}: expected one of {_METHODS}.")
    current = _raw_time()
    cur_ntp = current.get("dNtpConfig") or {}

    tz = _validate_timezone_name(timezone) if timezone else current.get("sTimezone")
    if not tz:
        raise RecameraError("No timezone configured on the device; pass timezone=.")
    if method == "manual":
        ts = int(timestamp) if timestamp is not None else int(time.time())
        if ts <= 0:
            raise RecameraError(f"Invalid timestamp {ts}: expected unix seconds > 0.")
        ntp_cfg = None
    else:
        ts = int(timestamp) if timestamp is not None else int(time.time())
        address = (ntp_server or "").strip() or cur_ntp.get("sAddress") or "pool.ntp.org"
        port = str(ntp_port if ntp_port is not None else (cur_ntp.get("sPort") or "123"))
        ntp_cfg = {"sAddress": address, "sPort": port}

    payload = {
        "sMethod": method,
        "dNtpConfig": ntp_cfg,
        "iTimestamp": ts,
        "sTimezone": tz,
    }
    resp = http.put_json(PATH_TIME, payload)
    http.expect_ok(resp, "set system time")

    # Read-back verification — the CGI returns success for a rejected timezone.
    after = _raw_time()
    problems = []
    if timezone and after.get("sTimezone") != tz:
        problems.append(
            f"device kept timezone {after.get('sTimezone')!r} instead of {tz!r} "
            "(unknown or unsupported IANA name — pick one from the Web Console list)"
        )
    if method == "manual":
        drift = abs(int(after.get("iTimestamp") or 0) - ts)
        if drift > 5:
            problems.append(
                f"device clock is {drift}s away from the requested timestamp"
            )
    if problems:
        raise RecameraError(
            "set_system_time was applied but verification failed: "
            + "; ".join(problems)
        )
    ntp = after.get("dNtpConfig") or {}
    return {
        "method": after.get("sMethod"),
        "timestamp": after.get("iTimestamp"),
        "timezone": after.get("sTimezone"),
        "tz": after.get("sTz"),
        "ntp": {"address": ntp.get("sAddress"), "port": ntp.get("sPort")},
        "verified": True,
    }


def get_battery_status() -> Dict[str, Any]:
    """Battery/power status: ``{attached, charging, display_steps,
    total_steps}``. ``attached`` is false on base plates without a battery."""
    d = http.get_json(PATH_BATTERY) or {}
    return {
        "attached": bool(d.get("isAttached", False)),
        "charging": bool(d.get("isCharging", False)),
        "display_steps": int(d.get("displaySteps", 0)),
        "total_steps": int(d.get("totalSteps", 0)),
    }


def reboot_device(*, confirm: bool = False) -> None:
    """Reboot the device. Disruptive: all streams, captures, and sessions drop.

    Requires `confirm=True`.
    """
    require_confirm(confirm, "reboot device")
    resp = http.post_json(PATH_REBOOT)
    http.expect_ok(resp, "reboot device")


def get_status() -> Dict[str, Any]:
    """One-call snapshot: identity, load, storage, capture, rule pipeline, model.

    The cheap "where do I stand" first call — replaces six round-trips of
    individual getters. Individual getters stay available for drilling down.
    """
    from . import capture as _capture
    from . import detect as _detect
    from . import storage as _storage
    from . import trigger as _trigger

    slots = [
        {
            "dev_path": s["dev_path"],
            "mount_path": s["mount_path"],
            "enabled": s["enabled"],
            "selected": s["selected"],
            "state": s["state"],
            "free_bytes": s["free_bytes"],
            "size_bytes": s["size_bytes"],
            "quota_rotate": s["quota_rotate"],
        }
        for s in _storage.get_storage_status()
    ]
    record_config = _trigger.get_record_config()
    try:
        active_trigger = _trigger.get_record_trigger()
    except (ValueError, RecameraError):
        active_trigger = None
    try:
        detection_model = _detect.get_detection_model()
    except RecameraError:
        detection_model = None
    try:
        battery = get_battery_status()
    except RecameraError:
        battery = None  # base plates without a battery may not serve this
    return {
        "device": get_device_info(),
        "resources": get_resource_info(),
        "battery": battery,
        "storage": slots,
        "capture": _capture.get_capture_status(),
        "record": {**record_config, "trigger": active_trigger},
        "detection_model": detection_model,
    }


COMMANDS = {
    "get_device_info": get_device_info,
    "get_resource_info": get_resource_info,
    "get_system_time": get_system_time,
    "set_system_time": set_system_time,
    "get_battery_status": get_battery_status,
    "reboot_device": reboot_device,
    "get_status": get_status,
}
