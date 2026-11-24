"""A stored review and a stored explanation, with fields a newer release wrote.

A review is stored as JSON; a newer release may add a field to it, and the code
that reads it back may be older (the debt DD-SP-DL). These builders give the
tests of that debt one stored review that holds every shape of the review's
models — a nested model, ``Model | None``, a list of models — and a way to put
a field the models do not know at any place in it.
"""

from __future__ import annotations

import copy
from typing import Any, Final

from course_supporter.models import review_structure
from course_supporter.models.review_schema import REVIEW_SCHEMA_VERSION
from course_supporter.models.review_structure import (
    PassRule,
    Position,
    Reference,
    Remark,
    Reply,
    ReviewStructureV1,
    Verdict,
    Verification,
)

UNKNOWN: Final = "from_a_newer_release"
"""The name of a field no model of the review knows."""

MARKER: Final = "marker-3f9a-the-value-of-an-unknown-field"
"""The value put into an unknown field: it must never reach a log."""

Path = tuple[str | int, ...]

# Where an unknown field can sit in a stored review: one place for every shape
# of the review's models, named by what the shape is. ``loc`` of pydantic's
# error must equal each of these paths in the data (the debt's check).
REVIEW_PLACES: Final[dict[str, Path]] = {
    "the root": (),
    "a nested model": ("verdict",),
    "a model in a nested model (Model | None)": ("verdict", "rule"),
    "a model of a Model | None": ("test",),
    "an item of a list in a Model | None": ("test", "questions", 1),
    "an item of a list in an item of a list": (
        "test",
        "questions",
        1,
        "correct_answer",
        0,
    ),
    "an item of fixed": ("fixed", 0),
    "an item of new_remarks": ("new_remarks", 1),
    "an item of open": ("open", 0),
    "an item of broken": ("broken", 0),
    "an item of a list of a remark": ("new_remarks", 1, "read", 0),
    "a position of an item of a list of a remark": (
        "new_remarks",
        1,
        "read",
        0,
        "position",
    ),
    "a position of a remark": ("new_remarks", 0, "position"),
    "an item of replies": ("replies", 0),
    "a verification": ("verification",),
}


def _remark(item_id: str) -> Remark:
    return Remark(
        what="The password is written as plain text.",
        why="Anyone who opens the file sees it.",
        todo="Move the password to an environment variable.",
        read=[
            Reference(
                title="Secrets in code",
                url="https://example.test/secrets",
                position=Position(kind="slide", value="12"),
            )
        ],
        position=Position(kind="file", value="main.py"),
        item_id=item_id,
    )


def a_review() -> ReviewStructureV1:
    """A version-1 review with every shape of the models in it, and passed."""
    return ReviewStructureV1(
        schema_version=REVIEW_SCHEMA_VERSION,
        language="ukr",
        verdict=Verdict(
            passed=True,
            why="Every requirement is met.",
            rule=PassRule(basis="author_musts_met", score=90, threshold=80),
        ),
        test=review_structure.TestSection(
            score=50,
            questions=[
                review_structure.TestQuestionResult(number="1", correct=True),
                review_structure.TestQuestionResult(
                    number="2",
                    correct=False,
                    correct_answer=[review_structure.TestOption(label="b", text="x")],
                ),
            ],
        ),
        fixed=[_remark("c1")],
        new_remarks=[_remark("c2"), _remark("c3")],
        open=[_remark("c4")],
        broken=[_remark("c5")],
        replies=[Reply(kind="question", said="Why?", answer="Because.")],
        verification=Verification(by_run=["It ran."], by_reading=[]),
        progress="The third task in a row without remarks on style.",
    )


def stored_review() -> dict[str, Any]:
    """The review as the database holds it: a fresh plain dict every call."""
    return a_review().model_dump(mode="json")


def stored_explanation() -> dict[str, Any]:
    """An explanation as its stage stores it: two remarks, one of them linked."""
    return {
        "passed": False,
        "why": "One requirement is not met.",
        "remarks": [
            {
                "id": "c1",
                "what": "The test for an empty list is missing.",
                "why": "An edge case is not shown to work.",
                "todo": "Add a test for the empty list.",
                "links": ["c1.l1"],
            },
            {
                "id": "c2",
                "what": "The function has no docstring.",
                "why": "A reader cannot tell what it promises.",
                "todo": "Describe what it returns.",
                "links": [],
            },
        ],
        "mentor_voice": None,
    }


def at(data: Any, path: Path) -> Any:
    """The node of ``data`` at ``path`` (names and list indexes)."""
    for step in path:
        data = data[step]
    return data


def with_unknown(data: dict[str, Any], *places: Path) -> dict[str, Any]:
    """A copy of ``data`` with an unknown field (valued MARKER) at each place."""
    noisy = copy.deepcopy(data)
    for place in places:
        at(noisy, place)[UNKNOWN] = MARKER
    return noisy


def dotted(place: Path) -> str:
    """The path of the unknown field at ``place``, as the log line writes it."""
    return ".".join(str(step) for step in (*place, UNKNOWN))
