"""Lock of condition (в), BIL-5: the review path does not read the test mark.

``students.is_test_student`` is read by the calibration report only
(``cost_estimate/report.py`` and ``scripts/cost_estimate_report.py``). Agreed
with the owner of the student model (19.11): nothing in homework/, agents/,
llm/, workers/ or cost_estimate/ beyond the report reads it, and the public
numbers (``billing/public_numbers.py``) are computed with the service students
for now. A text search: the name may not appear there in any form — attribute,
``getattr`` string or SQL.
"""

from __future__ import annotations

import re
from pathlib import Path

_PKG = Path(__file__).resolve().parents[2] / "src" / "course_supporter"
_NAME = re.compile(r"\bis_test_student\b")
_LOCKED_DIRS = ("homework", "agents", "llm", "workers", "cost_estimate")
_LOCKED_FILES = (_PKG / "billing" / "public_numbers.py",)
_ALLOWED = {_PKG / "cost_estimate" / "report.py"}  # the calibration report


def _locked() -> list[Path]:
    files = [p for d in _LOCKED_DIRS for p in sorted((_PKG / d).rglob("*.py"))]
    return [p for p in [*files, *_LOCKED_FILES] if p not in _ALLOWED]


def test_the_review_path_does_not_read_the_test_mark() -> None:
    offenders = [
        f"{path.relative_to(_PKG)}:{i}: {line.strip()}"
        for path in _locked()
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if _NAME.search(line)
    ]
    assert not offenders, "is_test_student read on a locked path:\n" + "\n".join(
        offenders
    )


def test_the_lock_sees_every_locked_place() -> None:
    """Positive control: the search covers real files and fires on each form."""
    locked = _locked()
    for d in _LOCKED_DIRS:
        assert any(p.is_relative_to(_PKG / d) for p in locked), d
    assert all(p in locked for p in _LOCKED_FILES)
    assert _NAME.search("if student.is_test_student:")
    assert _NAME.search('getattr(student, "is_test_student")')
    assert _NAME.search("WHERE s.is_test_student")
    assert not _NAME.search("is_test_student_count = 0")
