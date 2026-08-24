"""voiceruntime.py -- on-demand audio runtime for apps with capabilities:["audio"].

Motivation (INSTALL_ASSETS_SPEC §3): voice-transcribe needs five aarch64/cp311
wheels (sherpa_onnx_core, sherpa_onnx, sentencepiece, kaldi_native_fbank,
voxedge, ~18 MB). Putting them in the kit package would make all nine vision apps
re-download 18 MB they never import on every kit update, so the bundle ships
separately and is installed only when an audio app is installed. Without it the
app installs fine and then dies with `ModuleNotFoundError: No module named
'voxedge'`.

Mechanism is deliberately the one that already exists -- release/kit-extra
INSTALL.sh's `pip install --no-index --find-links <wheels>` into /userdata/rknnenv
(the device has no network). The bundle reaches the device through the same
browser-relayed POST /api/appMgr/upload used for app packages, so nothing new is
introduced on the transport side either.

Two properties the spec calls out:

  * PRESENCE IS AN IMPORT, NOT A FILE LISTING. `.dist-info` on disk proves a wheel
    was unpacked, not that it loads: sherpa_onnx pulls a native libonnxruntime out
    of sherpa_onnx_core, and a wheel built for the wrong ABI unpacks perfectly and
    then fails at import. So the check runs `import voxedge, sherpa_onnx` inside
    the TARGET venv's interpreter, as a subprocess.
  * FAILURE NAMES THE PACKAGE. The probe reports which module failed and with what
    error, and install() surfaces pip's own stderr tail, because "runtime install
    failed" on a headless device costs an SSH session to turn into a fact.

Idempotent: install() probes first and returns already_present without running
pip.

Second shape -- FILES (RUNTIME_BUNDLE_SPEC §1/§2)
------------------------------------------------
The GStreamer RK hardware codec runtime distributes three `.so` files and three
environment variables, not wheels. Registry entries therefore carry a `kind`:

  * kind "wheels" (the DEFAULT, so the existing `voice` entry is untouched):
    install = offline pip into the venv, presence = import in that venv.
  * kind "files": install = unpack into `dest` (which must live under /userdata,
    checked with paths.is_within), presence = the `probe` command exits 0, and
    the entry additionally declares `env` that supervisor merges into the process
    environment of apps that DECLARE the matching capability.

Presence stays a real check in both shapes: a `.so` copied into place proves
nothing (wrong ABI, missing dependency, stale GStreamer registry all leave the
file exactly where it should be), so `gst-inspect-1.0 mppvideodec` is what
decides, not os.path.exists.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tarfile
import tempfile

from . import installer, paths, signing

# Runtime registry. `modules` is what must import; `packages` is what pip is asked
# for (pip project names, resolved offline from the bundle's wheels/ dir). The two
# differ on purpose: sherpa_onnx_core is a dependency that has no import of its own
# worth probing, and sentencepiece/kaldi_native_fbank are pulled in by sherpa_onnx.
RUNTIMES = {
    "voice": {
        # Canonical name, echoed back regardless of whether the caller asked by
        # runtime name ("voice") or by capability ("audio") -- see _spec().
        "name": "voice",
        "modules": ["voxedge", "sherpa_onnx"],
        "packages": ["voxedge", "sherpa-onnx", "sherpa-onnx-core",
                     "sentencepiece", "kaldi-native-fbank"],
        "capability": "audio",
        "about": "SenseVoice ASR runtime (sherpa-onnx + voxedge), aarch64/cp311",
    },
    # File-shaped runtime: GStreamer RK MPP hardware DECODE. No `kind` on the
    # entry above on purpose -- absence means "wheels", so adding this one cannot
    # change how the audio runtime behaves.
    #
    # Scope note (RUNTIME_BUNDLE_SPEC §6): the bundle also carries the ENCODER
    # elements (mpph264enc/mpph265enc) because they live in the same plugin .so,
    # but encoding is UNVERIFIED -- it contends with rkipc for the VEPU and has
    # not been tested. Only decode (mppvideodec) is probed and claimed.
    "hwcodec": {
        "name": "hwcodec",
        "capability": "hwcodec",
        "kind": "files",
        # Unpack target. Must be inside /userdata (validated via _files_dest);
        # everything else on the device is either read-only or wiped by OTA.
        "dest": "/userdata/lib",
        # Presence = this exits 0. NOT "the .so is on disk": a plugin built
        # against the wrong glibc/gst ABI, or one whose libgstcodecparsers
        # dependency is missing, sits there and reports MISSING to GStreamer.
        "probe": ["gst-inspect-1.0", "mppvideodec"],
        # Injected by supervisor into apps declaring capabilities:["hwcodec"].
        "env": {
            "GST_PLUGIN_PATH": {"append": "/userdata/lib/gstreamer-1.0"},
            # APPEND, never set: `export LD_LIBRARY_PATH=/userdata/lib` wipes the
            # system-first /usr/lib:/oem/usr/lib:/oem/lib base and
            # librockchip_mpp.so.1 stops resolving immediately (observed, not
            # theoretical).
            "LD_LIBRARY_PATH": {"append": "/userdata/lib"},
            # SET: the default registry path may be unwritable, and a stale cache
            # lies (plugin in place, still reported MISSING).
            "GST_REGISTRY": {"set": "/userdata/gst-registry.bin"},
        },
        "about": "RK MPP hardware H.264/H.265 decode for GStreamer "
                 "(aarch64, gst 1.22.6). Encoders ship but are UNVERIFIED.",
    },
}

# Containment root for file-shaped runtimes' `dest`. Env-overridable only so the
# unit tests can build a whole fake /userdata in a temp dir.
USERDATA_ROOT = os.environ.get("APPMGR_USERDATA_ROOT", "/userdata")

# Runtime probes need system ABI libraries first while retaining OEM-only RK
# libraries. INSTALL.sh's own self-check uses the same safe order.
LD_LIBRARY_PATH = os.environ.get(
    "APPMGR_RUNTIME_LD_PATH", "/usr/lib:/oem/usr/lib:/oem/lib")
PROBE_TIMEOUT = int(os.environ.get("APPMGR_RUNTIME_PROBE_TIMEOUT", "60"))
INSTALL_TIMEOUT = int(os.environ.get("APPMGR_RUNTIME_INSTALL_TIMEOUT", "900"))


class RuntimeError_(Exception):
    """Runtime provisioning failure (named to avoid shadowing the builtin)."""


def _spec(name: str) -> dict:
    """Resolve a runtime by its own name OR by the capability it provides.

    The store reaches this endpoint from the other end: it sees an app declaring
    `capabilities: ["audio"]` and asks whether that is covered. Requiring it to
    know that "audio" is served by the runtime called "voice" would put a
    capability->runtime table in the browser, where it would silently rot the
    next time a runtime is added here. Accepting both spellings keeps that
    mapping in one place -- this registry, which already declares it.
    """
    key = (name or "").strip().lower()
    spec = RUNTIMES.get(key)
    if spec is None:
        for cand in RUNTIMES.values():
            if cand.get("capability") == key:
                return cand
    if spec is None:
        known = sorted(set(RUNTIMES) |
                       {c["capability"] for c in RUNTIMES.values() if c.get("capability")})
        raise ValueError(f"unknown runtime {name!r}: known {known}")
    return spec


def venv_python() -> str:
    return os.path.join(paths.RKNNENV_DIR, "bin", "python3")


def venv_pip() -> str:
    return os.path.join(paths.RKNNENV_DIR, "bin", "pip")


def _probe_env() -> dict:
    env = dict(os.environ)
    env["LD_LIBRARY_PATH"] = LD_LIBRARY_PATH + ":" + env.get("LD_LIBRARY_PATH", "")
    return env


_PROBE_SRC = """
import json, sys
missing = []
for m in {modules!r}:
    try:
        __import__(m)
    except BaseException as e:
        missing.append({{"module": m, "error": "%s: %s" % (type(e).__name__, e)}})
