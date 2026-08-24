"""Logging helpers shared by the public reCamera Python interfaces.

The module deliberately is not named ``logging``.  ``kit/run.py`` also
supports direct path execution, where its own directory is first on
``sys.path``; a sibling named ``logging.py`` would then shadow Python's
standard-library module before the package bootstrap can run.

Importing a library must never call :func:`logging.basicConfig` or replace the
application's handlers.  The kit therefore installs only a ``NullHandler`` and
leaves configuration to ``kit.run`` or to the embedding application.
"""
from __future__ import annotations

import logging
import re
import sys
import threading
from collections import defaultdict
from typing import IO, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


LOGGER_NAME = "recamera"
_SECRET_QUERY_KEYS = {
    "access_token",
    "api_key",
    "apikey",
    "auth",
    "jwt",
    "key",
    "password",
    "secret",
    "sig",
    "signature",
    "token",
}


_root_logger = logging.getLogger(LOGGER_NAME)
if not any(isinstance(handler, logging.NullHandler)
           for handler in _root_logger.handlers):
    _root_logger.addHandler(logging.NullHandler())


def get_logger(name: str = "") -> logging.Logger:
    """Return a namespaced logger without changing global logging state.

    ``get_logger("media.rga")`` returns ``recamera.media.rga``.  Passing an
    already-qualified name is also supported.
    """

    clean = str(name or "").strip(".")
    if not clean or clean == LOGGER_NAME:
        return _root_logger
    if clean.startswith(LOGGER_NAME + "."):
        return logging.getLogger(clean)
    return logging.getLogger(f"{LOGGER_NAME}.{clean}")


def _coerce_level(level: int | str) -> int:
    if isinstance(level, int):
        return level
    value = logging.getLevelName(str(level).upper())
    if not isinstance(value, int):
        raise ValueError(f"unknown log level: {level!r}")
    return value


def configure_logging(
    level: int | str = logging.INFO,
    stream: Optional[IO[str]] = None,
) -> logging.Handler:
    """Configure the ``recamera`` logger for a command-line application.

    The function is idempotent: a handler previously created by this function
    is updated rather than duplicated.  Other handlers installed by the host
    application are left untouched.
    """

    numeric_level = _coerce_level(level)
    output = stream if stream is not None else sys.stderr
    handler = next(
        (h for h in _root_logger.handlers
         if getattr(h, "_recamera_managed", False)),
        None,
    )
    if handler is None:
        handler = logging.StreamHandler(output)
        handler._recamera_managed = True  # type: ignore[attr-defined]
        _root_logger.addHandler(handler)
    elif isinstance(handler, logging.StreamHandler):
        handler.setStream(output)
    handler.setLevel(numeric_level)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    ))
    _root_logger.setLevel(numeric_level)
    _root_logger.propagate = False
    return handler


def redact_url(value: str) -> str:
    """Remove credentials and secret query values from a URL for logging.

    The scheme, host, port, path, and non-secret query parameters are retained
    so the resulting diagnostic remains actionable.  Malformed/non-URL text is
    handled conservatively by masking ``user:password@`` patterns.
    """

    raw = str(value)
    try:
        parsed = urlsplit(raw)
        if not parsed.scheme or not parsed.netloc:
            return re.sub(r"(?<=://)[^/@\s]+@", "***@", raw)
        host = parsed.hostname or ""
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        try:
            port = f":{parsed.port}" if parsed.port is not None else ""
        except ValueError:
            port = ""
        netloc = host + port
        if parsed.username is not None or parsed.password is not None:
            netloc = "***@" + netloc
        query = urlencode([
            (key, "***" if key.lower() in _SECRET_QUERY_KEYS else item)
            for key, item in parse_qsl(parsed.query, keep_blank_values=True)
        ])
        return urlunsplit((parsed.scheme, netloc, parsed.path, query,
                           parsed.fragment))
    except Exception:
        return re.sub(r"(?<=://)[^/@\s]+@", "***@", raw)


class WarningLimiter:
    """Thread-safe limiter for repetitive warning messages.

    Embedded media loops can encounter the same recoverable fault every frame.
    ``warning(key, ...)`` logs the first ``limit`` occurrences and then emits a
    single suppression notice.  Counters remain queryable for health metrics.
    """

    def __init__(self, logger: logging.Logger, limit: int = 3) -> None:
        if int(limit) < 1:
            raise ValueError("warning limit must be at least 1")
        self.logger = logger
        self.limit = int(limit)
        self._counts: dict[str, int] = defaultdict(int)
        self._lock = threading.Lock()

    def warning(self, key: str, message: str, *args, **kwargs) -> int:
        """Record and conditionally log one occurrence; return total count."""

        stable_key = str(key)
        with self._lock:
            self._counts[stable_key] += 1
            count = self._counts[stable_key]
        if count <= self.limit:
            self.logger.warning(message, *args, **kwargs)
        elif count == self.limit + 1:
            self.logger.warning(
                "suppressing repeated warning %s after %d occurrences",
                stable_key,
                self.limit,
            )
        return count

    def count(self, key: str) -> int:
        """Return the total number of occurrences recorded for ``key``."""

        with self._lock:
            return self._counts.get(str(key), 0)


__all__ = [
    "LOGGER_NAME",
    "WarningLimiter",
    "configure_logging",
    "get_logger",
    "redact_url",
]
