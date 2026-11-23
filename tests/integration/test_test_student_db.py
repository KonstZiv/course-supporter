"""The test-student mark on a live database (BIL-5, DD-BIL-CAL).

``students.is_test_student``: the migration only adds it (``false`` for every
student already there); a new student is not a test one; the administrator's
``test-student`` command sets, clears and lists it; the calibration report
leaves a test student's review out unless asked.

Requires ``docker compose up -d`` (PostgreSQL), migrated to head; run with
``--run-db``.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from scripts import billing_admin
from scripts.cost_estimate_report import _load
from sqlalchemy import create_engine, delete, inspect, select, text
from sqlalchemy.engine import Connection
from sqlalchemy.engine.interfaces import ReflectedColumn
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session
from structlog.testing import capture_logs

from course_supporter.config import get_settings
from course_supporter.storage.homework_repository import HomeworkRepository
from course_supporter.storage.orm import (
    ActionCostEstimate,
    AuthoredDocument,
    CourseNode,
    Student,
    Tenant,
)
from course_supporter.storage.student_repository import StudentRepository

pytestmark = pytest.mark.requires_db

REVISION = "biltest_student_flag"
BELOW = "m3_interface_language"


# ── The migration ───────────────────────────────────────────────────────────


def test_one_head_and_the_revision_follows_the_interface_language() -> None:
    script = ScriptDirectory.from_config(Config("alembic.ini"))
    revision = script.get_revision(REVISION)
    assert script.get_heads() == [REVISION]
    assert revision is not None
    assert revision.down_revision == BELOW


def _column(conn: Connection) -> ReflectedColumn | None:
    columns = inspect(conn).get_columns("students")
    return next((c for c in columns if c["name"] == "is_test_student"), None)


def test_upgrade_over_students_already_there_then_down_and_up() -> None:
    cfg = Config("alembic.ini")
    engine = create_engine(get_settings().database_url)
    tenant_id = uuid.uuid4()
    try:
        command.downgrade(cfg, BELOW)
        with engine.begin() as conn:
            assert _column(conn) is None
            conn.execute(
                text("INSERT INTO tenants (id, name, is_active) VALUES (:t, :n, true)"),
                {"t": tenant_id, "n": f"bil5-mig-{tenant_id.hex[:8]}"},
            )
            for n in range(2):
                conn.execute(
                    text(
                        "INSERT INTO students (id, tenant_id, external_id) "
                        "VALUES (:s, :t, :e)"
                    ),
                    {"s": uuid.uuid4(), "t": tenant_id, "e": f"before-{n}"},
                )
        command.upgrade(cfg, REVISION)
        with engine.connect() as conn:
            column = _column(conn)
            assert column is not None
            assert column["nullable"] is False
            assert column["default"] == "false"
            marks = conn.scalars(
                text("SELECT is_test_student FROM students WHERE tenant_id = :t"),
                {"t": tenant_id},
            ).all()
            assert marks == [False, False]

        command.downgrade(cfg, BELOW)
        with engine.connect() as conn:
            assert _column(conn) is None
    finally:
        command.upgrade(cfg, "head")
        with engine.begin() as conn:
            conn.execute(
                text("DELETE FROM students WHERE tenant_id = :t"), {"t": tenant_id}
            )
            conn.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": tenant_id})
    with engine.connect() as conn:
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == REVISION
        assert _column(conn) is not None
    engine.dispose()


# ── The ORM ─────────────────────────────────────────────────────────────────


async def test_a_new_student_is_not_a_test_one(
    db_session: AsyncSession, seed_tenant: Tenant
) -> None:
    student = Student(tenant_id=seed_tenant.id, external_id="plain")
    db_session.add(student)
    await db_session.flush()
    await db_session.refresh(student)
    assert student.is_test_student is False


# ── The administrator's command ─────────────────────────────────────────────


@pytest.fixture()
def student() -> Iterator[Student]:
    engine = create_engine(get_settings().database_url)
    with Session(engine, expire_on_commit=False) as session:
        tenant = Tenant(name=f"bil5-script-{uuid.uuid4().hex[:8]}")
        session.add(tenant)
        session.flush()
        row = Student(tenant_id=tenant.id, external_id="control")
        session.add(row)
        session.commit()

    yield row

    with Session(engine) as session:
        session.execute(delete(Student).where(Student.tenant_id == row.tenant_id))
        session.execute(delete(Tenant).where(Tenant.id == row.tenant_id))
        session.commit()
    engine.dispose()


def _mark(student_id: uuid.UUID) -> bool:
    engine = create_engine(get_settings().database_url)
    with Session(engine) as session:
        mark = session.scalar(
            select(Student.is_test_student).where(Student.id == student_id)
        )
    engine.dispose()
    assert mark is not None
    return mark


class TestCommand:
    def test_mark_then_unmark_print_before_and_after_and_log_the_actor(
        self, student: Student, capsys: pytest.CaptureFixture[str]
    ) -> None:
        argv = ["test-student", "--student", str(student.id), "--actor", "kost"]
        with capture_logs() as logs:
            assert billing_admin.main([*argv, "--mark"]) == 0
        assert "is_test_student False → True" in capsys.readouterr().out
        assert _mark(student.id) is True
        [entry] = [e for e in logs if e["event"] == "student_test_mark_set"]
        assert entry == {
            "event": "student_test_mark_set",
            "log_level": "info",
            "actor": "kost",
            "student_id": str(student.id),
            "before": False,
            "after": True,
        }

        with capture_logs() as logs:
            assert billing_admin.main([*argv, "--unmark"]) == 0
        assert "is_test_student True → False" in capsys.readouterr().out
        assert _mark(student.id) is False
        assert [e["after"] for e in logs if e["event"] == "student_test_mark_set"] == [
            False
        ]

    def test_the_same_mark_again_changes_nothing(
        self, student: Student, capsys: pytest.CaptureFixture[str]
    ) -> None:
        argv = ["test-student", "--student", str(student.id), "--actor", "kost"]
        assert billing_admin.main([*argv, "--mark"]) == 0
        capsys.readouterr()
        with capture_logs() as logs:
            assert billing_admin.main([*argv, "--mark"]) == 0
        assert capsys.readouterr().out.startswith("unchanged (already so)")
        assert logs == []
        assert _mark(student.id) is True

    def test_an_unknown_or_deleted_student_is_refused(
        self, student: Student, capsys: pytest.CaptureFixture[str]
    ) -> None:
        unknown = ["test-student", "--student", str(uuid.uuid4()), "--mark"]
        assert billing_admin.main([*unknown, "--actor", "kost"]) == 1
        assert "not found" in capsys.readouterr().err

        engine = create_engine(get_settings().database_url)
        with Session(engine) as session:
            row = session.get(Student, student.id)
            assert row is not None
            row.deleted_at = datetime.now(UTC)
            session.commit()
        engine.dispose()
        deleted = ["test-student", "--student", str(student.id), "--mark"]
        assert billing_admin.main([*deleted, "--actor", "kost"]) == 1
        assert "not found" in capsys.readouterr().err
        assert _mark(student.id) is False

    def test_a_change_needs_a_student_and_an_actor(self, student: Student) -> None:
        for argv in (
            ["test-student", "--student", str(student.id), "--mark"],
            ["test-student", "--mark", "--actor", "kost"],
            ["test-student", "--student", str(student.id), "--actor", "kost"],
            ["test-student", "--list", "--mark"],
        ):
            with pytest.raises(SystemExit) as exc:
                billing_admin.main(argv)
            assert exc.value.code == 2
        assert _mark(student.id) is False

    def test_list_prints_the_marked_ids_and_writes_nothing(
        self, student: Student, capsys: pytest.CaptureFixture[str]
    ) -> None:
        argv = ["test-student", "--student", str(student.id), "--mark"]
        assert billing_admin.main([*argv, "--actor", "kost"]) == 0
        capsys.readouterr()
        with capture_logs() as logs:
            assert billing_admin.main(["test-student", "--list"]) == 0
        out = capsys.readouterr().out
        assert f"student {student.id}  tenant {student.tenant_id}\n" in out
        assert "control" not in out  # ids only, no name
        assert out.splitlines()[-1].startswith("total: ")
        assert logs == []
        assert _mark(student.id) is True

    async def test_list_runs_in_a_read_only_transaction(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        args = billing_admin.build_parser().parse_args(["test-student", "--list"])
        async with session_factory() as session:
            await billing_admin.test_student(session, args)
            with pytest.raises(DBAPIError, match="read-only transaction"):
                await session.execute(
                    text("UPDATE students SET is_test_student = false WHERE false")
                )


# ── The calibration report ──────────────────────────────────────────────────


async def test_the_report_leaves_a_test_students_review_out_unless_asked(
    db_session: AsyncSession,
    seed_tenant: Tenant,
    seed_root_node: CourseNode,
    seed_material_entry: AuthoredDocument,
) -> None:
    _, before = await _load(db_session, None, None)

    students = StudentRepository(db_session)
    tester = await students.create(tenant_id=seed_tenant.id, external_id="control")
    pupil = await students.create(tenant_id=seed_tenant.id, external_id="pupil")
    tester.is_test_student = True
    submissions = HomeworkRepository(db_session)
    keys: dict[str, str] = {}
    for name, who in (("test", tester), ("plain", pupil)):
        submission = await submissions.create(
            tenant_id=seed_tenant.id,
            student_id=who.id,
            course_node_id=seed_root_node.id,
            node_id=seed_root_node.id,
            authored_document_id=seed_material_entry.id,
            file_url="s3://bucket/solution.py",
            file_type="text/plain",
            original_filename="solution.py",
        )
        keys[name] = str(submission.id)
    keys["processing"] = str(uuid.uuid4())
    for kind, key in (
        ("review", keys["test"]),
        ("review", keys["plain"]),
        ("processing", keys["processing"]),
    ):
        db_session.add(
            ActionCostEstimate(
                action_kind=kind,
                action_key=key,
                tenant_id=seed_tenant.id,
                e_usd=Decimal("0.01"),
                margin=Decimal("0.2"),
                breakdown=[],
                inputs={},
                uncalibrated=True,
            )
        )
    await db_session.flush()

    rows, sample = await _load(db_session, None, None)
    reported = {r.action_key for r in rows} & set(keys.values())
    assert reported == {keys["plain"], keys["processing"]}
    assert sample.excluded is True
    assert sample.test_reviews == before.test_reviews + 1
    assert sample.non_test_students == before.non_test_students + 1

    rows, sample = await _load(db_session, None, None, include_test_students=True)
    reported = {r.action_key for r in rows} & set(keys.values())
    assert reported == set(keys.values())
    assert sample.excluded is False
    assert sample.non_test_students == before.non_test_students + 1