sys.stdout.write(json.dumps({{"missing": missing, "python": sys.executable}}))
"""


def kind_of(spec: dict) -> str:
    """Registry shape of an entry. Absent `kind` == "wheels" (§2)."""
    return spec.get("kind", "wheels")


def status(name: str = "voice") -> dict:
    """Is the runtime usable? Dispatches on the entry's `kind`.

    Returns {name, capability, about, present, missing: [...], ...}. Never raises
    for "not installed yet" in either shape -- that is `present: false` with a
    reason, since a fresh device legitimately has neither the venv nor the .so.
    """
    spec = _spec(name)
    if kind_of(spec) == "files":
        return _status_files(spec)
    return _status_wheels(spec)


def _status_wheels(spec: dict) -> dict:
    """Is the runtime importable in the target venv?

    Returns {name, venv, present, missing: [{module, error}], ...}. Never raises
    for a missing venv/interpreter -- that is just `present: false` with a reason,
    since "the venv was never created" is a normal fresh-device state the front
    end handles the same way as "the wheels are not installed".
    """
    out = {
        "name": spec["name"],
        "kind": "wheels",
        "capability": spec.get("capability"),
        "about": spec["about"],
        "venv": paths.RKNNENV_DIR,
        "modules": list(spec["modules"]),
        "present": False,
        "missing": [],
    }
    py = venv_python()
    if not os.path.isfile(py):
        out["missing"] = [{"module": m, "error": f"venv interpreter missing: {py}"}
                          for m in spec["modules"]]
        out["error"] = f"venv interpreter missing: {py}"
        return out
    try:
        proc = subprocess.run(
            [py, "-c", _PROBE_SRC.format(modules=list(spec["modules"]))],
            capture_output=True, timeout=PROBE_TIMEOUT, env=_probe_env())
    except (OSError, subprocess.SubprocessError) as e:
        out["error"] = f"probe failed to run: {e!r}"
        out["missing"] = [{"module": m, "error": repr(e)} for m in spec["modules"]]
        return out
    text = (proc.stdout or b"").decode("utf-8", "replace").strip()
    try:
        parsed = json.loads(text)
    except ValueError:
        out["error"] = ("probe produced no usable output; stderr="
                        + (proc.stderr or b"").decode("utf-8", "replace")[-500:])
        out["missing"] = [{"module": m, "error": "probe output unparsable"}
                          for m in spec["modules"]]
        return out
    out["missing"] = parsed.get("missing", [])
    out["present"] = not out["missing"]
    return out


def _find_wheel_dir(root: str) -> str:
    """Locate the bundle's wheels/ dir (top level or one level down)."""
    direct = os.path.join(root, "wheels")
    if os.path.isdir(direct):
        return direct
    for entry in sorted(os.listdir(root)):
        cand = os.path.join(root, entry, "wheels")
        if os.path.isdir(cand):
            return cand
    # A bundle that is just a flat pile of .whl files is acceptable too.
    if any(f.endswith(".whl") for f in os.listdir(root)):
        return root
    raise RuntimeError_(
        "runtime bundle contains no wheels/ directory and no .whl files")


