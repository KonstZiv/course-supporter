"""The explanation prompts v4 of a text task: examples and the remark made before.

Task 13, explanation side (the product owner's decisions A and B of
2026-11-23). v4 is v3, and v4_with_replies is v3_with_replies, with two
changes and nothing else:

* the section "Never the solution" gives way to "Examples": the code's limit
  (``example_limit``) of criteria the examples may cover, what "covers" means,
  the general case preferred, short, never the solution of the whole task —
  and, at the limit 0 and for every criterion no example covers, the old
  rule word for word;
* a new section "A remark made before" on ``previous_remark``;
* v4_with_replies also reads ``reply_example_limit``, in a marked block.

The locks:

* taking the new sections out and putting the old one back gives v3 (and
  v3_with_replies) byte for byte — no other byte moved;
* the old rule stands in v4 word for word;
* the prompts name the fields the code fills in — the same names;
* the project's explanation request is byte for byte what it was on main
  (e80f772): the digest below was taken there, on this input.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from course_supporter.agents import review_explainer
from course_supporter.criteria_kinds import CriteriaLayer
from course_supporter.homework.criteria_form import Criterion
from course_supporter.homework.verdict_explanation import explanation_facts
from course_supporter.llm.prompt_loader_md import load_prompt
from course_supporter.models.review_structure import Position

_REPO_ROOT = Path(__file__).resolve().parents[3]
_DIR = _REPO_ROOT / "prompts" / "review_explanation"

_NEVER = """### Never the solution

Show the way, never walk it for the student. Never write the code, the
function, the query or the corrected file that would satisfy a criterion,
and never a text the student could copy into the work as it stands. You may
name a construct, a function of a language's standard library or a tool,
and say where in the student's own code the change belongs.
"""
_RULE = _NEVER.split("\n\n", 1)[1]

_REPLIES_BLOCK_BEGIN = (
    "\n{# ── task 13: the limits of the answers to the replies — begin ── -#}"
)
_REPLIES_BLOCK_END = (
    "{#- ── task 13: the limits of the answers to the replies — end ── #}\n"
)


def _read(name: str) -> str:
    return (_DIR / name).read_text(encoding="utf-8")


def _as_before(text: str) -> str:
    """``text`` with task 13's sections out and "Never the solution" back."""
    if _REPLIES_BLOCK_BEGIN in text:
        head, rest = text.split(_REPLIES_BLOCK_BEGIN, 1)
        text = head + rest.split(_REPLIES_BLOCK_END, 1)[1]
    head, rest = text.split("### A remark made before\n", 1)
    tail = rest.split("### Evidence\n", 1)[1]
    return head + _NEVER + "\n### Evidence\n" + tail


class TestNothingElseMoved:
    def test_v4_is_v3_but_for_its_two_sections(self) -> None:
        assert _as_before(_read("v4.md")) == _read("v3.md")

    def test_v4_with_replies_is_v3_with_replies_but_for_the_same(self) -> None:
        assert _as_before(_read("v4_with_replies.md")) == _read("v3_with_replies.md")

    def test_the_old_rule_stands_word_for_word(self) -> None:
        for name in ("v4.md", "v4_with_replies.md"):
            text = _read(name)
            assert "### Never the solution" not in text
            assert _RULE in text.split("### Examples\n", 1)[1], name


class TestTheFieldsAreTheCodes:
    def test_each_prompt_names_what_its_request_carries(self) -> None:
        v4, with_replies = _read("v4.md"), _read("v4_with_replies.md")

        for text in (v4, with_replies):
            assert "`example_limit`" in text
            assert "`previous_remark`" in text
            assert "`previous_remark.what`" in text
        assert "reply_example_limit" not in v4
        assert "`reply_example_limit`" in with_replies

    def test_the_rendered_marked_block_leaves_no_marker(self) -> None:
        rendered = load_prompt(
            "review_explanation/v4_with_replies.md", base_path=_REPO_ROOT / "prompts"
        ).render(**_project_context(), replies={"threads": [], "to_answer": []})
        assert rendered.system is not None
        assert "task 13" not in rendered.system
        assert "\n\n`<result>` also holds `reply_example_limit`" in rendered.system


# ── The project's request, byte for byte ──

# sha256 of the project's rendered request (system, NUL, user) on the input
# below, taken on main (e80f772) before task 13; re-taken on purpose by
# DD-SP-DQ (2026-11-27): the input's c3 is a "may" the work does not have, so
# the request no longer lists it among the criteria nor among the places of
# the course — nothing else in it moved.
_PROJECT_REQUEST_DIGEST = (
    "7c1b53bc27aa7b4942517e5e64f24d73a365b1c81c8c7a9acfcb706530bd5227"
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


@dataclass(frozen=True)
class _Row:
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


def _project_context() -> dict[str, object]:
    """The project's input as its stage builds it: no field of task 13."""
    criteria = (
        _criterion("c1", "must"),
        _criterion("c2", "should", points=2),
        _criterion("c3", "may"),
        _criterion("c4", "should"),
    )
    rows = (
        _Row(
            "c1", "criterion", "must", "met", "def fibonacci(n):", "app/main.py", 1, 1
        ),
        _Row("c2", "criterion", "should", "not_met"),
        _Row(
            "c2.p1",
            "point",
            None,
            "met",
            "return fibonacci(n - 1)",
            "app/main.py",
            4,
            4,
            safeguard_fired=True,
        ),
        _Row(
            "c2.p2",
            "point",
            None,
            "not_met",
            missing="Тест є лише в стартовому проєкті.",
            quote_from_base=True,
        ),
        _Row("c3", "criterion", "may", "not_met", quote_not_found=True),
        _Row("c4", "criterion", "should", "not_met", missing="Немає документації."),
    )
    shown = review_explainer.ExplanationInput(
        task_title="Числа Фібоначчі",
        task_description="Проєкт",
        task_text="Допишіть модуль.",
        facts=explanation_facts(criteria, rows, layer=CriteriaLayer.MODEL),
        submission_text="--- app/main.py ---\ndef fibonacci(n):\n    return n\n",
        language="Ukrainian",
        course_links={
            "c4": (
                review_explainer.ShownLink(
                    id="c4.l1",
                    material="Лекція 3",
                    position=Position(kind="paragraph", value="2"),
                    excerpt="Документація",
                ),
            )
        },
    )
    return review_explainer.render_context(shown)


def test_the_projects_request_is_byte_for_byte_mains() -> None:
    rendered = load_prompt(
        "review_explanation_project/v1.md", base_path=_REPO_ROOT / "prompts"
    ).render(**_project_context())

    digest = hashlib.sha256(f"{rendered.system}\x00{rendered.user}".encode())
    assert digest.hexdigest() == _PROJECT_REQUEST_DIGEST
    assert "example_limit" not in (rendered.user or "")
    assert "previous_remark" not in (rendered.user or "")
