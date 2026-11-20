"""Record browsing and file access — local filesystem edition.

The recordings are ordinary files on this device, so there is no relay and
no base64 detour: `list_records` walks the active slot's record directory,
`read_file` reads absolute paths under the allowed roots (default `/mnt`),
`delete_file` removes them (confirm-gated).
"""

from __future__ import annotations

import base64
import mimetypes
import os
from typing import Any, Dict, List, Optional, Union

from . import storage as _storage
from ._coerce import require_confirm
from ._const import IMAGE_EXTENSIONS, MAX_INLINE_BYTES
from ._errors import RecameraError

__all__ = ["list_records", "read_file", "delete_file"]

LIST_RECORDS_DEFAULT_LIMIT = 100
LIST_RECORDS_MAX_LIMIT = 500

# Read/delete roots; overridable for tests (the daemon enforces /mnt remotely).
def _read_roots() -> List[str]:
    raw = os.environ.get("RECAMERA_LOCAL_READ_ROOTS", "/mnt")
    roots = [r.strip() for r in raw.split(":") if r.strip()]
    return roots or ["/mnt"]


def _validate_read_path(path: Any) -> str:
    """Resolve *path* (symlinks included) and require it to stay under a root."""
    if not isinstance(path, str) or not path:
        raise ValueError("'path' must be a non-empty string.")
    if "\x00" in path:
        raise ValueError("'path' must not contain NUL bytes.")
    if not path.startswith("/"):
        raise ValueError(f"'path' must be an absolute (POSIX) path; got {path!r}.")
    segments = [s for s in path.split("/") if s not in ("", ".")]
    if any(s == ".." for s in segments):
        raise ValueError(f"'path' must not contain '..' segments; got {path!r}.")
    real = os.path.realpath(path)
    for root in _read_roots():
        real_root = os.path.realpath(root)
        if real == real_root or real.startswith(real_root.rstrip("/") + "/"):
            return real
    raise ValueError(
        f"'path' must be under {' or '.join(_read_roots())}; got {path!r}."
    )


def list_records(
    *,
    path: str = "",
    dev_path: Optional[str] = None,
    limit: Optional[int] = None,
    offset: Optional[int] = None,
) -> Dict[str, Any]:
    """List the directory at *path* (relative to the record data directory).

    Returns a paginated dict (same shape as the fleet skill)::

        {"entries": [{"name", "is_dir", "size?", "mtime?"}...],
         "offset": int, "limit": int, "total": int, "has_more": bool}

    Directories sort first, then entries by name for stable pagination.
    `limit` defaults to 100, clamped to [1, 500]. `mtime` is Unix seconds.
    """
    root = _storage.record_data_root(dev_path)
    rel = (path or "").strip("/")
    if rel:
        segments = [s for s in rel.split("/") if s not in ("", ".")]
        if any(s == ".." for s in segments) or "\x00" in rel:
            raise ValueError(f"'path' must not contain '..' or NUL; got {path!r}.")
        target = os.path.join(root, *segments)
    else:
        target = root
    # Defeat symlink escapes out of the record root.
    real_target = os.path.realpath(target)
    real_root = os.path.realpath(root)
    if real_target != real_root and not real_target.startswith(real_root.rstrip("/") + "/"):
        raise ValueError(f"'path' escapes the record data directory; got {path!r}.")
    if not os.path.isdir(real_target):
        raise RecameraError(f"Record directory not found: {real_target}")

    entries: List[Dict[str, Any]] = []
    with os.scandir(real_target) as it:
        for entry in it:
            try:
                st = entry.stat(follow_symlinks=True)
            except OSError:
                continue
            item: Dict[str, Any] = {"name": entry.name, "is_dir": entry.is_dir()}
            if not item["is_dir"]:
                item["size"] = int(st.st_size)
            item["mtime"] = int(st.st_mtime)
            entries.append(item)

    entries.sort(key=lambda e: (0 if e.get("is_dir") else 1, str(e.get("name", ""))))
    total = len(entries)
    off = max(0, int(offset) if offset is not None else 0)
    off = min(off, total)
    lim = int(limit) if limit is not None else LIST_RECORDS_DEFAULT_LIMIT
    lim = max(1, min(lim, LIST_RECORDS_MAX_LIMIT))
    end = min(off + lim, total)
    return {
        "entries": entries[off:end],
        "offset": off,
        "limit": lim,
        "total": total,
        "has_more": end < total,
        "root": real_target,
    }


def read_file(
    *,
    path: str,
    max_inline_bytes: int = MAX_INLINE_BYTES,
    raw: bool = False,
) -> Union[bytes, Dict[str, Any]]:
    """Read a local file under the allowed roots (default `/mnt`).

    Images and payloads ≤ `max_inline_bytes` come back inline as base64;
    larger payloads return metadata plus a note. `raw=True` (Python API only)
    returns raw `bytes` for pipelines.
    """
    real = _validate_read_path(path)
    if not os.path.isfile(real):
        raise RecameraError(f"Not a file (or missing): {path!r}")
    if raw:
        with open(real, "rb") as fh:
            return fh.read()
    ct = mimetypes.guess_type(real)[0] or "application/octet-stream"
    is_image = any(real.lower().endswith(ext) for ext in IMAGE_EXTENSIONS)
    # Stat before reading: clips can be tens of MiB; never slurp to say "too big".
    size = os.path.getsize(real)
    if is_image or size <= int(max_inline_bytes):
        with open(real, "rb") as fh:
            body = fh.read()
        return {
            "path": real,
            "content_type": ct,
            "size": len(body),
            "content_base64": base64.b64encode(body).decode("ascii"),
        }
    return {
        "path": real,
        "content_type": ct,
        "size": size,
        "note": "payload exceeds the inline budget; open the file directly "
        "(it is local) or re-read with a higher max_inline_bytes.",
    }


def delete_file(*, path: str, confirm: bool = False) -> None:
    """Delete a local file under the allowed roots. Destructive: `confirm=True` required."""
    real = _validate_read_path(path)
    require_confirm(confirm, f"delete file {path!r}")
    if not os.path.isfile(real):
        raise RecameraError(f"Not a file (or missing): {path!r}")
    try:
        os.remove(real)
    except OSError as exc:
        raise RecameraError(f"Failed to delete {path!r}: {exc}") from exc


COMMANDS = {
    "list_records": list_records,
    "read_file": read_file,
    "delete_file": delete_file,
}
