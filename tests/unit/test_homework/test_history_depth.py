"""How far back the Mentor's memory of a student reaches (step E).

The history stays course-wide — systematic habits show across tasks, not inside
one — so what is bounded is DEPTH IN TASKS, and the ordering is the student's
own: tasks ranked by their newest reviewed attempt, newest first.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from course_supporter.homework.review_graph import _within_depth


class _Row:
    """The three fields ``_within_depth`` reads, and nothing else."""

    def __init__(
        self, doc: uuid.UUID, when: datetime, status: str = "completed"
    ) -> None:
        self.authored_document_id = doc
        self.created_at = when
        self.status = status


_T0 = datetime(2026, 9, 1, tzinfo=UTC)

# Four tasks. The rows are deliberately NOT in task order and NOT in time order:
# the helper must rank tasks by their own newest reviewed attempt, whatever
# order the repository hands them in.
TASK_OLD = uuid.uuid4()  # newest reviewed attempt: day 1
TASK_MID = uuid.uuid4()  # day 5 (two attempts)
TASK_NEW = uuid.uuid4()  # day 9
CURRENT = uuid.uuid4()  # the task being submitted now — no reviewed attempt yet

_ROWS = [
    _Row(TASK_MID, _T0 + timedelta(days=5)),
    _Row(TASK_OLD, _T0 + timedelta(days=1)),
    _Row(TASK_NEW, _T0 + timedelta(days=9)),
    _Row(TASK_MID, _T0 + timedelta(days=3)),
    # Not reviewed: never ranks a task and never survives.
    _Row(TASK_OLD, _T0 + timedelta(days=11), status="rejected"),
]


class _Submission:
    def __init__(self, doc: uuid.UUID) -> None:
        self.authored_document_id = doc


def _docs(rows: list[_Row]) -> list[uuid.UUID]:
    return [row.authored_document_id for row in rows]


class TestDepth:
    def test_none_keeps_every_row(self) -> None:
        kept = _within_depth(_ROWS, _Submission(CURRENT), None)  # type: ignore[arg-type]
        assert kept == _ROWS

    def test_zero_keeps_only_the_current_task(self) -> None:
        kept = _within_depth(_ROWS, _Submission(CURRENT), 0)  # type: ignore[arg-type]
        assert kept == []

    def test_zero_on_a_task_with_history_keeps_that_task_only(self) -> None:
        kept = _within_depth(_ROWS, _Submission(TASK_MID), 0)  # type: ignore[arg-type]
        assert set(_docs(kept)) == {TASK_MID}
        assert len(kept) == 2  # every attempt on a kept task, not just the newest

    def test_depth_three_keeps_the_three_most_recent_others(self) -> None:
        kept = _within_depth(_ROWS, _Submission(CURRENT), 3)  # type: ignore[arg-type]
        assert set(_docs(kept)) == {TASK_NEW, TASK_MID, TASK_OLD}

    def test_depth_one_keeps_the_newest_other_by_the_student_s_own_clock(
        self,
    ) -> None:
        kept = _within_depth(_ROWS, _Submission(CURRENT), 1)  # type: ignore[arg-type]
        assert set(_docs(kept)) == {TASK_NEW}

    def test_an_unreviewed_attempt_does_not_rank_its_task(self) -> None:
        # TASK_OLD's newest row is day 11 but rejected; it must still rank at
        # day 1 and lose to TASK_MID (day 5) at depth 1.
        kept = _within_depth(_ROWS, _Submission(TASK_NEW), 1)  # type: ignore[arg-type]
        assert set(_docs(kept)) == {TASK_NEW, TASK_MID}

    def test_the_current_task_is_kept_even_with_no_reviewed_attempt(self) -> None:
        rows = [*_ROWS, _Row(CURRENT, _T0 + timedelta(days=12))]
        kept = _within_depth(rows, _Submission(CURRENT), 0)  # type: ignore[arg-type]
        assert set(_docs(kept)) == {CURRENT}