def _verify_and_extract(pkg_path: str, signature, dest_dir: str) -> list:
    """Verify a runtime bundle's release signature and unpack it into `dest_dir`.

    Authenticity parity with app install (C1): a runtime bundle is "deliver root
    code to a SHARED tree" (/userdata/rknnenv, /userdata/lib) exactly as an app
    package is, so it goes through the SAME detached-signature check
    (signing.verify_package) under the SAME policy (paths.REQUIRE_SIGNATURE ->
    unsigned refused by default; a bad signature always refused). This runs in
    addition to installer.validate_pkg_path + the per-member zip-slip vetting,
    not instead of them.

    TOCTOU binding (C12): the package is opened ONCE and both the signature
    check and the extraction read that single open file description -- we never
    verify one path and then re-open the path to unpack (which is where a swap
    could slip an unsigned tar past a path-only check). On Linux the signature
    is verified over /proc/self/fd/<n>, the same inode we extract from; where
    /proc is absent (dev/CI) we fall back to verifying the real path and then
    assert fstat(fd) still names the inode we verified before unpacking it.
    """
    real = installer.validate_pkg_path(pkg_path)
    fd = os.open(real, os.O_RDONLY)
    try:
        st = os.fstat(fd)
        proc = "/proc/self/fd/%d" % fd
        verify_path = proc if os.path.exists(proc) else real
        # Sidecar `<pkg>.sig` is keyed on the REAL path, not the /proc alias.
        sig_b64 = signing.load_signature(real, signature)
        try:
            signing.verify_package(verify_path, sig_b64)
        except signing.SignatureError as e:
            raise RuntimeError_(f"runtime bundle signature rejected: {e}")
        if verify_path is real:
            st2 = os.stat(real)
            if (st2.st_dev, st2.st_ino) != (st.st_dev, st.st_ino):
                raise RuntimeError_(
                    "runtime bundle changed on disk between verify and extract")
        os.lseek(fd, 0, os.SEEK_SET)
        with os.fdopen(fd, "rb", closefd=True) as fobj:
            fd = None  # ownership passes to fobj; do not double-close
            with tarfile.open(fileobj=fobj, mode="r:gz") as tar:
                return installer.extract_vetted_tar(tar, dest_dir)
    finally:
        if fd is not None:
            os.close(fd)


