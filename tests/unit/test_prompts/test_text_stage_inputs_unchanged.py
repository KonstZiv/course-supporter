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

The explanation prompt v4 (task 13, explanation side, 2026-11-23) is the next
such decision, and it moves the user message on purpose: a text type's
request shows the limit of examples the code counted from the number of
criteria (``result.example_limit``), on every request, the first attempt
included, and — on a repeated attempt — the explanation's own remark on a
criterion still open (``previous_remark``). So the stage's definition and its
request have new digests, taken on purpose from the input the stage builds
now; on this input, a first attempt, the user message differs from v3's by
the limit alone. v3's request stays pinned as it was calibrated — rendered
from the input without the fields of task 13, as v3's stage sent it — and v3
stays in the repository unchanged.

The evaluation prompt v2 (task 13, K4, 2026-11-23) is the next such
decision: the stage reads ``prompts/criteria_evaluation/v2.md`` since, so its
definition has a new digest, taken on purpose — its ``prompt_ref`` the one
field that changed. What v2 must NOT move is a first attempt's request and
every second request: rendered by the stage's own prompt from the same
input, both are v1's digests byte for byte. A repeated attempt's first
request moves on purpose — the earlier verdicts and the difference of the
work in slots of their own — and its digest is pinned on its own, taken from
the input below with a fixed memory. v1 stays in the repository unchanged.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from pathlib import Path

import pytest

from course_supporter.agents import criteria_evaluator, review_explainer
from course_supporter.criteria_kinds import CriteriaLayer, VerdictValue
from course_supporter.homework.criteria_form import Criterion
from course_supporter.homework.earlier_review import AttemptMemory, PreviousVerdict
from course_supporter.homework.path_config import ModelStage, load_path_config
from course_supporter.homework.verdict_explanation import (
    example_limits,
    explanation_facts,
)
from course_supporter.llm.prompt_loader_md import load_prompt
from course_supporter.models.review_structure import Position

_REPO_ROOT = Path(__file__).resolve().parents[3]

