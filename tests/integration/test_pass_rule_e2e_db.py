"""The pass rule of 2026-11-12 through the body: lock d and lock e of the order.

The order's locks, transliterated: d — end to end, by name; e — one function.

Lock d: a ``task`` on the model's list, the recorded answers of the model give
one "must" not met and a score of 82 — the review says "passed", with the
phrase of the 80 threshold in its markdown. The author then saves a list with
the same "must" (what ``PUT /documents/{id}/criteria/override`` stores), and
the same work submitted again is "not passed", with the phrase of the
author's mandatory criteria.

Lock e: in both walks the pass is one: the explanation stage's facts (what the
model was told it may not contradict, read from the stage's log), the builder's
structure, a recount from the stored rows, the webhook and the portal.

What is real and what is a double — as in ``test_text_review_e2e_db.py``, whose
doubles and helpers this module reuses: the router answers with recorded
answers, the funds port allows, the webhook's transport records. The list in
force is read for real: :func:`load_in_force`, the choice the criteria service
makes between the model's list and the author's, over rows written by the
repositories the service and the route write through.

Requires ``docker compose up -d``; run with ``--run-db``.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from structlog.testing import capture_logs

from course_supporter.api.routes._portal_shared import curated_verdict
from course_supporter.criteria_kinds import CriteriaLayer
from course_supporter.homework.criteria_form import Criterion, criteria_to_document
from course_supporter.homework.criteria_list_service import (
    CriteriaInForce,
    CriteriaUnavailable,
    UnavailableReason,
    load_in_force,
)
from course_supporter.homework.criteria_verdicts import is_passed, score_percent
from course_supporter.homework.judged_list import judged_layer
from course_supporter.homework.path_stages import (
    CRITERIA_EVALUATION,
    REVIEW_EXPLANATION,
)
from course_supporter.models.review_structure import PassRule, ReviewStructureV1
from course_supporter.storage.orm import (
    AuthoredDocument,
    CourseNode,
    ExternalServiceCall,
    HomeworkSubmission,
    Job,
    Student,
    Tenant,
)
from course_supporter.storage.submission_verdict_repository import (
    SubmissionVerdictRepository,
)
from course_supporter.storage.task_criteria_list_repository import (
    TaskCriteriaListRepository,
)
from course_supporter.storage.task_criteria_override_repository import (
    TaskCriteriaOverrideRepository,
)
from tests.integration.test_text_review_e2e_db import (
    _evaluation,
    _reviewed,
    _RouterDouble,
    _run,
    _submission,
    _submit,
    _verdict,
    _webhook,
)

pytestmark = pytest.mark.requires_db

_HASH = "h" * 64


def _criterion(cid: str, weight: str) -> Criterion:
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
        }
    )


# c1 "must" (3) and seven "should" (2 each): with c1 alone not met the score is
# 14 of 17 -> 82 — at 80 or more, below 100.
_CRITERIA = (
    _criterion("c1", "must"),
    *(_criterion(f"c{n}", "should") for n in range(2, 9)),
)

# The recorded answer of the model: c1 not met, the rest met on lines of the
# work (``def solve(n):`` / ``return n * 2``).
_EVALUATION = _evaluation(
    _verdict("c1", "not_met", missing="Немає перевірки від'ємного n."),
    *(
        _verdict(f"c{n}", "met", quote="def solve(n):" if n % 2 else "return n * 2")
        for n in range(2, 9)
    ),
)


def _explanation(*, passed: bool) -> str:
    """The recorded explanation; its ``passed`` must repeat the code's."""
    return json.dumps(
        {
            "passed": passed,
            "why": "Функція є, але від'ємне n не перевірено.",
            "remarks": [
                {
                    "id": "c1",
                    "what": "Зараз від'ємне n не перевіряється.",
                    "why": "Функція поверне хибний результат.",
                    "todo": "Додайте перевірку на початку функції.",
                    "links": [],
                }
            ],
            "mentor_voice": None,
        },
        ensure_ascii=False,
    )


