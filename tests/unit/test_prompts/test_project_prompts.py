"""The project's two prompts, rendered from a recorded project (chain K4).

``prompts/criteria_evaluation_project/v1.md`` and
``prompts/review_explanation_project/v1.md`` are rendered by the text
stages' own executors, so each must take exactly its twin's variables and
slots. Rendered on what the project's door really reads
(``tests/_helpers/recorded_project.py``): the delta context lands whole in
its slot, every frame on its line — the trusted block, NEW, CHANGED, NEIGHBOR,
the block of what was not opened — and the prompt names every frame the
context's builder writes (lock b). The explanation is shown the author's-code
flag of a verdict, and the prompt says how to speak of it (lock h, in part;
the stage on recorded answers is ``tests/integration/test_project_stages_db.py``).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from jinja2 import Environment, meta

from course_supporter.agents import criteria_evaluator, review_explainer
from course_supporter.criteria_kinds import CriteriaLayer
from course_supporter.homework import mentor_context
from course_supporter.homework.criteria_form import Criterion
from course_supporter.homework.doors import DoorReading
from course_supporter.homework.verdict_explanation import explanation_facts
from course_supporter.llm.prompt_loader_md import load_prompt, slot_names
from tests._helpers.recorded_project import STATS, UTILS, read_recorded_project

_PROMPTS = Path(__file__).resolve().parents[3] / "prompts"

_EVALUATION = "criteria_evaluation_project/v1.md"
_EXPLANATION = "review_explanation_project/v1.md"
_TWINS = {
    _EVALUATION: "criteria_evaluation/v1.md",
    _EXPLANATION: "review_explanation/v2.md",
}

# Every kind of file block the context's builder writes, and its own lines.
_FILE_KINDS = ("NEW", "CHANGED-FULL", "CHANGED-DIFF", "NEIGHBOR")
_FRAME_WORDS = ("TRUSTED", "END-TRUSTED", "FILE type=", "END-FILE")


def _criterion(cid: str, weight: str, points: int = 0) -> Criterion:
    return Criterion.model_validate(
        {
            "id": cid,
            "text": f"Критерій {cid}",
            "evidence": f"Що показує {cid}",
            "weight": weight,
            "check_method": "mandatory_points" if points else "model_verdict",
            "soft_descent": False,
            "concepts": [],
            "mandatory_points": [
                {"id": f"{cid}.p{n}", "text": f"Пункт {n} критерію {cid}"}
                for n in range(1, points + 1)
            ],
        }
    )


_CRITERIA = (_criterion("c1", "must"), _criterion("c2", "should", points=2))


def _variables(ref: str) -> set[str]:
    env = Environment(autoescape=False)  # noqa: S701 -- parsed, never rendered
    env.filters["tojson_unicode"] = json.dumps
    prompt = load_prompt(ref, base_path=_PROMPTS)
    found: set[str] = set()
    for section in (prompt.system, prompt.user, prompt.assistant):
        if section:
            found |= meta.find_undeclared_variables(env.parse(section))
    return found


def _render(ref: str, context: dict[str, Any]) -> tuple[str, str]:
    rendered = load_prompt(ref, base_path=_PROMPTS).render(**context)
    assert rendered.system is not None
    assert rendered.user is not None
    return rendered.system, rendered.user


def _slot(text: str, tag: str) -> str:
    return text.split(f"\n<{tag}>\n", 1)[1].split(f"\n</{tag}>\n", 1)[0]


class TestTheSameExecutorRendersThem:
    @pytest.mark.parametrize("ref", sorted(_TWINS))
    def test_the_variables_are_the_twins(self, ref: str) -> None:
        assert _variables(ref) == _variables(_TWINS[ref])

    @pytest.mark.parametrize("ref", sorted(_TWINS))
    def test_the_slots_are_the_twins(self, ref: str) -> None:
        own = load_prompt(ref, base_path=_PROMPTS)
        twin = load_prompt(_TWINS[ref], base_path=_PROMPTS)
        assert slot_names(own.system, own.user, own.assistant) == slot_names(
            twin.system, twin.user, twin.assistant
        )


class TestTheEvaluationOfARecordedProject:
    """Lock b: the request of a project's evaluation, rendered."""

    @pytest.fixture()
    async def reading(self, monkeypatch: pytest.MonkeyPatch) -> DoorReading:
        return await read_recorded_project(monkeypatch)

    def _request(
        self, reading: DoorReading, *, repeat: list[dict[str, Any]]
    ) -> tuple[str, str, dict[str, Any]]:
        shown = criteria_evaluator.EvaluationInput(
            task_title="Статистика рядків",
            task_description="Доповніть стартовий проєкт.",
            task_text="Додайте модуль stats.py із середньою довжиною рядка.",
            criteria=_CRITERIA,
            submission_text=reading.text,
            language="Ukrainian",
        )
        expected = ["c1"] if repeat else ["c1", "c2.p1", "c2.p2"]
        context = criteria_evaluator.render_context(
            shown, expected=expected, repeat=repeat
        )
        system, user = _render(_EVALUATION, context)
        return system, user, context

    @pytest.mark.parametrize(
        "repeat",
        [
            [],
            [
                {
                    "id": "c1",
                    "problem": "the quote is taken from the code the author gave",
                    "your_answer": {"verdict": "met", "quote": "def read_lines"},
                }
            ],
        ],
    )
    def test_every_slot_is_filled_and_the_context_is_whole_in_its_own(
        self, reading: DoorReading, repeat: list[dict[str, Any]]
    ) -> None:
        system, user, context = self._request(reading, repeat=repeat)

        for text in (system, user):
            assert "{{" not in text
            assert "{%" not in text
        words = " ".join(system.split())
        assert "at most 200 characters and at least 8 characters" in words
        assert "**Ukrainian**" in system
        assert _slot(user, "student_submission") == reading.text
        assert json.loads(_slot(user, "criteria")) == context["criteria"]
        assert json.loads(_slot(user, "items_to_judge")) == context["items"]
        if repeat:
            assert json.loads(_slot(user, "previous_problems")) == repeat
            assert "second request" in system
        else:
            assert "<previous_problems>" not in user
            assert "second request" not in system

    def test_every_frame_is_on_its_line(self, reading: DoorReading) -> None:
        _, user, _ = self._request(reading, repeat=[])
        work = _slot(user, "student_submission")
        lines = work.splitlines()

        assert lines[0] == "@@MENTOR_CTX@@ BEGIN"
        assert lines[1].startswith("@@MENTOR_CTX@@ TRUSTED -- SYSTEM-COMPUTED")
        assert "@@MENTOR_CTX@@ END-TRUSTED" in lines
        assert "CHANGED (1): app.py (214 B)" in lines
        assert "NEW (1): stats.py (118 B)" in lines
        for header in (
            "@@MENTOR_CTX@@ FILE type=CHANGED-FULL path=app.py",
            "@@MENTOR_CTX@@ FILE type=NEW path=stats.py",
            "@@MENTOR_CTX@@ FILE type=NEIGHBOR path=utils.py",
        ):
            assert any(line.startswith(header) for line in lines), header
        assert lines.count("@@MENTOR_CTX@@ END-FILE") == 3
        assert "@@MENTOR_CTX@@ END" in lines
        assert "=== NOT OPENED (1) ===" in lines
        assert STATS in work
        assert UTILS in work

    def test_the_prompt_names_every_frame_the_builder_writes(self) -> None:
        """A frame renamed in ``mentor_context`` leaves this prompt telling the
        model of a frame that no longer exists: red here first. Each kind of
        file block is described in the section on the frames, under its own
        name."""
        system = load_prompt(_EVALUATION, base_path=_PROMPTS).system or ""
        frames = system.split("### How the project is shown to you", 1)[1].split(
            "\n### ", 1
        )[0]
        builder = Path(mentor_context.__file__).read_text(encoding="utf-8")

        assert f"`{mentor_context._SENTINEL}`" in frames
        assert f"`{mentor_context._SENTINEL_ESCAPED}`" in frames
        assert f"`{mentor_context._BODY_UNAVAILABLE}`" in frames
        for kind in _FILE_KINDS:
            assert f'"{kind}"' in builder, kind
            assert f"\n  - `{kind}` — " in frames, kind
        for word in _FRAME_WORDS:
            assert word in builder, word
            assert word in frames, word

    def test_the_quote_rule_is_the_students_files_alone(self) -> None:
        system = load_prompt(_EVALUATION, base_path=_PROMPTS).system or ""
        evidence = system.split("### Evidence", 1)[1].split("### ", 1)[0]

        assert "a quote of the author's code does not count" in evidence
        assert "never comes from a `NEIGHBOR` file, the\n  trusted block" in (evidence)
        assert "quote only a line that begins with `+`" in evidence


