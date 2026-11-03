"""The course links a review gives, checked before it is given (task 10, K6).

Against a live database, through the real builder of a text review
(:class:`~course_supporter.homework.text_result.TextResultBuilder`), the real
search for course links and the real finder:

* lock b — a place the model chose that is no candidate when the review is
  built gives way to a place of the same material; else to the most
  confident; with none, the link is left out and a ``course_link_stale`` row
  is written without a model call;
* lock v — a second build of the same review gives the links of the first,
  even after a place was deleted in between, and searches nothing;
* lock g — after delivery the review's authenticity is the share of the
  criteria not met whose remark gives a link; with every criterion met there
  is no row;
* lock e — a kept place whose anchor does not fit its material is given
  without a place, and the build goes on.

The course: ``lesson1`` holds material A (segments A1, A2), ``lesson2``
material B (segment B1) and then the task; all three teach recursion. The
search ranks them B1, A2, A1 for c1 — B1 sits on the task's own lesson — so
c1.l2 is A2. c2 is about closures, which nothing teaches.

The locks are named by the brief's Ukrainian letters, transliterated
(``a b v h g d e``), as the locks of K5 are.

Requires PostgreSQL (``--run-db``).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncGenerator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from structlog.testing import capture_logs

from course_supporter.criteria_kinds import (
    CriteriaLayer,
    VerdictItemKind,
    VerdictValue,
    WeightCategory,
)
from course_supporter.homework.course_links import (
    CriterionLinks,
    FailedCriterion,
    FoundCourseLinks,
    LinkCandidate,
    SubmissionScope,
    look_up_course_links,
)
from course_supporter.homework.criteria_form import Criterion, criteria_to_document
from course_supporter.homework.result_builders import BuildContext, BuiltResult
from course_supporter.homework.text_result import TextResultBuilder
from course_supporter.models.review_structure import Position, Reference
from course_supporter.review_metrics import REVIEW_METRICS_ACTION
from course_supporter.service_logging import CourseLinkTrace, job_scope
from course_supporter.storage.document_segment_repository import SegmentAnchor
from course_supporter.storage.homework_repository import HomeworkRepository
from course_supporter.storage.orm import (
    CourseNode,
    DocumentSegment,
    ExternalServiceCall,
    HomeworkSubmission,
    Job,
    Tenant,
)
from course_supporter.storage.student_repository import StudentRepository
from course_supporter.storage.submission_verdict_repository import (
    SubmissionVerdictRepository,
    VerdictRecord,
)
from course_supporter.storage.task_criteria_override_repository import (
    TaskCriteriaOverrideRepository,
)
from tests.integration.test_document_segment_repository import (
    _seed_child,
    _seed_material,
    _seed_root,
    _seed_segment,
    _seed_summary,
)

pytestmark = pytest.mark.requires_db

_FINDER = "course_supporter.homework.course_link_delivery.build_course_link_finder"


def _criterion(cid: str, weight: str, concepts: list[str]) -> Criterion:
    return Criterion.model_validate(
        {
            "id": cid,
            "text": f"Criterion {cid}",
            "evidence": f"What shows {cid}",
            "weight": weight,
            "check_method": "model_verdict",
            "soft_descent": False,
            "concepts": concepts,
            "mandatory_points": [],
        }
    )


_CRITERIA = (
    _criterion("c1", "must", ["recursion"]),
    _criterion("c2", "should", ["closure"]),
)
_WEIGHTS = {"c1": WeightCategory.MUST, "c2": WeightCategory.SHOULD}


def _records(*, unmet: Sequence[str]) -> tuple[VerdictRecord, ...]:
    return tuple(
        VerdictRecord(
            c.id,
            VerdictItemKind.CRITERION,
            _WEIGHTS[c.id],
            VerdictValue.NOT_MET if c.id in unmet else VerdictValue.MET,
            VerdictValue.NOT_MET if c.id in unmet else VerdictValue.MET,
            quote=None if c.id in unmet else "def fib(n):",
            quote_file=None if c.id in unmet else "solution.py",
            quote_lines=None if c.id in unmet else (1, 1),
            missing="Немає." if c.id in unmet else None,
        )
        for c in _CRITERIA
    )


def _explanation(links: Mapping[str, Sequence[str]]) -> dict[str, object]:
    """The explanation as the stage keeps it: a remark on each criterion of
    ``links``, choosing those identifiers."""
    return {
        "passed": "c1" not in links,
        "why": "Причина вердикту за курсом.",
        "remarks": [
            {
                "id": cid,
                "what": f"Як зроблено {cid}.",
                "why": f"Чому {cid} не гаразд.",
                "todo": f"Як зробити {cid}.",
                "links": list(ids),
            }
            for cid, ids in links.items()
        ],
        "mentor_voice": None,
    }


@dataclass
class _World:
    root: CourseNode
    a1: DocumentSegment
    a2: DocumentSegment
    b1: DocumentSegment
    submission: HomeworkSubmission


@pytest.fixture()
async def world(db_session: AsyncSession, seed_tenant: Tenant) -> _World:
    root = await _seed_root(db_session, seed_tenant)
    lesson1 = await _seed_child(db_session, seed_tenant, root, order=0)
    lesson2 = await _seed_child(db_session, seed_tenant, root, order=1)
    material_a = await _seed_material(db_session, lesson1, root, order=0)
    summary_a = await _seed_summary(db_session, material_a, root, title="Рекурсія")
    a1 = await _seed_segment(
        db_session,
        summary_a,
        root,
        order=0,
        main=["recursion"],
        content="Рекурсія: база.",
        start_paragraph=1,
        end_paragraph=2,
    )
    a2 = await _seed_segment(
        db_session,
        summary_a,
        root,
        order=1,
        main=["recursion"],
        content="Рекурсія: крок.",
        start_paragraph=5,
        end_paragraph=6,
    )
    material_b = await _seed_material(db_session, lesson2, root, order=0)
    summary_b = await _seed_summary(
        db_session, material_b, root, title="Рекурсія на практиці"
    )
    b1 = await _seed_segment(
        db_session,
        summary_b,
        root,
        main=["recursion"],
        content="Рекурсія в задачах.",
        start_paragraph=2,
        end_paragraph=3,
    )
    task = await _seed_material(db_session, lesson2, root, task_type="task", order=5)
    student = await StudentRepository(db_session).create(
        tenant_id=seed_tenant.id, external_id=f"ext-{uuid.uuid4().hex[:8]}"
    )
    submission = await HomeworkRepository(db_session).create(
        tenant_id=seed_tenant.id,
        student_id=student.id,
        course_node_id=root.id,
        node_id=lesson2.id,
        authored_document_id=task.id,
        file_url="s3://bucket/solution.py",
        file_type="text/plain",
        original_filename="solution.py",
    )
    edit = await TaskCriteriaOverrideRepository(db_session).replace(
        authored_document_id=task.id,
        source_content_hash="h" * 64,
        source_task_type="task",
        criteria=criteria_to_document(_CRITERIA),
    )
    await SubmissionVerdictRepository(db_session).replace_for_submission(
        tenant_id=seed_tenant.id,
        submission_id=submission.id,
        criteria_layer=CriteriaLayer.AUTHOR,
        criteria_source_id=edit.id,
        records=_records(unmet=("c1", "c2")),
    )
    return _World(root, a1, a2, b1, submission)


@pytest.fixture()
async def register_job(
    session_factory: async_sessionmaker[AsyncSession],
    committed_seeds: dict[str, uuid.UUID],
) -> AsyncGenerator[uuid.UUID]:
    """A committed job: register rows are written in their own session."""
    async with session_factory() as session:
        job = Job(
            tenant_id=committed_seeds["tenant_id"],
            course_node_id=committed_seeds["course_node_id"],
            job_type="homework_processing",
        )
        session.add(job)
        await session.commit()
        job_id = job.id

    yield job_id

    async with session_factory() as session:
        await session.execute(
            delete(ExternalServiceCall).where(ExternalServiceCall.job_id == job_id)
        )
        await session.execute(delete(Job).where(Job.id == job_id))
        await session.commit()


async def _searched_and_explained(
    session: AsyncSession, world: _World, links: Mapping[str, Sequence[str]]
) -> FoundCourseLinks:
    """The two stages before the builder: the real search, a kept explanation."""
    found = await look_up_course_links(session, world.submission)
    await SubmissionVerdictRepository(session).store_explanation(
        tenant_id=world.submission.tenant_id,
        submission_id=world.submission.id,
        language="ukr",
        body=_explanation(links),
    )
    return found


def _context(
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    world: _World,
) -> BuildContext:
    return BuildContext(
        session=session,
        session_factory=session_factory,
        submission=world.submission,
        submission_text="def fib(n): ...",
        review_language="ukr",
        redis=None,
    )


async def _build(
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    world: _World,
    job_id: uuid.UUID,
) -> BuiltResult:
    with job_scope(job_id):
        return await TextResultBuilder().build(
            _context(session, session_factory, world)
        )


async def _gone(session: AsyncSession, *segments: DocumentSegment) -> None:
    for segment in segments:
        segment.deleted_at = datetime.now(UTC)
    await session.flush()


def _linked(built: BuiltResult) -> list[list[str | None]]:
    """Each remark's links, heaviest first, as the fragments they point at."""
    return [
        [ref.segment_id for ref in remark.read]
        for remark in built.structure.new_remarks
    ]