class _InForce:
    """The criteria source: the list in force, read for real; no composing.

    :func:`load_in_force` is the choice the criteria service makes over the
    task's rows — the author's live edit for this version, else the model's
    ready list. The service's own composing on a miss is the one thing left
    out: the model's list is seeded ready.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get_or_compose(
        self, authored_document_id: uuid.UUID
    ) -> CriteriaInForce | CriteriaUnavailable:
        async with self._session_factory() as session:
            in_force = await load_in_force(session, authored_document_id)
        if in_force is None:
            return CriteriaUnavailable(UnavailableReason.COMPOSITION_FAILED)
        return in_force


@pytest.fixture()
async def world(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[dict[str, Any]]:
    """A ``task`` of the current version with the model's ready list; a student."""
    async with session_factory() as session:
        tenant = Tenant(name=f"pass-rule-{uuid.uuid4().hex[:8]}")
        session.add(tenant)
        await session.flush()
        node = CourseNode(
            tenant_id=tenant.id, title="Course", order=0, default_language="ukr"
        )
        session.add(node)
        await session.flush()
        doc = AuthoredDocument(
            course_node_id=node.id,
            course_root_id=node.id,
            source_type="text",
            source_url=f"https://example.com/{uuid.uuid4().hex}",
            task_type="task",
            language="ukr",
            content_hash=_HASH,
        )
        session.add(doc)
        await session.flush()
        lists = TaskCriteriaListRepository(session)
        claimed = await lists.claim(
            authored_document_id=doc.id,
            source_content_hash=_HASH,
            source_task_type="task",
            form_version=2,
        )
        assert claimed is not None
        assert await lists.mark_ready(
            claimed.id,
            claimed_at=claimed.claimed_at,
            criteria=criteria_to_document(_CRITERIA),
            contradictions=[],
            concepts_in_input=False,
            dropped_concept_count=0,
            input_fingerprint="f" * 64,
        )
        student = Student(
            tenant_id=tenant.id, external_id=f"stu-{uuid.uuid4().hex[:6]}"
        )
        session.add(student)
        await session.commit()
        ids: dict[str, Any] = {
            "tenant_id": tenant.id,
            "node_id": node.id,
            "doc_id": doc.id,
            "student_id": student.id,
            "model_list_id": claimed.id,
            "submissions": [],
        }

    yield ids

    async with session_factory() as session:
        for submission_id in ids["submissions"]:
            job_ids = select(Job.id).where(Job.subject_id == submission_id)
            await session.execute(
                delete(ExternalServiceCall).where(
                    ExternalServiceCall.job_id.in_(job_ids)
                )
            )
            await session.execute(
                delete(HomeworkSubmission).where(HomeworkSubmission.id == submission_id)
            )
            await session.execute(delete(Job).where(Job.subject_id == submission_id))
        await session.commit()


async def _author_saves_the_list(
    session_factory: async_sessionmaker[AsyncSession], world: dict[str, Any]
) -> uuid.UUID:
    """The author keeps the model's criteria, the "must" with them, as their own.

    What ``PUT /documents/{id}/criteria/override`` stores: the whole list, for
    the task's current version, through the same repository.
    """
    async with session_factory() as session:
        saved = await TaskCriteriaOverrideRepository(session).replace(
            authored_document_id=world["doc_id"],
            source_content_hash=_HASH,
            source_task_type="task",
            criteria=criteria_to_document(_CRITERIA),
        )
        await session.commit()
        return saved.id


