"""Tests for MentorContext builder."""

from __future__ import annotations

from course_supporter.homework.mentor_context import (
    _build_student_history,
)


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
