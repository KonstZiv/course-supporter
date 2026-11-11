"""DB integration tests for ``billing_admin.py costs`` (BIL-COSTS).

Seeds two tenants, three students, author processing jobs
(``document_processing``, ``node_summary_regeneration``), task preparation
calls (``key_explanation`` job + ``criteria_decomposition`` inside a
``homework_processing`` job), student submissions (including a continuation job
that overwrote ``HomeworkSubmission.job_id``), ``exact`` / ``estimated`` /
``unknown`` cost kinds, a non-paid ``funds_port`` trace row, and exact UTC
boundary timestamps.
"""

from __future__ import annotations

import asyncio
import csv
import io
import uuid
from collections.abc import Generator
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from scripts import billing_admin
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import InternalError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from course_supporter.billing import costs as billing_costs
from course_supporter.config import get_settings
from course_supporter.cost_kinds import CostKind, TariffWindow
from course_supporter.jobs.job_type import JOB_SUBJECT_TYPE, JobType
from course_supporter.storage.orm import (
    AuthoredDocument,
    BillingAccount,
    BillingAuditEntry,
    BillingMovement,
    CourseNode,
    ExternalServiceCall,
    HomeworkSubmission,
    Job,
    Student,
    Tenant,
)
from tests._helpers.course_node_factory import make_root_course_node

pytestmark = pytest.mark.requires_db


@dataclass(frozen=True)
class _SeededWorld:
    t1_id: uuid.UUID
    t1_name: str
    t2_id: uuid.UUID
    t2_name: str
    s1_id: uuid.UUID
    s2_id: uuid.UUID
    s3_id: uuid.UUID
    job_ids: tuple[uuid.UUID, ...]
    node_ids: tuple[uuid.UUID, ...]
    doc_ids: tuple[uuid.UUID, ...]
    submission_ids: tuple[uuid.UUID, ...]
    student_ids: tuple[uuid.UUID, ...]
    tenant_ids: tuple[uuid.UUID, ...]


def _make_esc(
    *,
    job_id: uuid.UUID,
    action: str,
    created_at: datetime,
    cost_kind: CostKind | None,
    cost_usd: Decimal | None,
) -> ExternalServiceCall:
    if cost_kind is None:
        return ExternalServiceCall(
            job_id=job_id,
            action=action,
            provider=None,
            model_id=None,
            success=None,
            cost_usd=None,
            cost_kind=None,
            ceiling_estimate_usd=Decimal("0.990000"),
            funds_decision="allowed",
            created_at=created_at,
        )
    return ExternalServiceCall(
        job_id=job_id,
        action=action,
        provider="gemini",
        model_id="gemini-2.5-flash",
        success=True,
        cost_usd=cost_usd,
        cost_kind=cost_kind.value,
        catalog_version="test-catalog" if cost_kind == CostKind.EXACT else None,
        tariff_window=TariffWindow.FLAT.value if cost_kind == CostKind.EXACT else None,
        call_started_at=created_at,
        created_at=created_at,
    )


