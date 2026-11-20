"""Capture status, start/stop, and the `capture_image` helper (local edition).

Captures land on the local filesystem, so `capture_image` returns the
**path** by default — the agent opens it with its own file/vision tooling.
Base64 is opt-in (`inline=true`) for agents without file tools.
"""

from __future__ import annotations

import base64
import os
import time
from typing import Any, Dict, Optional

from . import _local as http
from ._coerce import to_bool
from ._const import MAX_INLINE_BYTES
from ._errors import RecameraError

__all__ = ["get_capture_status", "start_capture", "stop_capture", "capture_image"]

PATH_STATUS = "/cgi-bin/entry.cgi/record/capture/status"
PATH_START = "/cgi-bin/entry.cgi/record/capture/start"
PATH_STOP = "/cgi-bin/entry.cgi/record/capture/stop"

FORMAT_IMAGE = "JPG"
OUTPUT_FALLBACK = "/mnt/rc_mmcblk0p8/DCIM/100RECAM"
_POLL_INTERVAL_S = 0.25
_DEFAULT_TIMEOUT_S = 5.0
_TERMINAL = {"COMPLETED", "FAILED", "INTERRUPTED", "CANCELED"}


def _parse_event(d: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": d.get("sID", ""),
        "output_directory": d.get("sOutputDirectory", ""),
        "format": d.get("sFormat", ""),
        "video_length_seconds": d.get("iVideoLengthSeconds"),
        "status": d.get("sStatus", "UNKNOWN"),
        "timestamp_unix_ms": int(d.get("iTimestamp", 0) or 0),
        "file_name": d.get("sFileName", ""),
    }


def get_capture_status() -> Dict[str, Any]:
    """Current capture state (includes the last event, if any)."""
    d = http.get_json(PATH_STATUS) or {}
    last = d.get("dLastCapture")
    return {
        "last_capture": _parse_event(last) if isinstance(last, dict) else None,
        "ready_to_start_new": bool(d.get("bReadyToStartNew", False)),
        "stop_requested": bool(d.get("bStopRequested", False)),
    }


def _validate_output_dir(output: Optional[str]) -> Optional[str]:
    """`output` is an absolute directory on the *camera's* filesystem."""
    if output is None:
        return None
    if not isinstance(output, str) or not output.strip():
        raise ValueError("'output' must be a non-empty string.")
    if "\x00" in output:
        raise ValueError("'output' must not contain NUL bytes.")
    if not output.startswith("/"):
        raise ValueError(
            f"'output' must be an absolute on-device directory path; got {output!r}. "
            "Use get_storage_status to find a mount path (e.g. "
            "'/mnt/rc_mmcblk0p8/DCIM/100RECAM'), or omit 'output' to use the "
            "selected storage slot."
        )
    return output


def start_capture(
    *,
    output: Optional[str] = None,
    format: str = FORMAT_IMAGE,
    video_length_seconds: Optional[int] = None,
) -> Dict[str, Any]:
    """Start a capture; returns the initial capture event.

    `output` is an on-device directory. Omit it to auto-resolve from the
    currently selected storage slot. The device picks the file name; the
    resulting file is `event.output_directory + '/' + event.file_name` —
    readable directly once the event reaches a terminal status.
    """
    output = _validate_output_dir(output)
    payload: Dict[str, Any] = {
        "sOutput": output or OUTPUT_FALLBACK,
        "sFormat": str(format).upper(),
    }
    if video_length_seconds is not None:
        payload["iVideoLengthSeconds"] = int(video_length_seconds)
    try:
        resp = http.post_json(PATH_START, payload=payload)
        http.expect_ok(resp, "start capture")
    except RecameraError as exc:
        # Device rejects paths outside a storage mount (error code 30022).
        if exc.code == 30022 or "30022" in str(exc):
            raise RecameraError(
                f"{exc} Hint: 'output' must be an on-device directory under a "
                "mounted storage slot (see get_storage_status for mount_path), "
                "or omit 'output' to use the default.",
                status=exc.status,
                code=exc.code,
                body=exc.body,
            ) from exc
        raise
    capture = resp.get("dCapture")
    if not isinstance(capture, dict):
        raise RecameraError("start_capture response missing dCapture field.")
    return _parse_event(capture)


def stop_capture() -> None:
    """Stop the running capture (no-op for JPG)."""
    resp = http.post_json(PATH_STOP)
    http.expect_ok(resp, "stop capture")


def capture_image(
    *,
    output: Optional[str] = None,
    timeout: float = _DEFAULT_TIMEOUT_S,
    inline: bool = False,
    max_inline_bytes: int = MAX_INLINE_BYTES,
) -> Dict[str, Any]:
    """One-shot JPG: start, wait for completion, return `{event, path, size}`.

    With `inline=true`, also embeds `content_base64` (skipped with a `note`
    when the file exceeds `max_inline_bytes`).
    """
    inline = to_bool(inline, "inline")
    if output is None:
        try:
            from .storage import get_storage_status

            slots = get_storage_status()
            slot = next((s for s in slots if s["enabled"] and s["mount_path"]), None)
            if slot:
                base = slot["mount_path"].rstrip("/")
                data_dir = slot.get("data_dir", "").strip("/")
                output = f"{base}/{data_dir}" if data_dir else base
        except RecameraError:
            output = None
    output = output or OUTPUT_FALLBACK

    capture = start_capture(output=output, format=FORMAT_IMAGE)
    deadline = time.time() + float(timeout)
    final = dict(capture)
    while time.time() < deadline:
        time.sleep(_POLL_INTERVAL_S)
        status = get_capture_status()
        last = status["last_capture"]
        if last and last["id"] == capture["id"] and last["status"] in _TERMINAL:
            final = last
            break
    if final["status"] != "COMPLETED":
        raise RecameraError(f"Capture did not complete (status: {final['status']!r})")

    remote = f"{final['output_directory'].rstrip('/')}/{final['file_name']}"
    try:
        size = os.path.getsize(remote)
    except OSError as exc:
        raise RecameraError(
            f"Capture completed but {remote!r} is not readable locally: {exc}"
        ) from exc

    result: Dict[str, Any] = {"event": final, "path": remote, "size": size}
    if inline:
        if size <= int(max_inline_bytes):
            with open(remote, "rb") as fh:
                result["content_base64"] = base64.b64encode(fh.read()).decode("ascii")
        else:
            result["note"] = (
                f"file is {size} bytes, over the inline budget "
                f"({int(max_inline_bytes)}); open {remote!r} directly instead."
            )
    return result


COMMANDS = {
    "get_capture_status": get_capture_status,
    "start_capture": start_capture,
    "stop_capture": stop_capture,
    "capture_image": capture_image,
}
