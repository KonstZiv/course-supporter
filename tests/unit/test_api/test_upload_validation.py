"""Tests for shared upload validation utilities.

Phase 1.2 C3 removed the legacy ``ALLOWED_EXTENSIONS`` constant from
:mod:`course_supporter.api.upload_validation` (replaced by canonical
``security/run_stage1`` against ``AUTHORED_POLICY`` in the
``api/routes/documents.py`` callsites). Whitelist coverage moved to:

* :class:`tests.unit.security.test_policies.TestPolicyConsistency` —
  ``AUTHORED_POLICY.allowed_extensions`` values + consistency.
* ``tests/integration/test_authored_upload_validation.py`` —
  ``TestAuthoredUploadDriftEnumeration`` — endpoint-level wiring of
  the authored policy whitelist (8 drift extensions + 2 baseline
  cases per Phase 1.2 §6.5 ratify).

This module retains only the ``file_extension`` helper tests; the
helper survives Phase 1.2 because ``api/routes/homework.py`` still
uses it for homework's quick-fail extension parsing (separate from
the canonical ``security.file_type.extension_of`` used in the
authored path).
"""

from __future__ import annotations

from course_supporter.api.upload_validation import file_extension


class TestFileExtension:
    """file_extension() extracts lowercase extension."""

    def test_pdf(self) -> None:
        assert file_extension("slides.pdf") == ".pdf"

    def test_uppercase(self) -> None:
        assert file_extension("VIDEO.MP4") == ".mp4"

    def test_no_extension(self) -> None:
        assert file_extension("videofile") == ""

    def test_none(self) -> None:
        assert file_extension(None) == ""

    def test_empty(self) -> None:
        assert file_extension("") == ""

    def test_double_dot(self) -> None:
        assert file_extension("archive.tar.gz") == ".gz"
