"""Unified CLI dispatcher for the local reCamera skill.

Usage::

    rcl <command> [key=value ...] [--key value ...] [--key=value ...]
    rcl <command> '{"key": "value", ...}'
    rcl <command> --help
    rcl --pretty <command> ...        # pretty-printed JSON output
    rcl serve                         # persistent JSONL REPL (see below)

Arguments are matched to the command function's keyword parameters and
coerced using its type annotations:

* ``str``               — passed through verbatim
* ``int`` / ``float``   — parsed numerically
* ``bool``              — strict: ``true/false``, ``yes/no``, ``on/off``, ``1/0``
* ``Optional[...]``     — ``null`` / ``none`` select ``None``
* ``List`` / ``Dict``   — inline JSON, or ``@path/to/file.json`` (``@-`` = stdin)

In the key=value form, any value may come from a file via ``@path``;
``@@`` gives a literal leading ``@``. A single argument starting with ``{``
is parsed as one JSON object for the whole call (no ``@`` expansion there).

Output is compact one-line JSON on stdout (set ``--pretty`` or
``RECAMERA_PRETTY=1`` for indented). Usage errors print the command's
``usage:`` + ``example:`` lines to stderr and exit 2; device/IO errors print
an error JSON object to stderr and exit 1.

Serve mode keeps one process (and one keep-alive connection) alive for a
whole agent session — the lowest-latency way to drive the camera from a
harness that can hold a long-running process::

    $ rcl serve
    {"cmd": "get_status"}
    {"ok": true, "result": {...}}
    {"id": 7, "cmd": "wait_event", "args": {"timeout_s": 60}}
    {"id": 7, "ok": true, "result": {"events": [...], ...}}

Each input line is one JSON object: ``cmd`` (required), ``args`` (object,
optional), ``id`` (echoed back, optional). Special commands:
``list-commands``, ``exit``. One JSON response per line, always flushed.
"""

from __future__ import annotations

import inspect
import json
import os
import sys
import typing
from typing import Any, Callable, Dict, Iterable, Tuple

if __name__ == "__main__" and __package__ is None:
    # Direct execution: make absolute imports work like `python3 -m`.
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from recamera_local import (
        acoustic,
        apps,
        backup,
        capture,
        clock,
        detect,
        gpio,
        image,
        notify,
        records,
        status,
        storage,
        trigger,
        video,
    )
    from recamera_local._coerce import to_bool
    from recamera_local._errors import RecameraError
else:
    from . import (
        acoustic,
        apps,
        backup,
        capture,
        clock,
        detect,
        gpio,
        image,
        notify,
        records,
        status,
        storage,
        trigger,
        video,
    )
    from ._coerce import to_bool
    from ._errors import RecameraError

_MODULES = (
    status,
    clock,
    storage,
    capture,
    detect,
    acoustic,
    trigger,
    records,
    gpio,
    image,
    apps,
    video,
    notify,
    backup,
)

PROG = "rcl"

# Python-API-only parameters, excluded from CLI argument parsing.
CLI_EXCLUDE = {"raw"}


def _derive_schema(fn: Callable[..., Any]) -> Dict[str, set]:
    """required = params without defaults; optional = params with defaults."""
    params = inspect.signature(fn).parameters.values()
    empty = inspect.Parameter.empty
    return {
        "required": {p.name for p in params if p.default is empty} - CLI_EXCLUDE,
        "optional": {p.name for p in params if p.default is not empty} - CLI_EXCLUDE,
    }


def _collect() -> Tuple[Dict[str, Callable[..., Any]], Dict[str, Dict[str, set]]]:
    cmds: Dict[str, Callable[..., Any]] = {}
    schemas: Dict[str, Dict[str, set]] = {}
    for mod in _MODULES:
        for name, fn in getattr(mod, "COMMANDS", {}).items():
            if name in cmds:
                raise RuntimeError(f"Duplicate CLI command {name!r}")
            cmds[name] = fn
            schemas[name] = _derive_schema(fn)
    return cmds, schemas


COMMANDS, COMMAND_SCHEMAS = _collect()