class TestTheExplanationOfARecordedProject:
    """Lock h, the request: the author's-code flag reaches the model, and the
    prompt says how to speak of it — and of nothing of the mechanics."""

    @pytest.fixture()
    async def reading(self, monkeypatch: pytest.MonkeyPatch) -> DoorReading:
        return await read_recorded_project(monkeypatch)

    def _request(self, reading: DoorReading) -> tuple[str, str]:
        rows = (
            _Row("c1", "criterion", "must", "met", "def mean_length(lines):"),
            _Row("c2", "criterion", "should", "not_met", quote_from_base=True),
            _Row("c2.p1", "point", None, "not_met", quote_from_base=True),
            _Row("c2.p2", "point", None, "not_met", missing="Немає тестів."),
        )
        facts = explanation_facts(_CRITERIA, rows, layer=CriteriaLayer.AUTHOR)
        shown = review_explainer.ExplanationInput(
            task_title="Статистика рядків",
            task_description="Доповніть стартовий проєкт.",
            task_text="Додайте модуль stats.py.",
            facts=facts,
            submission_text=reading.text,
            language="Ukrainian",
            course_links={},
        )
        return _render(_EXPLANATION, review_explainer.render_context(shown))

    def test_the_flag_is_shown_where_it_stands_and_nowhere_else(
        self, reading: DoorReading
    ) -> None:
        system, user = self._request(reading)
        [c1, c2] = json.loads(_slot(user, "verdicts"))
        p1, p2 = c2["points"]

        assert p1["quote_from_base"] is True
        assert "quote_from_base" not in p2
        assert "quote_from_base" not in c1
        # The author's line the model gave is never shown: only what stands.
        assert "quote" not in p1
        assert _slot(user, "student_submission") == reading.text
        assert "**Ukrainian**" in system

    def test_the_prompt_carries_the_rules_of_the_first_day(self) -> None:
        system = load_prompt(_EXPLANATION, base_path=_PROMPTS).system or ""

        # The author's code, in plain words.
        assert "`quote_from_base: true`" in system
        assert "present only in the code of the starting project" in system
        # One form of address.
        assert "«ви»" in system
        # No account of the mechanics; the check by its name.
        assert "Never describe how the check works" in system
        assert "«автоматична перевірка»" in system
        # Nothing the excerpt does not show.
        assert "beyond what its title and its `excerpt` show" in system
        # No rule of the pass: the builder prints it.
        assert "never that a criterion, or a kind of criterion, decides" in system
        assert "decides the pass;" not in system
        # Never the solution.
        assert "### Never the solution" in system


class _Row:
    """A stored verdict row, as a project's evaluation writes it."""

    def __init__(
        self,
        item_id: str,
        item_kind: str,
        weight: str | None,
        verdict: str,
        quote: str | None = None,
        *,
        missing: str | None = None,
        quote_from_base: bool = False,
    ) -> None:
        self.item_id = item_id
        self.item_kind = item_kind
        self.weight = weight
        self.verdict = verdict
        self.quote = quote
        self.quote_file = "stats.py" if quote else None
        self.quote_line_start = 1 if quote else None
        self.quote_line_end = 1 if quote else None
        self.missing = missing
        self.quote_not_found = False
        self.safeguard_fired = False
        self.quote_from_base = quote_from_base
