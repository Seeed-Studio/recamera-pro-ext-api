"""Tests for the app packager (market/packaging/build.py).

Regression guard for the DX P0 "silent multi-file drop" bug: the launcher lets
an app import sibling helper modules / ship templates + data, but the old packer
only collected a fixed whitelist (manifest/app.py/models/hooks/run/icon), so
those extra files ran on the dev machine yet vanished from the shipped package.
The packer now collects the WHOLE app tree minus a junk deny-list.
"""
import gzip
import importlib.util
import io
import json
import os
import tarfile

_HERE = os.path.dirname(os.path.abspath(__file__))


def _load_build():
    spec = importlib.util.spec_from_file_location(
        "packaging_build", os.path.join(_HERE, "build.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


build_mod = _load_build()


def _make_app(root, *, manifest=None, files=None, dirs=None):
    os.makedirs(root, exist_ok=True)
    m = {"id": "demo-app", "version": "1.0.0", "entry": "app.py"}
    if manifest:
        m.update(manifest)
    with open(os.path.join(root, "manifest.json"), "w") as f:
        json.dump(m, f)
    with open(os.path.join(root, "app.py"), "w") as f:
        f.write("# entry\n")
    for rel, content in (files or {}).items():
        p = os.path.join(root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True) if os.path.dirname(rel) else None
        with open(p, "w") as f:
            f.write(content)
    for d in (dirs or []):
        os.makedirs(os.path.join(root, d), exist_ok=True)
    return root


def _members(root, exclude=()):
    return sorted(arc for _, arc in build_mod._members(root, exclude))


def test_whole_tree_is_packed(tmp_path):
    """Sibling helper module, nested package module, template and data asset --
    all of which the old whitelist dropped -- are now packed."""
    root = _make_app(str(tmp_path / "app"), files={
        "helper.py": "# sibling\n",
        "lib/__init__.py": "",
        "lib/util.py": "# nested\n",
        "templates/report.j2": "{{ x }}\n",
        "data/labels.txt": "cat\ndog\n",
        "models/m.rknn": "BINARY",
    })
    mem = _members(root)
    for expected in ("app.py", "manifest.json", "helper.py", "lib/__init__.py",
                     "lib/util.py", "templates/report.j2", "data/labels.txt",
                     "models/m.rknn"):
        assert expected in mem, expected


def test_junk_and_build_products_excluded(tmp_path):
    """__pycache__/.pyc/.DS_Store/hidden/run.pid/*.log and a stray kit/ copy are
    never packed (reverse verification of the deny-list)."""
    root = _make_app(str(tmp_path / "app"), files={
        "helper.py": "# keep\n",
        "helper.pyc": "x",
        "run.pid": "123",
        "debug.log": "log",
        ".DS_Store": "x",
        ".gitignore": "x",
        "__pycache__/app.cpython-311.pyc": "x",
        "lib/util.py": "# keep\n",
        "lib/__pycache__/util.pyc": "x",
        ".git/config": "x",
        "kit/app.py": "# stray\n",
    })
    mem = _members(root)
    assert "helper.py" in mem and "lib/util.py" in mem
    for junk in ("helper.pyc", "run.pid", "debug.log", ".DS_Store", ".gitignore",
                 "__pycache__/app.cpython-311.pyc", "lib/__pycache__/util.pyc",
                 ".git/config", "kit/app.py"):
        assert junk not in mem, junk
    # No path component may be an excluded dir either.
    assert not any(part in build_mod.EXCLUDE_DIRS
                   for arc in mem for part in arc.split("/"))


def test_manifest_package_exclude_globs(tmp_path):
    """manifest.package.exclude prunes app-specific dev-only trees/files."""
    root = _make_app(
        str(tmp_path / "app"),
        manifest={"package": {"exclude": ["evaluation", "tools", "test_*.py",
                                          "*.md"]}},
        files={
            "helper.py": "# keep\n",
            "README.md": "# doc\n",
            "test_app.py": "# test\n",
            "evaluation/run.json": "{}",
            "tools/train.py": "# tool\n",
        })
    mem = _members(root, ("evaluation", "tools", "test_*.py", "*.md"))
    assert "helper.py" in mem
    for pruned in ("README.md", "test_app.py", "evaluation/run.json",
                   "tools/train.py"):
        assert pruned not in mem, pruned


def test_shipped_sample_icons_survive_package_excludes():
    """Every sample's declared icon is selected by the real packager walk."""
    repo_root = os.path.dirname(os.path.dirname(_HERE))
    apps_root = os.path.join(repo_root, "apps")
    manifests = sorted(
        os.path.join(apps_root, name, "manifest.json")
        for name in os.listdir(apps_root)
        if os.path.isfile(os.path.join(apps_root, name, "manifest.json"))
    )
    assert len(manifests) >= 9, manifests
    for manifest_path in manifests:
        with open(manifest_path, encoding="utf-8") as source:
            manifest = json.load(source)
        app_root = os.path.dirname(manifest_path)
        exclude = (manifest.get("package") or {}).get("exclude") or ()
        icon_path = manifest["icon"]["path"]
        assert icon_path in _members(app_root, exclude), manifest["id"]


def test_build_is_deterministic(tmp_path):
    """Same input bytes -> identical package bytes (catalog checksum stability)."""
    root = _make_app(str(tmp_path / "app"), files={
        "helper.py": "# sibling\n", "models/m.rknn": "BINARY"})
    out1 = build_mod.build(root, str(tmp_path / "d1"))
    out2 = build_mod.build(root, str(tmp_path / "d2"))
    assert open(out1, "rb").read() == open(out2, "rb").read()
    # And the tarball actually contains the sibling helper.
    raw = gzip.decompress(open(out1, "rb").read())
    names = tarfile.open(fileobj=io.BytesIO(raw)).getnames()
    assert "helper.py" in names
