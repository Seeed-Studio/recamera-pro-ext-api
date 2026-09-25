"""Host tests for publish_app.py (no network, no ossutil; openssl CLI required)."""
from __future__ import annotations

import base64
import importlib.util
import io
import json
import os
import shutil
import subprocess
import tarfile

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


pa = _load("publish_app_under_test", os.path.join(_HERE, "publish_app.py"))
build_mod = _load("publish_app_build", os.path.join(_HERE, "build.py"))
contract = pa.contract

ICON_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmM"
    "IQAAAABJRU5ErkJggg==")


def manifest_v2(app_id, version, icon=True, extra=None):
    m = {
        "manifest_version": 2, "id": app_id, "name": app_id.title(),
        "version": version, "type": "self-hosted", "entry": "src/main.py",
        "description": f"{app_id} {extra or ''}".strip(),
        "release": {"sequence": 1, "channel": "stable"},
        "compatibility": {"platform_profile": contract.DEFAULT_PLATFORM_PROFILE,
                          "arch": "aarch64", "python": "==3.11.*"},
        "python": {"runtime_profile": "system-cp311-rknn232", "isolation": "per-release",
                   "wheels": [], "imports": []},
        "artifacts": [],
        "config_schema": {"revision": 1, "groups": []},
        "resources": {"claims": []},
        "permissions": {"sdk": [], "filesystem": {"read": ["app"], "write": ["appdata", "tmp"]},
                        "network": {"listen": [], "outbound": []}},
        "health": {"protocol": "kit-health-v1", "startup_timeout_sec": 30,
                   "stabilization_sec": 0, "liveness_interval_sec": 10,
                   "liveness_failures": 3,
                   "restart": {"policy": "on-failure", "max_attempts": 3,
                               "window_sec": 60, "backoff_sec": [1, 2]}},
        "instances": {"max": 1, "config_scope": "app", "data_scope": "app",
                      "endpoint_mode": "allocated"},
        "capabilities": [],
    }
    if icon:
        m["icon"] = {"path": "icon.png", "media_type": "image/png"}
    return m


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Keys, a dist with 2 live apps + 1 non-live app + stray sig, and a fake live catalog."""
    keys = tmp_path / "keys"
    keys.mkdir()
    priv, pub = str(keys / "priv.pem"), str(keys / "pub.pem")
    subprocess.run(["openssl", "ecparam", "-name", "prime256v1", "-genkey", "-noout",
                    "-out", priv], check=True, capture_output=True)
    subprocess.run(["openssl", "ec", "-in", priv, "-pubout", "-out", pub],
                   check=True, capture_output=True)

    work = tmp_path / "work"
    work.mkdir()

    def make_pkg(app_id, version, out_dir, icon=True, extra=None):
        src = work / f"{app_id}-{version}-{extra or 'x'}"
        (src / "src").mkdir(parents=True)
        (src / "src" / "main.py").write_text(f"# {app_id} {version} {extra}\n")
        if icon:
            (src / "icon.png").write_bytes(ICON_PNG)
        (src / "manifest.json").write_text(json.dumps(manifest_v2(app_id, version, icon, extra)))
        os.makedirs(out_dir, exist_ok=True)
        return build_mod.build(str(src), str(out_dir))

    dist = tmp_path / "dist"
    for app in ("alpha", "beta", "extra"):
        pa.sign.sign_one(make_pkg(app, "1.0.0", dist), priv)
    (dist / "alpha-0.9.0-arm64.tar.gz.sig").write_text("stray\n")

    # Fake live catalog: alpha + beta only (built with the real generator).
    live_dist = tmp_path / "live_dist"
    live_icons = tmp_path / "live_icons"
    live_dist.mkdir()
    live_icons.mkdir()
    for app in ("alpha", "beta"):
        for suf in ("", ".sig"):
            shutil.copy(dist / f"{app}-1.0.0-arm64.tar.gz{suf}", live_dist)
    (live_icons / "alpha.png").write_bytes(ICON_PNG)
    empty = tmp_path / "empty"
    empty.mkdir()
    nospec = str(tmp_path / "no-models.json")
    monkeypatch.setattr(pa.gen_catalog, "DEFAULT_MODELS_SPEC", nospec)
    live = pa.gen_catalog.build_catalog(str(live_dist), pa.PACKAGES_BASE,
                                        models_spec_path=nospec, runtimes_dir=str(empty),
                                        icons_dir=str(live_icons))
    live["runtimes"] = {"audio": {"url": "u", "sha256": "0" * 64}}
    raw = json.dumps(live, indent=2).encode()
    monkeypatch.setattr(pa, "fetch_live", lambda: raw)
    remote = {a["package"]["url"]: str(live_dist / a["package"]["filename"])
              for a in live["apps"]}
    monkeypatch.setattr(pa, "http_download", lambda url, dest: shutil.copy(remote[url], dest))

    def run(pkg, *extra_args):
        staging = tmp_path / f"staging{len(list(tmp_path.glob('staging*')))}"
        rc = pa.main([str(pkg), "--dist", str(dist), "--key", priv, "--pub", pub,
                      "--staging", str(staging), *extra_args])
        return rc, staging

    return {"dist": dist, "live": live, "make_pkg": make_pkg, "run": run, "priv": priv,
            "pub": pub, "out": tmp_path / "new", "tmp": tmp_path}


def staged_names(staging):
    return sorted(os.listdir(staging / "packages"))


def load_cat(staging):
    return json.loads((staging / "catalog.json").read_text())


def test_add_stages_only_live_packages(env):
    pkg = env["make_pkg"]("gamma", "0.1.0", env["out"])
    rc, st = env["run"](pkg, "--sign")
    assert rc == 0
    assert staged_names(st) == sorted([
        "alpha-1.0.0-arm64.tar.gz", "alpha-1.0.0-arm64.tar.gz.sig",
        "beta-1.0.0-arm64.tar.gz", "beta-1.0.0-arm64.tar.gz.sig",
        "gamma-0.1.0-arm64.tar.gz", "gamma-0.1.0-arm64.tar.gz.sig"])
    cat = load_cat(st)
    by = {a["id"]: a for a in cat["apps"]}
    assert set(by) == {"alpha", "beta", "gamma"}          # "extra" never published
    for a in env["live"]["apps"]:
        assert by[a["id"]] == a
    assert by["gamma"]["icon_url"] == pa.ICONS_BASE + "gamma.png"
    assert cat["runtimes"] == env["live"]["runtimes"]
    assert (st / "catalog.prev.json").read_bytes() == pa.fetch_live()
    assert not os.path.exists(pkg + ".sig")               # source never modified


def test_repo_shared_model_spec_does_not_affect_store_catalog(env, monkeypatch):
    # The repo's models.json may still list legacy shared models (with no
    # staged files); store packages are self-contained, so it must be ignored.
    spec = env["tmp"] / "repo-models.json"
    spec.write_text(json.dumps(
        {"beta": {"target_path": "/userdata/local/models/beta", "files": ["m.rknn"]}}))
    monkeypatch.setattr(pa.gen_catalog, "DEFAULT_MODELS_SPEC", str(spec))
    pkg = env["make_pkg"]("gamma", "0.1.0", env["out"])
    rc, st = env["run"](pkg, "--sign")
    assert rc == 0
    by = {a["id"]: a for a in load_cat(st)["apps"]}
    assert by["beta"]["models"] == [] and by["gamma"]["models"] == []


def test_download_when_local_copy_missing_or_different(env):
    os.unlink(env["dist"] / "beta-1.0.0-arm64.tar.gz")
    with open(env["dist"] / "alpha-1.0.0-arm64.tar.gz", "ab") as f:
        f.write(b"junk")
    pkg = env["make_pkg"]("gamma", "0.1.0", env["out"])
    rc, st = env["run"](pkg, "--sign")
    assert rc == 0
    assert "beta-1.0.0-arm64.tar.gz" in staged_names(st)


def test_download_hash_mismatch_refused(env, monkeypatch, tmp_path):
    os.unlink(env["dist"] / "beta-1.0.0-arm64.tar.gz")
    monkeypatch.setattr(pa, "http_download", lambda url, dest: open(dest, "wb").write(b"bad"))
    pkg = env["make_pkg"]("gamma", "0.1.0", env["out"])
    rc, _ = env["run"](pkg, "--sign")
    assert rc == 1


def test_same_version_different_bytes_refused(env):
    pkg = env["make_pkg"]("alpha", "1.0.0", env["out"], extra="changed")
    rc, st = env["run"](pkg, "--sign")
    assert rc == 1
    assert not (st / "catalog.json").exists()


def test_identical_live_package_is_noop(env):
    rc, st = env["run"](env["dist"] / "alpha-1.0.0-arm64.tar.gz")
    assert rc == 0
    assert not (st / "packages").exists()


def test_upgrade_replaces_old_version(env):
    pkg = env["make_pkg"]("alpha", "1.1.0", env["out"])
    rc, st = env["run"](pkg, "--sign")
    assert rc == 0
    names = staged_names(st)
    assert "alpha-1.0.0-arm64.tar.gz" not in names
    assert "alpha-1.1.0-arm64.tar.gz" in names
    by = {a["id"]: a for a in load_cat(st)["apps"]}
    assert by["alpha"]["version"] == "1.1.0"
    assert len(by) == 2


def test_downgrade_refused(env):
    pkg = env["make_pkg"]("alpha", "0.9.0", env["out"])
    rc, _ = env["run"](pkg, "--sign")
    assert rc == 1


def test_unsigned_refused_without_sign(env):
    pkg = env["make_pkg"]("gamma", "0.1.0", env["out"])
    rc, st = env["run"](pkg)
    assert rc == 1
    assert not (st / "catalog.json").exists()


def test_signature_from_other_key_refused(env, tmp_path):
    pkg = env["make_pkg"]("gamma", "0.1.0", env["out"])
    other = str(tmp_path / "other.pem")
    subprocess.run(["openssl", "ecparam", "-name", "prime256v1", "-genkey", "-noout",
                    "-out", other], check=True, capture_output=True)
    pa.sign.sign_one(pkg, other)
    rc, _ = env["run"](pkg)
    assert rc == 1


def test_existing_sidecar_verified_and_used(env):
    pkg = env["make_pkg"]("gamma", "0.1.0", env["out"])
    pa.sign.sign_one(pkg, env["priv"])
    rc, st = env["run"](pkg)
    assert rc == 0
    by = {a["id"]: a for a in load_cat(st)["apps"]}
    assert by["gamma"]["package"]["signature"] == open(pkg + ".sig").read().strip()


def test_catalog_diff_abort_when_live_entry_not_reproducible(env, monkeypatch):
    live = json.loads(pa.fetch_live())
    live["apps"][1]["description"] = "edited by hand on the CDN"
    raw = json.dumps(live).encode()
    monkeypatch.setattr(pa, "fetch_live", lambda: raw)
    pkg = env["make_pkg"]("gamma", "0.1.0", env["out"])
    rc, st = env["run"](pkg, "--sign")
    assert rc == 1
    assert not (st / "catalog.json").exists()


def test_check_diff_detects_missing_app(env):
    live = env["live"]
    new = json.loads(json.dumps(live))
    new["apps"] = [a for a in new["apps"] if a["id"] != "beta"]
    new["apps"].append({"id": "gamma"})
    with pytest.raises(pa.PublishError, match="beta: disappeared"):
        pa.check_diff(live, new, "gamma")


def _raw_tar(path, members):
    with tarfile.open(path, "w:gz") as tar:
        for info, data in members:
            tar.addfile(info, io.BytesIO(data) if data is not None else None)


def test_unsafe_members_refused(tmp_path):
    bad = tmp_path / "bad.tar.gz"
    info = tarfile.TarInfo("../evil.py")
    info.size = 1
    _raw_tar(bad, [(info, b"x")])
    with pytest.raises(pa.PublishError, match="unsafe member path"):
        pa.inspect_package(str(bad))
    link = tmp_path / "link.tar.gz"
    info = tarfile.TarInfo("app.py")
    info.type = tarfile.SYMTYPE
    info.linkname = "/etc/passwd"
    _raw_tar(link, [(info, None)])
    with pytest.raises(pa.PublishError, match="link member"):
        pa.inspect_package(str(link))


def test_bad_icon_override_refused(env, tmp_path):
    fake = tmp_path / "icon.png"
    fake.write_bytes(b"GIF89a not a png")
    pkg = env["make_pkg"]("gamma", "0.1.0", env["out"])
    rc, _ = env["run"](pkg, "--sign", "--icon", str(fake))
    assert rc == 1


def test_yes_uploads_in_order_and_verifies(env, monkeypatch):
    store, calls = {}, []

    def fake_ossutil(*args):
        calls.append(args)
        _, _, src, dst = args
        if dst.startswith("oss://"):
            store[dst] = open(src, "rb").read()
        else:
            open(dst, "wb").write(store[src])

    monkeypatch.setattr(pa, "ossutil", fake_ossutil)
    monkeypatch.setattr(pa, "wait_cdn", lambda entry: True)
    pkg = env["make_pkg"]("gamma", "0.1.0", env["out"])
    rc, _ = env["run"](pkg, "--sign", "--yes")
    assert rc == 0
    uploads = [c[3] for c in calls if c[3].startswith("oss://")]
    assert uploads == [f"{pa.OSS_BASE}/packages/gamma-0.1.0-arm64.tar.gz",
                       f"{pa.OSS_BASE}/icons/gamma.png",
                       f"{pa.OSS_BASE}/catalog.json"]


def test_yes_stops_before_catalog_on_verify_failure(env, monkeypatch):
    uploaded = []

    def fake_ossutil(*args):
        _, _, src, dst = args
        if dst.startswith("oss://"):
            uploaded.append(dst)
        else:
            open(dst, "wb").write(b"corrupted")

    monkeypatch.setattr(pa, "ossutil", fake_ossutil)
    pkg = env["make_pkg"]("gamma", "0.1.0", env["out"])
    rc, _ = env["run"](pkg, "--sign", "--yes")
    assert rc == 1
    assert uploaded == [f"{pa.OSS_BASE}/packages/gamma-0.1.0-arm64.tar.gz"]