EXAMPLES: Dict[str, str] = {
    "get_status": "rcl get_status",
    "get_device_info": "rcl get_device_info",
    "get_resource_info": "rcl get_resource_info",
    "get_system_time": "rcl get_system_time",
    "set_system_time": "rcl set_system_time method=ntp timezone=Asia/Shanghai",
    "probe_timezone": "rcl probe_timezone",
    "sync_system_time": "rcl sync_system_time timezone=auto",
    "get_battery_status": "rcl get_battery_status",
    "export_device_config": "rcl export_device_config output=./backup-$(date +%s).tar",
    "reboot_device": "rcl reboot_device confirm=true",
    "get_storage_status": "rcl get_storage_status",
    "set_storage_slot": "rcl set_storage_slot by_dev_path=/dev/mmcblk0p8",
    "configure_storage_quota": (
        "rcl configure_storage_quota dev_path=/dev/mmcblk0p8 quota_limit_bytes=-1 quota_rotate=true"
    ),
    "get_capture_status": "rcl get_capture_status",
    "start_capture": "rcl start_capture format=JPG",
    "stop_capture": "rcl stop_capture",
    "capture_image": "rcl capture_image",
    "get_detection_models_info": "rcl get_detection_models_info",
    "get_detection_model": "rcl get_detection_model",
    "set_detection_model": "rcl set_detection_model model_name=yolo11n fps=30",
    "get_detection_schedule": "rcl get_detection_schedule",
    "set_detection_schedule": "rcl set_detection_schedule schedule=null",
    "get_detection_rules": "rcl get_detection_rules",
    "set_detection_rules": (
        "rcl set_detection_rules 'rules=[{\"name\":\"person\",\"label_filter\":[\"person\"]}]'"
    ),
    "get_detection_events": "rcl get_detection_events start_unix_ms=1745150000000",
    "clear_detection_events": "rcl clear_detection_events",
    "wait_event": "rcl wait_event timeout_s=60",
    "get_active_acoustic_model": "rcl get_active_acoustic_model",
    "list_acoustic_models": "rcl list_acoustic_models",
    "set_acoustic_model": "rcl set_acoustic_model workspace_id=ws1 head_id=head_abc",
    "get_record_sources": "rcl get_record_sources",
    "get_record_config": "rcl get_record_config",
    "set_record_config": "rcl set_record_config rule_enabled=true writer_format=JPG",
    "get_schedule_rule": "rcl get_schedule_rule",
    "set_schedule_rule": "rcl set_schedule_rule schedule=null",
    "get_record_trigger": "rcl get_record_trigger",
    "set_record_trigger": (
        "rcl set_record_trigger 'trigger={\"kind\":\"timer\",\"interval_seconds\":60}'"
    ),
    "activate_http_trigger": "rcl activate_http_trigger",
    "list_records": "rcl list_records path=2026-04-20 limit=100",
    "read_file": "rcl read_file path=/mnt/rc_mmcblk0p8/reCamera/snapshot.jpg",
    "delete_file": "rcl delete_file path=/mnt/rc_mmcblk0p8/reCamera/snapshot.jpg confirm=true",
    "list_gpios": "rcl list_gpios",
    "get_gpio_info": "rcl get_gpio_info pin_id=106",
    "set_gpio_value": "rcl set_gpio_value pin_id=106 value=1",
    "get_gpio_value": "rcl get_gpio_value pin_id=106 debounce_ms=100",
    "get_image_settings": "rcl get_image_settings",
    "set_image_settings": (
        "rcl set_image_settings section=adjustment 'values={\"saturation\":80}'"
    ),
    "list_apps": "rcl list_apps",
    "get_app_logs": "rcl get_app_logs app_id=acousticslab tail=100",
    "start_app": "rcl start_app app_id=acousticslab",
    "stop_app": "rcl stop_app app_id=acousticslab confirm=true",
    "restart_app": "rcl restart_app app_id=acousticslab confirm=true",
    "get_video_encode": "rcl get_video_encode stream=main",
    "set_video_encode": "rcl set_video_encode stream=main resolution=1920x1080 frame_rate=30",
    "get_notify_config": "rcl get_notify_config",
    "set_notify_config": (
        "rcl set_notify_config mode=1 'mqtt={\"url\":\"broker.local\",\"topic\":\"recamera/events\"}'"
    ),
}


