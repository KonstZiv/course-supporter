"""The port a submission's result is built through (task 07, decision 6).

The body knows a builder by its task type and nothing more; this pins the table
it asks and the one refusal a builder may give by name.
"""

from __future__ import annotations

import pytest

from course_supporter.homework import result_builders, test_result, text_result
from course_supporter.models.source import AssignmentType


class TestTheRegistry:
    def test_a_test_is_built_by_the_test_builder(self) -> None:
        builder = result_builders.get_result_builder(AssignmentType.TEST)

        assert isinstance(builder, test_result.TestResultBuilder)

    @pytest.mark.parametrize(
        "task_type", [AssignmentType.TASK, AssignmentType.SHORT_TASK]
    )
    def test_a_text_task_is_built_by_the_text_builder(
        self, task_type: AssignmentType
    ) -> None:
        """From its verdicts and their explanation (task 09b)."""
        builder = result_builders.get_result_builder(task_type)

        assert isinstance(builder, text_result.TextResultBuilder)

    def test_a_project_has_no_builder_yet(self) -> None:
        """Its path ends without a written review until its own switch."""
        assert result_builders.get_result_builder(AssignmentType.PROJECT) is None

    def test_a_refusal_carries_the_code_the_submission_fails_with(self) -> None:
        refusal = result_builders.ResultNotBuiltError("test_not_ready", "no key")

        assert refusal.code == "test_not_ready"
        assert refusal.details == "no key"
        assert str(refusal) == "test_not_ready: no key"
