"""Whole-device configuration backup (``/config/export``): the device stages a
tarball and returns a URL; we download it to a local file. Read-only with
respect to the device."""

from __future__ import annotations

import os
from typing import Any, Dict

from . import _local as http
from ._errors import RecameraError

__all__ = ["export_device_config"]

PATH_EXPORT = "/cgi-bin/entry.cgi/config/export"

_DOWNLOAD_TIMEOUT = 120.0  # tarball can be tens of MiB


def export_device_config(*, output: str) -> Dict[str, Any]:
    """Download the full device configuration tarball to the local `output` path.

    Returns ``{output, size_bytes}``. Snapshot the device before making
    configuration changes, so a known-good state can be restored by a human
    from the Web Console. Refuses to overwrite an existing file — pick a
    fresh name per snapshot.
    """
    if not output or not isinstance(output, str):
        raise ValueError("output (local file path) is required")
    out_path = os.path.abspath(os.path.expanduser(output))
    if os.path.exists(out_path):
        raise ValueError(f"output already exists (refusing to overwrite): {out_path}")
    meta = http.get_json(PATH_EXPORT)
    if not isinstance(meta, dict) or not meta.get("url"):
        raise RecameraError(f"config export failed: unexpected payload {meta!r}")
    url = str(meta["url"])
    body, _ = http.get_bytes(url, timeout=_DOWNLOAD_TIMEOUT)
    with open(out_path, "wb") as fh:
        fh.write(body)
    return {"output": out_path, "size_bytes": len(body)}


COMMANDS = {"export_device_config": export_device_config}