async def _seed_world() -> _SeededWorld:
    suffix = uuid.uuid4().hex[:8]
    t1_name = f"Alpha Cost Academy-{suffix}"
    t2_name = f"Beta Cost School-{suffix}"

    engine = create_async_engine(get_settings().database_url)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            t1 = Tenant(name=t1_name)
            t2 = Tenant(name=t2_name)
            session.add_all([t1, t2])
            await session.flush()

            node1 = make_root_course_node(tenant_id=t1.id, title="Course 1", order=0)
            node2 = make_root_course_node(tenant_id=t2.id, title="Course 2", order=0)
            session.add_all([node1, node2])
            await session.flush()

            doc1 = AuthoredDocument(
                course_node_id=node1.id,
                course_root_id=node1.id,
                source_type="text",
                source_url="https://example.com/task1",
                filename="task1.txt",
            )
            doc2 = AuthoredDocument(
                course_node_id=node2.id,
                course_root_id=node2.id,
                source_type="text",
                source_url="https://example.com/task2",
                filename="task2.txt",
            )
            session.add_all([doc1, doc2])
            await session.flush()

            s1 = Student(
                tenant_id=t1.id,
                external_id=f"pii-ext-alice-{suffix}",
                display_name="Alice SecretName <alice@secret.example>",
            )
            s2 = Student(
                tenant_id=t1.id,
                external_id=f"pii-ext-bob-{suffix}",
                display_name="Bob PrivateUser <bob@secret.example>",
            )
            s3 = Student(
                tenant_id=t2.id,
                external_id=f"pii-ext-carol-{suffix}",
                display_name="Carol Confidential <carol@secret.example>",
            )
            session.add_all([s1, s2, s3])
            await session.flush()

            sub1a = HomeworkSubmission(
                tenant_id=t1.id,
                student_id=s1.id,
                course_node_id=node1.id,
                node_id=node1.id,
                authored_document_id=doc1.id,
                file_url="s3://bucket/s1a.py",
                file_type="text/plain",
                original_filename="s1a.py",
                status="delivered",
            )
            sub1b = HomeworkSubmission(
                tenant_id=t1.id,
                student_id=s1.id,
                course_node_id=node1.id,
                node_id=node1.id,
                authored_document_id=doc1.id,
                file_url="s3://bucket/s1b.py",
                file_type="text/plain",
                original_filename="s1b.py",
                status="delivered",
            )
            sub2 = HomeworkSubmission(
                tenant_id=t1.id,
                student_id=s2.id,
                course_node_id=node1.id,
                node_id=node1.id,
                authored_document_id=doc1.id,
                file_url="s3://bucket/s2.py",
                file_type="text/plain",
                original_filename="s2.py",
                status="delivered",
            )
            sub3 = HomeworkSubmission(
                tenant_id=t2.id,
                student_id=s3.id,
                course_node_id=node2.id,
                node_id=node2.id,
                authored_document_id=doc2.id,
                file_url="s3://bucket/s3.py",
                file_type="text/plain",
                original_filename="s3.py",
                status="delivered",
            )
            session.add_all([sub1a, sub1b, sub2, sub3])
            await session.flush()

            def _job(
                tenant_id: uuid.UUID,
                node_id: uuid.UUID,
                job_type: JobType,
                subject_id: uuid.UUID,
            ) -> Job:
                return Job(
                    tenant_id=tenant_id,
                    course_node_id=node_id,
                    job_type=job_type.value,
                    subject_type=JOB_SUBJECT_TYPE[job_type],
                    subject_id=subject_id,
                    status="complete",
                )

            j_before = _job(t1.id, node1.id, JobType.DOCUMENT_PROCESSING, doc1.id)
            j_doc1 = _job(t1.id, node1.id, JobType.DOCUMENT_PROCESSING, doc1.id)
            j_regen1 = _job(
                t1.id, node1.id, JobType.NODE_SUMMARY_REGENERATION, node1.id
            )
            j_key1 = _job(t1.id, node1.id, JobType.KEY_EXPLANATION, doc1.id)
            j_hw_s1a = _job(t1.id, node1.id, JobType.HOMEWORK_PROCESSING, sub1a.id)
            j_hw_s1a_cont = _job(t1.id, node1.id, JobType.HOMEWORK_PROCESSING, sub1a.id)
            j_hw_s1b = _job(t1.id, node1.id, JobType.HOMEWORK_PROCESSING, sub1b.id)
            j_hw_s2 = _job(t1.id, node1.id, JobType.HOMEWORK_PROCESSING, sub2.id)
            j_hw_s3 = _job(t2.id, node2.id, JobType.HOMEWORK_PROCESSING, sub3.id)
            j_after = _job(t2.id, node2.id, JobType.DOCUMENT_PROCESSING, doc2.id)

            all_jobs = [
                j_before,
                j_doc1,
                j_regen1,
                j_key1,
                j_hw_s1a,
                j_hw_s1a_cont,
                j_hw_s1b,
                j_hw_s2,
                j_hw_s3,
                j_after,
            ]
            session.add_all(all_jobs)
            await session.flush()

            # Simulate continuation overwriting HomeworkSubmission.job_id.
            sub1a.job_id = j_hw_s1a_cont.id
            sub1b.job_id = j_hw_s1b.id
            sub2.job_id = j_hw_s2.id
            sub3.job_id = j_hw_s3.id

            calls = [
                # Boundary: 1 microsecond before --from 2024-06-10 -> EXCLUDED
                _make_esc(
                    job_id=j_before.id,
                    action="ingest_pdf",
                    created_at=datetime(2024, 6, 9, 23, 59, 59, 999999, tzinfo=UTC),
                    cost_kind=CostKind.EXACT,
                    cost_usd=Decimal("9.999999"),
                ),
                # Boundary: exact --from start -> INCLUDED
                _make_esc(
                    job_id=j_doc1.id,
                    action="ingest_pdf",
                    created_at=datetime(2024, 6, 10, 0, 0, 0, 0, tzinfo=UTC),
                    cost_kind=CostKind.EXACT,
                    cost_usd=Decimal("0.010000"),
                ),
                # NODE_SUMMARY_REGENERATION -> author processing
                _make_esc(
                    job_id=j_regen1.id,
                    action="summarize_node",
                    created_at=datetime(2024, 6, 10, 12, 0, 0, tzinfo=UTC),
                    cost_kind=CostKind.ESTIMATED,
                    cost_usd=Decimal("0.020000"),
                ),
                # KEY_EXPLANATION -> author task_preparation
                _make_esc(
                    job_id=j_key1.id,
                    action="key_explanation",
                    created_at=datetime(2024, 6, 10, 15, 0, 0, tzinfo=UTC),
                    cost_kind=CostKind.EXACT,
                    cost_usd=Decimal("0.030000"),
                ),
                # criteria_decomposition inside homework_processing
                # belongs to author task_preparation only:
                _make_esc(
                    job_id=j_hw_s1a.id,
                    action="criteria_decomposition",
                    created_at=datetime(2024, 6, 11, 9, 0, 0, tzinfo=UTC),
                    cost_kind=CostKind.EXACT,
                    cost_usd=Decimal("0.040000"),
                ),
                # Homework review call on initial job -> Tenant 1 review + Student 1
                _make_esc(
                    job_id=j_hw_s1a.id,
                    action="homework_review",
                    created_at=datetime(2024, 6, 11, 9, 1, 0, tzinfo=UTC),
                    cost_kind=CostKind.EXACT,
                    cost_usd=Decimal("0.050000"),
                ),
                # Non-paid funds_port trace row -> IGNORED everywhere
                _make_esc(
                    job_id=j_hw_s1a.id,
                    action="funds_port",
                    created_at=datetime(2024, 6, 11, 9, 1, 30, tzinfo=UTC),
                    cost_kind=None,
                    cost_usd=None,
                ),
                # Continuation job for sub1a with unknown cost
                _make_esc(
                    job_id=j_hw_s1a_cont.id,
                    action="homework_evaluate",
                    created_at=datetime(2024, 6, 11, 9, 5, 0, tzinfo=UTC),
                    cost_kind=CostKind.UNKNOWN,
                    cost_usd=None,
                ),
                # Second submission for Student 1
                _make_esc(
                    job_id=j_hw_s1b.id,
                    action="homework_review",
                    created_at=datetime(2024, 6, 11, 10, 0, 0, tzinfo=UTC),
                    cost_kind=CostKind.ESTIMATED,
                    cost_usd=Decimal("0.060000"),
                ),
                # Student 2 under Tenant 1
                _make_esc(
                    job_id=j_hw_s2.id,
                    action="homework_review",
                    created_at=datetime(2024, 6, 12, 18, 0, 0, tzinfo=UTC),
                    cost_kind=CostKind.EXACT,
                    cost_usd=Decimal("0.005000"),
                ),
                # Boundary: end of --to date -> INCLUDED (Tenant 2, Student 3)
                _make_esc(
                    job_id=j_hw_s3.id,
                    action="homework_review",
                    created_at=datetime(2024, 6, 12, 23, 59, 59, 999999, tzinfo=UTC),
                    cost_kind=CostKind.ESTIMATED,
                    cost_usd=Decimal("0.125000"),
                ),
                # Boundary: midnight after --to date -> EXCLUDED
                _make_esc(
                    job_id=j_after.id,
                    action="ingest_pdf",
                    created_at=datetime(2024, 6, 13, 0, 0, 0, 0, tzinfo=UTC),
                    cost_kind=CostKind.EXACT,
                    cost_usd=Decimal("7.777777"),
                ),
            ]
            session.add_all(calls)
            await session.commit()

            return _SeededWorld(
                t1_id=t1.id,
                t1_name=t1_name,
                t2_id=t2.id,
                t2_name=t2_name,
                s1_id=s1.id,
                s2_id=s2.id,
                s3_id=s3.id,
                job_ids=tuple(j.id for j in all_jobs),
                node_ids=(node1.id, node2.id),
                doc_ids=(doc1.id, doc2.id),
                submission_ids=(sub1a.id, sub1b.id, sub2.id, sub3.id),
                student_ids=(s1.id, s2.id, s3.id),
                tenant_ids=(t1.id, t2.id),
            )
    finally:
        await engine.dispose()


