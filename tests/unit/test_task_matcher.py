"""Tests for task matcher module."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from course_supporter.homework.matcher import TaskMatcher, TaskMatchError
from course_supporter.models.matching import LLMMatchResponse, LLMTaskMatch
from course_supporter.models.safety import FileContent, SubmissionContent


def _mock_editable(
    *,
    node_id: uuid.UUID | None = None,
    title: str = "Test Task",
    node_type: str = "exercise",
    description: str | None = "Solve the problem",
    learning_goal: str | None = None,
    success_criteria: str | None = None,
    methodological_content: dict[str, object] | None = None,
) -> MagicMock:
    """Create a mock StructureNodeEditable."""
    node = MagicMock()
    node.id = node_id or uuid.uuid4()
    node.title = title
    node.node_type = node_type
    node.description = description
    node.learning_goal = learning_goal
    node.success_criteria = success_criteria
    node.methodological_content = methodological_content
    return node


def _submission(text: str = "x = 42\nprint(x)") -> SubmissionContent:
    """Create a simple SubmissionContent."""
    return SubmissionContent(
        files=[FileContent(filename="solution.py", content=text, size=len(text))],
        total_size=len(text),
    )


@pytest.fixture()
def mock_router() -> MagicMock:
    router = MagicMock()
    router.complete_structured = AsyncMock()
    return router


@pytest.fixture()
def mock_response() -> MagicMock:
    resp = MagicMock()
    resp.model_id = "deepseek-chat"
    resp.tokens_in = 200
    resp.tokens_out = 100
    resp.cost_usd = 0.001
    return resp


class TestTaskMatcherSingleTask:
    """Single file → single task matching."""

    async def test_matches_single_task(
        self, mock_router: MagicMock, mock_response: MagicMock
    ) -> None:
        """Submission matches a single exercise."""
        task_node = _mock_editable(title="Print Hello World")
        llm_result = LLMMatchResponse(
            matches=[
                LLMTaskMatch(task_index=0, confidence=0.95, section_hint="entire file")
            ],
            explanation="Submission prints 'hello', matches the hello world exercise.",
        )
        mock_router.complete_structured.return_value = (llm_result, mock_response)

        matcher = TaskMatcher()
        result = await matcher.match(
            _submission("print('hello')"),
            [task_node],
            mock_router,
        )

        assert len(result.matches) == 1
        assert result.primary_task_id == task_node.id
        assert result.matches[0].confidence == 0.95
        assert result.matches[0].task_title == "Print Hello World"

    async def test_validates_task_hint(
        self, mock_router: MagicMock, mock_response: MagicMock
    ) -> None:
        """task_hint_id is accepted when it exists in task list."""
        task_node = _mock_editable(title="Variables Exercise")
        llm_result = LLMMatchResponse(
            matches=[LLMTaskMatch(task_index=0, confidence=0.9, section_hint="")],
            explanation="Matches variables exercise.",
        )
        mock_router.complete_structured.return_value = (llm_result, mock_response)

        matcher = TaskMatcher()
        result = await matcher.match(
            _submission(),
            [task_node],
            mock_router,
            task_hint_id=task_node.id,
        )

        assert result.primary_task_id == task_node.id


class TestTaskMatcherMultipleTasks:
    """Single file → multiple tasks matching."""

    async def test_matches_multiple_tasks(
        self, mock_router: MagicMock, mock_response: MagicMock
    ) -> None:
        """One file solving two exercises."""
        task1 = _mock_editable(title="Exercise 1: Sum")
        task2 = _mock_editable(title="Exercise 2: Product")
        task3 = _mock_editable(title="Exercise 3: Unrelated")

        llm_result = LLMMatchResponse(
            matches=[
                LLMTaskMatch(
                    task_index=0,
                    confidence=0.95,
                    section_hint="function sum_numbers()",
                ),
                LLMTaskMatch(
                    task_index=1,
                    confidence=0.88,
                    section_hint="function product()",
                ),
            ],
            explanation="File contains solutions for exercises 1 and 2.",
        )
        mock_router.complete_structured.return_value = (llm_result, mock_response)

        matcher = TaskMatcher()
        result = await matcher.match(
            _submission("def sum_numbers(): ...\ndef product(): ..."),
            [task1, task2, task3],
            mock_router,
        )

        assert len(result.matches) == 2
        assert result.primary_task_id == task1.id
        assert result.matches[0].confidence > result.matches[1].confidence

    async def test_project_as_single_task(
        self, mock_router: MagicMock, mock_response: MagicMock
    ) -> None:
        """Archive with many files → one project task."""
        project_node = _mock_editable(
            title="Final Project",
            node_type="lesson",
            methodological_content={
                "tasks": [{"type": "project", "title": "Build app"}],
            },
        )

        llm_result = LLMMatchResponse(
            matches=[
                LLMTaskMatch(
                    task_index=0, confidence=0.92, section_hint="entire project"
                )
            ],
            explanation="Multi-file submission is a project.",
        )
        mock_router.complete_structured.return_value = (llm_result, mock_response)

        content = SubmissionContent(
            files=[
                FileContent(filename="main.py", content="...", size=100),
                FileContent(filename="utils.py", content="...", size=80),
                FileContent(filename="tests/test_main.py", content="...", size=60),
            ],
            total_size=240,
        )

        matcher = TaskMatcher()
        result = await matcher.match(content, [project_node], mock_router)

        assert len(result.matches) == 1
        assert result.matches[0].task_type == "lesson"


class TestTaskMatcherErrors:
    """Error handling in task matching."""

    async def test_no_task_nodes_raises(self, mock_router: MagicMock) -> None:
        """No task nodes in course → TaskMatchError."""
        # Only a module node (not task-like)
        module_node = _mock_editable(
            title="Module 1",
            node_type="module",
            methodological_content=None,
        )

        matcher = TaskMatcher()
        with pytest.raises(TaskMatchError, match="No task nodes"):
            await matcher.match(_submission(), [module_node], mock_router)

    async def test_llm_returns_no_matches(
        self, mock_router: MagicMock, mock_response: MagicMock
    ) -> None:
        """LLM returns empty matches → TaskMatchError."""
        task_node = _mock_editable(title="Some Exercise")
        llm_result = LLMMatchResponse(
            matches=[],
            explanation="Submission does not match any available task.",
        )
        mock_router.complete_structured.return_value = (llm_result, mock_response)

        matcher = TaskMatcher()
        with pytest.raises(TaskMatchError, match="could not match"):
            await matcher.match(_submission(), [task_node], mock_router)

    async def test_invalid_index_filtered(
        self, mock_router: MagicMock, mock_response: MagicMock
    ) -> None:
        """LLM returns out-of-range index → filtered out."""
        task_node = _mock_editable(title="Exercise 1")
        llm_result = LLMMatchResponse(
            matches=[
                LLMTaskMatch(task_index=0, confidence=0.9, section_hint=""),
                LLMTaskMatch(task_index=99, confidence=0.5, section_hint=""),
            ],
            explanation="Index 99 is out of range.",
        )
        mock_router.complete_structured.return_value = (llm_result, mock_response)

        matcher = TaskMatcher()
        result = await matcher.match(_submission(), [task_node], mock_router)

        assert len(result.matches) == 1
        assert result.matches[0].node_id == task_node.id
