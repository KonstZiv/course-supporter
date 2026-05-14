"""Tests for AssignmentType taxonomy (post-C9.3 translocation to models.source)."""

from __future__ import annotations

from course_supporter.models.source import AssignmentType


class TestAssignmentType:
    def test_all_types_exist(self) -> None:
        assert AssignmentType.TEST == "test"
        assert AssignmentType.SHORT_TASK == "short_task"
        assert AssignmentType.TASK == "task"
        assert AssignmentType.PROJECT == "project"
