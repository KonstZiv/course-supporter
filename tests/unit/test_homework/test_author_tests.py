"""Author tests a student touched in a project submission."""

from course_supporter.homework.author_tests import (
    AuthorTestsTouched,
    author_tests_touched,
)
from course_supporter.normalizer.models import Delta

BASE = (
    "main.py",
    "src/stats.py",
    "tests/test_stats.py",
    "tests/test_io.py",
    "requirements.txt",
)


def _delta(
    changed: tuple[str, ...] = (),
    new: tuple[str, ...] = (),
    deleted: tuple[str, ...] = (),
) -> Delta:
    return Delta(changed=changed, new=new, deleted=deleted, hygiene_new_excluded=())


def test_untouched_tests_are_not_reported() -> None:
    touched = author_tests_touched(BASE, _delta(changed=("src/stats.py",)))
    assert touched == AuthorTestsTouched(changed=(), deleted=())
    assert touched.any is False


def test_a_changed_author_test_is_reported() -> None:
    touched = author_tests_touched(
        BASE, _delta(changed=("src/stats.py", "tests/test_stats.py"))
    )
    assert touched.changed == ("tests/test_stats.py",)
    assert touched.deleted == ()
    assert touched.any is True


def test_a_deleted_author_test_is_reported() -> None:
    touched = author_tests_touched(BASE, _delta(deleted=("tests/test_io.py",)))
    assert touched.deleted == ("tests/test_io.py",)
    assert touched.any is True


def test_tests_the_student_added_are_allowed() -> None:
    touched = author_tests_touched(
        BASE, _delta(new=("tests/test_extra.py", "test_more.py"))
    )
    assert touched.any is False


def test_changed_files_that_are_not_tests_are_ignored() -> None:
    touched = author_tests_touched(
        BASE, _delta(changed=("main.py", "requirements.txt"))
    )
    assert touched.any is False


def test_a_base_without_tests_reports_nothing() -> None:
    touched = author_tests_touched(
        ("main.py", "src/stats.py"),
        _delta(changed=("src/stats.py",), deleted=("main.py",)),
    )
    assert touched.any is False
