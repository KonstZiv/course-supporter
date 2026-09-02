"""The submission text budget: derived, not written, and never truncating."""

from __future__ import annotations

import pytest

from course_supporter.homework.text_budget import (
    STAGES_READING_SUBMISSION,
    ensure_single_file_fits,
    fit_archive_entries,
    submission_text_budget_chars,
)
from course_supporter.llm.ladder_config import load_ladder_config
from course_supporter.llm.registry import load_registry
from course_supporter.security.archive import ExtractedFile
from course_supporter.security.exceptions import ErrorCategory, SecurityRejectedError


def _entry(name: str, size: int) -> ExtractedFile:
    return ExtractedFile(arcname=name, content=b"x" * size, depth=0)


class TestBudgetIsDerived:
    """The number comes from the models that will read the text."""

    def test_equals_the_tightest_reading_window(self) -> None:
        from pathlib import Path

        ladders = load_ladder_config(Path("config"))
        registry = load_registry(Path("config/external_services.yaml"))
        windows = [
            model.max_context
            for name in STAGES_READING_SUBMISSION
            for rung in ladders.stages[name].ladder
            if (model := registry.models.get(rung.model)) is not None
            and model.max_context is not None
        ]
        # Half the tightest window, two characters per token — recomputed here
        # from the config rather than restated as a literal, so the test fails
        # if the derivation stops matching its inputs rather than if someone
        # changes a rung.
        assert submission_text_budget_chars() == int(min(windows) * 0.5 * 2.0)

    def test_the_four_reading_stages_are_the_ones_that_read(self) -> None:
        # criteria_decomposition is deliberately absent: it reads the task, not
        # the submission, which is why its result caches across attempts.
        assert set(STAGES_READING_SUBMISSION) == {
            "safety_check",
            "sanity_check",
            "mentor_layered_evaluation_node_course",
            "mentor_layered_evaluation_industry",
        }

    def test_lands_in_the_ratified_order_of_magnitude(self) -> None:
        # FORMATS.md put the expectation at 100-150k characters. This is not a
        # restatement of the formula — it is the sanity check that the formula
        # produces the size the product decision assumed.
        assert 100_000 <= submission_text_budget_chars() <= 150_000


class TestSingleFile:
    def test_within_budget_passes(self) -> None:
        ensure_single_file_fits("a" * 100, filename="hw.md", budget_chars=1000)

    def test_over_budget_is_refused_not_trimmed(self) -> None:
        with pytest.raises(SecurityRejectedError) as exc_info:
            ensure_single_file_fits("a" * 1001, filename="hw.md", budget_chars=1000)
        assert exc_info.value.category is ErrorCategory.OVER_BUDGET
        assert "1001" in exc_info.value.detail


class TestArchiveFitting:
    def test_smallest_first_keeps_the_most_files(self) -> None:
        # One generated artefact next to three hand-written files. Fitting the
        # big one first would drop all three; smallest-first keeps them.
        entries = [
            _entry("bundle.js", 400),
            *(_entry(f"m{i}.py", 60) for i in range(3)),
        ]
        fitted = fit_archive_entries(entries, budget_chars=300)

        assert [n.arcname for n in fitted.over_budget] == ["bundle.js"]
        assert fitted.over_budget[0].reason is ErrorCategory.OVER_BUDGET
        for i in range(3):
            assert f"--- m{i}.py ---" in fitted.text
        assert "bundle.js" not in fitted.text

    def test_kept_files_stay_in_archive_order(self) -> None:
        # Selection is by size; presentation is not. The Mentor should see the
        # work as the student packed it.
        entries = [_entry("z_last.py", 90), _entry("a_first.py", 10)]
        fitted = fit_archive_entries(entries, budget_chars=10_000)
        assert fitted.text.index("z_last.py") < fitted.text.index("a_first.py")

    def test_nothing_is_truncated(self) -> None:
        entries = [_entry("solution.py", 50)]
        fitted = fit_archive_entries(entries, budget_chars=10_000)
        assert fitted.text.endswith("x" * 50)
        assert fitted.over_budget == ()

    def test_everything_over_budget_leaves_an_empty_body(self) -> None:
        # The caller turns this into a refusal; the fitter's job is only to
        # report honestly that nothing fit.
        entries = [_entry("huge.py", 5000)]
        fitted = fit_archive_entries(entries, budget_chars=100)
        assert fitted.text == ""
        assert [n.arcname for n in fitted.over_budget] == ["huge.py"]

    def test_the_frame_counts_against_the_budget(self) -> None:
        # The separator is characters the model pays for like any other.
        entry = _entry("a.py", 10)
        frame_len = len("--- a.py ---\n")
        assert fit_archive_entries([entry], budget_chars=10 + frame_len).text != ""
        assert fit_archive_entries([entry], budget_chars=10 + frame_len - 1).text == ""
