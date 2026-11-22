"""What the review's builder reads for the pass rule (2026-11-17, 2026-11-18).

:func:`~course_supporter.homework.judged_list.judged_author_musts` is the one
read the builder makes for the author's own "must" criteria. The product
owner accepted its price — one read of the edit's row by its primary key —
for the author's list with a "must" row alone: for the model's list and for
the author's list without a "must", nothing is read. A session double that
records every call holds both.
"""

from __future__ import annotations

import uuid
from typing import Any

from course_supporter.criteria_kinds import CriteriaLayer, VerdictItemKind
from course_supporter.homework.criteria_form import Criterion, criteria_to_document
from course_supporter.homework.judged_list import judged_author_musts
from course_supporter.storage.orm import (
    HomeworkSubmission,
    SubmissionCriterionVerdict,
    TaskCriteriaOverride,
)

_TASK = uuid.UUID("01999999-0000-7000-8000-0000000000a1")
_LIST = uuid.UUID("01999999-0000-7000-8000-0000000000b1")


class _Session:
    """A session that records every read and answers with ``row``."""

    def __init__(self, row: Any = None) -> None:
        self.row = row
        self.reads: list[tuple[type, uuid.UUID]] = []

    async def get(self, model: type, key: uuid.UUID) -> Any:
        self.reads.append((model, key))
        return self.row


def _criterion(cid: str, weight: str, mark: bool | None) -> Criterion:
    return Criterion.model_validate(
        {
            "id": cid,
            "text": f"Criterion {cid}",
            "evidence": f"What shows {cid}",
            "weight": weight,
            "check_method": "model_verdict",
            "soft_descent": False,
            "concepts": [],
            "mandatory_points": [],
            "must_by_author": mark,
        }
    )


def _rows(layer: CriteriaLayer, *weights: str) -> list[SubmissionCriterionVerdict]:
    return [
        SubmissionCriterionVerdict(
            criteria_layer=layer.value,
            criteria_source_id=_LIST,
            item_id=f"c{n}",
            item_kind=VerdictItemKind.CRITERION.value,
            weight=weight,
            verdict="met",
        )
        for n, weight in enumerate(weights, start=1)
    ]


_SUBMISSION = HomeworkSubmission(authored_document_id=_TASK)


class TestNothingIsReadWithoutAnAuthorsMust:
    async def test_the_models_list_reads_nothing(self) -> None:
        session = _Session()

        own = await judged_author_musts(
            session,  # type: ignore[arg-type]
            _SUBMISSION,
            _rows(CriteriaLayer.MODEL, "must", "should"),
        )

        assert own == frozenset()
        assert session.reads == []

    async def test_the_authors_list_without_a_must_reads_nothing(self) -> None:
        session = _Session()

        own = await judged_author_musts(
            session,  # type: ignore[arg-type]
            _SUBMISSION,
            _rows(CriteriaLayer.AUTHOR, "should", "may"),
        )

        assert own == frozenset()
        assert session.reads == []


class TestTheAuthorsListWithAMust:
    async def test_one_read_of_the_edits_row_by_its_key(self) -> None:
        edit = TaskCriteriaOverride(
            id=_LIST,
            authored_document_id=_TASK,
            criteria=criteria_to_document(
                [_criterion("c1", "must", True), _criterion("c2", "must", False)]
            ),
        )
        session = _Session(edit)

        own = await judged_author_musts(
            session,  # type: ignore[arg-type]
            _SUBMISSION,
            _rows(CriteriaLayer.AUTHOR, "must", "must"),
        )

        assert own == {"c1"}
        assert session.reads == [(TaskCriteriaOverride, _LIST)]

    async def test_an_edit_stored_before_the_mark_gives_none(self) -> None:
        stored = criteria_to_document([_criterion("c1", "must", None)])
        assert "must_by_author" not in stored[0]
        session = _Session(
            TaskCriteriaOverride(id=_LIST, authored_document_id=_TASK, criteria=stored)
        )

        own = await judged_author_musts(
            session,  # type: ignore[arg-type]
            _SUBMISSION,
            _rows(CriteriaLayer.AUTHOR, "must"),
        )

        assert own == frozenset()
        assert len(session.reads) == 1
