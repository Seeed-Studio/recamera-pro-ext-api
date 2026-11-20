"""Shared error type for all local-skill failures."""

from __future__ import annotations

from typing import Optional


class RecameraError(RuntimeError):
    """Device / connectivity failure. Carries HTTP `status`, device `code`, raw `body`."""

    def __init__(
        self,
        message: str,
        *,
        status: Optional[int] = None,
        code: Optional[int] = None,
        body: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.body = body[:2048] if isinstance(body, str) else body