async def _cleanup_world(world: _SeededWorld) -> None:
    engine = create_async_engine(get_settings().database_url)
    try:
        async with AsyncSession(engine) as session:
            # Break circular Job <-> HomeworkSubmission link before deletes.
            await session.execute(
                delete(ExternalServiceCall).where(
                    ExternalServiceCall.job_id.in_(world.job_ids)
                )
            )
            await session.execute(delete(Job).where(Job.id.in_(world.job_ids)))
            await session.execute(
                delete(HomeworkSubmission).where(
                    HomeworkSubmission.id.in_(world.submission_ids)
                )
            )
            await session.execute(
                delete(AuthoredDocument).where(AuthoredDocument.id.in_(world.doc_ids))
            )
            await session.execute(
                delete(CourseNode).where(CourseNode.id.in_(world.node_ids))
            )
            await session.execute(
                delete(Student).where(Student.id.in_(world.student_ids))
            )
            await session.execute(delete(Tenant).where(Tenant.id.in_(world.tenant_ids)))
            await session.commit()
    finally:
        await engine.dispose()


@pytest.fixture()
def seeded_world() -> Generator[_SeededWorld]:
    world = asyncio.run(_seed_world())
    try:
        yield world
    finally:
        asyncio.run(_cleanup_world(world))


