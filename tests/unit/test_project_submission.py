"""Unit tests for the KD18 P3 interim submission-text builder (Edit II).

Pure — no DB / S3. Builds a real NormalizedSnapshot via the P1 normalizer (fast,
in-memory) and asserts the bounded, over-inclusive delimited concat that feeds
safety → sanity → review until P4 replaces it.
"""

from __future__ import annotations

import io
import zipfile

import pytest

from course_supporter.homework.project_submission import (
    PROJECT_SAFETY_TEXT_MAX_BYTES,
    _project_failure_reason,
    _submission_snapshot_key,
    build_interim_submission_text,
)
from course_supporter.normalizer import NormalizedSnapshot, normalize_archive
from course_supporter.normalizer.exceptions import NormalizerLimitError
from course_supporter.security.exceptions import ErrorCategory, SecurityRejectedError


def _snapshot(files: dict[str, bytes]) -> NormalizedSnapshot:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return normalize_archive(buf.getvalue(), archive_kind="zip")


class TestBuildInterimText:
    def test_delimited_concat_of_text_entries(self) -> None:
        snap = _snapshot({"a.py": b"print(1)\n", "sub/b.md": b"# title\n"})
        text = build_interim_submission_text(snap)
        assert "--- a.py ---\nprint(1)" in text
        assert "--- sub/b.md ---\n# title" in text

    def test_binary_entries_skipped(self) -> None:
        """A BINARY entry (e.g. .png) is hash-tracked but outside review scope."""
        snap = _snapshot({"main.py": b"x = 1\n", "logo.png": b"\x89PNG\r\n\x1a\n\x00"})
        text = build_interim_submission_text(snap)
        assert "main.py" in text
        assert "logo.png" not in text

    def test_empty_when_no_text_entries(self) -> None:
        snap = _snapshot({"logo.png": b"\x89PNG\r\n\x1a\n\x00"})
        assert build_interim_submission_text(snap) == ""

    def test_budget_truncates_and_marks(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """With a tiny budget, whole entries stop and the rest are summarised."""
        monkeypatch.setattr(
            "course_supporter.homework.project_submission.PROJECT_SAFETY_TEXT_MAX_BYTES",
            40,
        )
        snap = _snapshot({"a.py": b"a = 1\n", "b.py": b"b = 2\n", "c.py": b"c = 3\n"})
        text = build_interim_submission_text(snap)
        assert "omitted for budget" in text
        # The marker names an entry count + a byte count.
        assert "entries" in text and "bytes" in text

    def test_first_oversize_entry_is_truncated_not_empty(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A single entry larger than the budget is truncated, never dropped to
        empty — safety must never be handed a blank body."""
        monkeypatch.setattr(
            "course_supporter.homework.project_submission.PROJECT_SAFETY_TEXT_MAX_BYTES",
            20,
        )
        snap = _snapshot({"big.py": b"x = 1  # " + b"pad " * 50})
        text = build_interim_submission_text(snap)
        assert text != ""
        assert len(text.encode("utf-8")) <= 20 + len(
            "\n--- omitted for budget: 0 entries, 0 bytes ---"
        )

    def test_default_budget_is_one_megabyte_order(self) -> None:
        assert PROJECT_SAFETY_TEXT_MAX_BYTES == 1024 * 1024


class TestHelpers:
    def test_snapshot_key_is_sibling(self) -> None:
        assert (
            _submission_snapshot_key("homework/t/sid/proj.zip")
            == "homework/t/sid/snapshot.zip"
        )

    def test_failure_reason_security_prefixed_with_category(self) -> None:
        exc = SecurityRejectedError(ErrorCategory.ARCHIVE_BOMB, "too big")
        assert _project_failure_reason(exc) == "archive_bomb: too big"

    def test_failure_reason_normalizer_carries_class_name(self) -> None:
        exc = NormalizerLimitError("kept_total exceeded")
        reason = _project_failure_reason(exc)
        assert reason.startswith("NormalizerLimitError:")
        assert "kept_total exceeded" in reason
