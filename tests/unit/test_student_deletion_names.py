"""Б-2: the pure rules of the student deletion — the neutral file name, the
reason, the rate-limit counters it deletes."""

from __future__ import annotations

from pathlib import Path

import pytest

from course_supporter.student_deletion import (
    DeletionRefusedError,
    neutral_key,
    neutral_name,
)
from course_supporter.student_deletion.service import (
    EMAIL_COUNTERS,
    STUDENT_COUNTERS,
    check_reason,
)

ROOT = Path(__file__).resolve().parents[2]
TEST_ANSWERS_FILENAME = "answers.json"
"""``homework.submission_core.TEST_ANSWERS_FILENAME``; that module cannot be
the first one imported (an import cycle of its own), so it is not imported."""


@pytest.mark.parametrize(
    ("original", "neutral"),
    [
        ("Ivanenko_dz3.py", "submission.py"),
        ("Ivanenko HW.PDF", "submission.pdf"),
        ("project-ivanenko.tar.gz", "submission.tar.gz"),
        ("project.TGZ", "submission.tgz"),
        ("work.zip", "submission.zip"),
        ("README", "submission"),
        # An extension the upload door does not accept may be a word of the name.
        ("homework.ivanenko", "submission"),
    ],
)
def test_the_neutral_name_keeps_only_a_known_extension(
    original: str, neutral: str
) -> None:
    assert neutral_name(original) == neutral


def test_the_key_keeps_its_folder_and_takes_the_neutral_name() -> None:
    key = "homework/t-1/s-1/Ivanenko_dz3.py"
    assert neutral_key(key, "Ivanenko_dz3.py") == (
        "homework/t-1/s-1/submission.py",
        "submission.py",
    )


def test_a_name_with_slashes_stays_in_its_submission_folder() -> None:
    key = "homework/t-1/s-1/Users/ivanenko/hw.py"
    assert neutral_key(key, "Users/ivanenko/hw.py") == (
        "homework/t-1/s-1/submission.py",
        "submission.py",
    )


def test_a_tests_answers_keep_the_platforms_own_name() -> None:
    key = f"homework/t-1/s-1/{TEST_ANSWERS_FILENAME}"
    assert neutral_key(key, TEST_ANSWERS_FILENAME) is None


def test_an_already_neutral_key_is_left_alone() -> None:
    assert neutral_key("homework/t-1/s-1/submission.py", "submission.py") is None


def test_without_an_original_name_the_key_names_the_file() -> None:
    assert neutral_key("homework/t-1/s-1/Ivanenko.py", None) == (
        "homework/t-1/s-1/submission.py",
        "submission.py",
    )


@pytest.mark.parametrize("reason", ["", "   ", "asked by ivan@example.org", "x@y"])
def test_a_reason_that_is_empty_or_holds_an_address_is_refused(reason: str) -> None:
    with pytest.raises(DeletionRefusedError):
        check_reason(reason)


def test_a_reason_is_stripped() -> None:
    assert check_reason("  ticket 42 ") == "ticket 42"


def test_the_counters_deleted_are_the_ones_the_routes_keep() -> None:
    """The deletion names the rate-limit counters by the routes' limit names;
    a renamed limit would leave its counter behind, so the names are checked
    against the route that counts them."""
    routes = (ROOT / "src/course_supporter/api/routes/portal_join.py").read_text()
    for name in (*EMAIL_COUNTERS, *STUDENT_COUNTERS):
        assert f'"{name}"' in routes, name
