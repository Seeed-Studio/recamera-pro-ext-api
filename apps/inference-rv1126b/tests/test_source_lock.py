import importlib.util
import json
import subprocess
from pathlib import Path

import pytest


@pytest.fixture
def builder():
    path = Path(__file__).resolve().parents[1] / "build.py"
    spec = importlib.util.spec_from_file_location("workflow_source_builder", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def repository(tmp_path):
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    (repo / "tracked.py").write_text("VALUE = 1\n")
    subprocess.run(["git", "-C", str(repo), "add", "tracked.py"], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=Test",
                    "-c", "user.email=test@example.invalid", "commit", "-m", "fixture"],
                   check=True, capture_output=True)
    commit = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    return repo, {"commit": commit}


def test_export_uses_only_locked_commit_files(builder, repository, tmp_path):
    repo, lock = repository
    (repo / "private.py").write_text("not part of source\n")
    with builder.export_engine(lock, tmp_path / "cache", repo) as source:
        assert (source / "tracked.py").read_text() == "VALUE = 1\n"
        assert not (source / "private.py").exists()
    assert not source.exists()


def test_export_rejects_wrong_commit_or_dirty_tracked_source(builder, repository, tmp_path):
    repo, lock = repository
    with pytest.raises(ValueError, match="HEAD differs"):
        with builder.export_engine({"commit": "0" * 40}, tmp_path / "cache", repo):
            pytest.fail("wrong source exported")
    (repo / "tracked.py").write_text("VALUE = 2\n")
    with pytest.raises(ValueError, match="uncommitted"):
        with builder.export_engine(lock, tmp_path / "cache", repo):
            pytest.fail("dirty source exported")


def test_source_lock_rejects_branch_in_place_of_commit(builder, tmp_path):
    path = tmp_path / "lock.json"
    path.write_text(json.dumps({"schema_version": 1, "commit": "rv1126b",
                               "repository": "https://github.com/mjq2020/inference.git",
                               "wheel_version": "0.3.8"}))
    with pytest.raises(ValueError, match="Invalid engine source lock"):
        builder.read_lock(path)