def install(name: str = "voice", pkg_path: str = None, signature=None) -> dict:
    """Install a runtime bundle. Idempotent. Dispatches on the entry's `kind`.

    `signature` is the detached base64 release signature (from the catalog's
    runtime descriptor, forwarded by POST /api/appMgr/runtime); when absent the
    `<pkg>.sig` sidecar is consulted, and policy decides whether an unsigned
    bundle is refused (paths.REQUIRE_SIGNATURE, default on).
    """
    spec = _spec(name)
    if kind_of(spec) == "files":
        return _install_files(spec, pkg_path, signature)
    return _install_wheels(spec, pkg_path, signature)


def _install_wheels(spec: dict, pkg_path: str = None, signature=None) -> dict:
    """Install a runtime bundle into the venv, offline. Idempotent.

    `pkg_path` is a device path to voice-runtime-<ver>.tar.gz, normally the value
    POST /api/appMgr/upload just returned; it goes through the app installer's own
    path gate, release-signature verification and member vetting
    (_verify_and_extract), so an unsigned/forged bundle is refused and a hostile
    bundle cannot write outside the temp dir.

    Returns the post-install status() dict plus {installed, already_present}.
    Raises RuntimeError_ naming the failing step / the modules still missing.
    """
    name = spec["name"]
    before = _status_wheels(spec)
    if before["present"]:
        before["installed"] = False
        before["already_present"] = True
        return before

    if not pkg_path:
        raise ValueError(
            f"runtime {name!r} is not installed and no bundle path was given "
            "(upload voice-runtime-<ver>.tar.gz via /api/appMgr/upload first); "
            "missing: " + ", ".join(m["module"] for m in before["missing"]))
    # Cheap path/root/size gate first (unchanged ordering); the signature check
    # and the bound extraction happen together in _verify_and_extract below.
    installer.validate_pkg_path(pkg_path)

    pip = venv_pip()
    if not os.path.isfile(pip):
        raise RuntimeError_(
            f"venv {paths.RKNNENV_DIR} has no pip ({pip}) -- run "
            "release/kit-extra/INSTALL.sh to create the runtime venv first")

    work = tempfile.mkdtemp(prefix=".runtime.", dir=paths.ensure_appstage())
    try:
        _verify_and_extract(pkg_path, signature, work)
        wheels = _find_wheel_dir(work)
        # --no-deps is deliberate, and it is NOT "skip the checks".
        #
        # The bundle already ships the whole closure, so there is nothing for pip
        # to resolve. What pip WOULD do is enforce voxedge's `numpy>=1.24` floor
        # against the venv's numpy 1.23.5 and abort -- and satisfying that floor
        # is the wrong move: /userdata/rknnenv is SHARED, every vision app runs
        # rknn-toolkit-lite2 out of it, and 1.23.5 is the version that toolchain
        # ships with. Upgrading numpy to enable one optional voice app risks the
        # nine that already work. (The floor is also not real: voxedge touches
        # only long-standing numpy APIs -- array/float32/hanning/fft/linalg -- and
        # nothing introduced after 1.23.)
        #
        # The check that replaces pip's is stronger anyway: status() below
        # actually imports voxedge and sherpa_onnx in the target interpreter, so
        # a genuinely missing dependency surfaces as a named ImportError instead
        # of a metadata assertion. Install is only reported successful if that
        # import passes.
        cmd = [pip, "install", "--no-index", "--no-deps",
               "--find-links", wheels] + spec["packages"]
        try:
            proc = subprocess.run(cmd, capture_output=True,
                                  timeout=INSTALL_TIMEOUT, env=_probe_env())
        except (OSError, subprocess.SubprocessError) as e:
            raise RuntimeError_(f"pip install failed to run: {e!r}")
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or b"").decode("utf-8", "replace")[-800:]
            raise RuntimeError_(
                f"pip install returned {proc.returncode} for "
                f"{' '.join(spec['packages'])} from {wheels}:\n{tail}")
    finally:
        shutil.rmtree(work, ignore_errors=True)

    after = _status_wheels(spec)
    if not after["present"]:
        detail = "; ".join(f"{m['module']}: {m['error']}" for m in after["missing"])
        raise RuntimeError_(
            f"runtime {name!r} still incomplete after install -- {detail}")
    after["installed"] = True
    after["already_present"] = False
    return after


