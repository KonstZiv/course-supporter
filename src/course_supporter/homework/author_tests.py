"""Author tests a student touched: changed or deleted test files of the base.

A project is submitted on top of the author's base, and the base may carry
the author's tests. The code-run service runs those tests; a student must not
pass by editing a test instead of the code. This pure function names the
author's test files the student changed or deleted, so the run can restore
them and the review can say so. Tests the student added are allowed and are
not reported. No I/O, no models, no network.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from course_supporter.homework.run_hints import detect_run_hints
from course_supporter.normalizer.models import Delta


@dataclass(frozen=True, slots=True)
class AuthorTestsTouched:
    """The author's test files the student changed or deleted (sorted)."""

    changed: tuple[str, ...]
    deleted: tuple[str, ...]

    @property
    def any(self) -> bool:
        """Whether the student touched at least one author test."""
        return bool(self.changed or self.deleted)


def author_tests_touched(base_paths: Iterable[str], delta: Delta) -> AuthorTestsTouched:
    """Name the author's test files that ``delta`` shows changed or deleted."""
    author_tests = set(detect_run_hints(base_paths).test_files)
    return AuthorTestsTouched(
        changed=tuple(sorted(author_tests.intersection(delta.changed))),
        deleted=tuple(sorted(author_tests.intersection(delta.deleted))),
    )