async def _register(
    session_factory: async_sessionmaker[AsyncSession], job_id: uuid.UUID
) -> list[ExternalServiceCall]:
    async with session_factory() as session:
        rows = await session.execute(
            select(ExternalServiceCall)
            .where(ExternalServiceCall.job_id == job_id)
            .order_by(ExternalServiceCall.created_at)
        )
        return list(rows.scalars())


class _NoSearch:
    """A finder that must not be asked."""

    async def find(
        self, scope: SubmissionScope, failed: Sequence[FailedCriterion]
    ) -> Mapping[str, tuple[LinkCandidate, ...]]:
        raise AssertionError("a settled review searches nothing")


class _SpyFinder:
    """The real finder, recording what it is asked."""

    def __init__(self, session: AsyncSession) -> None:
        from course_supporter.homework.course_links import ConceptCourseLinkFinder

        self._real = ConceptCourseLinkFinder(session)
        self.asked: list[list[str]] = []

    async def find(
        self, scope: SubmissionScope, failed: Sequence[FailedCriterion]
    ) -> Mapping[str, tuple[LinkCandidate, ...]]:
        self.asked.append([criterion.id for criterion in failed])
        return await self._real.find(scope, failed)


class TestTheWorld:
    async def test_c1_l2_is_a2(self, db_session: AsyncSession, world: _World) -> None:
        """The search ranks the places as the module says: the other tests
        rest on it."""
        found = await look_up_course_links(db_session, world.submission)

        assert [place.segment_id for _, place in found.shown("c1")] == [
            world.b1.id,
            world.a2.id,
            world.a1.id,
        ]
        assert found.shown("c2") == ()


