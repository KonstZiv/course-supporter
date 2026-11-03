"""The shipped submission-path file, with chosen types handed back to today's Mentor.

Since task 10 (K5) the shipped file serves ``task`` and ``short_task`` on the
new path (in test mode). A test of TODAY's Mentor that drives one of them takes
the way back the file keeps for every type — ``served_by: todays_mentor`` — in
the test alone, and hands the result to the body in place of the cached file.
"""

from __future__ import annotations

from pathlib import Path

from course_supporter.homework.path_config import (
    PathConfig,
    ServedBy,
    load_path_config,
)
from course_supporter.models.source import AssignmentType

_SHIPPED = Path(__file__).resolve().parents[2] / "config" / "submission_paths.yaml"


def shipped_with_todays_mentor_for(*task_types: AssignmentType) -> PathConfig:
    """The shipped file with ``task_types`` served by today's Mentor, charged."""
    raw = load_path_config(_SHIPPED).model_dump(mode="json")
    for task_type in task_types:
        raw["task_types"][task_type.value].update(
            served_by=ServedBy.TODAYS_MENTOR.value, test_mode=False
        )
    return PathConfig.model_validate(raw)