# ---- file-shaped runtimes (RUNTIME_BUNDLE_SPEC §2/§3) ----------------------- #
def _files_dest(spec: dict) -> str:
    """The validated unpack target of a `kind: "files"` entry.

    `dest` decides where a downloaded archive is written, so it is the one field
    an attacker (or a typo) could turn into "overwrite /lib". It must be absolute
    and inside USERDATA_ROOT, checked with paths.is_within -- the same predicate
    the installer's zip-slip vetting uses, rather than a second startswith() that
    would let /userdata-evil through.
    """
    dest = spec.get("dest")
    if not isinstance(dest, str) or not dest or not os.path.isabs(dest):
        raise RuntimeError_(
            f"runtime {spec['name']!r}: dest must be an absolute path, got {dest!r}")
    real = os.path.normpath(dest)
    root = os.path.normpath(USERDATA_ROOT)
    if not paths.is_within(real, root):
        raise RuntimeError_(
            f"runtime {spec['name']!r}: dest {dest!r} escapes {root} -- refused")
    return real


def merge_env(env: dict, env_spec: dict) -> dict:
    """Apply one runtime's `env` block to `env`, in place.

    Two semantics, deliberately not one (§2):
      * "set"    -- assign. GST_REGISTRY needs this: a stale cache reports a
                    present plugin as MISSING, and the default path may not be
                    writable.
      * "append" -- add to the end of the existing value, keeping what is already
                    there. LD_LIBRARY_PATH can ONLY be done this way: assigning
                    /userdata/lib drops the device's ordered
                    /usr/lib:/oem/usr/lib:/oem/lib base and
                    librockchip_mpp.so.1 stops resolving.
    Appends are DEDUPED, so restarting an app repeatedly cannot grow the variable
    without bound (each start inherits the appmgr environment afresh, but a
    caller re-applying the same spec twice must be a no-op the second time).
    """
    for var, rule in (env_spec or {}).items():
        if not isinstance(rule, dict):
            raise RuntimeError_(f"env rule for {var} must be a dict, got {rule!r}")
        if "set" in rule:
            env[var] = str(rule["set"])
        elif "append" in rule:
            parts = [p for p in env.get(var, "").split(os.pathsep) if p]
            for piece in str(rule["append"]).split(os.pathsep):
                if piece and piece not in parts:
                    parts.append(piece)
            env[var] = os.pathsep.join(parts)
        else:
            raise RuntimeError_(
                f"env rule for {var} has neither 'set' nor 'append': {rule!r}")
    return env