class TestAChosenPlaceGone:
    """Lock b: replaced from the same material, else the most confident;
    without a replacement left out, with a ``course_link_stale`` row."""

    async def test_a_place_of_the_same_material_replaces_it(
        self,
        db_session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
        register_job: uuid.UUID,
        world: _World,
    ) -> None:
        """A2 gone: A1 — its material's — not B1, though B1 is more confident."""
        await _searched_and_explained(db_session, world, {"c1": ["c1.l2"], "c2": []})
        await _gone(db_session, world.a2)
        spy = _SpyFinder(db_session)

        with patch(_FINDER, return_value=spy):
            built = await _build(db_session, session_factory, world, register_job)

        assert built.structure.new_remarks[0].read == [
            Reference(
                title="Рекурсія",
                position=Position(kind="paragraph", value="1"),
                excerpt="Рекурсія: база.",
                segment_id=str(world.a1.id),
            )
        ]
        assert built.structure.new_remarks[1].read == []
        # Searched again for c1 alone, the criterion with a place gone.
        assert spy.asked == [["c1"]]
        assert await _register(session_factory, register_job) == []

    async def test_without_one_of_its_material_the_most_confident(
        self,
        db_session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
        register_job: uuid.UUID,
        world: _World,
    ) -> None:
        await _searched_and_explained(db_session, world, {"c1": ["c1.l2"], "c2": []})
        await _gone(db_session, world.a2, world.a1)

        built = await _build(db_session, session_factory, world, register_job)

        assert _linked(built) == [[str(world.b1.id)], []]

    async def test_a_replacement_is_never_a_place_the_remark_gives(
        self,
        db_session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
        register_job: uuid.UUID,
        world: _World,
    ) -> None:
        """c1.l3 (A1) stays; A2, gone, would be replaced by A1 — taken — so B1."""
        await _searched_and_explained(
            db_session, world, {"c1": ["c1.l2", "c1.l3"], "c2": []}
        )
        await _gone(db_session, world.a2)

        built = await _build(db_session, session_factory, world, register_job)

        assert _linked(built) == [[str(world.b1.id), str(world.a1.id)], []]

    async def test_without_a_replacement_the_link_is_left_out_and_traced(
        self,
        db_session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
        register_job: uuid.UUID,
        world: _World,
    ) -> None:
        await _searched_and_explained(db_session, world, {"c1": ["c1.l2"], "c2": []})
        await _gone(db_session, world.a1, world.a2, world.b1)

        built = await _build(db_session, session_factory, world, register_job)

        assert _linked(built) == [[], []]
        (trace,) = await _register(session_factory, register_job)
        assert trace.action == CourseLinkTrace.STALE.value == "course_link_stale"
        assert json.loads(trace.output_text or "") == {
            "criterion_id": "c1",
            "ids": ["c1.l2"],
        }
        # No call: out of every cost sum and of the failure count.
        assert (trace.provider, trace.model_id, trace.success) == (None, None, None)
        assert (trace.cost_usd, trace.error_message, trace.outcome) == (
            None,
            None,
            None,
        )


