from __future__ import annotations

import io
import logging
from pathlib import Path

import pytest

from kit.diagnostics import (
    WarningLimiter,
    configure_logging,
    get_logger,
    redact_url,
)


def test_import_does_not_configure_python_root_logger():
    assert not any(getattr(h, "_recamera_managed", False)
                   for h in logging.getLogger().handlers)


def test_module_name_does_not_shadow_stdlib_logging():
    import kit

    assert not (Path(kit.__file__).parent / "logging.py").exists()
    assert logging.__name__ == "logging"


def test_configure_is_idempotent_and_warning_limiter_bounds_noise():
    stream = io.StringIO()
    first = configure_logging("WARNING", stream)
    second = configure_logging(logging.WARNING, stream)
    assert first is second

    limiter = WarningLimiter(get_logger("tests.limit"), limit=2)
    for _ in range(5):
        limiter.warning("frame-drop", "frame dropped")
    text = stream.getvalue()
    assert text.count("frame dropped") == 2
    assert text.count("suppressing repeated warning") == 1
    assert limiter.count("frame-drop") == 5


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("rtsp://admin:secret@127.0.0.1:5554/live/1",
         "rtsp://***@127.0.0.1:5554/live/1"),
        ("https://device/path?token=abc&mode=fast",
         "https://device/path?token=%2A%2A%2A&mode=fast"),
        ("not-a-url", "not-a-url"),
    ],
)
def test_redact_url(raw, expected):
    assert redact_url(raw) == expected
