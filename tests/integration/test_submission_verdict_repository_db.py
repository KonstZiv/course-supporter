"""Storage of verdicts and explanations against a live database (task 09b, K1).

What only a real PostgreSQL answers is here: that a repeat write of a
submission's verdicts leaves one set of rows; that every read is bounded by the
tenant; that the resubmission safeguard reads back exactly the LATEST earlier
verdict of the same item of the same list — not another student's, not another
task's, not another list's, not a soft-deleted submission's, not a later one's;
and that the rows go with a hard delete of their submission.

Submissions written in one transaction share ``now()`` as their creation time,
so "earlier" falls through to the id here — which is the tie-break the
repository promises, tested by the same token.

Requires ``docker compose up -d``; run with ``--run-db``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.exc import NoResultFound
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.criteria_kinds import (
    CriteriaLayer,
    VerdictItemKind,
    VerdictValue,
    WeightCategory,
)
from course_supporter.storage.homework_repository import HomeworkRepository
from course_supporter.storage.orm import (
    AuthoredDocument,
    CourseNode,
    HomeworkSubmission,
    Student,
    SubmissionCriterionVerdict,
    SubmissionExplanation,
    Tenant,
)
from course_supporter.storage.student_repository import StudentRepository
from course_supporter.storage.submission_verdict_repository import (
    SubmissionVerdictRepository,
    VerdictRecord,
)
from tests._helpers.course_node_factory import make_root_course_node

pytestmark = pytest.mark.requires_db


async def _student(session: AsyncSession, tenant: Tenant) -> Student:
    return await StudentRepository(session).create(
        tenant_id=tenant.id, external_id=f"ext-{uuid.uuid4().hex[:8]}"
    )


async def _submission(
    session: AsyncSession,
    *,
    tenant: Tenant,
    student: Student,
    root: CourseNode,
    doc: AuthoredDocument,
) -> HomeworkSubmission:
    return await HomeworkRepository(session).create(
        tenant_id=tenant.id,
        student_id=student.id,
        course_node_id=root.id,
        node_id=root.id,
        authored_document_id=doc.id,
        file_url="s3://bucket/solution.py",
        file_type="text/plain",
        original_filename="solution.py",
    )


async def _other_task(session: AsyncSession, root: CourseNode) -> AuthoredDocument:
    doc = AuthoredDocument(
        course_node_id=root.id,
        course_root_id=root.id,
        source_type="web",
        source_url="https://example.com/other",
    )
    session.add(doc)
    await session.flush()
    return doc


async def _other_tenant(session: AsyncSession) -> tuple[Tenant, CourseNode]:
    tenant = Tenant(name=f"other-tenant-{uuid.uuid4().hex[:8]}")
    session.add(tenant)
    await session.flush()
    root = make_root_course_node(tenant_id=tenant.id, title="Other", order=0)
    session.add(root)
    await session.flush()
    return tenant, root


def _met(item_id: str, quote: str) -> VerdictRecord:
    return VerdictRecord(
        item_id=item_id,
        item_kind=VerdictItemKind.CRITERION,
        weight=WeightCategory.MUST,
        verdict=VerdictValue.MET,
        model_verdict=VerdictValue.MET,
        quote=quote,
        quote_file="solution.py",
        quote_lines=(2, 3),
    )


def _not_met(item_id: str) -> VerdictRecord:
    return VerdictRecord(
        item_id=item_id,
        item_kind=VerdictItemKind.CRITERION,
        weight=WeightCategory.SHOULD,
        verdict=VerdictValue.NOT_MET,
        model_verdict=VerdictValue.NOT_MET,
        missing="Немає перевірки порожнього списку.",
    )


async def _write(
    session: AsyncSession,
    submission: HomeworkSubmission,
    records: list[VerdictRecord],
    *,
    source_id: uuid.UUID,
    layer: CriteriaLayer = CriteriaLayer.MODEL,
) -> None:
    await SubmissionVerdictRepository(session).replace_for_submission(
        tenant_id=submission.tenant_id,
        submission_id=submission.id,
        criteria_layer=layer,
        criteria_source_id=source_id,
        records=records,
    )


async def _previous(
    session: AsyncSession,
    submission: HomeworkSubmission,
    *,
    source_id: uuid.UUID,
    layer: CriteriaLayer = CriteriaLayer.MODEL,
    tenant_id: uuid.UUID | None = None,
) -> dict[str, SubmissionCriterionVerdict]:
    return await SubmissionVerdictRepository(session).previous_verdicts(
        tenant_id=tenant_id or submission.tenant_id,
        submission_id=submission.id,
        criteria_layer=layer,
        criteria_source_id=source_id,
    )


@pytest.fixture()
async def student(db_session: AsyncSession, seed_tenant: Tenant) -> Student:
    return await _student(db_session, seed_tenant)


@pytest.fixture()
async def first(
    db_session: AsyncSession,
    seed_tenant: Tenant,
    student: Student,
    seed_root_node: CourseNode,
    seed_material_entry: AuthoredDocument,
) -> HomeworkSubmission:
    return await _submission(
        db_session,
        tenant=seed_tenant,
        student=student,
        root=seed_root_node,
        doc=seed_material_entry,
    )


class TestReplaceForSubmission:
    async def test_the_records_are_written_with_the_list_address(
        self, db_session: AsyncSession, first: HomeworkSubmission
    ) -> None:
        source = uuid.uuid4()
        await _write(
            db_session, first, [_met("c1", "a = 1"), _not_met("c2")], source_id=source
        )

        rows = await SubmissionVerdictRepository(db_session).list_for_submission(
            tenant_id=first.tenant_id, submission_id=first.id
        )

        assert [(r.item_id, r.verdict) for r in rows] == [
            ("c1", "met"),
            ("c2", "not_met"),
        ]
        assert {(r.criteria_layer, r.criteria_source_id) for r in rows} == {
            ("model", source)
        }
        assert (rows[0].quote_line_start, rows[0].quote_line_end) == (2, 3)
        assert rows[1].missing == "Немає перевірки порожнього списку."

    async def test_a_repeat_write_leaves_one_set_of_rows(
        self, db_session: AsyncSession, first: HomeworkSubmission
    ) -> None:
        """A stage that runs again after a crash rewrites, it does not add."""
        source = uuid.uuid4()
        await _write(
            db_session, first, [_met("c1", "a = 1"), _not_met("c2")], source_id=source
        )
        await _write(db_session, first, [_not_met("c1")], source_id=source)

        rows = await SubmissionVerdictRepository(db_session).list_for_submission(
            tenant_id=first.tenant_id, submission_id=first.id
        )

        assert [(r.item_id, r.verdict) for r in rows] == [("c1", "not_met")]

    async def test_another_submissions_rows_are_left_alone(
        self,
        db_session: AsyncSession,
        seed_tenant: Tenant,
        student: Student,
        seed_root_node: CourseNode,
        seed_material_entry: AuthoredDocument,
        first: HomeworkSubmission,
    ) -> None:
        second = await _submission(
            db_session,
            tenant=seed_tenant,
            student=student,
            root=seed_root_node,
            doc=seed_material_entry,
        )
        source = uuid.uuid4()
        await _write(db_session, first, [_met("c1", "a = 1")], source_id=source)
        await _write(db_session, second, [_not_met("c1")], source_id=source)

        kept = await SubmissionVerdictRepository(db_session).list_for_submission(
            tenant_id=first.tenant_id, submission_id=first.id
        )

        assert [(r.item_id, r.verdict) for r in kept] == [("c1", "met")]

    async def test_the_safeguard_flag_follows_its_source(
        self, db_session: AsyncSession, first: HomeworkSubmission
    ) -> None:
        kept_from = uuid.uuid4()
        record = VerdictRecord(
            item_id="c1",
            item_kind=VerdictItemKind.CRITERION,
            weight=WeightCategory.MUST,
            verdict=VerdictValue.MET,
            model_verdict=VerdictValue.NOT_MET,
            quote="a = 1",
            quote_file="solution.py",
            quote_lines=(1, 1),
            safeguard_submission_id=kept_from,
        )
        await _write(db_session, first, [record], source_id=uuid.uuid4())

        (row,) = await SubmissionVerdictRepository(db_session).list_for_submission(
            tenant_id=first.tenant_id, submission_id=first.id
        )

        assert row.safeguard_fired is True
        assert row.safeguard_submission_id == kept_from
        assert (row.model_verdict, row.verdict) == ("not_met", "met")


class TestEveryReadIsBoundedByTheTenant:
    async def test_another_tenant_reads_no_verdicts(
        self, db_session: AsyncSession, first: HomeworkSubmission
    ) -> None:
        await _write(db_session, first, [_met("c1", "a = 1")], source_id=uuid.uuid4())
        stranger, _ = await _other_tenant(db_session)
        repo = SubmissionVerdictRepository(db_session)

        own = await repo.list_for_submission(
            tenant_id=first.tenant_id, submission_id=first.id
        )
        foreign = await repo.list_for_submission(
            tenant_id=stranger.id, submission_id=first.id
        )

        assert len(own) == 1  # the premise: there is something to hide
        assert foreign == []

    async def test_another_tenant_reads_no_previous_verdicts(
        self,
        db_session: AsyncSession,
        seed_tenant: Tenant,
        student: Student,
        seed_root_node: CourseNode,
        seed_material_entry: AuthoredDocument,
        first: HomeworkSubmission,
    ) -> None:
        source = uuid.uuid4()
        await _write(db_session, first, [_met("c1", "a = 1")], source_id=source)
        second = await _submission(
            db_session,
            tenant=seed_tenant,
            student=student,
            root=seed_root_node,
            doc=seed_material_entry,
        )
        stranger, _ = await _other_tenant(db_session)

        own = await _previous(db_session, second, source_id=source)
        foreign = await _previous(
            db_session, second, source_id=source, tenant_id=stranger.id
        )

        assert set(own) == {"c1"}
        assert foreign == {}

    async def test_another_tenant_reads_no_explanation(
        self, db_session: AsyncSession, first: HomeworkSubmission
    ) -> None:
        repo = SubmissionVerdictRepository(db_session)
        await repo.store_explanation(
            tenant_id=first.tenant_id,
            submission_id=first.id,
            language="ukr",
            body={"why": "Усе на місці."},
        )
        stranger, _ = await _other_tenant(db_session)

        own = await repo.get_explanation(
            tenant_id=first.tenant_id, submission_id=first.id
        )
        foreign = await repo.get_explanation(
            tenant_id=stranger.id, submission_id=first.id
        )

        assert own is not None
        assert foreign is None


class TestPreviousVerdicts:
    """What the resubmission safeguard reads: the last earlier verdict per item."""

    async def test_the_latest_earlier_submission_wins_per_item(
        self,
        db_session: AsyncSession,
        seed_tenant: Tenant,
        student: Student,
        seed_root_node: CourseNode,
        seed_material_entry: AuthoredDocument,
        first: HomeworkSubmission,
    ) -> None:
        source = uuid.uuid4()
        second = await _submission(
            db_session,
            tenant=seed_tenant,
            student=student,
            root=seed_root_node,
            doc=seed_material_entry,
        )
        third = await _submission(
            db_session,
            tenant=seed_tenant,
            student=student,
            root=seed_root_node,
            doc=seed_material_entry,
        )
        await _write(
            db_session,
            first,
            [_met("c1", "first"), _met("c2", "only first")],
            source_id=source,
        )
        await _write(db_session, second, [_not_met("c1")], source_id=source)

        previous = await _previous(db_session, third, source_id=source)

        # c1: the second submission is the latest earlier one — its "not met"
        # stands, not the first one's "met"; c2: only the first judged it.
        assert {item: row.verdict for item, row in previous.items()} == {
            "c1": "not_met",
            "c2": "met",
        }
        assert previous["c2"].submission_id == first.id

    async def test_a_later_submission_is_not_earlier(
        self,
        db_session: AsyncSession,
        seed_tenant: Tenant,
        student: Student,
        seed_root_node: CourseNode,
        seed_material_entry: AuthoredDocument,
        first: HomeworkSubmission,
    ) -> None:
        source = uuid.uuid4()
        later = await _submission(
            db_session,
            tenant=seed_tenant,
            student=student,
            root=seed_root_node,
            doc=seed_material_entry,
        )
        await _write(db_session, later, [_met("c1", "later")], source_id=source)

        assert await _previous(db_session, first, source_id=source) == {}
        # The premise, from the other side: the later one does see the first.
        await _write(db_session, first, [_met("c1", "first")], source_id=source)
        assert set(await _previous(db_session, later, source_id=source)) == {"c1"}

    async def test_the_submission_itself_is_not_its_own_past(
        self, db_session: AsyncSession, first: HomeworkSubmission
    ) -> None:
        source = uuid.uuid4()
        await _write(db_session, first, [_met("c1", "a = 1")], source_id=source)

        assert await _previous(db_session, first, source_id=source) == {}

    @pytest.mark.parametrize("differs_by", ["list", "layer"])
    async def test_another_list_is_not_the_same_list(
        self,
        db_session: AsyncSession,
        seed_tenant: Tenant,
        student: Student,
        seed_root_node: CourseNode,
        seed_material_entry: AuthoredDocument,
        first: HomeworkSubmission,
        differs_by: str,
    ) -> None:
        """Identifiers are stable within one list only (task 08, decision 21)."""
        source = uuid.uuid4()
        await _write(db_session, first, [_met("c1", "a = 1")], source_id=source)
        second = await _submission(
            db_session,
            tenant=seed_tenant,
            student=student,
            root=seed_root_node,
            doc=seed_material_entry,
        )

        same = await _previous(db_session, second, source_id=source)
        other = (
            await _previous(db_session, second, source_id=uuid.uuid4())
            if differs_by == "list"
            else await _previous(
                db_session, second, source_id=source, layer=CriteriaLayer.AUTHOR
            )
        )

        assert set(same) == {"c1"}
        assert other == {}

    async def test_another_student_is_not_this_students_past(
        self,
        db_session: AsyncSession,
        seed_tenant: Tenant,
        seed_root_node: CourseNode,
        seed_material_entry: AuthoredDocument,
        first: HomeworkSubmission,
    ) -> None:
        source = uuid.uuid4()
        await _write(db_session, first, [_met("c1", "a = 1")], source_id=source)
        someone_else = await _submission(
            db_session,
            tenant=seed_tenant,
            student=await _student(db_session, seed_tenant),
            root=seed_root_node,
            doc=seed_material_entry,
        )

        assert await _previous(db_session, someone_else, source_id=source) == {}

    async def test_another_task_is_not_this_tasks_past(
        self,
        db_session: AsyncSession,
        seed_tenant: Tenant,
        student: Student,
        seed_root_node: CourseNode,
        first: HomeworkSubmission,
    ) -> None:
        source = uuid.uuid4()
        await _write(db_session, first, [_met("c1", "a = 1")], source_id=source)
        elsewhere = await _submission(
            db_session,
            tenant=seed_tenant,
            student=student,
            root=seed_root_node,
            doc=await _other_task(db_session, seed_root_node),
        )

        assert await _previous(db_session, elsewhere, source_id=source) == {}

    async def test_a_soft_deleted_submission_does_not_count(
        self,
        db_session: AsyncSession,
        seed_tenant: Tenant,
        student: Student,
        seed_root_node: CourseNode,
        seed_material_entry: AuthoredDocument,
        first: HomeworkSubmission,
    ) -> None:
        source = uuid.uuid4()
        await _write(db_session, first, [_met("c1", "a = 1")], source_id=source)
        second = await _submission(
            db_session,
            tenant=seed_tenant,
            student=student,
            root=seed_root_node,
            doc=seed_material_entry,
        )
        assert set(await _previous(db_session, second, source_id=source)) == {"c1"}

        first.deleted_at = datetime.now(UTC)
        await db_session.flush()

        assert await _previous(db_session, second, source_id=source) == {}


class TestEachTenantConditionHoldsOnItsOwn:
    """The safeguard's read is bounded by the tenant three times over: on the
    verdict rows, on the submission asked about, and on the earlier one.

    With consistent data any one of the three would do, and a mutation that
    drops one of them stays green on every other test here. The database does
    not tie a verdict's tenant to its submission's, nor a submission's tenant to
    its student's — the mismatches below can only come from a writer's bug, and
    each test builds one of them, so each condition is proven on its own
    (``impl-rules#9``: an exemption argued from the shape of today's data is an
    exemption by form, not by area).
    """

    async def test_a_verdict_written_under_another_tenant_is_not_read(
        self,
        db_session: AsyncSession,
        seed_tenant: Tenant,
        student: Student,
        seed_root_node: CourseNode,
        seed_material_entry: AuthoredDocument,
        first: HomeworkSubmission,
    ) -> None:
        stranger, _ = await _other_tenant(db_session)
        source = uuid.uuid4()
        # The writer's bug: this tenant's submission, the stranger's tenant id.
        await SubmissionVerdictRepository(db_session).replace_for_submission(
            tenant_id=stranger.id,
            submission_id=first.id,
            criteria_layer=CriteriaLayer.MODEL,
            criteria_source_id=source,
            records=[_met("c1", "a = 1")],
        )
        second = await _submission(
            db_session,
            tenant=seed_tenant,
            student=student,
            root=seed_root_node,
            doc=seed_material_entry,
        )

        assert await _previous(db_session, second, source_id=source) == {}

    async def test_a_submission_of_another_tenant_reads_no_past(
        self,
        db_session: AsyncSession,
        seed_tenant: Tenant,
        student: Student,
        seed_root_node: CourseNode,
        seed_material_entry: AuthoredDocument,
    ) -> None:
        stranger, stranger_root = await _other_tenant(db_session)
        source = uuid.uuid4()
        # The stranger's own submission and verdicts — but naming this
        # tenant's student and task, as only a writer's bug could.
        theirs = await _submission(
            db_session,
            tenant=stranger,
            student=student,
            root=stranger_root,
            doc=seed_material_entry,
        )
        await _write(db_session, theirs, [_met("c1", "a = 1")], source_id=source)
        ours = await _submission(
            db_session,
            tenant=seed_tenant,
            student=student,
            root=seed_root_node,
            doc=seed_material_entry,
        )

        assert (
            await _previous(db_session, ours, source_id=source, tenant_id=stranger.id)
            == {}
        )

    async def test_an_earlier_submission_of_another_tenant_is_not_this_past(
        self,
        db_session: AsyncSession,
        seed_tenant: Tenant,
        student: Student,
        seed_root_node: CourseNode,
        seed_material_entry: AuthoredDocument,
    ) -> None:
        stranger, stranger_root = await _other_tenant(db_session)
        source = uuid.uuid4()
        theirs = await _submission(
            db_session,
            tenant=stranger,
            student=student,
            root=stranger_root,
            doc=seed_material_entry,
        )
        # The writer's bug: the stranger's submission, this tenant's verdicts.
        await SubmissionVerdictRepository(db_session).replace_for_submission(
            tenant_id=seed_tenant.id,
            submission_id=theirs.id,
            criteria_layer=CriteriaLayer.MODEL,
            criteria_source_id=source,
            records=[_met("c1", "a = 1")],
        )
        ours = await _submission(
            db_session,
            tenant=seed_tenant,
            student=student,
            root=seed_root_node,
            doc=seed_material_entry,
        )

        assert await _previous(db_session, ours, source_id=source) == {}


class TestTheRowsGoWithTheirSubmission:
    async def test_a_hard_delete_of_the_submission_takes_its_rows(
        self, db_session: AsyncSession, first: HomeworkSubmission
    ) -> None:
        await _write(db_session, first, [_met("c1", "a = 1")], source_id=uuid.uuid4())
        await SubmissionVerdictRepository(db_session).store_explanation(
            tenant_id=first.tenant_id,
            submission_id=first.id,
            language="ukr",
            body={"why": "x"},
        )

        await db_session.execute(
            delete(HomeworkSubmission).where(HomeworkSubmission.id == first.id)
        )

        for model in (SubmissionCriterionVerdict, SubmissionExplanation):
            count = await db_session.scalar(
                select(func.count())
                .select_from(model)
                .where(model.submission_id == first.id)
            )
            assert count == 0, model.__tablename__


class TestTheExplanation:
    async def test_a_repeat_store_replaces_the_answer(
        self, db_session: AsyncSession, first: HomeworkSubmission
    ) -> None:
        repo = SubmissionVerdictRepository(db_session)
        await repo.store_explanation(
            tenant_id=first.tenant_id,
            submission_id=first.id,
            language="ukr",
            body={"why": "перше"},
        )
        await repo.store_explanation(
            tenant_id=first.tenant_id,
            submission_id=first.id,
            language="eng",
            body={"why": "second"},
        )

        stored = await repo.get_explanation(
            tenant_id=first.tenant_id, submission_id=first.id
        )
        count = await db_session.scalar(
            select(func.count())
            .select_from(SubmissionExplanation)
            .where(SubmissionExplanation.submission_id == first.id)
        )

        assert count == 1
        assert stored is not None
        assert (stored.language, stored.body) == ("eng", {"why": "second"})

    async def test_another_tenants_answer_is_never_overwritten(
        self, db_session: AsyncSession, first: HomeworkSubmission
    ) -> None:
        repo = SubmissionVerdictRepository(db_session)
        await repo.store_explanation(
            tenant_id=first.tenant_id,
            submission_id=first.id,
            language="ukr",
            body={"why": "своє"},
        )
        stranger, _ = await _other_tenant(db_session)

        with pytest.raises(NoResultFound):
            await repo.store_explanation(
                tenant_id=stranger.id,
                submission_id=first.id,
                language="eng",
                body={"why": "foreign"},
            )

        stored = await repo.get_explanation(
            tenant_id=first.tenant_id, submission_id=first.id
        )
        assert stored is not None
        assert (stored.language, stored.body) == ("ukr", {"why": "своє"})
