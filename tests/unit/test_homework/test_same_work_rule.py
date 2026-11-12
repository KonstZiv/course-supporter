"""One rule of the same work: whose identical file goes on to the doors (task 13).

The entry's raw-hash check ("already submitted") is skipped for a type the new
path serves AND whose attempt adapter deduplicates — read from the
submission-path file and the adapter, never from a list of types: switch a
type, and the answer follows the file.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from tests._helpers.path_config import shipped_with_todays_mentor_for

from course_supporter.homework.attempt_dedup import same_work_reaches_the_new_path
from course_supporter.homework.path_config import (
    PathConfig,
    ServedBy,
    load_path_config,
)
from course_supporter.models.source import AssignmentType

_SHIPPED = Path(__file__).resolve().parents[3] / "config" / "submission_paths.yaml"


def _shipped_with_project_on_the_new_path() -> PathConfig:
    raw = load_path_config(_SHIPPED).model_dump(mode="json")
    raw["task_types"]["project"]["served_by"] = ServedBy.NEW_PATH.value
    return PathConfig.model_validate(raw)


class TestTheRule:
    @pytest.mark.parametrize(
        ("task_type", "reaches"),
        [
            # On the new path, deduplicated: the doors meet an identical file.
            ("task", True),
            ("short_task", True),
            # On the new path, never deduplicated: every attempt scored anew.
            ("test", False),
            # On today's Mentor: the entry's check stays.
            ("project", False),
        ],
    )
    def test_the_shipped_file(self, task_type: str, reaches: bool) -> None:
        shipped = load_path_config(_SHIPPED)

        assert same_work_reaches_the_new_path(task_type, shipped) is reaches

    def test_a_text_type_handed_back_to_todays_mentor_keeps_the_check(self) -> None:
        handed_back = shipped_with_todays_mentor_for(AssignmentType.TASK)

        assert same_work_reaches_the_new_path("task", handed_back) is False
        assert same_work_reaches_the_new_path("short_task", handed_back) is True

    def test_a_project_moved_to_the_new_path_follows_the_file(self) -> None:
        """No list of types: the project's adapter deduplicates, so it goes."""
        moved = _shipped_with_project_on_the_new_path()

        assert same_work_reaches_the_new_path("project", moved) is True

    def test_the_cached_file_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "course_supporter.homework.attempt_dedup.get_path_config",
            lambda: shipped_with_todays_mentor_for(AssignmentType.SHORT_TASK),
        )

        assert same_work_reaches_the_new_path("short_task") is False
        assert same_work_reaches_the_new_path("task") is True