class TestASecondBuild:
    """Lock v: the links of the first build, whatever happened in between."""

    async def test_it_gives_the_same_links_after_a_place_was_deleted(
        self,
        db_session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
        register_job: uuid.UUID,
        world: _World,
    ) -> None:
        await _searched_and_explained(db_session, world, {"c1": ["c1.l2"], "c2": []})
        first = await _build(db_session, session_factory, world, register_job)
        await _gone(db_session, world.a2)

        with patch(_FINDER, return_value=_NoSearch()):
            second = await _build(db_session, session_factory, world, register_job)

        assert _linked(first) == [[str(world.a2.id)], []]
        assert second.structure == first.structure
        assert second.markdown == first.markdown
        assert await _register(session_factory, register_job) == []

    async def test_the_settled_links_are_kept_beside_what_was_found(
        self,
        db_session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
        register_job: uuid.UUID,
        world: _World,
    ) -> None:
        found = await _searched_and_explained(
            db_session, world, {"c1": ["c1.l2"], "c2": []}
        )
        await _gone(db_session, world.a2)

        await _build(db_session, session_factory, world, register_job)

        row = await SubmissionVerdictRepository(db_session).get_candidates(
            tenant_id=world.submission.tenant_id,
            submission_id=world.submission.id,
        )
        assert row is not None
        kept = FoundCourseLinks.model_validate(row.body)
        assert kept.criteria == found.criteria
        assert [place.segment_id for place in kept.settled_of("c1")] == [world.a1.id]
        assert kept.settled_of("c2") == ()


