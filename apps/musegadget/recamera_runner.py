#!/usr/bin/python3
# Copyright (c) Meta Platforms, Inc. and affiliates.
# Licensed under the Apache License, Version 2.0.
"""Muse service runner with the reCamera Pro camera/audio inspection commands.

Runs in the app's own root service process (the same process that owns the
Muse link).  The reCamera-Intellisense `recamera-local` skill is bundled in
this app directory under `skills/recamera-local/`; nothing is copied to a
shared /userdata directory.  The extra commands are read-only local-API calls,
so they are safe to handle in-process.  Muse's system.run/file.* commands
still go through the normal run-as account.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import socket
import sys
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
SKILLS_DIR = Path(os.environ.get("MUSEGADGET_SKILLS_DIR") or (APP_DIR / "skills"))
SKILL_NAME = "recamera-local"
SKILL_DIR = SKILLS_DIR / SKILL_NAME
sys.path.insert(0, str(SKILL_DIR / "scripts"))

import recamera_local as rci  # noqa: E402

from musegadget import __version__, commands, config, device_guide, identity  # noqa: E402
from musegadget.executor import (  # noqa: E402
    COMMAND_SPECS, Account, Executor, error, ok,
)
from musegadget.link_client import DEFAULT_MODEL_ID, DEFAULT_MODEL_NAME  # noqa: E402
from musegadget.service import Service  # noqa: E402

log = logging.getLogger("musegadget")

SKILL_READ_LIMIT = 256 * 1024


def _skill_files() -> list[str]:
    """Regular files in the bundled skill, excluding bytecode and cache dirs."""
    root = SKILL_DIR.resolve()
    files: list[str] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        files.append(path.relative_to(root).as_posix())
    return files


def _skill_metadata() -> dict:
    try:
        upstream = json.loads(
            (SKILL_DIR / "upstream.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        upstream = {}
    return {
        "name": SKILL_NAME,
        "package_version": getattr(rci, "__version__", None),
        "path": str(SKILL_DIR),
        "repository": upstream.get("repository"),
        "branch": upstream.get("branch"),
        "commit": upstream.get("commit"),
        "commit_date": upstream.get("commit_date"),
        "files": _skill_files(),
    }


def list_skills() -> dict:
    return {"skills": [_skill_metadata()]}


def read_skill(file: str = "SKILL.md") -> dict:
    """Read a file from the bundled skill (default: its SKILL.md)."""
    if not isinstance(file, str) or not file.strip():
        raise ValueError("'file' must be a non-empty relative path")
    root = SKILL_DIR.resolve()
    candidate = (root / file.strip()).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        raise ValueError("'file' must stay inside the skill directory") from None
    if not candidate.is_file():
        raise ValueError(f"no such skill file: {file}")
    if "__pycache__" in candidate.parts or candidate.suffix == ".pyc":
        raise ValueError(f"not a readable skill file: {file}")
    data = candidate.read_bytes()
    if len(data) > SKILL_READ_LIMIT:
        raise ValueError(f"skill file is too large: {file}")
    return {
        "name": SKILL_NAME,
        "file": candidate.relative_to(root).as_posix(),
        "bytes": len(data),
        "content": data.decode("utf-8", errors="replace"),
    }


# Command name -> (function, allowed parameter names).  Read-only only.
HANDLERS = {
    "camera.info": (rci.get_device_info, frozenset()),
    "camera.resources": (rci.get_resource_info, frozenset()),
    "camera.models": (rci.get_detection_models_info, frozenset()),
    "camera.events": (rci.get_detection_events,
                      frozenset({"start_unix_ms", "end_unix_ms"})),
    "camera.rules": (rci.get_detection_rules, frozenset()),
    "camera.records": (rci.list_records, frozenset({"path", "limit", "offset"})),
    "camera.video": (rci.get_video_encode, frozenset({"stream"})),
    "camera.snapshot": (None, frozenset({"output", "timeout"})),
    "audio.models": (rci.list_acoustic_models, frozenset()),
    "audio.sources": (rci.get_record_sources, frozenset()),
    "apps.list": (rci.list_apps, frozenset()),
    "gpio.list": (rci.list_gpios, frozenset()),
    "recamera.time": (rci.get_system_time, frozenset()),
    "skills.list": (list_skills, frozenset()),
    "skills.read": (read_skill, frozenset({"file"})),
}

SPECS = {
    "camera.info": {
        "description": "Read the reCamera Pro hardware identity (serial, firmware, sensor and base-board model).",
        "required": {}, "optional": {}, "timeout_ms": 30000,
    },
    "camera.resources": {
        "description": "Read reCamera Pro CPU, NPU, memory and storage utilisation.",
        "required": {}, "optional": {}, "timeout_ms": 30000,
    },
    "camera.models": {
        "description": "List installed AI detection models on the reCamera Pro with their labels and ids.",
        "required": {}, "optional": {}, "timeout_ms": 30000,
    },
    "camera.events": {
        "description": "Read recent on-device detection events with timestamps and captured-file paths.",
        "required": {},
        "optional": {
            "start_unix_ms": {"type": "integer", "description": "Only events at or after this Unix time in ms."},
            "end_unix_ms": {"type": "integer", "description": "Only events at or before this Unix time in ms."},
        },
        "timeout_ms": 30000,
    },
    "camera.rules": {
        "description": "Read the active AI detection rules (labels, confidence, regions, debounce).",
        "required": {}, "optional": {}, "timeout_ms": 30000,
    },
    "camera.records": {
        "description": "List recorded images and clips in the camera's storage.",
        "required": {},
        "optional": {
            "path": {"type": "string", "description": "Directory relative to the record root; omit for the root."},
            "limit": {"type": "integer", "description": "Maximum entries to return."},
            "offset": {"type": "integer", "description": "Pagination offset."},
        },
        "timeout_ms": 30000,
    },
    "camera.video": {
        "description": "Read the reCamera Pro video encoder settings for the main or sub stream.",
        "required": {},
        "optional": {
            "stream": {"type": "string", "description": "'main' (default) or 'sub'."},
        },
        "timeout_ms": 30000,
    },
    "camera.snapshot": {
        "description": "Capture one JPEG on the reCamera Pro; returns its on-device path and size, not the image bytes.",
        "required": {},
        "optional": {
            "output": {"type": "string", "description": "Absolute on-device directory under a mounted storage slot; omit for the selected slot."},
            "timeout": {"type": "number", "description": "Seconds to wait for the capture to finish."},
        },
        "timeout_ms": 90000,
    },
    "audio.models": {
        "description": "List AcousticsLab sound-detection heads; the active one is marked.",
        "required": {}, "optional": {}, "timeout_ms": 30000,
    },
    "audio.sources": {
        "description": "Read recording sources and the sound classes AcousticsLab currently provides.",
        "required": {}, "optional": {}, "timeout_ms": 30000,
    },
    "apps.list": {
        "description": "List reCamera App Center applications with status, version and system flag.",
        "required": {}, "optional": {}, "timeout_ms": 30000,
    },
    "gpio.list": {
        "description": "List reCamera Pro GPIO pins and their configuration (read-only).",
        "required": {}, "optional": {}, "timeout_ms": 30000,
    },
    "recamera.time": {
        "description": "Read the reCamera Pro clock, timezone and NTP configuration.",
        "required": {}, "optional": {}, "timeout_ms": 30000,
    },
    "skills.list": {
        "description": "List the agent skills bundled with this app, including the recamera-local skill's upstream commit and files.",
        "required": {}, "optional": {}, "timeout_ms": 30000,
    },
    "skills.read": {
        "description": "Read a file from a bundled skill. Start with SKILL.md, then REFERENCE.md and OBSERVATION.md as needed.",
        "required": {},
        "optional": {
            "file": {"type": "string", "description": "Path relative to the skill directory; defaults to SKILL.md."},
        },
        "timeout_ms": 30000,
    },
}

# Publish the extra commands exactly like the SDK's built-ins.
COMMAND_SPECS.update(SPECS)


def _snapshot(**params) -> dict:
    result = rci.capture_image(**params)
    if isinstance(result, dict):
        result.pop("content_base64", None)
        result["inline_base64_omitted"] = True
    return result


def handle(command: str, params: dict):
    function, allowed = HANDLERS[command]
    unknown = set(params) - allowed
    if unknown:
        raise ValueError("unknown parameter(s): " + ", ".join(sorted(unknown)))
    if command == "camera.snapshot":
        return _snapshot(**params)
    return function(**params)


class RecameraExecutor(Executor):
    """Executor that handles the read-only reCamera commands in-process."""

    def run(self, command: str, params: dict, timeout_ms: int | None = None) -> dict:
        if command in HANDLERS:
            try:
                result = handle(command, params)
            except Exception as exc:  # noqa: BLE001 - never expose the token
                return error(f"{type(exc).__name__}: {exc}")
            return ok(result if isinstance(result, dict) else {"output": result})
        return super().run(command, params, timeout_ms)


def _guide() -> str | None:
    path = os.environ.get("MUSEGADGET_GUIDE_FILE")
    if path:
        try:
            return Path(path).read_text(encoding="utf-8")
        except OSError:
            pass
    return device_guide.default_guide()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    run_as = (os.environ.get("MUSEGADGET_RUN_AS") or "admin").strip()
    try:
        account = Account.lookup(run_as)
    except KeyError:
        log.error("run-as account %r does not exist", run_as)
        raise SystemExit(1)
    try:
        sdk_token = config.sdk_token()
    except ValueError as exc:
        log.warning("running without an SDK token: %s", exc)
        sdk_token = None

    executor = RecameraExecutor(account, commands.load(config.commands_dir()))
    log.info("musegadget %s: commands run as %s", __version__, account.name)

    async def run() -> None:
        service = Service(
            identity=identity.load_or_create(),
            executor=executor,
            sdk_token=sdk_token,
            display_name=(os.environ.get("MUSEGADGET_DISPLAY_NAME")
                          or socket.gethostname()),
            model_id=os.environ.get("MUSEGADGET_MODEL_ID") or DEFAULT_MODEL_ID,
            model_name=os.environ.get("MUSEGADGET_MODEL_NAME") or DEFAULT_MODEL_NAME,
            guide=_guide(),
        )
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(signum, service.stop)
        server = await service.serve_local(config.socket_path())
        try:
            await service.run()
        finally:
            server.close()

    asyncio.run(run())


if __name__ == "__main__":
    main()