class _UsageError(Exception):
    """Raised for malformed invocations; main() prints usage + example."""


def _hints(fn: Callable[..., Any]) -> Dict[str, Any]:
    target = (
        fn
        if inspect.isfunction(fn) or inspect.ismethod(fn)
        else getattr(fn, "__call__", None)
    )
    if target is None:
        return {}
    try:
        hints = typing.get_type_hints(target)
    except (NameError, TypeError, AttributeError):  # unresolvable annotations
        return {}
    hints.pop("return", None)
    return hints


def _unwrap_optional(hint: Any) -> Any:
    """Reduce ``Optional[X]``/``Union[X, None]`` to ``X``; pass anything else through."""
    if typing.get_origin(hint) is typing.Union:
        inner = [a for a in typing.get_args(hint) if a is not type(None)]
        if inner:
            return inner[0]
    return hint


def _command_help(name: str) -> str:
    spec = COMMAND_SCHEMAS.get(name, {"required": set(), "optional": set()})
    req = sorted(spec.get("required", set()))
    opt = sorted(spec.get("optional", set()))
    fn = COMMANDS.get(name)
    hints = _hints(fn) if fn is not None else {}

    def _label(key: str) -> str:
        hint = hints.get(key)
        if hint is None:
            return f"{key}=<value>"
        hint = _unwrap_optional(hint)
        if hint is bool:
            return f"{key}=<true|false>"
        if hint is int:
            return f"{key}=<int>"
        if hint is float:
            return f"{key}=<number>"
        origin = typing.get_origin(hint)
        if origin is list or hint is list:
            return f"{key}=<json-array>"
        if origin is dict or hint is dict:
            return f"{key}=<json-object>"
        return f"{key}=<string>"

    parts = [f"{PROG} {name}"]
    parts.extend(_label(k) for k in req)
    if opt:
        parts.append("[" + "] [".join(_label(k) for k in opt) + "]")
    lines = ["usage: " + " ".join(parts)]
    if name in EXAMPLES:
        lines.append(f"example: {EXAMPLES[name]}")
    return "\n".join(lines)


def _print_help(stream=sys.stdout) -> None:
    stream.write(__doc__ or "")
    stream.write("\nAvailable commands:\n")
    for name in sorted(COMMANDS):
        spec = COMMAND_SCHEMAS.get(name, {"required": set(), "optional": set()})
        req = sorted(spec.get("required", set()))
        opt = sorted(spec.get("optional", set()))
        parts = []
        if req:
            parts.append("required=" + ",".join(req))
        if opt:
            parts.append("optional=" + ",".join(opt))
        stream.write(f"  {name:32s} {'  '.join(parts)}\n")
    stream.write(f"\nRun '{PROG} <command> --help' for usage and an example.\n")


_NULL_TOKENS = {"null", "none"}


def _load_at_value(raw: str) -> Any:
    """Resolve ``@path`` (or ``@-`` for stdin) values; ``@@`` escapes a literal ``@``."""
    if raw.startswith("@@"):
        return raw[1:]
    if not raw.startswith("@"):
        return raw
    source = raw[1:]
    try:
        if source == "-":
            text = sys.stdin.read()
        else:
            with open(source, "r", encoding="utf-8") as fh:
                text = fh.read()
    except OSError as exc:
        raise _UsageError(f"cannot read value file {source!r}: {exc}") from exc
    return text.strip()


