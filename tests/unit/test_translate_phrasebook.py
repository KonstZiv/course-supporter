"""Tests for the phrasebook translation script (mentor-rebuild task 04).

Nothing here calls a provider: the router is a double that hands back whatever
the test scripted, so the run's rules — the order, the one-file-per-language
write, the placeholder refusal, the absence of retries — are checked without
paying for them.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from scripts.translate_phrasebook import (
    CONSECUTIVE_FAILURE_LIMIT,
    LanguagePlan,
    estimate_run_usd,
    parse_answer,
    plan_run,
    run,
    write_language,
)

from course_supporter.phrasebook import SOURCE_LANGUAGE, Phrase, load_language_file

_SOURCE = {
    "section.fixed": Phrase("Виправлено", reviewed=False),
    "position.slide": Phrase("слайд {number}", reviewed=False),
}


@dataclass
class _Answer:
    content: str
    attempt_count: int = 1


class _Router:
    """Stands in for the router: one scripted answer per language, calls counted."""

    def __init__(self, answers: dict[str, Any]) -> None:
        self._answers = answers
        self.calls: list[str] = []

    async def execute_for_stage(self, stage: str, **context: Any) -> _Answer:
        code = context["language_code"]
        self.calls.append(code)
        answer = self._answers[code]
        if isinstance(answer, Exception):
            raise answer
        return _Answer(content=answer)


def _translation(*keys: str) -> str:
    bodies = {"section.fixed": "Fixed", "position.slide": "slide {number}"}
    return json.dumps({key: bodies[key] for key in keys}, ensure_ascii=False)


class TestPlan:
    def test_english_goes_first_and_the_source_is_not_a_target(
        self, tmp_path: Path
    ) -> None:
        plans = plan_run(_SOURCE, [SOURCE_LANGUAGE, "pol", "eng", "fas"], tmp_path)

        assert [plan.code for plan in plans] == ["eng", "fas", "pol"]
        assert plans[0].name == "English"

    def test_a_finished_language_is_not_called_again(self, tmp_path: Path) -> None:
        write_language(
            tmp_path,
            "eng",
            {key: Phrase(f"{key}!", reviewed=False) for key in _SOURCE},
        )

        plans = plan_run(_SOURCE, [SOURCE_LANGUAGE, "eng", "pol"], tmp_path)

        # This is what makes an interrupted run finishable by starting it again.
        assert [plan.code for plan in plans] == ["pol"]

    def test_a_reviewed_phrase_is_never_asked_for_again(self, tmp_path: Path) -> None:
        write_language(
            tmp_path, "eng", {"section.fixed": Phrase("Fixed", reviewed=True)}
        )

        plans = plan_run(
            _SOURCE,
            [SOURCE_LANGUAGE, "eng"],
            tmp_path,
            only_keys=("section.fixed", "position.slide"),
        )

        assert [plan.keys for plan in plans] == [("position.slide",)]


class TestAnswer:
    def test_a_good_answer_reads_as_phrases(self) -> None:
        parsed = parse_answer(_translation("section.fixed"), ("section.fixed",))

        assert parsed == {"section.fixed": "Fixed"}

    @pytest.mark.parametrize(
        ("content", "match"),
        [
            ("not json at all", "not JSON"),
            ("[1, 2]", "not an object"),
            ('{"section.fixed": "Fixed"}', "misses"),
            ('{"section.fixed": "F", "position.slide": "s", "x": "y"}', "invents"),
            ('{"section.fixed": 1, "position.slide": "s"}', "non-string"),
        ],
        ids=["not-json", "not-object", "short", "invented", "non-string"],
    )
    def test_a_batch_that_came_back_wrong_is_refused(
        self, content: str, match: str
    ) -> None:
        with pytest.raises(ValueError, match=match):
            parse_answer(content, ("section.fixed", "position.slide"))


class TestWrite:
    def test_the_header_says_the_file_is_machine_made(self, tmp_path: Path) -> None:
        write_language(tmp_path, "pol", {"a": Phrase("A", reviewed=False)})

        text = (tmp_path / "pol.yaml").read_text(encoding="utf-8")
        assert text.startswith("# Machine translation, not reviewed.")

    def test_a_fully_reviewed_file_carries_no_such_header(self, tmp_path: Path) -> None:
        write_language(tmp_path, "pol", {"a": Phrase("A", reviewed=True)})

        text = (tmp_path / "pol.yaml").read_text(encoding="utf-8")
        assert "Machine translation" not in text
        assert load_language_file(tmp_path / "pol.yaml")["a"].reviewed is True


class TestRun:
    async def test_each_language_is_written_as_it_lands(self, tmp_path: Path) -> None:
        plans = [
            LanguagePlan("eng", "English", tuple(_SOURCE)),
            LanguagePlan("pol", "Polish", tuple(_SOURCE)),
        ]
        router = _Router({code: _translation(*_SOURCE) for code in ("eng", "pol")})

        report = await run(router, plans, _SOURCE, tmp_path)  # type: ignore[arg-type]

        assert report.written == ["eng", "pol"]
        assert report.calls == 2
        assert (tmp_path / "eng.yaml").exists()
        assert load_language_file(tmp_path / "pol.yaml")["position.slide"].text == (
            "slide {number}"
        )

    async def test_a_mangled_placeholder_is_refused_and_not_retried(
        self, tmp_path: Path
    ) -> None:
        plans = [LanguagePlan("pol", "Polish", tuple(_SOURCE))]
        router = _Router(
            {
                "pol": json.dumps(
                    {"section.fixed": "Poprawione", "position.slide": "slajd numer"}
                )
            }
        )

        report = await run(router, plans, _SOURCE, tmp_path)  # type: ignore[arg-type]

        assert report.written == []
        assert report.lock_failures == 1
        assert "position.slide" in report.failed[0][1]
        # No second attempt at the same language: a silent retry would make the
        # call count disagree with the Usage difference.
        assert router.calls == ["pol"]
        assert not (tmp_path / "pol.yaml").exists()

    async def test_a_reviewed_phrase_survives_the_run(self, tmp_path: Path) -> None:
        write_language(
            tmp_path, "pol", {"section.fixed": Phrase("Poprawione", reviewed=True)}
        )
        plans = [LanguagePlan("pol", "Polish", ("position.slide",))]
        router = _Router({"pol": json.dumps({"position.slide": "slajd {number}"})})

        await run(router, plans, _SOURCE, tmp_path)  # type: ignore[arg-type]

        written = load_language_file(tmp_path / "pol.yaml")
        assert written["section.fixed"].text == "Poprawione"
        assert written["section.fixed"].reviewed is True
        assert written["position.slide"].text == "slajd {number}"

    async def test_five_in_a_row_stops_the_run(self, tmp_path: Path) -> None:
        codes = ["eng", "fas", "pol", "spa", "tur", "ukr-x"]
        plans = [LanguagePlan(code, code, tuple(_SOURCE)) for code in codes]
        mangled = json.dumps(
            {"section.fixed": "x", "position.slide": "y"}
        )  # placeholder dropped
        router = _Router(dict.fromkeys(codes, mangled))

        report = await run(router, plans, _SOURCE, tmp_path)  # type: ignore[arg-type]

        assert report.lock_failures == CONSECUTIVE_FAILURE_LIMIT
        assert report.stopped_early is not None
        assert "prompt" in report.stopped_early
        # The sixth language was never called: the run stopped paying.
        assert router.calls == codes[:CONSECUTIVE_FAILURE_LIMIT]

    async def test_a_failing_first_call_stops_the_run_at_once(
        self, tmp_path: Path
    ) -> None:
        plans = [
            LanguagePlan("eng", "English", tuple(_SOURCE)),
            LanguagePlan("pol", "Polish", tuple(_SOURCE)),
        ]
        router = _Router({"eng": RuntimeError("403 model not accessible")})

        report = await run(router, plans, _SOURCE, tmp_path)  # type: ignore[arg-type]

        # An access refusal shows up here, and paying it fifty-nine more times
        # proves nothing.
        assert report.stopped_early is not None
        assert "403" in report.stopped_early
        assert router.calls == ["eng"]


class TestEstimate:
    def test_it_prices_the_ceiling_times_the_languages(self) -> None:
        plans = [LanguagePlan(code, code, tuple(_SOURCE)) for code in ("eng", "pol")]

        estimate, tokens_in = estimate_run_usd(
            plans,
            _SOURCE,
            output_ceiling=8192,
            price_in_per_1k=0.00125,
            price_out_per_1k=0.010,
        )

        per_call = tokens_in / 1000 * 0.00125 + 8192 / 1000 * 0.010
        assert estimate == pytest.approx(per_call * 2)
        # The output side is the ceiling, not a guess: the estimate can only
        # overstate what the run costs.
        assert estimate > 2 * 8192 / 1000 * 0.010