class TestAuthenticity:
    """Lock g: the share of the criteria not met whose remark is linked."""

    async def _metrics(
        self, session_factory: async_sessionmaker[AsyncSession], job_id: uuid.UUID
    ) -> list[tuple[float | None, float | None]]:
        return [
            (row.authenticity, row.completeness)
            for row in await _register(session_factory, job_id)
            if row.action == REVIEW_METRICS_ACTION
        ]

    async def test_one_of_two_linked_is_a_half(
        self,
        db_session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
        register_job: uuid.UUID,
        world: _World,
    ) -> None:
        await _searched_and_explained(db_session, world, {"c1": ["c1.l1"], "c2": []})
        await _build(db_session, session_factory, world, register_job)

        with job_scope(register_job):
            await TextResultBuilder().after_delivery(
                _context(db_session, session_factory, world)
            )

        assert await self._metrics(session_factory, register_job) == [(0.5, None)]

    async def test_a_link_left_out_does_not_count(
        self,
        db_session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
        register_job: uuid.UUID,
        world: _World,
    ) -> None:
        """What the review GAVE is measured, not what the model chose."""
        await _searched_and_explained(db_session, world, {"c1": ["c1.l1"], "c2": []})
        await _gone(db_session, world.a1, world.a2, world.b1)
        await _build(db_session, session_factory, world, register_job)

        with job_scope(register_job):
            await TextResultBuilder().after_delivery(
                _context(db_session, session_factory, world)
            )

        assert await self._metrics(session_factory, register_job) == [(0.0, None)]

    async def test_with_every_criterion_met_there_is_no_row(
        self,
        db_session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
        register_job: uuid.UUID,
        world: _World,
    ) -> None:
        verdicts = SubmissionVerdictRepository(db_session)
        rows = await verdicts.list_for_submission(
            tenant_id=world.submission.tenant_id,
            submission_id=world.submission.id,
        )
        await verdicts.replace_for_submission(
            tenant_id=world.submission.tenant_id,
            submission_id=world.submission.id,
            criteria_layer=CriteriaLayer.AUTHOR,
            criteria_source_id=rows[0].criteria_source_id,
            records=_records(unmet=()),
        )

        with job_scope(register_job):
            await TextResultBuilder().after_delivery(
                _context(db_session, session_factory, world)
            )

        assert await self._metrics(session_factory, register_job) == []


class TestAnAnchorThatDoesNotFit:
    """Lock e: the place is skipped, the build goes on, the log names it."""

    async def test_the_link_keeps_its_material_and_loses_its_place(
        self,
        db_session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
        register_job: uuid.UUID,
        world: _World,
    ) -> None:
        # A live segment, kept as a video's with a paragraph's anchor.
        misfit = LinkCandidate(
            segment_id=world.a1.id,
            authored_document_id=uuid.uuid4(),
            material_title="Рекурсія",
            source_type="video",
            anchor=SegmentAnchor("paragraph", 1, 2),
            excerpt="Рекурсія: база.",
            later_in_course=False,
            confidence=0.9,
            matched_concepts=("recursion",),
        )
        await SubmissionVerdictRepository(db_session).store_candidates(
            tenant_id=world.submission.tenant_id,
            submission_id=world.submission.id,
            body=FoundCourseLinks(
                criteria=(
                    CriterionLinks(item_id="c1", candidates=(misfit,)),
                    CriterionLinks(item_id="c2", candidates=()),
                )
            ).model_dump(mode="json"),
        )
        await SubmissionVerdictRepository(db_session).store_explanation(
            tenant_id=world.submission.tenant_id,
            submission_id=world.submission.id,
            language="ukr",
            body=_explanation({"c1": ["c1.l1"], "c2": []}),
        )

        with capture_logs() as logs:
            built = await _build(db_session, session_factory, world, register_job)

        assert built.structure.new_remarks[0].read == [
            Reference(
                title="Рекурсія",
                excerpt="Рекурсія: база.",
                segment_id=str(world.a1.id),
            )
        ]
        assert "Рекурсія" in built.markdown
        skipped = [e for e in logs if e["event"] == "course_link_position_skipped"]
        assert [e["segment_id"] for e in skipped] == [str(world.a1.id)]
        assert skipped[0]["log_level"] == "warning"
