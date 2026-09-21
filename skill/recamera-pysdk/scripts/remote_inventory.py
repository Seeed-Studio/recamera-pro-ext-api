"""Read-only bounded inventory, sent to target python3 via SSH stdin.

No SDK imports, socket connections, subprocesses or hardware device opens.
The root parameter exists for offline procfs/filesystem fixtures.
"""
import grp
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import stat
import time

ENV_KEYS = {
    "RECAMERA_APP_ID", "RECAMERA_APP_INSTANCE", "RECAMERA_APP_GENERATION",
    "RECAMERA_RESULT_GATEWAY_SOCK", "RECAMERA_RESULT_GATEWAY_REQUIRED",
    "RECAMERA_NPU_MODE", "RECAMERA_INFERENCE_SERVICE_SOCK",
}
SECRET_KEY = re.compile(r"password|passwd|token|secret|authorization|credential|api[_-]?key", re.I)


def redact(value):
    if isinstance(value, dict):
        return {str(k): "<redacted>" if SECRET_KEY.search(str(k)) else redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    if not isinstance(value, str):
        return value
    value = re.sub(r"(\w+://)[^\s/]+@", r"\1<redacted>@", value)
    value = re.sub(r"(?i)(bearer\s+)[\w.+/=-]+", r"\1<redacted>", value)
    return re.sub(r'''(?ix)(["']?(?:password|passwd|token|secret|authorization|credential|api[_-]?key)["']?\s*[:=]\s*)(?:"[^"]*"|'[^']*'|[^\s,;&]+)''', r"\1<redacted>", value)


def read_file(path, limit=65536, tail=False):
    result = {"path": str(path), "ok": False}
    fd = None
    try:
        # O_NONBLOCK prevents an unexpected FIFO/device from blocking inventory.
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return dict(result, error="not_regular_file")
        if tail and info.st_size > limit:
            os.lseek(fd, info.st_size - limit, os.SEEK_SET)
        raw = bytearray()
        while len(raw) <= limit:
            part = os.read(fd, min(16384, limit + 1 - len(raw)))
            if not part:
                break
            raw.extend(part)
        truncated = len(raw) > limit or (tail and info.st_size > limit)
        text = bytes(raw[:limit]).decode("utf-8", "replace")
        if tail and info.st_size > limit:
            text = text.partition("\n")[2]  # discard a partial first record
        return dict(result, ok=True, text=text, truncated=truncated,
                    size=info.st_size, mtime=info.st_mtime, ctime=info.st_ctime)
    except OSError as exc:
        return dict(result, error=exc.__class__.__name__)
    finally:
        if fd is not None:
            os.close(fd)


def read_json(path, limit=1024 * 1024):
    item = read_file(path, limit)
    if not item["ok"] or item.get("truncated"):
        return None
    try:
        return json.loads(item["text"])
    except (ValueError, RecursionError):
        return None


def path_info(path, registered=None):
    try:
        info = os.stat(path)
        kind = ("socket" if stat.S_ISSOCK(info.st_mode) else "char" if stat.S_ISCHR(info.st_mode)
                else "file" if stat.S_ISREG(info.st_mode) else "directory" if stat.S_ISDIR(info.st_mode) else "other")
        return dict(exists=True, kind=kind, readable=os.access(path, os.R_OK),
                    writable=os.access(path, os.W_OK), kernel_registered=registered,
                    protocol_verified=False)
    except FileNotFoundError:
        return dict(exists=False, error="missing", kernel_registered=registered, protocol_verified=False)
    except OSError as exc:
        return dict(exists=None, error=exc.__class__.__name__, kernel_registered=registered, protocol_verified=False)


def collect(app_id=None, root="/"):
    root = Path(root)
    def at(path):
        return root / path.lstrip("/")
    errors = []
    def obj(path):
        value = read_json(at(path))
        if not isinstance(value, dict):
            errors.append({"path": path, "error": "missing_unreadable_truncated_or_invalid_json"})
            return {}
        return value
    unix = read_file(at("/proc/net/unix"), 256 * 1024)
    registered = set()
    for line in unix.get("text", "").splitlines()[1:]:
        columns = line.split()
        if len(columns) >= 8:
            registered.add(columns[-1])
    sockets = {}
    for path in ["/run/recamera/frame.sock", "/run/recamera/result-in.sock",
                 "/run/recamera/probe.sock", "/run/recamera/inference-control.sock",
                 "/run/recamera/inferenced.sock", "/dev/shm/gmgr.sock"]:
        registered_here = path in registered if unix["ok"] and not unix["truncated"] else None
        sockets[path] = path_info(at(path), registered_here)
    uptime = read_file(at("/proc/uptime"), 128)
    observed = time.time()
    try:
        boot_time = observed - float(uptime["text"].split()[0])
    except (KeyError, ValueError, IndexError):
        boot_time = None
    boot_id = read_file(at("/proc/sys/kernel/random/boot_id"), 128).get("text", "").strip()
    processes = []
    proc_paths = sorted(at("/proc").glob("[0-9]*"))
    for proc in proc_paths[:2048]:
        cmd = read_file(proc / "cmdline", 16384)
        if not cmd["ok"]:
            errors.append({"path": str(proc / "cmdline"), "error": cmd.get("error")})
            continue
        argv = cmd["text"].rstrip("\0").split("\0")
        service = None
        for name in ("appmgr", "inferenced"):
            if any(argv[i:i+2] == ["-m", name] for i in range(len(argv))) or any(
                a.endswith("/" + name + "/__main__.py") for a in argv):
                service = name
        managed = any(a.endswith("/kit/run.py") for a in argv) or any(
            argv[i:i+2] == ["-m", "kit.run"] for i in range(len(argv)))
        app_path = "/userdata/local/apps/" + app_id + "/" if app_id else None
        if not service and not (app_path and managed and any(a.startswith(app_path) for a in argv)):
            continue
        rawstat = read_file(proc / "stat", 4096).get("text", "")
        fields = rawstat.rpartition(")")[2].split()
        try:
            ticks = int(fields[19])
            state = fields[0]
        except (ValueError, IndexError):
            errors.append({"path": str(proc / "stat"), "error": "unreadable_or_invalid"})
            continue
        env = {}
        if managed and app_id:
            environ = read_file(proc / "environ", 65536)
            for line in environ.get("text", "").split("\0"):
                key, _, value = line.partition("=")
                if key in ENV_KEYS:
                    env[key] = value
        # Re-read process stat to reject PID reuse or exit during collection.
        again = read_file(proc / "stat", 4096).get("text", "").rpartition(")")[2].split()
        if len(again) < 20 or again[19] != str(ticks):
            errors.append({"path": str(proc), "error": "process_changed_during_probe"})
            continue
        processes.append(dict(pid=int(proc.name), state=state, service=service, managed=managed,
                              app_id=app_id if managed else None, environment=env,
                              start_ticks=ticks, boot_id=boot_id,
                              started_at=boot_time + ticks / os.sysconf("SC_CLK_TCK") if boot_time else None))
    gid = os.getgid()
    groups = set(os.getgroups()) | {gid}
    try:
        audio_gid = grp.getgrnam("audio").gr_gid
    except KeyError:
        audio_gid = None
    audio_nodes = {str(p).removeprefix(str(root).rstrip("/")): path_info(p)
                   for p in sorted(at("/dev/snd").glob("pcmC*D*p"))[:64]}
    paths = ["/usr/lib/libasound.so.2", "/etc/asound.conf", "/oem/usr/share/speaker_test.wav",
             "/usr/lib/librecamera_ext.so.1"]
    modules = {}
    for name in ("kit", "recamera_ext"):
        try:
            modules[name] = importlib.util.find_spec(name) is not None if root == Path("/") else None
        except (ValueError, ImportError, AttributeError):
            modules[name] = None
    report = dict(schema=2, observed_at=observed, boot_id=boot_id, sockets=sockets,
                  processes=processes, process_scan_complete=len(proc_paths) <= 2048 and not errors,
                  paths={p: path_info(at(p)) for p in paths}, modules_discoverable=modules,
                  identity=dict(uid=os.geteuid(), gid=gid, groups=sorted(groups), audio_gid=audio_gid),
                  audio_nodes=audio_nodes, audio_cards=read_file(at("/proc/asound/cards"), 8192),
                  commands={n: bool(shutil.which(n)) if root == Path("/") else None for n in ("aplay", "arecord", "gpioinfo")})
    if app_id:
        base = "/userdata/local/apps/" + app_id
        manifest = obj(base + "/manifest.json")
        # Only fields needed for diagnosis, never config defaults / full permissions.
        summary = {k: manifest[k] for k in ("id", "version", "entry", "models", "artifacts", "resources") if k in manifest}
        for artifact in summary.get("artifacts", []):
            if not isinstance(artifact, dict):
                continue
            file = artifact.get("file")
            if isinstance(file, str) and not Path(file).is_absolute() and ".." not in Path(file).parts:
                artifact["file_inventory"] = path_info(at(base) / file)
        states = obj("/userdata/local/apps/state.json").get("apps", {})
        state = states.get(app_id, {}) if isinstance(states, dict) else {}
        state = state if isinstance(state, dict) else {}
        state = {k: state[k] for k in ("id", "pid", "instance_id", "generation", "observed_state",
                 "desired_state", "started_at", "updated_at", "reason", "restart_history") if k in state}
        operations = obj("/userdata/local/appmgr/operations.json").get("operations", [])
        operations = [{k: o[k] for k in ("app_id", "id", "kind", "status", "started_at", "finished_at", "error") if k in o}
                      for o in operations if isinstance(o, dict) and o.get("app_id") == app_id][-10:] if isinstance(operations, list) else []
        audit = read_file(at("/userdata/local/appmgr/audit.log"), 65536, tail=True)
        records = []
        for line in audit.get("text", "").splitlines():
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if isinstance(rec, dict) and (rec.get("app_id") == app_id or rec.get("id") == app_id):
                records.append({k: rec[k] for k in ("ts", "action", "app_id", "id", "pid", "instance_id", "generation", "error") if k in rec})
        pid = read_file(at(base + "/run.pid"), 64).get("text", "").strip()
        report["app"] = dict(id=app_id, manifest=summary, lifecycle=state,
                             pidfile=int(pid) if pid.isdigit() else None,
                             operations=operations, audit=records[-30:],
                             log=read_file(at(base + "/logs/app.log"), 32768, tail=True))
    report["read_errors"] = errors[:30]
    return redact(report)
