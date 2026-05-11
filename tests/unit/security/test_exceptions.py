"""Tests for canonical security exception layer (KD14 + KD-2.1-N).

Covers Phase 2.1 C2 additions to ``security/exceptions.py``:

* ``ErrorCategory`` extensions: ``ARCHIVE_BOMB`` + ``SYMLINK_VIOLATION``
  values (KD-2.1-I 2-set ratify — 2026-05-11).
* ``SecurityRejectedError`` API extension: ``.enrich(ctx)`` +
  ``.ctx`` attribute + ``.as_log_dict()`` method (KD-2.1-N — mirror
  legacy ``safety.exceptions.SecurityViolationError`` signature).

Tests verify the new surface in isolation; integration with the
canonical ``extract_submission_content`` archive raiser is covered in
``test_archive.py``.
"""

from __future__ import annotations

import uuid

from course_supporter.security.exceptions import (
    ErrorCategory,
    SecurityRejectedError,
)
from course_supporter.security.schemas import SecurityContext


class TestErrorCategoryPhase21Additions:
    """Verify ARCHIVE_BOMB + SYMLINK_VIOLATION exist with correct values."""

    def test_archive_bomb_value(self) -> None:
        assert ErrorCategory.ARCHIVE_BOMB.value == "archive_bomb"

    def test_symlink_violation_value(self) -> None:
        assert ErrorCategory.SYMLINK_VIOLATION.value == "symlink_violation"

    def test_archive_bomb_is_str_enum(self) -> None:
        # StrEnum members are str instances (per Python 3.11+ StrEnum
        # semantics). ``.value`` access used for the equality check so
        # mypy resolves the comparison as ``str == str`` -- bare
        # ``StrEnum`` instances compare equal to strings at runtime but
        # mypy treats them as non-overlapping literal types and reports
        # a false positive (mirrors test_schemas.py:215-222 pattern).
        assert isinstance(ErrorCategory.ARCHIVE_BOMB, str)
        assert ErrorCategory.ARCHIVE_BOMB.value == "archive_bomb"

    def test_symlink_violation_is_str_enum(self) -> None:
        assert isinstance(ErrorCategory.SYMLINK_VIOLATION, str)
        assert ErrorCategory.SYMLINK_VIOLATION.value == "symlink_violation"

    def test_phase_21_values_dont_collide_with_existing(self) -> None:
        # Verify no overlap з legacy 7 ErrorCategory values
        legacy_values = {
            "size_limit",
            "forbidden_type",
            "magic_mismatch",
            "archive_violation",
            "suspicious_unicode",
            "prompt_injection",
            "charset_violation",
        }
        phase_21_values = {"archive_bomb", "symlink_violation"}
        assert not (legacy_values & phase_21_values)


class TestSecurityRejectedErrorBaseConstructor:
    """Verify constructor preserves legacy contract (category + detail)."""

    def test_constructor_sets_category_and_detail(self) -> None:
        exc = SecurityRejectedError(
            ErrorCategory.SIZE_LIMIT,
            "file size 31MB exceeds limit 20MB",
        )
        assert exc.category == ErrorCategory.SIZE_LIMIT
        assert exc.detail == "file size 31MB exceeds limit 20MB"

    def test_constructor_sets_ctx_none_by_default(self) -> None:
        exc = SecurityRejectedError(ErrorCategory.FORBIDDEN_TYPE, "bad ext")
        assert exc.ctx is None

    def test_str_includes_category_and_detail(self) -> None:
        exc = SecurityRejectedError(
            ErrorCategory.ARCHIVE_BOMB,
            "decompressed size exceeds limit",
        )
        assert str(exc) == "archive_bomb: decompressed size exceeds limit"


class TestSecurityRejectedErrorEnrich:
    """Verify KD-2.1-N .enrich(ctx) + .ctx + .as_log_dict() API extension."""

    def test_enrich_attaches_ctx(self) -> None:
        exc = SecurityRejectedError(
            ErrorCategory.ARCHIVE_BOMB,
            "too many files",
        )
        ctx = SecurityContext(
            tenant_id=uuid.UUID("00000000-0000-7000-8000-000000000001"),
            submission_id=uuid.UUID("00000000-0000-7000-8000-000000000002"),
        )
        exc.enrich(ctx)
        assert exc.ctx is ctx

    def test_enrich_overwrites_previous_ctx(self) -> None:
        exc = SecurityRejectedError(
            ErrorCategory.SYMLINK_VIOLATION,
            "refusing to process symlink",
        )
        ctx1 = SecurityContext(filename="first.zip")
        ctx2 = SecurityContext(filename="second.zip")
        exc.enrich(ctx1)
        exc.enrich(ctx2)
        assert exc.ctx is ctx2

    def test_as_log_dict_without_ctx(self) -> None:
        exc = SecurityRejectedError(
            ErrorCategory.MAGIC_MISMATCH,
            "pdf extension but PNG magic",
        )
        payload = exc.as_log_dict()
        assert payload == {
            "category": "magic_mismatch",
            "detail": "pdf extension but PNG magic",
        }

    def test_as_log_dict_with_ctx(self) -> None:
        tenant_id = uuid.UUID("00000000-0000-7000-8000-000000000010")
        student_id = uuid.UUID("00000000-0000-7000-8000-000000000020")
        exc = SecurityRejectedError(
            ErrorCategory.ARCHIVE_BOMB,
            "nesting depth exceeded",
        )
        exc.enrich(
            SecurityContext(
                tenant_id=tenant_id,
                student_id=student_id,
                filename="bomb.zip",
            )
        )
        payload = exc.as_log_dict()
        assert payload["category"] == "archive_bomb"
        assert payload["detail"] == "nesting depth exceeded"
        assert payload["tenant_id"] == str(tenant_id)
        assert payload["student_id"] == str(student_id)
        assert payload["filename"] == "bomb.zip"

    def test_as_log_dict_with_ctx_excludes_none_fields(self) -> None:
        exc = SecurityRejectedError(
            ErrorCategory.SYMLINK_VIOLATION,
            "symlink rejected",
        )
        # Only filename set; tenant_id/student_id/submission_id/file_url None.
        exc.enrich(SecurityContext(filename="link.txt"))
        payload = exc.as_log_dict()
        assert payload == {
            "category": "symlink_violation",
            "detail": "symlink rejected",
            "filename": "link.txt",
        }
