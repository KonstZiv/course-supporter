"""Places below the score threshold never reach the explanation model (task 10).

The product owner's decision of 2026-11-13: a place the search found with a
score below ``MIN_COURSE_LINK_SCORE`` is kept in the row for review and is not
shown to the model (production 2026-11-12: two places of 0.14, both given).

Lock ґ — the model's input for a criterion without a low place is what it was
before the decision, byte for byte. The digest was taken on ``main`` before the
threshold (f797553), on the search's row below as it stands without its weak
places; the same row WITH its weak places — a weak third place of c2, c3 left
with only weak ones — renders to the same digest, because they are not shown.
A change of any byte the model reads is then a decision made on purpose, with a
new digest in this file.

The explanation prompt v3 (2026-11-13) is such a decision: the stage reads
``prompts/review_explanation/v3.md`` since, so the request is rendered by the
stage's own prompt and pinned anew. v3 moves the system message alone: the
digest taken on main is kept for v2's request, which v2 still renders, and the
user message — the only part the course links reach — is v2's byte for byte.
"""

from __future__ import annotations

import uuid

from course_supporter.agents import review_explainer
from course_supporter.criteria_kinds import CriteriaLayer
from course_supporter.homework.course_links import (
    CriterionLinks,
    FoundCourseLinks,
    LinkCandidate,
)
from course_supporter.homework.review_explanation import links_to_show
from course_supporter.homework.verdict_explanation import explanation_facts
from course_supporter.storage.document_segment_repository import SegmentAnchor
from tests.unit.test_prompts.test_text_stage_inputs_unchanged import (
    _CRITERIA,
    _ROWS,
    _WORK,
    _digest,
    _rendered,
    _the_stages_prompt,
)

# sha256 of the rendered request (system, NUL, user) for ``_WITHOUT_WEAK``:
# v2's, taken on main before the threshold; and the stage's since prompt v3
# (2026-11-13), re-taken on purpose — only its system message differs.
_V2_REQUEST_DIGEST = "5a4cd716953e209ef28bbcf5f007a7ee63964f25adcb1c33b1aea199c5503d75"
_REQUEST_DIGEST = "b2126cfdd9e494b7141e3c8919cb28644eac48e73bf501a6729e80eeccdc3c22"


def _place(title: str, paragraph: int, confidence: float) -> LinkCandidate:
    return LinkCandidate(
        segment_id=uuid.uuid4(),
        authored_document_id=uuid.uuid4(),
        material_title=title,
        source_type="text",
        anchor=SegmentAnchor("paragraph", paragraph, paragraph + 1),
        excerpt=f"Початок: {title}",
        later_in_course=False,
        confidence=confidence,
        matched_concepts=("testing",),
    )


def _row(**kept: list[LinkCandidate]) -> FoundCourseLinks:
    return FoundCourseLinks(
        criteria=tuple(
            CriterionLinks(item_id=item_id, candidates=tuple(places))
            for item_id, places in kept.items()
        )
    )


# c2, c3 and c4 are the unmet criteria of the shared input; c3 has no place.
_WITHOUT_WEAK = _row(
    c2=[_place("Рекурсія", 3, 0.9), _place("Функції", 1, 0.55)],
    c3=[],
    c4=[
        _place("Лекція 3", 2, 0.8),
        _place("Відео", 4, 0.31),
        _place("Практикум", 6, 0.3),
    ],
)
# The same row as the search keeps it now: c2 gets a weak third place and c3
# only weak ones (0.14 twice, as on production); c4 is full already.
_WITH_WEAK = _row(
    c2=[
        _place("Рекурсія", 3, 0.9),
        _place("Функції", 1, 0.55),
        _place("Фінальний проєкт", 2, 0.29),
    ],
    c3=[_place("Фінальний проєкт", 5, 0.14), _place("Підсумок", 7, 0.14)],
    c4=list(_WITHOUT_WEAK.candidates_of("c4")),
)


def _context(found: FoundCourseLinks) -> dict[str, object]:
    facts = explanation_facts(_CRITERIA, _ROWS, layer=CriteriaLayer.MODEL)
    shown = review_explainer.ExplanationInput(
        task_title="Числа Фібоначчі",
        task_description="Рекурсія",
        task_text="Напишіть функцію fibonacci(n).",
        facts=facts,
        submission_text=_WORK,
        language="Ukrainian",
        course_links=links_to_show(facts.unmet, found),
    )
    return review_explainer.render_context(shown)


def _request(found: FoundCourseLinks, ref: str | None = None) -> str:
    """The request's digest, by ``ref`` or else by the stage's own prompt."""
    return _digest(ref or _the_stages_prompt(), _context(found))


def test_a_request_without_weak_places_is_byte_for_byte_the_same() -> None:
    assert _request(_WITHOUT_WEAK) == _REQUEST_DIGEST


def test_weak_places_in_the_row_change_not_a_byte_of_the_request() -> None:
    assert _request(_WITH_WEAK) == _REQUEST_DIGEST


def test_v3_moved_the_system_message_alone() -> None:
    """v2 still renders the request pinned on main; the stage's v3 renders
    the same user message — the links in it — and another system message."""
    v2 = "review_explanation/v2.md"
    for found in (_WITHOUT_WEAK, _WITH_WEAK):
        assert _request(found, v2) == _V2_REQUEST_DIGEST
        system, user = _rendered(_the_stages_prompt(), _context(found))
        v2_system, v2_user = _rendered(v2, _context(found))
        assert user == v2_user
        assert system != v2_system
