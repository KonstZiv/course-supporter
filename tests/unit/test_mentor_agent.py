"""Tests for MentorAgent two-step review pipeline."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from course_supporter.homework.mentor import (
    MentorAgent,
    _build_analysis_user_prompt,
    _extract_code_fragments,
)
from course_supporter.homework.mentor_context import (
    MentorContext,
    PastSubmissionSummary,
)
from course_supporter.models.mentor import (
    MentorAnalysis,
    NotableSolution,
    ReviewIssue,
)


def _minimal_context(**overrides: object) -> MentorContext:
    """Create a minimal MentorContext for testing."""
    defaults = {
        "submission_content": "def add(a, b):\n    return a + b\n",
        "task_title": "Simple addition",
        "node_type": "exercise",
        "response_language": "uk",
    }
    defaults.update(overrides)
    return MentorContext(**defaults)  # type: ignore[arg-type]


def _rich_context() -> MentorContext:
    """Create a context with all fields populated."""
    return MentorContext(
        submission_content="def add(a, b):\n    return a + b\n",
        task_title="Implement addition",
        node_type="exercise",
        response_language="uk",
        task_description="Write a function that adds two numbers.",
        learning_goal="Understand functions and return values.",
        difficulty="beginner",
        estimated_duration=15,
        success_criteria="Function returns correct sum for all inputs.",
        assessment_method="automated test",
        grading_criteria="Correct output, handles edge cases.",
        assignment_description="Create a function add(a, b).",
        assignment_steps=["Define function", "Return sum", "Test with edge cases"],
        learning_objectives=["Understand parameters", "Use return statement"],
        common_mistakes=[
            "Forgetting to return",
            "String concatenation instead of addition",
        ],
        common_misconceptions=["+ always means addition"],
        key_concepts=[
            {"name": "function", "definition": "Reusable block of code"},
            {"name": "return", "definition": "Sends value back to caller"},
        ],
        course_title="Python Basics",
        course_description="Introductory Python course.",
        student_history=[
            PastSubmissionSummary(
                task_title="Variables",
                score=85,
                passed=True,
                issues_summary=["Mutable default argument"],
                notable_solutions_summary=["Used f-strings consistently"],
            ),
        ],
    )


def _sample_analysis() -> MentorAnalysis:
    """Create a sample MentorAnalysis for testing."""
    return MentorAnalysis(
        passed=True,
        score=85,
        task_understanding="Add two numbers and return the result.",
        correctness="correct",
        issues=[
            ReviewIssue(
                severity="minor",
                description="No type hints.",
                suggestion="Add type annotations: def add(a: int, b: int) -> int",
                code_fragment="def add(a, b):",
            ),
        ],
        notable_solutions=[
            NotableSolution(
                description="Clean, minimal implementation.",
                concept="KISS principle",
            ),
        ],
        strengths=["Readable code"],
        recommendations=["Add type hints for better documentation."],
    )


class TestBuildAnalysisUserPrompt:
    """Dynamic user prompt construction."""

    def test_minimal_context(self) -> None:
        """Minimal context produces task + submission sections."""
        ctx = _minimal_context()
        prompt = _build_analysis_user_prompt(ctx)
        assert "## Task: Simple addition" in prompt
        assert "exercise" in prompt
        assert "def add(a, b):" in prompt
        # No criteria sections
        assert "Evaluation Criteria" not in prompt
        assert "Expected Steps" not in prompt

    def test_rich_context(self) -> None:
        """Rich context includes all sections."""
        ctx = _rich_context()
        prompt = _build_analysis_user_prompt(ctx)
        assert "## Task: Implement addition" in prompt
        assert "Grading criteria" in prompt
        assert "Success criteria" in prompt
        assert "Expected Steps" in prompt
        assert "1. Define function" in prompt
        assert "Learning Objectives" in prompt
        assert "Key Concepts" in prompt
        assert "Known Pitfalls" in prompt
        assert "Forgetting to return" in prompt
        assert "Student History" in prompt
        assert "Variables" in prompt
        assert "Python Basics" in prompt

    def test_truncates_long_submission(self) -> None:
        """Submission content is truncated at limit."""
        ctx = _minimal_context(submission_content="x" * 100_000)
        prompt = _build_analysis_user_prompt(ctx)
        assert "[... truncated ...]" in prompt

    def test_partial_context(self) -> None:
        """Only available fields appear in prompt."""
        ctx = _minimal_context(
            task_description="Write addition",
            grading_criteria="Must return int",
        )
        prompt = _build_analysis_user_prompt(ctx)
        assert "Write addition" in prompt
        assert "Must return int" in prompt
        assert "Expected Steps" not in prompt
        assert "Student History" not in prompt

    def test_empty_strings_treated_as_missing(self) -> None:
        """Empty strings are excluded from prompt (spectrum context principle)."""
        ctx = _minimal_context(
            task_description="",
            learning_goal="",
            grading_criteria="",
            success_criteria="",
        )
        prompt = _build_analysis_user_prompt(ctx)
        assert "## Task: Simple addition" in prompt
        assert "Description" not in prompt
        assert "Learning goal" not in prompt
        assert "Evaluation Criteria" not in prompt


class TestExtractCodeFragments:
    """Code fragments extraction for humanize step."""

    def test_from_issues_and_notable(self) -> None:
        """Extracts fragments from issues and notable_solutions."""
        ctx = _minimal_context()
        analysis = _sample_analysis()
        fragments = _extract_code_fragments(ctx, analysis)
        assert "def add(a, b):" in fragments
        assert "Issue:" in fragments

    def test_fallback_to_submission(self) -> None:
        """No fragments in analysis → uses submission content."""
        ctx = _minimal_context()
        analysis = MentorAnalysis(
            passed=True,
            score=90,
            task_understanding="Add numbers.",
            correctness="correct",
        )
        fragments = _extract_code_fragments(ctx, analysis)
        assert "def add(a, b):" in fragments


class TestMentorAgent:
    """Integration test for the two-step pipeline (mocked LLM)."""

    async def test_review_calls_both_steps(self) -> None:
        """Review calls analysis then humanize, returns MentorReview."""
        ctx = _minimal_context()
        router = MagicMock()

        # Mock step 1: complete_structured returns (MentorAnalysis, response)
        mock_response_1 = MagicMock()
        mock_response_1.model_id = "deepseek-chat"
        mock_response_1.tokens_in = 1000
        mock_response_1.tokens_out = 500
        mock_response_1.cost_usd = 0.001
        router.complete_structured = AsyncMock(
            return_value=(_sample_analysis(), mock_response_1),
        )

        # Mock step 2: complete returns LLMResponse
        mock_response_2 = MagicMock()
        mock_response_2.model_id = "claude-opus-4-20250514"
        mock_response_2.tokens_in = 800
        mock_response_2.tokens_out = 300
        mock_response_2.cost_usd = 0.005
        mock_response_2.content = "Функція працює правильно. Додай type hints."
        router.complete = AsyncMock(return_value=mock_response_2)

        agent = MentorAgent()
        review = await agent.review(ctx, router)

        assert review.analysis.passed is True
        assert review.analysis.score == 85
        assert review.review_text == "Функція працює правильно. Додай type hints."
        assert review.response_language == "uk"
        assert review.reviewed_at is not None

        # Verify both LLM calls were made
        router.complete_structured.assert_awaited_once()
        router.complete.assert_awaited_once()

        # Verify actions
        call_args_1 = router.complete_structured.call_args
        assert call_args_1.kwargs["action"] == "homework_analysis"

        call_args_2 = router.complete.call_args
        assert call_args_2.kwargs["action"] == "homework_humanize"
