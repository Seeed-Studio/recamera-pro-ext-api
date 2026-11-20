"""Storage slots and quota (`/record/storage/{status,config}`), local edition.

Destructive batch tasks (FORMAT/FREE_UP/...) are intentionally not exposed by
the local skill — a realtime agent manages space with `records.delete_file`
and quota rotation instead.
"""

from __future__ import annotations

import sys
import time
from typing import Any, Dict, List, Optional

from . import _local as http
from ._coerce import to_bool

__all__ = [
    "get_storage_status",
    "set_storage_slot",
    "configure_storage_quota",
    "ensure_storage",
    "DEFAULT_INTERNAL_DEV_PATH",
]

PATH_STATUS = "/cgi-bin/entry.cgi/record/storage/status"
PATH_CONFIG = "/cgi-bin/entry.cgi/record/storage/config"
PATH_CONTROL = "/cgi-bin/entry.cgi/record/storage/control"

DEFAULT_INTERNAL_DEV_PATH = "/dev/mmcblk0p8"


def _parse_slot(s: Dict[str, Any], data_dir: str) -> Dict[str, Any]:
    def _s(k: str) -> str:
        return str(s.get(k, ""))

    def _b(k: str) -> bool:
        return bool(s.get(k, False))

    def _i(k: str) -> int:
        return int(s.get(k, 0) or 0)

    return {
        "dev_path": _s("sDevPath"),
        "mount_path": _s("sMountPath"),
        "removable": _b("bRemovable"),
        "internal": _b("bInternal"),
        "label": s.get("sLabel"),
        "uuid": s.get("sUUID"),
        "fs_type": s.get("sType"),
        "selected": _b("bSelected"),
        "enabled": _b("bEnabled"),
        "syncing": _b("bSyncing"),
        "writing": _b("bWriting"),
        "rotating": _b("bRotating"),
        "state_code": _i("eState"),
        "state": _s("sState"),
        "size_bytes": _i("iStatsSizeBytes"),
        "free_bytes": _i("iStatsFreeBytes"),
        "quota_min_recommend_bytes": _i("iQuotaMinimumRecommendBytes"),
        "quota_preserved_bytes": _i("iQuotaPreservedBytes"),
        "quota_used_bytes": s.get("iQuotaUsedBytes"),
        "quota_limit_bytes": _i("iQuotaLimitBytes"),
        "quota_rotate": _b("bQuotaRotate"),
        "data_dir": data_dir,
    }


def get_storage_status() -> List[Dict[str, Any]]:
    """List the storage slots (each as a dict)."""
    data = http.get_json(PATH_STATUS) or {}
    data_dir = data.get("sDataDirName", "") or ""
    slots = data.get("lSlots") or []
    return [_parse_slot(s, data_dir) for s in slots if isinstance(s, dict)]


def active_slot(dev_path: Optional[str] = None) -> Dict[str, Any]:
    """The slot recordings/captures go to: *dev_path* match, else the selected
    or first enabled slot. Raises when no slot is usable."""
    slots = get_storage_status()
    if dev_path:
        match = next((s for s in slots if s["dev_path"] == dev_path), None)
        if match is None:
            known = ", ".join(s["dev_path"] for s in slots) or "none"
            raise ValueError(f"Storage slot {dev_path!r} not found (known: {known}).")
        return match
    match = next((s for s in slots if s["enabled"] and s["selected"]), None)
    if match is None:
        match = next((s for s in slots if s["enabled"]), None)
    if match is None:
        raise ValueError(
            "No storage slot is enabled. Call set_detection_rules (auto-enables "
            "internal storage) or set_storage_slot first."
        )
    return match


def record_data_root(dev_path: Optional[str] = None) -> str:
    """Absolute on-device directory the active slot stores recordings in."""
    slot = active_slot(dev_path)
    base = slot["mount_path"].rstrip("/")
    data_dir = (slot.get("data_dir") or "").strip("/")
    return f"{base}/{data_dir}" if data_dir else base


def set_storage_slot(*, by_dev_path: str = "", by_uuid: str = "") -> None:
    """Select the slot to enable; pass both empty to disable all slots."""
    if by_dev_path or by_uuid:
        select: Any = {"sByDevPath": by_dev_path, "sByUUID": by_uuid}
    else:
        select = None
    resp = http.post_json(PATH_CONFIG, payload={"dSelectSlotToEnable": select})
    http.expect_ok(resp, "set storage selection")


def configure_storage_quota(
    *,
    dev_path: str,
    quota_limit_bytes: int,
    quota_rotate: bool = True,
) -> None:
    """Set per-slot quota. `quota_limit_bytes = -1` means no limit."""
    payload = {
        "sTask": "SYNC",
        "sAction": "CONFIG",
        "sSlotDevPath": dev_path,
        "dSlotConfig": {
            "iQuotaLimitBytes": int(quota_limit_bytes),
            "bQuotaRotate": to_bool(quota_rotate, "quota_rotate"),
        },
    }
    resp = http.post_json(PATH_CONTROL, payload=payload)
    http.expect_ok(resp, "configure storage quota")


def ensure_storage(*, timeout_s: float = 3.0) -> None:
    """Ensure one slot is enabled with rotate-quota on (mirrors the fleet SDK)."""
    slots = get_storage_status()
    if not any(s["enabled"] for s in slots):
        default = next(
            (s for s in slots if s["dev_path"] == DEFAULT_INTERNAL_DEV_PATH), None
        )
        if default is None:
            raise ValueError(
                f"Default storage '{DEFAULT_INTERNAL_DEV_PATH}' not found; "
                "call set_storage_slot to pick one."
            )
        set_storage_slot(by_dev_path=default["dev_path"])
        print(
            f"note: auto-enabled internal storage slot {default['dev_path']} "
            "with quota rotation",
            file=sys.stderr,
        )
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            current = get_storage_status()
            match = next(
                (s for s in current if s["dev_path"] == default["dev_path"]), None
            )
            if (
                match
                and match["enabled"]
                and match.get("state") in ("IDLE", "WRITING", "READY")
            ):
                break
            time.sleep(0.25)
        configure_storage_quota(
            dev_path=default["dev_path"],
            quota_limit_bytes=-1,
            quota_rotate=True,
        )
        return
    for s in slots:
        if s["enabled"] and not s["quota_rotate"]:
            configure_storage_quota(
                dev_path=s["dev_path"],
                quota_limit_bytes=s["quota_limit_bytes"],
                quota_rotate=True,
            )
            print(
                f"note: enabled quota rotation on {s['dev_path']}",
                file=sys.stderr,
            )


COMMANDS = {
    "get_storage_status": get_storage_status,
    "set_storage_slot": set_storage_slot,
    "configure_storage_quota": configure_storage_quota,
}
