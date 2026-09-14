"""Read and acknowledge the fixed boot-time recording migration report."""
import json
import os
from pathlib import Path
import tempfile
import threading

REPORT_PATH = Path("/userdata/config/record/recording_migration_report.json")
_lock = threading.Lock()
_REASONS = {"event_conditions_require_app_configuration", "invalid_or_conflicting_source"}


def _load():
    try:
        if REPORT_PATH.is_symlink():
            raise ValueError("Invalid recording migration report")
        with REPORT_PATH.open("rb") as stream:
            data = stream.read(1024 * 1024 + 1)
        if len(data) > 1024 * 1024:
            raise ValueError("Recording migration report is too large")
        report = json.loads(data)
        if (not isinstance(report, dict) or report.get("version") != 1 or
                report.get("status") not in {"pending", "complete", "acknowledged"} or
                not isinstance(report.get("requires_review"), bool) or
                report["requires_review"] != (report["status"] == "pending") or
                not isinstance(report.get("reason_codes", []), list) or
                any(not isinstance(code, str) for code in report.get("reason_codes", []))):
            raise ValueError("Invalid recording migration report")
        return report
    except FileNotFoundError:
        return {"version": 1, "status": "complete", "requires_review": False}


def _view(report):
    return {"status": report["status"], "requires_review": report["requires_review"],
            "reason_codes": [code for code in report.get("reason_codes", []) if code in _REASONS]}


def migration_view():
    with _lock:
        try:
            return _view(_load())
        except (OSError, ValueError, TypeError):
            return {"status": "pending", "requires_review": True,
                    "reason_codes": ["migration_report_unavailable"]}


def acknowledge_migration():
    """Acknowledge review only; recording must still be explicitly enabled."""
    with _lock:
        report = _load()
        if not report["requires_review"]:
            return _view(report)
        report.update(status="acknowledged", requires_review=False)
        fd, temporary = tempfile.mkstemp(prefix=".recording-migration-", dir=REPORT_PATH.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(report, stream, ensure_ascii=False, allow_nan=False)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, REPORT_PATH)
            directory = os.open(REPORT_PATH.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return _view(report)
