#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["packaging==24.2", "python-dotenv==1.1.1", "pip==25.0.1"]
# ///
"""Build the Workflow app from its pinned engine, never from a moving branch."""

import argparse
import contextlib
import json
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent


def read_lock(path):
    lock = json.loads(Path(path).read_text())
    if (
        lock.get("schema_version") != 1
        or not re.fullmatch(r"[0-9a-f]{40}", lock.get("commit", ""))
        or not lock.get("repository", "").startswith("https://github.com/")
        or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", lock.get("wheel_version", ""))
    ):
        raise ValueError("Invalid engine source lock")
    return lock


def git(root, *args):
    return subprocess.check_output(
        ["git", "-C", str(root), *args], text=True
    ).strip()


@contextlib.contextmanager
def export_engine(lock, cache, local_source=None):
    """Export only committed files; untracked local files cannot enter wheels."""
    commit = lock["commit"]
    cache = Path(cache).resolve()
    cache.mkdir(parents=True, exist_ok=True)
    if local_source is not None:
        repo = Path(local_source).resolve()
        if git(repo, "rev-parse", "HEAD") != commit:
            raise ValueError("Local engine HEAD differs from engine-source.lock.json")
        if git(repo, "status", "--porcelain", "--untracked-files=no"):
            raise ValueError("Local engine has uncommitted tracked changes")
    else:
        repo = cache / "engine.git"
        if not repo.exists():
            subprocess.run(["git", "init", "--bare", str(repo)], check=True)
        exists = subprocess.run(
            ["git", "-C", str(repo), "cat-file", "-e", commit + "^{commit}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        if exists.returncode:
            subprocess.run(
                ["git", "-C", str(repo), "fetch", "--depth=1", "--no-tags",
                 lock["repository"], commit], check=True,
            )
    if git(repo, "rev-parse", commit + "^{commit}") != commit:
        raise ValueError("Engine commit verification failed")
    with tempfile.TemporaryDirectory(prefix="engine-", dir=cache) as temporary:
        directory = Path(temporary)
        archive = directory / "source.tar"
        subprocess.run(
            ["git", "-C", str(repo), "archive", "--format=tar",
             "--output", str(archive), commit], check=True,
        )
        source = directory / "source"
        source.mkdir()
        with tarfile.open(archive) as bundle:
            bundle.extractall(source, filter="data")
        archive.unlink()
        yield source


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sdk-root", type=Path, default=HERE.parents[1])
    parser.add_argument("--model-root", type=Path, required=True)
    parser.add_argument("--out", type=Path, default=HERE / "dist")
    parser.add_argument("--cache", type=Path, default=HERE / ".build-cache")
    parser.add_argument("--engine-source", type=Path,
                        help="Optional local checkout at the exact locked commit")
    parser.add_argument("--wheelhouse", type=Path,
                        help="Reuse offline ARM64 dependencies; rebuild the engine wheel")
    parser.add_argument("--fonts-dir", type=Path,
                        help="Verified font assets (required for offline wheel builds)")
    args = parser.parse_args()
    lock = read_lock(HERE / "engine-source.lock.json")
    with export_engine(lock, args.cache, args.engine_source) as engine:
        scripts = engine / "deploy/rv1126b"
        with tempfile.TemporaryDirectory(prefix="workflow-wheels-") as temporary:
            wheels = Path(temporary)
            fonts = ["--fonts-dir", str(args.fonts_dir.resolve())] if args.fonts_dir else []
            if args.wheelhouse:
                if not args.wheelhouse.is_dir():
                    raise ValueError("Offline wheelhouse does not exist")
                for wheel in sorted(args.wheelhouse.glob("*.whl")):
                    if not wheel.name.startswith("inference_rv1126b-"):
                        if wheel.is_symlink():
                            raise ValueError("Wheelhouse must contain regular files")
                        shutil.copyfile(wheel, wheels / wheel.name)
                subprocess.run(
                    [sys.executable, str(scripts / "build_wheel.py"),
                     "--output", str(wheels), *fonts], check=True,
                )
            else:
                subprocess.run(
                    [sys.executable, str(scripts / "download_wheels.py"),
                     "--output", str(wheels), *fonts], check=True,
                )
            expected = wheels / f"inference_rv1126b-{lock['wheel_version']}-py3-none-any.whl"
            if not expected.is_file():
                raise ValueError("Built engine wheel version differs from source lock")
            # Narrow compatible upstream wheel tags to the device contract and
            # remove the reviewed fonttools manpage before SDK archive checks.
            subprocess.run(
                [sys.executable, str(scripts / "audit_wheels.py"),
                 str(wheels), "--normalize"], check=True,
            )
            subprocess.run(
                [sys.executable, str(HERE / "build_app.py"),
                 "--sdk-root", str(args.sdk_root.resolve()),
                 "--engine-root", str(engine),
                 "--source-lock", str(HERE / "engine-source.lock.json"),
                 "--wheelhouse", str(wheels),
                 "--model-root", str(args.model_root.resolve()),
                 "--out", str(args.out.resolve())], check=True,
            )


if __name__ == "__main__":
    main()
