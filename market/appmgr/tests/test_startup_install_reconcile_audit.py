"""Boot-path regression coverage for the install-transaction reconciler audit.

``_serve`` finishes or rolls back an install journal left behind by a power cut
and then records the outcome by splatting the reconciler's return value into
``_audit``.  That dict carries its own ``action`` (``committed`` /
``rolled_back``) while ``_audit(action, **kv)`` already takes the record's
action positionally, so the splat raised ``TypeError``.  The failure happens
once per boot inside the boot path, which re-raises it, so the daemon never
reached boot restore and the app under upgrade never came back.

These cases drive the real boot step with the real ``_audit``: a stub here
would hide exactly the defect under test.
"""
from __future__ import annotations

import json
import os
import sys
import tarfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
from appmgr import (config as appconfig, installer, paths,  # noqa: E402
                    resources, server, state, supervisor, visualization)


APP_ID = "boot-reconcile"


class _BootReached(Exception):
    """Marker: the boot step under test finished and ``_serve`` moved on."""


@pytest.fixture
def layout(tmp_path, monkeypatch):
    apps = tmp_path / "apps"
    appmgr = tmp_path / "appmgr"
    appdata = tmp_path / "appdata"
    venvs = tmp_path / "venvs"
    for directory in (apps, appmgr, appdata, venvs):
        directory.mkdir()
    monkeypatch.setattr(paths, "APPS_DIR", str(apps))
    monkeypatch.setattr(paths, "APPMGR_DIR", str(appmgr))
    monkeypatch.setattr(paths, "APPDATA_DIR", str(appdata))
    monkeypatch.setattr(paths, "VENVS_DIR", str(venvs))
    monkeypatch.setattr(paths, "STATE_FILE", str(apps / "state.json"))
    monkeypatch.setattr(paths, "ALLOWED_PKG_ROOTS", (str(tmp_path),))
    monkeypatch.setattr(paths, "REQUIRE_SIGNATURE", False)
    monkeypatch.setattr(server, "_result_hub_instance", None)
    monkeypatch.setattr(server, "_coordinator_instance", None)
    monkeypatch.setattr(server, "_coordinator_layout", None)
    monkeypatch.setattr(server, "_service_stopping", False)
    server.cache_clear()
    return tmp_path


def _package(root, version):
    package = root / ("%s-%s.tar.gz" % (APP_ID, version))
    manifest = {
        "id": APP_ID,
        "name": "Boot Reconcile",
        "version": version,
        "entry": "app.py",
        "config_schema": {},
    }
    source = root / ("source-" + version)
    source.mkdir()
    (source / "manifest.json").write_text(json.dumps(manifest))
    (source / "app.py").write_text("# %s\n" % version)
    with tarfile.open(package, "w:gz") as archive:
        archive.add(source / "manifest.json", arcname="manifest.json")
        archive.add(source / "app.py", arcname="app.py")
    return str(package)


def _leave_interrupted_journal(root, phase):
    """Reproduce the on-disk state a power cut leaves behind mid-install."""
    server.do_install(_package(root, "1.0.0"))
    appdata = paths.appdata_dir(APP_ID)
    os.makedirs(appdata, exist_ok=True)
    config_path = os.path.join(appdata, "config.json")
    with open(config_path, "wb") as output:
        output.write(b'{"before":1}\n')
    config_snapshot = appconfig.snapshot_upgrade_config(APP_ID)
    lifecycle_snapshot = state.snapshot_app(APP_ID)
    candidate = installer.prepare(_package(root, "2.0.0"))
    installer.begin_install_transaction(
        candidate, config_snapshot=config_snapshot,
        lifecycle_snapshot=lifecycle_snapshot)
    installer.mark_install_transaction(candidate, "stopped")
    installer.mark_install_transaction(candidate, "publishing_code")
    os.rename(paths.app_dir(APP_ID), paths.app_dir(APP_ID) + ".prev")
    os.rename(candidate.staging, paths.app_dir(APP_ID))
    candidate.staging = None
    installer.mark_install_transaction(candidate, "code_published")
    if phase != "code_published":
        installer.mark_install_transaction(candidate, phase)
    with open(config_path, "wb") as output:
        output.write(b'{"after":2}\n')
    journal = installer.load_install_transaction()
    assert journal is not None and journal["phase"] == phase
    return journal


def _audit_records():
    with open(paths.audit_log()) as stream:
        return [json.loads(line) for line in stream if line.strip()]


def _installed_version():
    with open(os.path.join(paths.app_dir(APP_ID), "manifest.json")) as source:
        return json.load(source)["version"]


def _serve_until_after_the_install_reconciler(monkeypatch, reached):
    """Run the real boot path up to (and past) the install reconcile step.

    ``_reconcile_install_transaction`` and ``_audit`` are deliberately left
    alone; everything the boot does once past that step is stopped by a
    sentinel, so the assertion is about that one step and nothing else.
    """

    def boot_moved_on():
        reached.append(True)
        raise _BootReached

    monkeypatch.setattr(server, "_acquire_single_instance", lambda: True)
    monkeypatch.setattr(server, "_reconcile_startup_state", lambda: {})
    monkeypatch.setattr(server.acousticslab, "reconcile", lambda: None)
    monkeypatch.setattr(server.appuploads, "recover_startup", lambda: {})
    monkeypatch.setattr(server.appuploads, "gc_expired", lambda: [])
    monkeypatch.setattr(server.supervisor, "install_sigchld", lambda: True)
    monkeypatch.setattr(server.installer, "reconcile_interrupted_installs",
                        lambda: [])
    monkeypatch.setattr(server, "_recover_render_overrides", lambda: {})
    monkeypatch.setattr(server, "_coordinator", boot_moved_on)
    with pytest.raises(_BootReached):
        server._serve("127.0.0.1", 1,
                      lifecycle={"owned": False, "shutdown_attempted": False})
    assert reached == [True], "boot never got past the install reconciler"


@pytest.mark.parametrize("phase,expected_op", [
    ("restarting", "rolled_back"),
    ("code_published", "rolled_back"),
    ("ready", "committed"),
])
def test_boot_reconcile_audit_does_not_raise(layout, monkeypatch, phase,
                                             expected_op):
    _leave_interrupted_journal(layout, phase)
    reached = []

    _serve_until_after_the_install_reconciler(monkeypatch, reached)

    records = _audit_records()
    failed = [rec for rec in records
              if rec["action"] == "install_transaction_reconcile_failed"]
    assert failed == []
    reconciled = [rec for rec in records
                  if rec["action"] == "install_transaction_reconciled"]
    assert len(reconciled) == 1
    assert reconciled[0]["op"] == expected_op
    assert reconciled[0]["app_id"] == APP_ID
    assert reconciled[0]["phase"] == phase
    assert _installed_version() == ("2.0.0" if expected_op == "committed"
                                    else "1.0.0")
