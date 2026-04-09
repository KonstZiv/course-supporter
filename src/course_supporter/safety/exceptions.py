"""Security violation exceptions with full audit context."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class SecurityContext:
    """Audit context attached to every security violation."""

    tenant_id: uuid.UUID | None = None
    student_id: uuid.UUID | None = None
    submission_id: uuid.UUID | None = None
    file_url: str | None = None
    filename: str | None = None

    def as_log_dict(self) -> dict[str, Any]:
        """Return non-None fields as a dict suitable for structlog binding."""
        return {k: str(v) for k, v in self.__dict__.items() if v is not None}


class SecurityViolationError(Exception):
    """Base exception for all security violations in the safety module.

    Carries structured context for audit logging and incident response.
    """

    violation_type: str = "generic"

    def __init__(
        self,
        message: str,
        *,
        ctx: SecurityContext | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.ctx = ctx or SecurityContext()
        self.details = details or {}
        super().__init__(message)

    def enrich(self, ctx: SecurityContext) -> None:
        """Attach caller-side context (tenant, student, submission)."""
        self.ctx = ctx

    def as_log_dict(self) -> dict[str, Any]:
        """Full structured payload for log emission."""
        result: dict[str, Any] = {
            "violation_type": self.violation_type,
            "message": str(self),
            **self.ctx.as_log_dict(),
        }
        if self.details:
            result["details"] = self.details
        return result


class SymlinkViolationError(SecurityViolationError):
    """Submission file or archive entry is a symbolic link."""

    violation_type: str = "symlink"


class PathTraversalError(SecurityViolationError):
    """Archive entry contains path traversal (e.g. ../../etc/passwd)."""

    violation_type: str = "path_traversal"


class ArchiveBombError(SecurityViolationError):
    """Archive exceeds size/count/nesting limits (decompression bomb)."""

    violation_type: str = "archive_bomb"


class ContentSafetyError(SecurityViolationError):
    """Content failed safety check (prompt injection, off-topic, etc.)."""

    violation_type: str = "content_safety"


@dataclass
class SecurityWarning:
    """Non-fatal security observation collected during extraction."""

    violation_type: str
    message: str
    filename: str | None = None
    raw_filename: str | None = None

    def as_log_dict(self) -> dict[str, Any]:
        """Structured payload for log emission."""
        return {k: v for k, v in self.__dict__.items() if v is not None}
