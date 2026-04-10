"""Tests for MentorContext builder."""

from __future__ import annotations

import uuid

from course_supporter.homework.mentor_context import (
    _build_student_history,
    _extract_methodist_fields,
)


class TestExtractMethodistFields:
    """Extract grading criteria, steps, etc. from Methodist output."""

    def test_full_methodist_output(self) -> None:
        """All fields present in Methodist output."""
        mc = {
            "learning_objectives": ["Understand loops", "Apply list comprehension"],
            "common_misconceptions": ["for-else is like if-else"],
            "teaching_recommendations": "Start with simple examples",
            "key_concepts_detailed": [
                {"name": "list comprehension", "definition": "Concise list creation"},
                {"name": "generator", "definition": "Lazy evaluation"},
            ],
            "recommended_assignments": [
                {
                    "title": "Loop exercise",
                    "description": "Write a loop that filters data",
                    "grading_criteria": "Correct output, clean code",
                    "steps": ["Read input", "Filter", "Output"],
                }
            ],
        }
        result = _extract_methodist_fields(mc, uuid.uuid4())

        assert result["learning_objectives"] == [
            "Understand loops",
            "Apply list comprehension",
        ]
        assert result["grading_criteria"] == "Correct output, clean code"
        assert result["assignment_steps"] == ["Read input", "Filter", "Output"]
        assert result["common_misconceptions"] == ["for-else is like if-else"]
        assert len(result["key_concepts"]) == 2

    def test_empty_methodist_output(self) -> None:
        """None or empty Methodist output returns empty dict."""
        assert _extract_methodist_fields(None, None) == {}
        assert _extract_methodist_fields({}, None) == {}

    def test_partial_methodist_no_assignments(self) -> None:
        """Methodist output without assignment recommendations."""
        mc = {
            "learning_objectives": ["Understand OOP"],
            "common_misconceptions": [],
        }
        result = _extract_methodist_fields(mc, uuid.uuid4())
        assert result["learning_objectives"] == ["Understand OOP"]
        assert result.get("grading_criteria") is None

    def test_key_concepts_skip_empty_names(self) -> None:
        """Key concepts with empty names are filtered out."""
        mc = {
            "key_concepts_detailed": [
                {"name": "valid", "definition": "ok"},
                {"name": "", "definition": "skip"},
                {"definition": "also skip"},
            ],
        }
        result = _extract_methodist_fields(mc, uuid.uuid4())
        assert len(result["key_concepts"]) == 1
        assert result["key_concepts"][0]["name"] == "valid"


class TestBuildStudentHistory:
    """Build compact history from past submissions."""

    def _make_submission(
        self,
        *,
        task_title: str = "Exercise 1",
        score: int = 80,
        passed: bool = True,
        issues: list[str] | None = None,
        notable: list[str] | None = None,
    ) -> object:
        """Create a mock submission with review_result."""
        from unittest.mock import MagicMock

        sub = MagicMock()
        matched = MagicMock()
        matched.title = task_title
        sub.matched_task = matched
        sub.review_result = {
            "analysis": {
                "score": score,
                "passed": passed,
                "issues": [{"description": i} for i in (issues or [])],
                "notable_solutions": [{"description": n} for n in (notable or [])],
            }
        }
        return sub

    def test_builds_history(self) -> None:
        subs = [
            self._make_submission(
                task_title="Lists",
                score=90,
                passed=True,
                issues=["Missing edge case"],
                notable=["Elegant comprehension"],
            ),
            self._make_submission(
                task_title="Dicts",
                score=70,
                passed=True,
            ),
        ]
        history = _build_student_history(subs)
        assert len(history) == 2
        assert history[0].task_title == "Lists"
        assert history[0].score == 90
        assert "Missing edge case" in history[0].issues_summary
        assert "Elegant comprehension" in history[0].notable_solutions_summary

    def test_max_entries(self) -> None:
        subs = [self._make_submission(task_title=f"T{i}") for i in range(10)]
        history = _build_student_history(subs, max_entries=3)
        assert len(history) == 3

    def test_empty_submissions(self) -> None:
        assert _build_student_history([]) == []

    def test_no_review_result(self) -> None:
        """Submission without review_result still builds summary."""
        from unittest.mock import MagicMock

        sub = MagicMock()
        sub.matched_task = None
        sub.review_result = None
        history = _build_student_history([sub])
        assert len(history) == 1
        assert history[0].task_title == "(unknown)"
        assert history[0].score is None

    def test_malformed_review_result_empty_analysis(self) -> None:
        """Missing 'analysis' key in review_result is handled gracefully."""
        from unittest.mock import MagicMock

        sub = MagicMock()
        sub.id = "test-id"
        matched = MagicMock()
        matched.title = "Exercise 1"
        sub.matched_task = matched
        sub.review_result = {}
        history = _build_student_history([sub])
        assert len(history) == 1
        assert history[0].score is None
        assert history[0].issues_summary == []

    def test_malformed_review_result_issues_not_list(self) -> None:
        """Non-list 'issues' in review_result is handled gracefully."""
        from unittest.mock import MagicMock

        sub = MagicMock()
        sub.id = "test-id"
        matched = MagicMock()
        matched.title = "Exercise 1"
        sub.matched_task = matched
        sub.review_result = {"analysis": {"issues": "not a list", "score": 50}}
        history = _build_student_history([sub])
        assert len(history) == 1
        assert history[0].score == 50
        assert history[0].issues_summary == []
