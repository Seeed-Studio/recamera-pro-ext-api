import json
from appmgr import recording_migration as migration


def test_missing_report_is_not_a_migration_error(tmp_path, monkeypatch):
    monkeypatch.setattr(migration, "REPORT_PATH", tmp_path / "report.json")
    assert migration.migration_view() == {"status": "complete", "requires_review": False,
                                          "reason_codes": []}
    assert migration.acknowledge_migration()["requires_review"] is False
    assert not migration.REPORT_PATH.exists()


def test_acknowledgement_preserves_report_and_never_enables_recording(tmp_path, monkeypatch):
    path = tmp_path / "report.json"
    monkeypatch.setattr(migration, "REPORT_PATH", path)
    report = {"version": 1, "status": "pending", "requires_review": True,
              "reason_codes": ["event_conditions_require_app_configuration"],
              "backup_directory": "/private/backup", "review": [{"rule_id": "fall"}]}
    path.write_text(json.dumps(report))
    config = tmp_path / "record_config.json"
    config.write_text('{"bRuleEnabled":false}')
    assert "backup_directory" not in migration.migration_view()
    result = migration.acknowledge_migration()
    assert result["status"] == "acknowledged" and not result["requires_review"]
    saved = json.loads(path.read_text())
    assert saved["review"] == report["review"]
    assert saved["backup_directory"] == report["backup_directory"]
    assert config.read_text() == '{"bRuleEnabled":false}'
    assert migration.acknowledge_migration() == result


def test_corrupt_report_stays_pending_and_cannot_be_acknowledged(tmp_path, monkeypatch):
    import pytest
    monkeypatch.setattr(migration, "REPORT_PATH", tmp_path / "report.json")
    migration.REPORT_PATH.write_text("broken")
    assert migration.migration_view()["requires_review"]
    with pytest.raises(ValueError):
        migration.acknowledge_migration()
    assert migration.REPORT_PATH.read_text() == "broken"


def test_invalid_fields_cannot_partially_acknowledge_report(tmp_path, monkeypatch):
    import pytest
    monkeypatch.setattr(migration, "REPORT_PATH", tmp_path / "report.json")
    for fields in [{"reason_codes": None}, {"reason_codes": "event"},
                   {"reason_codes": [[]]}, {"requires_review": False}]:
        report = {"version": 1, "status": "pending", "requires_review": True,
                  "reason_codes": [], **fields}
        original = json.dumps(report)
        migration.REPORT_PATH.write_text(original)
        assert migration.migration_view()["requires_review"] is True
        with pytest.raises(ValueError):
            migration.acknowledge_migration()
        assert migration.REPORT_PATH.read_text() == original
