"""The text types' two model stages read what they read before chain K4.

Project chain K4 gives the project its own evaluation and explanation stages
and its own prompts, beside the calibrated ones of ``task`` and
``short_task``. Nothing a text type's model is sent may move with it (lock d):
the prompt files are pinned in ``test_calibrated_prompts_unchanged.py``; here
are the stage definitions the router runs them by, and the WHOLE rendered
request — system and user, every variable the code fills in — of both stages
on one task's input that carries every kind of verdict the explanation can be
shown. The digests were taken on ``main`` before K4 (b40dcf4), so a change
of any byte the model reads is a decision made on purpose, with a new digest
in this file, and never a side effect of the project's chain.

The explanation prompt v3 (2026-11-13) is such a decision: the stage reads
``prompts/review_explanation/v3.md`` since, so its definition and its
request have new digests, taken on purpose. What v3 must NOT move is the
user message — the same variables in the same slots — so the user message's
digest is pinned on its own, taken from v2's request on main (f797553), and
the stage's request must give it byte for byte. v2's whole request is kept
pinned too: v2 stays in the repository unchanged.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from course_supporter.agents import criteria_evaluator, review_explainer
from course_supporter.criteria_kinds import CriteriaLayer
from course_supporter.homework.criteria_form import Criterion
from course_supporter.homework.path_config import ModelStage, load_path_config
from course_supporter.homework.verdict_explanation import explanation_facts
from course_supporter.llm.prompt_loader_md import load_prompt
from course_supporter.models.review_structure import Position

_REPO_ROOT = Path(__file__).resolve().parents[3]

# sha256 of the stage's definition as the router is handed it, taken on main;
# review_explanation's re-taken for prompt v3 (2026-11-13), its prompt_ref the
# one field that changed.
_STAGE_DIGESTS = {
    "criteria_evaluation": (
        "4eacfb9544225e13b807b6d95a4978c9317d64963283ee42739fd2f17a0adbc4"
    ),
    "review_explanation": (
        "68bfe7e379bd8887b21e309e8a09bb50955383204860b2ef3368c444276c0c8c"
    ),
}
# sha256 of the rendered request (system, NUL, user), taken on main.
_REQUEST_DIGESTS = {
    "criteria_evaluation/first": (
        "58ccd2462213953777a7eb8aa3600a005c3b598b02dfff5bdd1afcfd50aa8e6b"
    ),
    "criteria_evaluation/repeat": (
        "f2bad1a66337366dfa495064469a91ecafa5cc2f98404fac5da4e6de919d1395"
    ),
    "review_explanation/v2": (
        "f75759c0158b63f2dc1740d80d72ecb7bdfb686eb6e2125bf1d254c639ef21fb"
    ),
    # The stage's prompt since 2026-11-13: v3.
    "review_explanation/v3": (
        "7345e8917587671afc1672a3a4f9d4fded18f766c91b4d68430c7ac52305ad7e"
    ),
}
# sha256 of the explanation's rendered USER message alone, taken on main from
# v2's request: v3 changes the system message and nothing the user one holds.
_EXPLANATION_USER_DIGEST = (
    "917cf8669ec2709ef4d1c745cdcfcde85fd226d9f4e8180793a851c3608f6c59"
)

_WORK = (
    "--- solution.py ---\n"
    "def fibonacci(n):\n"
    "    if n < 2:\n"
    "        return n\n"
    "    return fibonacci(n - 1) + fibonacci(n - 2)\n"
    "\n\n=== NOT OPENED (1) ===\ndata/sales.xls · forbidden_type · 855\n"
)


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


_CRITERIA = (
    _criterion("c1", "must"),
    _criterion("c2", "should", points=2),
    _criterion("c3", "may"),
    _criterion("c4", "should"),
)


@dataclass(frozen=True)
class _Row:
    """A stored verdict row, as a text type writes it: never of the base."""

    item_id: str
    item_kind: str
    weight: str | None
    verdict: str
    quote: str | None = None
    quote_file: str | None = None
    quote_line_start: int | None = None
    quote_line_end: int | None = None
    missing: str | None = None
    quote_not_found: bool = False
    safeguard_fired: bool = False
    quote_from_base: bool = False


_ROWS = (
    _Row("c1", "criterion", "must", "met", "def fibonacci(n):", "solution.py", 1, 1),
    _Row("c2", "criterion", "should", "not_met"),
    _Row(
        "c2.p1",
        "point",
        None,
        "met",
        "return fibonacci(n - 1) + fibonacci(n - 2)",
        "solution.py",
        4,
        4,
        safeguard_fired=True,
    ),
    _Row("c2.p2", "point", None, "not_met", missing="Немає жодного тесту."),
    _Row("c3", "criterion", "may", "not_met", missing=None, quote_not_found=True),
    _Row("c4", "criterion", "should", "not_met", missing="Немає документації."),
)


def _rendered(ref: str, context: dict[str, object]) -> tuple[str, str]:
    rendered = load_prompt(ref, base_path=_REPO_ROOT / "prompts").render(**context)
    assert rendered.system is not None
    assert rendered.user is not None
    return rendered.system, rendered.user


def _digest(ref: str, context: dict[str, object]) -> str:
    system, user = _rendered(ref, context)
    return hashlib.sha256(f"{system}\x00{user}".encode()).hexdigest()


def _evaluation_request(*, repeat: bool) -> str:
    shown = criteria_evaluator.EvaluationInput(
        task_title="Числа Фібоначчі",
        task_description="Рекурсія",
        task_text="Напишіть функцію fibonacci(n).",
        criteria=_CRITERIA,
        submission_text=_WORK,
        language="Ukrainian",
    )
    notes = (
        [
            {
                "id": "c1",
                "problem": "the quote is not in the work",
                "your_answer": {"verdict": "met", "quote": "def fib(n)"},
            }
        ]
        if repeat
        else []
    )
    expected = ["c1"] if repeat else ["c1", "c2.p1", "c2.p2", "c3", "c4"]
    return _digest(
        "criteria_evaluation/v1.md",
        criteria_evaluator.render_context(shown, expected=expected, repeat=notes),
    )


def _explanation_context() -> dict[str, object]:
    facts = explanation_facts(_CRITERIA, _ROWS, layer=CriteriaLayer.MODEL)
    links = {
        "c4": (
            review_explainer.ShownLink(
                id="c4.l1",
                material="Лекція 3",
                position=Position(kind="paragraph", value="2"),
                excerpt="Документація пояснює",
            ),
            review_explainer.ShownLink(
                id="c4.l2", material="Відео", position=None, excerpt=None
            ),
        ),
    }
    shown = review_explainer.ExplanationInput(
        task_title="Числа Фібоначчі",
        task_description="Рекурсія",
        task_text="Напишіть функцію fibonacci(n).",
        facts=facts,
        submission_text=_WORK,
        language="Ukrainian",
        course_links=links,
    )
    return review_explainer.render_context(shown)


def _explanation_request(ref: str) -> str:
    return _digest(ref, _explanation_context())


def _the_stages_prompt() -> str:
    """The explanation stage's prompt, as ``load_prompt`` takes it here."""
    config = load_path_config(_REPO_ROOT / "config" / "submission_paths.yaml")
    stage = config.stages["review_explanation"]
    assert isinstance(stage, ModelStage)
    return stage.prompt_ref.removeprefix("prompts/")


