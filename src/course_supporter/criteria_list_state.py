"""How far the criteria list of a task version has got (mentor-rebuild task 08).

Purpose:
    A task's criteria list is composed once per task version by an expensive
    model call, while several submissions of that version may arrive at once.
    The list therefore has a lifecycle the database can see: a submission that
    finds the list still being composed waits for it instead of paying for a
    second composition, and a composition that gave up leaves room for the
    next submission to try again.

Interface:
    :class:`CriteriaListState` — the closed vocabulary of
    ``task_criteria_lists.state``, enforced by ``ck_task_criteria_lists_state``.

Extending:
    A new state is a new enum member AND a migration re-issuing that CHECK —
    Postgres cannot widen a CHECK in place, and the database refuses a value it
    has not been told about, so Python and SQL cannot drift apart silently.
"""

from __future__ import annotations

from enum import StrEnum


class CriteriaListState(StrEnum):
    """The lifecycle of one ``task_criteria_lists`` row.

    The same three states as ``ReferenceState`` (task 06), which the claim
    protocol of task 08 mirrors:

    * ``PENDING`` — a submission has claimed the composition and is calling
      the model; the list itself is not there yet.
    * ``READY`` — the list is in place and can be read.
    * ``FAILED`` — the composition gave up; ``failure_reason`` says why. A
      failed row is history, never the live row of its task.
    """

    PENDING = "pending"
    READY = "ready"
    FAILED = "failed"
