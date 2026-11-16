"""The schema of task 12a, K1, where no database is needed.

* The student's three kinds of comment are the review's own kinds of reply:
  the review answers a comment in its replies (``ReplyKind``).
* K1 lands a table and a column that nothing writes or reads yet: the
  evaluator of the reviews is the one main had before it.

The database side is ``tests/integration/test_remark_comment_schema_db.py``.
"""

from __future__ import annotations

from pathlib import Path
from typing import get_args

from course_supporter.homework import criteria_verdicts
from course_supporter.homework.evaluator_version import evaluator_version
from course_supporter.homework.path_config import load_path_config
from course_supporter.models.review_structure import ReplyKind
from course_supporter.models.source import AssignmentType
from course_supporter.remark_kinds import CommentKind

_REPO_ROOT = Path(__file__).resolve().parents[2]


def test_the_students_kinds_are_the_reviews_reply_kinds() -> None:
    students = {kind.value for kind in CommentKind if kind is not CommentKind.ANSWER}

    assert students == set(get_args(ReplyKind))


def test_the_evaluator_is_where_it_was() -> None:
    """Lock (task 12a, K1): no prompt, first rung or verdict rule moved.

    The value on main before K1. A later change that moves the evaluator on
    purpose writes the new value here — and with it every earlier review
    stops being given again by the dedup.
    """
    shipped = load_path_config(_REPO_ROOT / "config" / "submission_paths.yaml")

    for task_type in (AssignmentType.TASK, AssignmentType.SHORT_TASK):
        version = evaluator_version(task_type, shipped, prompt_base_path=_REPO_ROOT)
        assert version == "ev1:3cddefc447c114ed", task_type
    assert criteria_verdicts.VERDICT_RULES_VERSION == 6
