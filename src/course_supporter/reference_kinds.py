"""What a reference is, and how far it has got (mentor-rebuild task 06).

Purpose:
    A reference is what a task is checked against: the answer key of a test,
    the one kind there is. A version still names its ``kind`` — a closed
    vocabulary rather than a free string, for the same reason the feedback
    vocabularies are closed (:mod:`course_supporter.feedback_kinds`): the first
    question anyone asks of this table is "how many of each", and a free string
    turns that into text-parsing.

Interface:
    :class:`ReferenceKind` — which kind of reference a version carries.
    :class:`ReferenceState` — how far the generation of its machine layer got.

Extending:
    A new value is a new enum member AND a migration re-issuing the matching
    ``CHECK`` on ``task_references`` / ``task_reference_overrides`` — Postgres
    cannot widen a CHECK in place. The database refuses a value it has not been
    told about, so Python and SQL cannot drift apart silently.

    The mandatory points of a written task, once planned here as a second
    kind, are not one: since task 08 they live inside the criteria list, beside
    their criterion (:mod:`course_supporter.homework.criteria_form`).
"""

from __future__ import annotations

from enum import StrEnum


class ReferenceKind(StrEnum):
    """Which kind of reference one version carries.

    * ``TEST_KEY`` — the answer key of a test: the author's answers plus one
      explanation per question. The only kind of the first echelon, because it
      is the only one a test can be graded against at all.
    """

    TEST_KEY = "test_key"


class ReferenceState(StrEnum):
    """How far the generation of a version's machine layer got.

    The same three-state shape as ``ProjectBase.state`` (KD18 P2), and for the
    same reason: the work runs in a queue, so the author needs to tell "not
    finished yet" from "finished badly", and the second needs a reason.

    * ``PENDING`` — the version exists, its explanations do not yet.
    * ``READY`` — explanations are in place; this version can be read.
    * ``FAILED`` — generation gave up; ``failure_reason`` says why.
    """

    PENDING = "pending"
    READY = "ready"
    FAILED = "failed"
