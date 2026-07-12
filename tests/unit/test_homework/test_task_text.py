"""Unit tests for the budgeted task_text stitcher (task-code-materials c6)."""

from __future__ import annotations

from course_supporter.homework.task_text import (
    MENTOR_TASK_TEXT_MAX_BYTES,
    stitch_task_text,
)


class TestStitchTaskText:
    def test_under_budget_byte_identical_to_plain_join(self) -> None:
        rows = ["first segment", None, "", "second segment"]
        assert stitch_task_text(rows) == "first segment\n\nsecond segment"

    def test_over_budget_drops_whole_segments_with_marker(self) -> None:
        rows = ["a" * 600, "b" * 600, "c" * 600]
        out = stitch_task_text(rows, max_bytes=1300)
        assert "a" * 600 in out
        assert "b" * 600 in out
        assert "c" * 600 not in out
        assert "[TASK_TEXT TRUNCATED: 1 of 3 segments dropped" in out

    def test_never_emits_partial_segment(self) -> None:
        rows = ["x" * 1000, "y" * 1000]
        out = stitch_task_text(rows, max_bytes=1100)
        assert "y" * 10 not in out
        assert out.startswith("x" * 1000)

    def test_default_budget_mirrors_mentor_context(self) -> None:
        assert MENTOR_TASK_TEXT_MAX_BYTES == 512 * 1024

    def test_empty_rows_yield_empty_string(self) -> None:
        assert stitch_task_text([]) == ""
        assert stitch_task_text([None, ""]) == ""