def _stage_digest(name: str) -> str:
    config = load_path_config(_REPO_ROOT / "config" / "submission_paths.yaml")
    stage = config.stages[name]
    assert isinstance(stage, ModelStage)
    canonical = json.dumps(stage.model_dump(mode="json"), sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()


@pytest.mark.parametrize("name", sorted(_STAGE_DIGESTS))
def test_the_stage_is_defined_as_before(name: str) -> None:
    """Ladder, ceilings, prompt, flags: the same bytes the router is handed."""
    assert _stage_digest(name) == _STAGE_DIGESTS[name], name


def test_the_first_evaluation_request_is_byte_for_byte_the_same() -> None:
    assert (
        _evaluation_request(repeat=False)
        == (_REQUEST_DIGESTS["criteria_evaluation/first"])
    )


def test_the_repeated_evaluation_request_is_byte_for_byte_the_same() -> None:
    assert (
        _evaluation_request(repeat=True)
        == (_REQUEST_DIGESTS["criteria_evaluation/repeat"])
    )


def test_the_explanation_request_v2_renders_is_byte_for_byte_the_same() -> None:
    """The fact the project's explanation is shown (``quote_from_base``) is
    never shown here: a text type has no base, so no row carries it."""
    assert (
        _explanation_request("review_explanation/v2.md")
        == _REQUEST_DIGESTS["review_explanation/v2"]
    )


def test_the_stage_sends_v3s_request() -> None:
    """The stage's own prompt, read from its definition, renders v3's request."""
    assert _the_stages_prompt() == "review_explanation/v3.md"
    assert (
        _explanation_request(_the_stages_prompt())
        == _REQUEST_DIGESTS["review_explanation/v3"]
    )


def test_the_stages_user_message_is_v2s_byte_for_byte() -> None:
    """v3 moves the system message alone: the user message — every variable
    the code fills in, in its slot — is the one v2 was calibrated on."""
    context = _explanation_context()
    system, user = _rendered(_the_stages_prompt(), context)
    v2_system, v2_user = _rendered("review_explanation/v2.md", context)

    assert hashlib.sha256(user.encode()).hexdigest() == _EXPLANATION_USER_DIGEST
    assert user == v2_user
    assert system != v2_system
