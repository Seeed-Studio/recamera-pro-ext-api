"""Device system endpoints: info, resources, time, reboot (`/system/...`)."""

from __future__ import annotations

import time

if __name__ == "__main__" and __package__ is None:
    import os
    import sys

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from recamera_intellisense._cli import main

    raise SystemExit(main())

from typing import Any, Dict, Optional

from . import _config, _http
from ._coerce import require_confirm
from ._errors import RecameraError

__all__ = ["get_device_info", "get_resource_info", "get_system_time",
           "set_system_time", "get_battery_status", "reboot_device"]

PATH_DEVICE_INFO = "/cgi-bin/entry.cgi/system/device-info"
PATH_RESOURCE_INFO = "/cgi-bin/entry.cgi/system/resource-info"
PATH_TIME = "/cgi-bin/entry.cgi/system/time"
PATH_BATTERY = "/cgi-bin/entry.cgi/system/battery"
PATH_REBOOT = "/cgi-bin/entry.cgi/system/reboot"


def get_device_info(device_name: Optional[str] = None) -> Dict[str, Any]:
    """Firmware/hardware identity of the device."""
    dev = _config.resolve(device_name)
    d = _http.get_json(dev, PATH_DEVICE_INFO) or {}
    return {
        "serial_number": d.get("sSerialNumber"),
        "firmware_version": d.get("sFirmwareVersion"),
        "sensor_model": d.get("sSensorModel"),
        "base_plate_model": d.get("sBasePlateModel"),
    }


def _usage_block(d: Dict[str, Any], total: str, used: str, pct: str) -> Dict[str, Any]:
    return {"total_gb": d.get(total), "used_gb": d.get(used), "usage_percent": d.get(pct)}


def get_resource_info(device_name: Optional[str] = None) -> Dict[str, Any]:
    """CPU/NPU/memory/storage utilisation (percentages 0-100)."""
    dev = _config.resolve(device_name)
    d = _http.get_json(dev, PATH_RESOURCE_INFO) or {}
    return {
        "cpu_usage": d.get("iCpuUsage"),
        "npu_usage": d.get("iNpuUsage"),
        "memory": _usage_block(d.get("sMem") or {}, "iMemTotal", "iMemUsed", "iMemUsage"),
        "storage": _usage_block(
            d.get("sStorage") or {}, "iStorageTotal", "iStorageUsed", "iStorageUsage"
        ),
    }


def _raw_time(dev: _config.DeviceRecord) -> Dict[str, Any]:
    return _http.get_json(dev, PATH_TIME) or {}


def get_system_time(device_name: Optional[str] = None) -> Dict[str, Any]:
    """Device clock, timezone, and NTP configuration."""
    dev = _config.resolve(device_name)
    d = _raw_time(dev)
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
    device_name: Optional[str] = None,
    *,
    method: str,
    timezone: Optional[str] = None,
    timestamp: Optional[int] = None,
    ntp_server: Optional[str] = None,
    ntp_port: Optional[int] = None,
) -> Dict[str, Any]:
    """Set the device clock / timezone / NTP config, then read back and verify.

    ``method="ntp"`` applies the NTP server config (keeping the current one
    unless ``ntp_server``/``ntp_port`` are given) and triggers an immediate
    sync on the device. ``method="manual"`` sets the clock to ``timestamp``
    (defaults to this host's current time). ``timezone`` takes an IANA name
    (e.g. ``Asia/Shanghai``); omitted = keep current.

    The firmware reports success even when it silently rejects an unknown
    timezone, so the device is re-read afterwards: a mismatch raises a loud
    error instead of returning a false success.
    """
    method = (method or "").strip().lower()
    if method not in _METHODS:
        raise RecameraError(
            f"Invalid method {method!r}: expected one of {_METHODS}."
        )
    dev = _config.resolve(device_name)
    current = _raw_time(dev)
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
    resp = _http.put_json(dev, PATH_TIME, payload)
    _http.expect_ok(resp, "set system time")

    # Read-back verification — the CGI returns success for a rejected timezone.
    after = _raw_time(dev)
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


def reboot_device(device_name: Optional[str] = None, *, confirm: bool = False) -> None:
    """Reboot the device. Disruptive: all streams, captures, and sessions drop.

    Requires `confirm=True`.
    """
    require_confirm(confirm, "reboot device")
    dev = _config.resolve(device_name)
    resp = _http.post_json(dev, PATH_REBOOT)
    _http.expect_ok(resp, "reboot device")


def get_battery_status(device_name: Optional[str] = None) -> Dict[str, Any]:
    """Battery/power status: ``{attached, charging, display_steps,
    total_steps}``. ``attached`` is false on base plates without a battery."""
    dev = _config.resolve(device_name)
    d = _http.get_json(dev, PATH_BATTERY) or {}
    return {
        "attached": bool(d.get("isAttached", False)),
        "charging": bool(d.get("isCharging", False)),
        "display_steps": int(d.get("displaySteps", 0)),
        "total_steps": int(d.get("totalSteps", 0)),
    }


COMMANDS = {
    "get_device_info": get_device_info,
    "get_resource_info": get_resource_info,
    "get_system_time": get_system_time,
    "set_system_time": set_system_time,
    "get_battery_status": get_battery_status,
    "reboot_device": reboot_device,
}