def _coerce(cmd: str, key: str, value: Any, hint: Any) -> Any:
    """Coerce *value* to *hint*; *value* is a raw CLI string or native JSON data."""
    where = f"{cmd}: argument {key!r}"
    if hint is None:
        return value
    origin = typing.get_origin(hint)
    if origin is typing.Union:
        if value is None or (isinstance(value, str) and value.strip().lower() in _NULL_TOKENS):
            return None
        hint = _unwrap_optional(hint)
        origin = typing.get_origin(hint)
    if hint is bool:
        try:
            return to_bool(value, where)
        except ValueError as exc:
            raise _UsageError(str(exc)) from exc
    if hint is int:
        if isinstance(value, bool):
            raise _UsageError(f"{where} must be an integer; got {value!r}")
        if isinstance(value, int):
            return value
        if isinstance(value, float) and value.is_integer():
            return int(value)
        if isinstance(value, str):
            try:
                return int(value.strip(), 10)
            except ValueError:
                pass
        raise _UsageError(f"{where} must be an integer; got {value!r}")
    if hint is float:
        if isinstance(value, bool):
            raise _UsageError(f"{where} must be a number; got {value!r}")
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value.strip())
            except ValueError:
                pass
        raise _UsageError(f"{where} must be a number; got {value!r}")
    if hint is str:
        if isinstance(value, str):
            return value
        if isinstance(value, (bool, list, dict)) or value is None:
            raise _UsageError(f"{where} must be a string; got {value!r}")
        return str(value)  # int/float spellings are unambiguous (e.g. a numeric name)
    if origin is list or hint is list:
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError as exc:
                raise _UsageError(
                    f"{where} must be a JSON array (or @file); got {value!r} ({exc})"
                ) from exc
        if not isinstance(value, list):
            raise _UsageError(f"{where} must be a JSON array; got {value!r}")
        return value
    if origin is dict or hint is dict:
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError as exc:
                raise _UsageError(
                    f"{where} must be a JSON object (or @file); got {value!r} ({exc})"
                ) from exc
        if not isinstance(value, dict):
            raise _UsageError(f"{where} must be a JSON object; got {value!r}")
        return value
    return value


def _parse_cli(name: str, argv: Iterable[str]) -> Dict[str, Any]:
    """Parse post-command argv into coerced kwargs.

    Two input forms, never mixed:

    * a single argument starting with ``{`` — one JSON object for the call;
    * ``key=value`` / ``--key=value`` / ``--key value`` tokens.
    """
    tokens = list(argv)
    if not tokens:
        return {}
    fn = COMMANDS[name]
    hints = _hints(fn)

    if len(tokens) == 1 and tokens[0].lstrip().startswith("{"):
        try:
            data = json.loads(tokens[0])
        except json.JSONDecodeError as exc:
            raise _UsageError(
                f"{name}: invalid JSON arguments: {exc}. "
                "Hint: flat key=value arguments also work — see usage below."
            ) from exc
        if not isinstance(data, dict):
            raise _UsageError(f"{name}: JSON arguments must be an object.")
        parsed: Dict[str, Any] = {}
        for k, v in data.items():
            key = str(k).replace("-", "_")
            parsed[key] = _coerce(name, key, v, hints.get(key))
        return parsed

    if any(t.lstrip().startswith("{") for t in tokens):
        raise _UsageError(
            f"{name}: cannot mix a JSON object with key=value arguments; use one form."
        )

    kwargs: Dict[str, Any] = {}
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token.startswith("--"):
            body = token[2:]
            if not body:
                raise _UsageError(f"{name}: stray '--' argument.")
            if "=" in body:
                key, raw = body.split("=", 1)
            else:
                key = body
                i += 1
                if i >= len(tokens):
                    raise _UsageError(f"{name}: --{key} requires a value.")
                raw = tokens[i]
        elif "=" in token:
            key, raw = token.split("=", 1)
        else:
            raise _UsageError(
                f"{name}: expected key=value or --key value, got {token!r}."
            )
        key = key.strip().replace("-", "_")
        if not key:
            raise _UsageError(f"{name}: empty argument name in {token!r}.")
        if key in kwargs:
            raise _UsageError(f"{name}: duplicate argument {key!r}.")
        value = _load_at_value(raw)
        kwargs[key] = _coerce(name, key, value, hints.get(key))
        i += 1
    return kwargs