def apply_runtime_env(env: dict, capabilities) -> list:
    """Merge the env of every runtime this app declares AND that is present.

    Called by supervisor.start() with the environment it is about to hand the
    child. Three rules from §3:
      * only apps that DECLARE the capability get the variables -- a vision app
        that never touches GStreamer keeps the environment it has today;
      * a runtime that is NOT provisioned injects nothing and does not stop the
        app from starting (the app decides whether to degrade or complain);
      * unknown capability strings are ignored, not fatal -- manifests are
        author-supplied and a future capability name must not brick an install.
    Returns the names of the runtimes whose env was applied.
    """
    applied = []
    if not capabilities:
        return applied
    if isinstance(capabilities, str):
        capabilities = [capabilities]
    for cap in capabilities:
        try:
            spec = _spec(cap)
        except ValueError:
            continue
        if not spec.get("env"):
            continue
        try:
            if not status(spec["name"])["present"]:
                continue
        except Exception:
            continue
        merge_env(env, spec["env"])
        applied.append(spec["name"])
    return applied


def _files_probe_env(spec: dict) -> dict:
    """Environment the presence probe runs under: the runtime's own env applied.

    Without it `gst-inspect-1.0 mppvideodec` would look only at the system plugin
    path and report MISSING for a perfectly installed bundle -- the probe has to
    see exactly what the app process will see.
    """
    env = _probe_env()
    merge_env(env, spec.get("env") or {})
    return env


def _list_dest(dest: str) -> list:
    """Flat inventory of `dest` (relative path + size) for failure messages."""
    out = []
    for root, dirs, files in os.walk(dest):
        dirs.sort()
        for fn in sorted(files):
            p = os.path.join(root, fn)
            try:
                size = os.path.getsize(p)
            except OSError:
                size = -1
            out.append({"file": os.path.relpath(p, dest), "size": size})
    return out


def _status_files(spec: dict) -> dict:
    """Presence of a file-shaped runtime = the probe command exits 0.

    Explicitly NOT a file listing (§5): replacing libgstrockchipmpp.so with an
    empty file leaves the inventory identical and gst-inspect fails -- which is
    the answer that matters, because that is exactly what the app will hit.
    """
    dest = _files_dest(spec)
    probe = list(spec.get("probe") or [])
    out = {
        "name": spec["name"],
        "kind": "files",
        "capability": spec.get("capability"),
        "about": spec["about"],
        "dest": dest,
        "probe": probe,
        "env": spec.get("env") or {},
        "present": False,
        "missing": [],
        "files": _list_dest(dest),
    }
    if not probe:
        raise RuntimeError_(f"runtime {spec['name']!r}: kind 'files' needs a probe")
    try:
        proc = subprocess.run(probe, capture_output=True, timeout=PROBE_TIMEOUT,
                              env=_files_probe_env(spec))
    except FileNotFoundError:
        # gst-inspect-1.0 itself is not on the device: nothing to fall back on,
        # and saying so beats "runtime unavailable".
        out["error"] = f"probe binary not found: {probe[0]}"
        out["missing"] = [{"probe": " ".join(probe), "error": out["error"]}]
        return out
    except (OSError, subprocess.SubprocessError) as e:
        out["error"] = f"probe failed to run: {e!r}"
        out["missing"] = [{"probe": " ".join(probe), "error": repr(e)}]
        return out
    if proc.returncode == 0:
        out["present"] = True
        return out
    tail = (proc.stderr or proc.stdout or b"").decode("utf-8", "replace")[-500:].strip()
    out["error"] = f"`{' '.join(probe)}` exited {proc.returncode}: {tail}"
    out["missing"] = [{"probe": " ".join(probe), "error": out["error"]}]
    return out


