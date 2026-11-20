#!/usr/bin/python3
# Copyright (c) Meta Platforms, Inc. and affiliates.
# Licensed under the Apache License, Version 2.0.
"""Muse service runner with the reCamera Pro camera/audio inspection commands.

Runs in the app's own root service process (the same process that owns the
Muse link).  The reCamera-Intellisense SDK is vendored in this app directory;
nothing is copied to a shared /userdata directory.  The extra commands are
read-only local-API calls, so they are safe to handle in-process.  Muse's
system.run/file.* commands still go through the normal run-as account.
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
sys.path.insert(0, str(APP_DIR / "vendor"))

import recamera_intellisense as rci  # noqa: E402

from musegadget import __version__, commands, config, device_guide, identity  # noqa: E402
from musegadget.executor import (  # noqa: E402
    COMMAND_SPECS, Account, Executor, error, ok,
)
from musegadget.link_client import DEFAULT_MODEL_ID, DEFAULT_MODEL_NAME  # noqa: E402
from musegadget.service import Service  # noqa: E402

log = logging.getLogger("musegadget")

DEVICE_NAME = "local"
KEY_FILE = "/userdata/config/system/http_key.json"


def _device_token() -> str:
    try:
        with open(KEY_FILE, "r", encoding="utf-8") as source:
            data = json.load(source)
        key = data.get("key") if isinstance(data, dict) else ""
        return key.strip() if isinstance(key, str) else ""
    except (OSError, ValueError):
        return ""


def _ensure_device() -> None:
    try:
        names = {record.get("name") for record in rci.list_devices()
                 if isinstance(record, dict)}
    except Exception:  # noqa: BLE001 - unreadable store is handled by add_device
        names = set()
    if DEVICE_NAME in names:
        return
    rci.add_device(
        DEVICE_NAME,
        "127.0.0.1",
        _device_token(),
        protocol="https",
        allow_unsecured=True,
    )


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
}

# Publish the extra commands exactly like the SDK's built-ins.
COMMAND_SPECS.update(SPECS)


def _snapshot(**params) -> dict:
    result = rci.capture_image(device_name=DEVICE_NAME, **params)
    if isinstance(result, dict):
        result.pop("content_base64", None)
        result["inline_base64_omitted"] = True
    return result


def handle(command: str, params: dict):
    function, allowed = HANDLERS[command]
    unknown = set(params) - allowed
    if unknown:
        raise ValueError("unknown parameter(s): " + ", ".join(sorted(unknown)))
    _ensure_device()
    if command == "camera.snapshot":
        return _snapshot(**params)
    return function(device_name=DEVICE_NAME, **params)


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