def _validate(name: str, kwargs: Dict[str, Any]) -> None:
    spec = COMMAND_SCHEMAS.get(name)
    if spec is None:
        return
    required = set(spec.get("required", set()))
    optional = set(spec.get("optional", set()))
    allowed = required | optional
    missing = required - set(kwargs)
    if missing:
        raise _UsageError(f"{name}: missing required argument(s): {sorted(missing)}")
    extra = set(kwargs) - allowed
    if extra:
        hint = f" Did you mean one of: {sorted(allowed)}?" if allowed else ""
        raise _UsageError(f"{name}: unknown argument(s): {sorted(extra)}.{hint}")


def _dumps(result: Any, pretty: bool) -> str:
    if pretty:
        return json.dumps(result, indent=2, ensure_ascii=False, sort_keys=False)
    return json.dumps(result, ensure_ascii=False, sort_keys=False, separators=(",", ":"))


def run_command(name: str, kwargs: Dict[str, Any]) -> Any:
    """Validate + invoke a command; used by both one-shot and serve mode."""
    if name not in COMMANDS:
        raise _UsageError(f"unknown command: {name}")
    _validate(name, kwargs)
    return COMMANDS[name](**kwargs)


def _serve(pretty: bool) -> int:
    """Persistent JSONL REPL: one command per input line, one JSON per output line."""
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        rid = None
        try:
            req = json.loads(line)
            if not isinstance(req, dict):
                raise _UsageError("each line must be a JSON object")
            rid = req.get("id")
            name = req.get("cmd", req.get("command"))
            if not isinstance(name, str) or not name:
                raise _UsageError("missing 'cmd' (command name)")
            if name == "exit":
                return 0
            if name == "list-commands":
                result: Any = sorted(COMMANDS)
            else:
                args = req.get("args") or {}
                if not isinstance(args, dict):
                    raise _UsageError("'args' must be a JSON object")
                fn = COMMANDS.get(name)
                if fn is None:
                    raise _UsageError(f"unknown command: {name}")
                hints = _hints(fn)
                kwargs = {}
                for k, v in args.items():
                    key = str(k).replace("-", "_")
                    kwargs[key] = _coerce(name, key, v, hints.get(key))
                result = run_command(name, kwargs)
            out: Dict[str, Any] = {"ok": True, "result": result}
        except _UsageError as exc:
            out = {"ok": False, "error": str(exc)}
        except RecameraError as exc:
            out = {"ok": False, "error": str(exc), "code": exc.code, "status": exc.status}
        except (TypeError, ValueError) as exc:
            out = {"ok": False, "error": str(exc)}
        except Exception as exc:  # keep the session alive on unexpected errors
            out = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        if rid is not None:
            out["id"] = rid
        sys.stdout.write(_dumps(out, pretty) + "\n")
        sys.stdout.flush()
    return 0


def main(argv: Iterable[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    pretty = bool(os.environ.get("RECAMERA_PRETTY"))
    if args and args[0] == "--pretty":
        pretty = True
        args = args[1:]
    if not args or args[0] in ("-h", "--help", "help"):
        _print_help()
        return 0
    if args[0] == "serve":
        return _serve(pretty)
    if args[0] == "list-commands":
        print("\n".join(sorted(COMMANDS)))
        return 0

    name = args[0]
    if name not in COMMANDS:
        print(f"unknown command: {name}", file=sys.stderr)
        _print_help(sys.stderr)
        return 2

    rest = args[1:]
    if any(a in ("-h", "--help") for a in rest):
        print(_command_help(name))
        return 0

    try:
        kwargs = _parse_cli(name, rest)
        result = run_command(name, kwargs)
    except _UsageError as exc:
        print(str(exc), file=sys.stderr)
        print(_command_help(name), file=sys.stderr)
        return 2
    except RecameraError as exc:
        print(
            _dumps({"error": str(exc), "code": exc.code, "status": exc.status}, pretty),
            file=sys.stderr,
        )
        return 1
    except (TypeError, ValueError) as exc:
        print(f"{name}: {exc}", file=sys.stderr)
        print(_command_help(name), file=sys.stderr)
        return 2

    print(_dumps(result, pretty))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