# sha256 of the stage's definition as the router is handed it, taken on main;
# review_explanation's re-taken for prompt v3 (2026-11-13), its prompt_ref the
# one field that changed, and again for prompt v4 (task 13, 2026-11-23), the
# same field; criteria_evaluation's re-taken for prompt v2 (task 13, K4,
# 2026-11-23), its prompt_ref the one field that changed (on main, with v1:
# 4eacfb9544225e13b807b6d95a4978c9317d64963283ee42739fd2f17a0adbc4).
_STAGE_DIGESTS = {
    "criteria_evaluation": (
        "db5e138bf50b49537208ff1fe0485cef9f922634e5e7bbde62136e051de5b505"
    ),
    "review_explanation": (
        "84d198df8f758e473c789c0b4cd5bd20738549a0933dccb06126e9ab16bb23b8"
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
    # The stage's prompt since task 13, K4 (2026-11-23): v2, a repeated
    # attempt's first request, with the earlier attempt (``_MEMORY``).
    "criteria_evaluation/v2/memory": (
        "3571fba975df211f67de7283a2704d1efc9c8349c130d7c35969bfddc2c7872b"
    ),
    "review_explanation/v2": (
        "f75759c0158b63f2dc1740d80d72ecb7bdfb686eb6e2125bf1d254c639ef21fb"
    ),
    # The stage's prompt from 2026-11-13: v3, rendered from the input it was
    # calibrated on — without the fields of task 13.
    "review_explanation/v3": (
        "7345e8917587671afc1672a3a4f9d4fded18f766c91b4d68430c7ac52305ad7e"
    ),
    # The stage's prompt since task 13 (2026-11-23): v4, rendered from the
    # input the stage builds — with the limit of examples.
    "review_explanation/v4": (
        "1bd27fafc044d73b6390fde0214f2fe6e018ee53aef724964ed93fd1ad9dee4d"
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


# The earlier attempt a repeated attempt's request is shown (task 13, K4):
# every kind of verdict, a note, and a difference that ends without its LF.
_MEMORY = AttemptMemory(
    earlier_submission_id=uuid.UUID("00000000-0000-7000-8000-000000000013"),
    verdicts=(
        PreviousVerdict("c1", VerdictValue.MET, "def fib(n):", None, False),
        PreviousVerdict("c2.p1", VerdictValue.NOT_MET, None, None, True),
        PreviousVerdict(
            "c2.p2", VerdictValue.NOT_MET, None, "Немає жодного тесту.", False
        ),
    ),
    diff=(
        "--- previous\n+++ current\n@@ -1,2 +1,2 @@\n"
        " --- solution.py ---\n-def fib(n):\n+def fibonacci(n):\n"
    ),
    criteria_changed=False,
    task_changed=True,
    rewritten=False,
    earlier_work_unavailable=False,
)


def _evaluation_request(
    *,
    repeat: bool,
    ref: str = "criteria_evaluation/v1.md",
    memory: AttemptMemory | None = None,
) -> str:
    shown = criteria_evaluator.EvaluationInput(
        task_title="Числа Фібоначчі",
        task_description="Рекурсія",
        task_text="Напишіть функцію fibonacci(n).",
        criteria=_CRITERIA,
        submission_text=_WORK,
        language="Ukrainian",
        memory=memory,
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
        ref, criteria_evaluator.render_context(shown, expected=expected, repeat=notes)
    )


def _explanation_context(*, task_13: bool = False) -> dict[str, object]:
    """The prompt's variables for the shared input.

    ``task_13``: as the stage builds the input since task 13 — with the limit
    of examples counted from the list (a first attempt: no remark made
    before); otherwise as v2's and v3's stages built it.
    """
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
        example_limits=example_limits(len(_CRITERIA)) if task_13 else None,
    )
    return review_explainer.render_context(shown)


def _explanation_request(ref: str, *, task_13: bool = False) -> str:
    return _digest(ref, _explanation_context(task_13=task_13))


def _the_stages_prompt(name: str = "review_explanation") -> str:
    """A stage's prompt, as ``load_prompt`` takes it here."""
    config = load_path_config(_REPO_ROOT / "config" / "submission_paths.yaml")
    stage = config.stages[name]
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


def test_the_evaluation_stage_sends_v2_which_is_v1_on_a_first_attempt() -> None:
    """Task 13, K4: the stage reads v2, and a first attempt's request — the
    first and the second alike — is v1's byte for byte."""
    ref = _the_stages_prompt("criteria_evaluation")

    assert ref == "criteria_evaluation/v2.md"
    assert (
        _evaluation_request(repeat=False, ref=ref)
        == _REQUEST_DIGESTS["criteria_evaluation/first"]
    )
    assert (
        _evaluation_request(repeat=True, ref=ref)
        == _REQUEST_DIGESTS["criteria_evaluation/repeat"]
    )


def test_a_repeated_attempts_second_request_is_v1s_byte_for_byte() -> None:
    """The memory is shown to the first request alone: the second, about some
    items, is what it was (``repeat``, ``<previous_problems>``)."""
    ref = _the_stages_prompt("criteria_evaluation")

    assert (
        _evaluation_request(repeat=True, ref=ref, memory=_MEMORY)
        == _REQUEST_DIGESTS["criteria_evaluation/repeat"]
    )


def test_a_repeated_attempts_first_request_is_pinned() -> None:
    """Moved on purpose by task 13, K4: the earlier verdicts, the difference
    and the note, in v2's slots — a new digest, taken from this input."""
    ref = _the_stages_prompt("criteria_evaluation")

    assert (
        _evaluation_request(repeat=False, ref=ref, memory=_MEMORY)
        == _REQUEST_DIGESTS["criteria_evaluation/v2/memory"]
    )


def test_the_explanation_request_v2_renders_is_byte_for_byte_the_same() -> None:
    """The fact the project's explanation is shown (``quote_from_base``) is
    never shown here: a text type has no base, so no row carries it."""
    assert (
        _explanation_request("review_explanation/v2.md")
        == _REQUEST_DIGESTS["review_explanation/v2"]
    )


def test_v3_renders_its_calibrated_request_byte_for_byte() -> None:
    """v3 stays as it was calibrated: from the input without task 13's fields."""
    assert (
        _explanation_request("review_explanation/v3.md")
        == _REQUEST_DIGESTS["review_explanation/v3"]
    )


def test_the_stage_sends_v4s_request() -> None:
    """The stage's own prompt, read from its definition, renders v4's request
    from the input the stage builds (task 13)."""
    assert _the_stages_prompt() == "review_explanation/v4.md"
    assert (
        _explanation_request(_the_stages_prompt(), task_13=True)
        == _REQUEST_DIGESTS["review_explanation/v4"]
    )


def test_v3s_user_message_is_v2s_byte_for_byte() -> None:
    """v3 moved the system message alone: the user message — every variable
    the code fills in, in its slot — is the one v2 was calibrated on."""
    context = _explanation_context()
    system, user = _rendered("review_explanation/v3.md", context)
    v2_system, v2_user = _rendered("review_explanation/v2.md", context)

    assert hashlib.sha256(user.encode()).hexdigest() == _EXPLANATION_USER_DIGEST
    assert user == v2_user
    assert system != v2_system


def test_the_stages_user_message_is_v3s_and_the_limit_on_a_first_attempt() -> None:
    """Task 13 moves the user message by the limit of examples alone on a
    first attempt — four criteria, ``4 // 2`` — and nothing else in it."""
    system, user = _rendered(_the_stages_prompt(), _explanation_context(task_13=True))
    v3_system, v3_user = _rendered("review_explanation/v3.md", _explanation_context())

    assert '"example_limit": 2' in user
    assert "previous_remark" not in user
    assert user == v3_user.replace(
        '{"passed": false, "score": 50}',
        '{"example_limit": 2, "passed": false, "score": 50}',
    )
    assert system != v3_system