def _find_files_payload(root: str) -> str:
    """Locate the bundle's `files/` tree (top level or one level down).

    The tree MIRRORS dest, so the bundle -- not this code -- decides that the
    plugin goes to gstreamer-1.0/ and the parser library to the dest root.
    """
    direct = os.path.join(root, "files")
    if os.path.isdir(direct):
        return direct
    for entry in sorted(os.listdir(root)):
        cand = os.path.join(root, entry, "files")
        if os.path.isdir(cand):
            return cand
    raise RuntimeError_(
        f"runtime bundle has no files/ directory (looked in {root} and one level "
        f"below; found: {sorted(os.listdir(root))})")


def _copy_tree_named(src: str, dest: str) -> list:
    """Copy src/** into dest, reporting the exact file that fails.

    "install failed" on a headless device costs an SSH session; "failed copying
    gstreamer-1.0/libgstrockchipmpp.so: [Errno 28] No space left on device" does
    not.
    """
    copied = []
    for root, dirs, files in os.walk(src):
        dirs.sort()
        rel = os.path.relpath(root, src)
        target_dir = dest if rel == "." else os.path.join(dest, rel)
        try:
            os.makedirs(target_dir, exist_ok=True)
        except OSError as e:
            raise RuntimeError_(f"failed creating {target_dir}: {e}")
        for fn in sorted(files):
            s = os.path.join(root, fn)
            d = os.path.join(target_dir, fn)
            try:
                shutil.copyfile(s, d)
                os.chmod(d, 0o755)
            except OSError as e:
                raise RuntimeError_(
                    f"failed copying {os.path.relpath(s, src)} -> {d}: {e}")
            copied.append(os.path.relpath(d, dest))
    return copied


def _install_files(spec: dict, pkg_path: str = None, signature=None) -> dict:
    """Unpack a file-shaped runtime bundle into `dest`. Idempotent.

    Nothing goes into the venv: these are native GStreamer plugins loaded by the
    dynamic linker, and the venv has no say in that. The bundle takes the same
    gate as an app package (release-signature verification + path gate + member
    vetting, all in _verify_and_extract), so an unsigned/forged archive is
    refused and a hostile one cannot write outside the staging dir; only the
    vetted payload is then copied into the validated dest.
    """
    name = spec["name"]
    before = _status_files(spec)
    if before["present"]:
        before["installed"] = False
        before["already_present"] = True
        return before

    if not pkg_path:
        raise ValueError(
            f"runtime {name!r} is not installed and no bundle path was given "
            f"(upload gst-hwcodec-<ver>.tar.gz via /api/appMgr/upload first); "
            f"probe says: {before.get('error', 'not present')}")
    installer.validate_pkg_path(pkg_path)
    dest = _files_dest(spec)

    work = tempfile.mkdtemp(prefix=".runtime.", dir=paths.ensure_appstage())
    try:
        _verify_and_extract(pkg_path, signature, work)
        payload = _find_files_payload(work)
        os.makedirs(dest, exist_ok=True)
        copied = _copy_tree_named(payload, dest)
        if not copied:
            raise RuntimeError_(
                f"runtime bundle's files/ tree is empty -- nothing to install "
                f"into {dest}")
    finally:
        shutil.rmtree(work, ignore_errors=True)

    after = _status_files(spec)
    after["copied"] = copied
    if not after["present"]:
        inventory = ", ".join(f"{f['file']} ({f['size']} B)" for f in after["files"])
        raise RuntimeError_(
            f"runtime {name!r} unpacked {len(copied)} file(s) into {dest} but is "
            f"still not usable -- {after.get('error')}; on disk: {inventory}")
    after["installed"] = True
    after["already_present"] = False
    return after
