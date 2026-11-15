"""The port a submission's result is built through (task 07, decision 6).

The body knows a builder by its task type and nothing more; this pins the table
it asks and the one refusal a builder may give by name.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from course_supporter.homework import result_builders, test_result, text_result
from course_supporter.homework.path_config import (
    PathConfig,
    ServedBy,
    load_path_config,
)
from course_supporter.models.source import AssignmentType

_SHIPPED = Path("config/submission_paths.yaml")


def _served(config: PathConfig, task_type: AssignmentType, by: ServedBy) -> PathConfig:
    """``config`` with ``task_type`` served by ``by``, its paths as they are."""
    declared = config.task_types[task_type].model_copy(update={"served_by": by})
    return config.model_copy(
        update={"task_types": {**config.task_types, task_type: declared}}
    )


def _without_a_builder_for(
    monkeypatch: pytest.MonkeyPatch, missing: AssignmentType
) -> None:
    """The shipped table, but ``missing`` has no builder in it."""
    shipped = result_builders.get_result_builder

    def _table(task_type: AssignmentType) -> result_builders.ResultBuilder | None:
        return None if task_type is missing else shipped(task_type)

    monkeypatch.setattr(result_builders, "get_result_builder", _table)


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

    def test_a_project_is_built_by_the_text_builder(self) -> None:
        """Project chain K6, lock a: the text task's builder, as it is -- the
        review it builds is ``tests/integration/test_project_review_db.py``."""
        builder = result_builders.get_result_builder(AssignmentType.PROJECT)

        assert isinstance(builder, text_result.TextResultBuilder)

    def test_every_type_has_a_builder(self) -> None:
        assert all(
            result_builders.get_result_builder(task_type) is not None
            for task_type in AssignmentType
        )

    def test_a_refusal_carries_the_code_the_submission_fails_with(self) -> None:
        refusal = result_builders.ResultNotBuiltError("test_not_ready", "no key")

        assert refusal.code == "test_not_ready"
        assert refusal.details == "no key"
        assert str(refusal) == "test_not_ready: no key"


class TestTheStartupCheck:
    """Project chain K6, lock b: a type served by the new path without a
    builder stops the boot, by name (``boot.py``, beside the stage check)."""

    def test_the_shipped_configuration_starts(self) -> None:
        config = load_path_config(_SHIPPED)

        assert result_builders.missing_result_builders(config) == []
        result_builders.validate_result_builders(config)

    def test_a_type_on_the_new_path_without_a_builder_does_not(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config = _served(
            load_path_config(_SHIPPED), AssignmentType.PROJECT, ServedBy.NEW_PATH
        )
        _without_a_builder_for(monkeypatch, AssignmentType.PROJECT)

        with pytest.raises(ValueError, match="'project'") as refused:
            result_builders.validate_result_builders(config)

        assert "without a result builder" in str(refused.value)
        assert "'task'" not in str(refused.value)

    def test_a_type_on_todays_mentor_needs_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Today's Mentor writes its own review: no builder is asked for."""
        config = _served(
            load_path_config(_SHIPPED), AssignmentType.TASK, ServedBy.TODAYS_MENTOR
        )
        _without_a_builder_for(monkeypatch, AssignmentType.TASK)

        result_builders.validate_result_builders(config)

    def test_every_such_type_is_named_at_once(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config = load_path_config(_SHIPPED)
        monkeypatch.setattr(result_builders, "get_result_builder", lambda _t: None)

        assert result_builders.missing_result_builders(config) == [
            task_type
            for task_type in AssignmentType
            if config.task_types[task_type].served_by is ServedBy.NEW_PATH
        ]