async def _walk(
    session_factory: async_sessionmaker[AsyncSession],
    world: dict[str, Any],
    router: _RouterDouble,
    tmp_path: Path,
) -> dict[str, Any]:
    """One submission of the work, reviewed and delivered; what each place says."""
    webhook = _webhook()
    submission_id, job_id = await _submit(session_factory, world)
    with capture_logs() as logs:
        await _run(
            session_factory,
            router,
            _InForce(session_factory),  # type: ignore[arg-type]
            webhook,
            tmp_path,
            submission_id=submission_id,
            job_id=job_id,
        )
    submission = await _submission(session_factory, submission_id)
    assert submission.status == "delivered", submission.error_message
    async with session_factory() as session:
        rows = await SubmissionVerdictRepository(session).list_for_submission(
            tenant_id=world["tenant_id"], submission_id=submission_id
        )
    [explained] = [log for log in logs if log["event"] == "review_explanation_written"]
    [payload] = _reviewed(webhook)
    portal = curated_verdict(submission.review_result, score=submission.score)
    assert portal is not None
    structure = ReviewStructureV1.model_validate(submission.review_result)
    assert structure.verdict is not None
    return {
        "submission": submission,
        "structure": structure,
        "layer": judged_layer(rows),
        "source_id": {row.criteria_source_id for row in rows},
        "verdicts": {row.item_id: row.verdict for row in rows},
        "score": score_percent(rows),
        # Lock e: the five places that state the pass.
        "passes": {
            "stage": explained["passed"],
            "builder": structure.verdict.passed,
            "recount": is_passed(rows, layer=judged_layer(rows)),
            "webhook": payload.review.passed,
            "portal": portal.passed,
        },
    }


class TestLockD:
    """d: the same "must" not met — passed on the model's list, not on the author's."""

    async def test_the_models_must_does_not_block_the_authors_does(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        world: dict[str, Any],
        tmp_path: Path,
    ) -> None:
        router = _RouterDouble(session_factory)
        router.content[CRITERIA_EVALUATION] = _EVALUATION

        # ── The model's list: c1 "must" not met, 82 -> passed (80) ──
        router.content[REVIEW_EXPLANATION] = _explanation(passed=True)
        first = await _walk(session_factory, world, router, tmp_path)

        assert first["layer"] is CriteriaLayer.MODEL
        assert first["source_id"] == {world["model_list_id"]}
        assert first["verdicts"]["c1"] == "not_met"
        assert first["score"] == first["submission"].score == 82
        assert first["passes"] == dict.fromkeys(first["passes"], True)
        assert first["structure"].verdict.rule == PassRule(
            basis="no_author_musts_passed", score=82, threshold=80
        )
        markdown = first["submission"].review_markdown
        assert "## Зараховано" in markdown
        assert (
            "Автор не затвердив обов'язкових критеріїв для цього завдання. Для "
            "таких завдань поріг заліку — 80 балів; ваш бал — 82, тож роботу "
            "зараховано."
        ) in markdown.splitlines()
        # The "must" is judged and shown: its remark is in the review.
        assert "Зараз від'ємне n не перевіряється." in markdown

        # ── The author saves the list with the same "must" ──
        override_id = await _author_saves_the_list(session_factory, world)

        # ── The same work again: c1 not met -> not passed, whatever the score ──
        router.content[REVIEW_EXPLANATION] = _explanation(passed=False)
        second = await _walk(session_factory, world, router, tmp_path)

        assert second["layer"] is CriteriaLayer.AUTHOR
        assert second["source_id"] == {override_id}
        assert second["verdicts"] == first["verdicts"]
        assert second["score"] == second["submission"].score == 82
        assert second["passes"] == dict.fromkeys(second["passes"], False)
        assert second["structure"].verdict.rule == PassRule(
            basis="author_musts_unmet", score=82, threshold=70
        )
        markdown = second["submission"].review_markdown
        assert "## Не зараховано" in markdown
        assert (
            "Не виконано обов'язкові критерії автора, тому роботу не зараховано. "
            "Ваш бал — 82."
        ) in markdown.splitlines()
        assert "Зараз від'ємне n не перевіряється." in markdown
