"""What the author reads back about student feedback (mentor-rebuild task 05).

Two levels, mirroring the homework-cost breakdown so an author looking at both
lists sees the same shape: a course with its tasks, and one task in total. No
per-student level — counters are aggregate by decision (``03-BINDING.md`` §2.14,
clarification of 2026-09-18), and a per-student breakdown would answer a
question nobody asked of a record the student can still change.

Counters are not stored anywhere: every number here is counted on read, from
the feedback rows, at the moment of the request. The shape is the stable part —
the day counters are kept on the targets instead, these models do not move and
their consumers do not notice.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, Field


class TouchCounters(BaseModel):
    """How many students answered each way."""

    helped: int = Field(description="Answers 'the review helped'.", ge=0)
    not_helped: int = Field(description="Answers 'it did not'.", ge=0)


class TaskTouchCountersEntry(TouchCounters):
    """One task in the by-task breakdown of a course.

    ``is_deleted`` marks a soft-deleted TASK: shown, not filtered, the same way
    the cost breakdown shows it — the answers students gave about its reviews
    were still given. What IS filtered out of the numbers is a soft-deleted
    submission and a soft-deleted student.
    """

    authored_document_id: UUID
    task_label: str = Field(
        description="The same label the cost breakdown uses: "
        "``filename | {source_type} #{order}``."
    )
    is_deleted: bool


class CourseTouchCountersResponse(BaseModel):
    """Response of ``GET /api/v1/feedback/counters/course/{course_node_id}``.

    A course whose id belongs to another tenant — or to nobody — answers with
    an empty list rather than an error, the way the cost breakdown does: an
    author must not learn which courses exist elsewhere by comparing codes.
    """

    course_node_id: UUID
    by_task: list[TaskTouchCountersEntry]


class TaskTouchCountersResponse(TouchCounters):
    """Response of ``GET /api/v1/feedback/counters/task/{authored_document_id}``.

    The task's totals over every student who answered. A foreign or unknown
    task answers zeros, for the same reason the course level answers empty.
    """

    authored_document_id: UUID
