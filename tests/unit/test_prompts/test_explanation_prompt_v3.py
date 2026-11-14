"""The explanation prompt v3 of a text task: what it keeps of v2, what it drops.

Since 2026-11-12 an unmet mandatory criterion blocks the pass only on the
list the author approved, and the review's builder prints the pass and its
rule beside the explanation. v2 still told the model that an unmet ``must``
decides the pass, and a review on production said "passed" in its reason and
"will not be passed" in a remark. v3 is v2 with the rule of the pass taken
out and the rules learnt from reviews since — worded as the project's prompt
(``review_explanation_project/v1.md``) words them — and nothing else; the
project's own rules (its frames, ``NEIGHBOR``, ``quote_from_base``) stay out.

The locks, by the order's letters:

* a — the same variables and slots as v2: the same executor renders it from
  the same input (the slot lock itself: ``test_prompt_slot_lock.py``);
* b — no sentence says what decides the pass; the new rules are there, word
  for word as in the project's prompt;
* c — the stage renders v3, and the user message is v2's byte for byte (the
  whole request pinned: ``test_text_stage_inputs_unchanged.py``);
* d — the project's explanation prompt and the evaluation prompts are the
  bytes they were on ``main`` (f797553).
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest
from jinja2 import Environment, meta

from course_supporter.agents import review_explainer
from course_supporter.criteria_kinds import CriteriaLayer
from course_supporter.homework.criteria_form import Criterion
from course_supporter.homework.path_config import ModelStage, load_path_config
from course_supporter.homework.verdict_explanation import explanation_facts
from course_supporter.llm.prompt_loader_md import StagePrompt, load_prompt, slot_names

_REPO_ROOT = Path(__file__).resolve().parents[3]
_PROMPTS = _REPO_ROOT / "prompts"

_V2 = "review_explanation/v2.md"
_V3 = "review_explanation/v3.md"
_PROJECT = "review_explanation_project/v1.md"


def _prompt(ref: str) -> StagePrompt:
    return load_prompt(ref, base_path=_PROMPTS)


def _folded(text: str | None) -> str:
    """Whitespace folded to single spaces: a rule reads the same however wrapped."""
    return " ".join((text or "").split())


def _variables(ref: str) -> set[str]:
    env = Environment(autoescape=False)  # noqa: S701 -- parsed, never rendered
    env.filters["tojson_unicode"] = json.dumps
    prompt = _prompt(ref)
    found: set[str] = set()
    for section in (prompt.system, prompt.user, prompt.assistant):
        if section:
            found |= meta.find_undeclared_variables(env.parse(section))
    return found


class TestTheSameInputAsV2:
    """Lock a."""

    def test_the_variables_are_v2s(self) -> None:
        assert (
            _variables(_V3)
            == _variables(_V2)
            == {
                "language",
                "task_title",
                "task_description",
                "task_text",
                "result",
                "criteria",
                "course_links",
                "submission_text",
            }
        )

    def test_the_slots_are_v2s(self) -> None:
        v2, v3 = _prompt(_V2), _prompt(_V3)
        assert slot_names(v3.system, v3.user, v3.assistant) == slot_names(
            v2.system, v2.user, v2.assistant
        )

    def test_the_user_template_is_v2s_byte_for_byte(self) -> None:
        assert _prompt(_V3).user == _prompt(_V2).user

    def test_the_sections_are_v2s_and_one_more(self) -> None:
        def headings(ref: str) -> list[str]:
            return re.findall(r"^### (.+)$", _prompt(ref).system or "", re.MULTILINE)

        v2 = headings(_V2)
        at = v2.index("Evidence")
        assert headings(_V3) == [*v2[:at], "Never the solution", *v2[at:]]


# ── Lock b: nothing says what decides the pass ──

# A sentence that speaks of the pass and of what decides it, depends on it or
# blocks it. Allowed only as a prohibition: "never" before the verb.
_THE_PASS = re.compile(r"\bpass(?:ed|es)?\b", re.IGNORECASE)
_DECIDES = re.compile(
    r"\b(?:decides?|decided|deciding|depends?|blocks?|blocking)\b", re.IGNORECASE
)


def _sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.;:])\s+|\s+- ", _folded(text)) if s]


def what_decides_the_pass(system: str) -> list[str]:
    """Every sentence of ``system`` that tells the model what decides the pass."""
    found = []
    for sentence in _sentences(system):
        verb = _DECIDES.search(sentence)
        if verb is None or _THE_PASS.search(sentence) is None:
            continue
        if "never" not in sentence[: verb.start()].lower():
            found.append(sentence)
    return found


# v2's paragraph on the weights, and its `why`, in their own words.
_V2_WORDS = (
    "decides the pass;",
    "does not decide the pass",
    "how much it weighs",
    "weight `must` that is not met",
    "why the work is passed or not",
)


class TestNothingSaysWhatDecidesThePass:
    """Lock b, the part taken out."""

    def test_no_sentence_of_v3_says_it(self) -> None:
        assert what_decides_the_pass(_prompt(_V3).system or "") == []

    def test_the_reader_finds_v2s_paragraph(self) -> None:
        """The reader above is not blind: on v2 it finds the paragraph v3 drops."""
        found = what_decides_the_pass(_prompt(_V2).system or "")
        assert any("`must` that is not met decides the pass" in s for s in found)
        assert any("does not decide the pass" in s for s in found)

    @pytest.mark.parametrize("words", _V2_WORDS)
    def test_v2s_words_are_gone(self, words: str) -> None:
        assert words in _folded(_prompt(_V2).system)
        assert words not in _folded(_prompt(_V3).system)

    @pytest.mark.parametrize("word", ["mandatory", "обов'язков", "обовʼязков"])
    def test_no_mandatory_criterion_is_named(self, word: str) -> None:
        assert word not in (_prompt(_V3).system or "").lower()


# The rules v3 adds, each as the project's prompt words it (the order: "take
# the wording from review_explanation_project/v1.md verbatim").
_RULES = {
    "a: the pass is the platform's": (
        "Never say what the pass depends on: no rule, no threshold, no share, and "
        "never that a criterion, or a kind of criterion, decides the pass. The "
        "platform prints the pass and its rule beside your text."
    ),
    "a: why agrees with the pass": (
        "`why` — the voice of the course: your overall reading of the work in "
        "terms of what the author required."
    ),
    "b: one form of address": (
        "Address the student in one form throughout every text: the polite «ви» "
        "in Ukrainian, and the polite form of the language you write in where it "
        "has one. Never switch to the familiar form, and never speak of the "
        "student in the third person."
    ),
    "c: no mechanics": (
        "Never describe how the check works: no search for lines, no second "
        "request, no length, no parser, no model, no field. When you need to "
        "name the check, call it the automatic check — «автоматична перевірка» "
        "in Ukrainian, its plain equivalent in another language."
    ),
    "d: only what the excerpt shows": (
        "You see only the title and the first words of a place. Never say what a "
        "place teaches beyond what its title and its `excerpt` show: never put an "
        "explanation, an example or a rule into the mouth of the course."
    ),
    "d: no place not shown": (
        "Never name or point to a lesson, material, page or place of the course "
        "that is not given in `<course_links>`"
    ),
    "e: never the solution": (
        "### Never the solution Show the way, never walk it for the student. "
        "Never write the code, the function, the query or the corrected file "
        "that would satisfy a criterion, and never a text the student could copy "
        "into the work as it stands. You may name a construct, a function of a "
        "language's standard library or a tool, and say where in the student's "
        "own code the change belongs."
    ),
    "e: todo is a direction": (
        "`todo` — what to do, as a direction: the idea, the construct, the place "
        "in the student's own code to work on, and why that solves the problem."
    ),
    "f: how much a remark matters": (
        "A remark may say in plain words how much it matters for the quality of "
        "the work — never by the names of the weights (`must`, `should`, `may`), "
        "and never that it decides the pass."
    ),
}


class TestTheNewRules:
    """Lock b, the part put in."""

    @pytest.mark.parametrize("rule", sorted(_RULES))
    def test_v3_carries_it(self, rule: str) -> None:
        assert _RULES[rule] in _folded(_prompt(_V3).system)

    @pytest.mark.parametrize("rule", sorted(_RULES))
    def test_word_for_word_as_the_project_says_it(self, rule: str) -> None:
        assert _RULES[rule] in _folded(_prompt(_PROJECT).system)

    @pytest.mark.parametrize(
        "project_only",
        [
            "NEIGHBOR",
            "quote_from_base",
            "@@MENTOR_CTX@@",
            "starting project",
            "How the project is shown to you",
        ],
    )
    def test_the_projects_own_rules_stay_out(self, project_only: str) -> None:
        assert project_only in (_prompt(_PROJECT).system or "")
        assert project_only not in (_prompt(_V3).system or "")


# ── Lock c: the stage renders v3, the user message is v2's ──


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


class _Row:
    def __init__(self, item_id: str, kind: str, weight: str | None, verdict: str):
        self.item_id, self.item_kind, self.weight, self.verdict = (
            item_id,
            kind,
            weight,
            verdict,
        )
        met = verdict == "met"
        self.quote = "def f():" if met else None
        self.quote_file = "f.py" if met else None
        self.quote_line_start = 1 if met else None
        self.quote_line_end = 1 if met else None
        self.missing = None if met else "Немає."
        self.quote_not_found = False
        self.safeguard_fired = False
        self.quote_from_base = False


def _context() -> dict[str, object]:
    criteria = (_criterion("c1", "must"), _criterion("c2", "should", points=1))
    rows = (
        _Row("c1", "criterion", "must", "not_met"),
        _Row("c2", "criterion", "should", "met"),
        _Row("c2.p1", "point", None, "met"),
    )
    facts = explanation_facts(criteria, rows, layer=CriteriaLayer.AUTHOR)
    shown = review_explainer.ExplanationInput(
        task_title="Т",
        task_description="О",
        task_text="Текст",
        facts=facts,
        submission_text="--- f.py ---\ndef f():\n    pass\n",
        language="Ukrainian",
        course_links={"c1": ()},
    )
    return review_explainer.render_context(shown)


class TestTheStageRendersV3:
    """Lock c."""

    def test_the_stage_names_v3(self) -> None:
        config = load_path_config(_REPO_ROOT / "config" / "submission_paths.yaml")
        stage = config.stages["review_explanation"]
        assert isinstance(stage, ModelStage)
        assert stage.prompt_ref == "prompts/" + _V3

    def test_the_user_message_is_v2s_and_the_system_message_is_not(self) -> None:
        context = _context()
        v2 = _prompt(_V2).render(**context)
        v3 = _prompt(_V3).render(**context)

        assert v3.user == v2.user
        assert v3.system != v2.system


# ── Lock d: what v3 does not touch ──

# sha256 of each file on main (f797553), before v3 — but the project's
# evaluation prompt, re-taken on purpose for project chain K5: a "not met"
# names the line where its problem stands. v3 did not touch it either.
_UNTOUCHED = {
    "prompts/review_explanation_project/v1.md": (
        "c7d933baa239583991e3404be08a1f306bd7cf84b47c5e799a5fc5ab8ea82c3c"
    ),
    "prompts/criteria_evaluation/v1.md": (
        "1067450153909de49254dfcd27d752b27d6753a142d04720149c845eb68d24a0"
    ),
    "prompts/criteria_evaluation_project/v1.md": (
        "13b6fe458f8023645244d049f3f65c0f49054598e5f3997d823110a72965aecf"
    ),
}


@pytest.mark.parametrize(("path", "digest"), sorted(_UNTOUCHED.items()))
def test_the_prompt_is_the_one_on_main(path: str, digest: str) -> None:
    data = (_REPO_ROOT / path).read_bytes()
    assert hashlib.sha256(data).hexdigest() == digest, path


@pytest.mark.parametrize(
    "folder", ["criteria_evaluation", "criteria_evaluation_project"]
)
def test_no_evaluation_prompt_was_added(folder: str) -> None:
    files = {
        str(p.relative_to(_REPO_ROOT))
        for p in (_PROMPTS / folder).glob("*")
        if p.is_file()
    }
    assert files == {
        path for path in _UNTOUCHED if path.startswith(f"prompts/{folder}/")
    }
