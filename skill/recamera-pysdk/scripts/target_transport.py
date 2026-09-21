"""SSH transport shared by read-only diagnostics (no shell interpolation)."""
import json
from pathlib import Path
import re
import subprocess

from remote_inventory import redact


def fetch_inventory(host, port=22, timeout=12, app_id=None):
    if not host or host.startswith("-") or any(c.isspace() or ord(c) < 32 for c in host):
        raise ValueError("invalid SSH destination")
    if not 1 <= port <= 65535 or not 0 < timeout <= 300:
        raise ValueError("port must be 1..65535 and total timeout 0..300 seconds")
    if app_id is not None and re.fullmatch(r"[a-z0-9-]{1,64}", app_id) is None:
        raise ValueError("invalid app ID")
    script = Path(__file__).with_name("remote_inventory.py").read_text()
    script += "\nprint(json.dumps(collect(" + repr(app_id) + "), ensure_ascii=True))\n"
    command = ["ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={max(1, int(timeout))}",
               "-p", str(port), "--", host, "python3 -"]
    try:
        result = subprocess.run(command, input=script, text=True, capture_output=True,
                                encoding="utf-8", errors="replace", check=False, timeout=timeout)
    except subprocess.TimeoutExpired:
        # Do not expose partial output (it could include unrelated login banners).
        return {"connected": False, "error": "total_timeout", "timeout_seconds": timeout}
    except OSError as exc:
        return {"connected": False, "error": redact(str(exc))}
    metadata = {"connected": False, "ssh_exit_code": result.returncode,
                "ssh_stderr": redact(result.stderr[:4096])}
    if result.returncode:
        return dict(metadata, error="ssh_or_remote_python_failed")
    try:
        if len(result.stdout) > 2 * 1024 * 1024:
            raise ValueError("inventory exceeds output limit")
        inventory = json.loads(result.stdout)
        if not isinstance(inventory, dict) or inventory.get("schema") != 2:
            raise ValueError("unsupported inventory schema")
    except (ValueError, RecursionError):
        return dict(metadata, error="invalid_inventory_response")
    return dict(metadata, connected=True, inventory=redact(inventory))