def test_costs_table_two_tenants_three_students_boundaries_and_no_pii(
    seeded_world: _SeededWorld, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = billing_admin.main(["costs", "--from", "2024-06-10", "--to", "2024-06-12"])
    assert rc == 0
    out = capsys.readouterr().out

    # Tenant IDs, names, and totals.
    assert seeded_world.t1_name in out
    assert str(seeded_world.t1_id) in out
    assert seeded_world.t2_name in out
    assert str(seeded_world.t2_id) in out
    assert "$0.215000" in out
    assert "$0.125000" in out
    assert "$0.340000" in out  # authors разом

    # Out-of-bounds calls ($9.999999 before --from, $7.777777 at --to+1d) excluded.
    assert "9.999999" not in out
    assert "7.777777" not in out

    # Student IDs are present; student display_name / email / external_id NEVER appear.
    assert str(seeded_world.s1_id) in out
    assert str(seeded_world.s2_id) in out
    assert str(seeded_world.s3_id) in out
    assert "Alice" not in out
    assert "Bob" not in out
    assert "Carol" not in out
    assert "secret.example" not in out
    assert "pii-ext-" not in out

    # Explicit totals rows & separate unknown cost lines.
    assert out.count("разом") >= 2
    assert out.count("вартість невідома: 1 викликів") == 2


def test_costs_csv_reconciles_authors_and_students(
    seeded_world: _SeededWorld, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = billing_admin.main(
        ["costs", "--from", "2024-06-10", "--to", "2024-06-12", "--format", "csv"]
    )
    assert rc == 0
    out = capsys.readouterr().out

    rows = list(csv.DictReader(io.StringIO(out)))
    author_rows = {
        r["tenant_id"]: r
        for r in rows
        if r["section"] == "author" and r["row_kind"] == "row"
    }
    author_tot = next(
        r for r in rows if r["section"] == "author" and r["row_kind"] == "total"
    )
    student_rows = {
        r["student_id"]: r
        for r in rows
        if r["section"] == "student" and r["row_kind"] == "row"
    }
    student_tot = next(
        r for r in rows if r["section"] == "student" and r["row_kind"] == "total"
    )

    t1_row = author_rows[str(seeded_world.t1_id)]
    assert t1_row["calls"] == "8"
    assert t1_row["exact_calls"] == "5"
    assert Decimal(t1_row["exact_usd"]) == Decimal("0.135000")
    assert t1_row["estimated_calls"] == "2"
    assert Decimal(t1_row["estimated_usd"]) == Decimal("0.080000")
    assert t1_row["unknown_calls"] == "1"
    assert Decimal(t1_row["total_usd"]) == Decimal("0.215000")
    assert t1_row["processing_calls"] == "2"
    assert Decimal(t1_row["processing_usd"]) == Decimal("0.030000")
    assert t1_row["task_preparation_calls"] == "2"
    assert Decimal(t1_row["task_preparation_usd"]) == Decimal("0.070000")
    assert t1_row["review_calls"] == "4"
    assert Decimal(t1_row["review_usd"]) == Decimal("0.115000")

    t2_row = author_rows[str(seeded_world.t2_id)]
    assert t2_row["calls"] == "1"
    assert Decimal(t2_row["total_usd"]) == Decimal("0.125000")
    assert Decimal(t2_row["review_usd"]) == Decimal("0.125000")

    assert author_tot["tenant_name"] == "разом"
    assert author_tot["calls"] == "9"
    assert author_tot["unknown_calls"] == "1"
    assert author_tot["note"] == "вартість невідома: 1 викликів"
    assert Decimal(author_tot["total_usd"]) == Decimal("0.340000")

    # Student 1 had 2 submissions, 3 student-attributed calls
    # (criteria_decomposition excluded; unknown call included in calls).
    s1_row = student_rows[str(seeded_world.s1_id)]
    assert s1_row["submissions"] == "2"
    assert s1_row["calls"] == "3"
    assert s1_row["exact_calls"] == "1"
    assert Decimal(s1_row["exact_usd"]) == Decimal("0.050000")
    assert s1_row["estimated_calls"] == "1"
    assert Decimal(s1_row["estimated_usd"]) == Decimal("0.060000")
    assert s1_row["unknown_calls"] == "1"
    assert Decimal(s1_row["total_usd"]) == Decimal("0.110000")

    s2_row = student_rows[str(seeded_world.s2_id)]
    assert s2_row["submissions"] == "1"
    assert s2_row["calls"] == "1"
    assert Decimal(s2_row["total_usd"]) == Decimal("0.005000")

    s3_row = student_rows[str(seeded_world.s3_id)]
    assert s3_row["submissions"] == "1"
    assert s3_row["calls"] == "1"
    assert Decimal(s3_row["total_usd"]) == Decimal("0.125000")

    # Reconciliation: student total equals author review total.
    assert student_tot["tenant_name"] == "разом"
    assert student_tot["submissions"] == "4"
    assert student_tot["calls"] == author_tot["review_calls"]
    assert Decimal(student_tot["total_usd"]) == Decimal(author_tot["review_usd"])
    assert student_tot["unknown_calls"] == "1"
    assert student_tot["note"] == "вартість невідома: 1 викликів"


@pytest.mark.parametrize("use_name", [True, False])
def test_costs_tenant_filter_by_name_and_id(
    seeded_world: _SeededWorld, use_name: bool, capsys: pytest.CaptureFixture[str]
) -> None:
    tenant_arg = seeded_world.t2_name if use_name else str(seeded_world.t2_id)
    rc = billing_admin.main(
        [
            "costs",
            "--from",
            "2024-06-10",
            "--to",
            "2024-06-12",
            "--tenant",
            tenant_arg,
            "--format",
            "csv",
        ]
    )
    assert rc == 0
    rows = list(csv.DictReader(io.StringIO(capsys.readouterr().out)))
    data_rows = [r for r in rows if r["row_kind"] == "row"]
    assert {r["tenant_id"] for r in data_rows} == {str(seeded_world.t2_id)}
    assert {r["student_id"] for r in data_rows if r["section"] == "student"} == {
        str(seeded_world.s3_id)
    }


def test_costs_nonexistent_tenant_returns_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = billing_admin.main(
        ["costs", "--tenant", str(uuid.UUID("99999999-9999-9999-9999-999999999999"))]
    )
    assert rc == 1
    assert "not found" in capsys.readouterr().err


def test_costs_by_single_scope_only(
    seeded_world: _SeededWorld, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        billing_admin.main(
            [
                "costs",
                "--from",
                "2024-06-10",
                "--to",
                "2024-06-12",
                "--by",
                "author",
            ]
        )
        == 0
    )
    author_out = capsys.readouterr().out
    assert "По авторах:" in author_out
    assert "По студентах:" not in author_out

    assert (
        billing_admin.main(
            [
                "costs",
                "--from",
                "2024-06-10",
                "--to",
                "2024-06-12",
                "--by",
                "student",
            ]
        )
        == 0
    )
    student_out = capsys.readouterr().out
    assert "По студентах:" in student_out
    assert "По авторах:" not in student_out


def test_costs_is_strictly_read_only(
    seeded_world: _SeededWorld, capsys: pytest.CaptureFixture[str]
) -> None:
    async def _counts() -> tuple[int, int, int, int]:
        engine = create_async_engine(get_settings().database_url)
        try:
            async with AsyncSession(engine) as session:
                audits = int(
                    (
                        await session.execute(select(func.count(BillingAuditEntry.id)))
                    ).scalar_one()
                )
                accounts = int(
                    (
                        await session.execute(select(func.count(BillingAccount.id)))
                    ).scalar_one()
                )
                movements = int(
                    (
                        await session.execute(select(func.count(BillingMovement.id)))
                    ).scalar_one()
                )
                esc = int(
                    (
                        await session.execute(
                            select(func.count(ExternalServiceCall.id))
                        )
                    ).scalar_one()
                )
                return audits, accounts, movements, esc
        finally:
            await engine.dispose()

    before = asyncio.run(_counts())
    rc = billing_admin.main(["costs", "--from", "2024-06-10", "--to", "2024-06-12"])
    assert rc == 0
    _ = capsys.readouterr()
    after = asyncio.run(_counts())
    assert after == before

    # Verify costs_cmd actually put the Postgres transaction into READ ONLY mode.
    async def _verify_pg_read_only() -> None:
        engine = create_async_engine(get_settings().database_url)
        try:
            async with AsyncSession(engine) as session, session.begin():
                args = billing_admin.build_parser().parse_args(
                    ["costs", "--from", "2024-06-10", "--to", "2024-06-12"]
                )
                await billing_admin.costs_cmd(session, args)
                ro = (
                    await session.execute(text("SHOW transaction_read_only"))
                ).scalar_one()
                assert ro == "on"
                with pytest.raises(InternalError):
                    await session.execute(
                        text("INSERT INTO tenants (id, name) VALUES (:id, :name)"),
                        {"id": str(uuid.uuid4()), "name": "should-fail-ro"},
                    )
        finally:
            await engine.dispose()

    asyncio.run(_verify_pg_read_only())


def test_costs_empty_window_and_default_month_invocation(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc_empty = billing_admin.main(
        ["costs", "--from", "2020-01-01", "--to", "2020-01-02"]
    )
    assert rc_empty == 0
    empty_out = capsys.readouterr().out
    assert empty_out.count("разом") == 2
    assert empty_out.count("вартість невідома: 0 викликів") == 2

    rc_default = billing_admin.main(["costs"])
    assert rc_default == 0
    default_out = capsys.readouterr().out
    assert "Період (UTC):" in default_out
    assert "разом" in default_out


async def test_build_cost_report_falls_back_to_submission_tenant_when_job_tenant_null(
    db_session: AsyncSession,
) -> None:
    tenant = Tenant(name=f"NullJobTenant-{uuid.uuid4().hex[:8]}")
    db_session.add(tenant)
    await db_session.flush()

    node = make_root_course_node(tenant_id=tenant.id, title="Course N", order=0)
    db_session.add(node)
    await db_session.flush()

    doc = AuthoredDocument(
        course_node_id=node.id,
        course_root_id=node.id,
        source_type="text",
        source_url="https://example.com/null-job-tenant",
        filename="task.txt",
    )
    student = Student(tenant_id=tenant.id, external_id=f"s-{uuid.uuid4().hex[:6]}")
    db_session.add_all([doc, student])
    await db_session.flush()

    sub = HomeworkSubmission(
        tenant_id=tenant.id,
        student_id=student.id,
        course_node_id=node.id,
        node_id=node.id,
        authored_document_id=doc.id,
        file_url="s3://bucket/sub.py",
        file_type="text/plain",
        original_filename="sub.py",
        status="delivered",
    )
    db_session.add(sub)
    await db_session.flush()

    orphan_tenant_job = Job(
        tenant_id=None,
        course_node_id=node.id,
        job_type=JobType.HOMEWORK_PROCESSING.value,
        subject_type=JOB_SUBJECT_TYPE[JobType.HOMEWORK_PROCESSING],
        subject_id=sub.id,
        status="complete",
    )
    db_session.add(orphan_tenant_job)
    await db_session.flush()

    day = datetime(2024, 7, 15, 12, 0, tzinfo=UTC)
    db_session.add(
        _make_esc(
            job_id=orphan_tenant_job.id,
            action="homework_review",
            created_at=day,
            cost_kind=CostKind.EXACT,
            cost_usd=Decimal("0.042000"),
        )
    )
    await db_session.flush()

    report = await billing_costs.build_cost_report(
        db_session,
        from_date=day.date(),
        to_date=day.date(),
        tenant_id=tenant.id,
        by="both",
    )
    assert len(report.authors) == 1
    assert report.authors[0].tenant_id == tenant.id
    assert report.authors[0].spend.known_usd == Decimal("0.042000")
    assert report.authors[0].review.calls == 1
    assert len(report.students) == 1
    assert report.students[0].student_id == student.id
    assert report.students[0].spend.known_usd == Decimal("0.042000")
