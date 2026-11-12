"""The attempt ledger (mentor-rebuild task 13, K2), locked on a real database.

The locks of the commit, one class each:

* а. the migration — one head; down and up on the database; the new columns
  NULL on a row written before it, ``quote_from_base`` false on such a row;
* б. a first submission is attempt 1 with no previous; the next one after its
  review is attempt 2 with the first as previous — through the real body;
* в. a submission refused for safety, judged off-task or failed takes no number;
* г. a soft-deleted attempt is not a previous one;
* ґ. a new version of the task between attempts is in the receipt;
* д. the fingerprint is K1's for the same work, in UTF-8 and in CP1251 alike —
  through the real doors;
* е. a test (the new path) and a project (today's Mentor) get numbers too;
* є. the review of the same submission — structure and markdown — is the one
  written without the ledger, byte for byte.

The doubles are the ones the end-to-end walks of the two roads already use
(``test_text_review_e2e_db``, ``test_project_submission_worker``): no model is
called. Run with ``--run-db --run-redis``; the project walks use the real S3.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, delete, inspect, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from course_supporter.config import get_settings
from course_supporter.criteria_kinds import CriteriaLayer
from course_supporter.funds_port import (
    FundsAnswer,
    FundsRefusalReason,
    SubmissionContext,
    SubmissionOutcome,
    VersionWorkContext,
)
from course_supporter.homework.attempt_ledger import record_attempt
from course_supporter.homework.attempts import AttemptWork, attempt_snapshotter_for
from course_supporter.homework.criteria_form import criteria_to_document
from course_supporter.homework.criteria_list_service import CriteriaInForce
from course_supporter.homework.doors import DoorReading
from course_supporter.homework.path_config import ServedBy
from course_supporter.homework.path_runner import run_new_path_if_switched
from course_supporter.homework.project_submission import _submission_snapshot_key
from course_supporter.homework.test_doors import stored_answers
from course_supporter.storage.orm import (
    AuthoredDocument,
    CourseNode,
    ExternalServiceCall,
    HomeworkSubmission,
    Job,
    Student,
    Tenant,
)
from course_supporter.storage.s3 import S3Client
from course_supporter.storage.task_criteria_list_repository import (
    TaskCriteriaListRepository,
)
from tests.integration.test_project_submission_worker import (
    _cleanup,
    _project_zip,
    _RecordingReviewService,
    _RecordingSanityService,
    _run_full_worker,
    _s3_purge,
    _safety_mock,
    _seed,
)
from tests.integration.test_text_review_e2e_db import (
    _CRITERIA,
    _ListSource,
    _PortDouble,
    _RouterDouble,
    _run,
    _shipped,
    _submit,
    _webhook,
)

pytestmark = pytest.mark.requires_db

_REVISION = "task13_attempt_ledger"
_DOWN_REVISION = "bila_billing_accounts"
_LEDGER_COLUMNS = (
    "attempt_no",
    "previous_submission_id",
    "task_version",
    "fingerprint",
    "attempt_receipt",
)
_VERSION_A = "a" * 64
_VERSION_B = "b" * 64
_T0 = datetime(2026, 11, 1, 9, 0, tzinfo=UTC)

# Ukrainian code and comments (the K1 sample): long enough for the doors to
# recover a CP1251 file rather than refuse it.
_UKRAINIAN_WORK = (
    "# Розв'язок домашнього завдання: пошук дублікатів у списку.\n"
    "def find_duplicates(items):\n"
    '    """Повертає елементи, що трапляються більше одного разу."""\n'
    "    seen = set()\n"
    "    duplicates = []\n"
    "    for item in items:\n"
    "        if item in seen:\n"
    "            duplicates.append(item)  # вже бачили цей елемент\n"
    "        seen.add(item)\n"
    "    return duplicates\n"
)


# ── The world: a course, a task, a student ──────────────────────────────────


async def _make_world(
    session_factory: async_sessionmaker[AsyncSession], *, task_type: str
) -> dict[str, Any]:
    """A course in Ukrainian, a task of ``task_type`` at version A, a student.

    A ``task`` gets a ready criteria list, so the real body can review it.
    """
    async with session_factory() as session:
        tenant = Tenant(name=f"ledger-{uuid.uuid4().hex[:8]}")
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
            task_type=task_type,
            language="ukr",
            content_hash=_VERSION_A,
        )
        session.add(doc)
        await session.flush()
        in_force: CriteriaInForce | None = None
        if task_type == "task":
            lists = TaskCriteriaListRepository(session)
            claimed = await lists.claim(
                authored_document_id=doc.id,
                source_content_hash=_VERSION_A,
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
            in_force = CriteriaInForce(
                criteria=_CRITERIA, layer=CriteriaLayer.MODEL, source_id=claimed.id
            )
        student = Student(tenant_id=tenant.id, external_id=f"s-{uuid.uuid4().hex[:6]}")
        session.add(student)
        await session.commit()
        return {
            "tenant_id": tenant.id,
            "node_id": node.id,
            "doc_id": doc.id,
            "student_id": student.id,
            "in_force": in_force,
            "submissions": [],
        }


async def _drop_world(
    session_factory: async_sessionmaker[AsyncSession], world: dict[str, Any]
) -> None:
    async with session_factory() as session:
        jobs = select(Job.id).where(Job.tenant_id == world["tenant_id"])
        await session.execute(
            delete(ExternalServiceCall).where(ExternalServiceCall.job_id.in_(jobs))
        )
        await session.execute(
            delete(HomeworkSubmission).where(
                HomeworkSubmission.tenant_id == world["tenant_id"]
            )
        )
        await session.execute(delete(Job).where(Job.tenant_id == world["tenant_id"]))
        await session.execute(delete(Tenant).where(Tenant.id == world["tenant_id"]))
        await session.commit()


@pytest.fixture()
async def task_world(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[dict[str, Any]]:
    world = await _make_world(session_factory, task_type="task")
    yield world
    await _drop_world(session_factory, world)


@pytest.fixture()
async def quiz_world(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[dict[str, Any]]:
    world = await _make_world(session_factory, task_type="test")
    yield world
    await _drop_world(session_factory, world)


async def _add(
    session_factory: async_sessionmaker[AsyncSession],
    world: dict[str, Any],
    *,
    status: str,
    minute: int,
    deleted: bool = False,
    task_version: str | None = None,
    student_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """A submission of the world's student, ``minute`` minutes after ``_T0``."""
    at = _T0 + timedelta(minutes=minute)
    async with session_factory() as session:
        submission = HomeworkSubmission(
            tenant_id=world["tenant_id"],
            student_id=student_id or world["student_id"],
            course_node_id=world["node_id"],
            node_id=world["node_id"],
            authored_document_id=world["doc_id"],
            file_url="s3://bucket/homework/solution.py",
            file_type="text/plain",
            original_filename="solution.py",
            status=status,
            task_version=task_version,
            created_at=at,
            deleted_at=at if deleted else None,
        )
        session.add(submission)
        await session.commit()
        return submission.id


async def _record(
    session_factory: async_sessionmaker[AsyncSession],
    submission_id: uuid.UUID,
    work: str = "print('hello')\n",
) -> None:
    async with session_factory() as session:
        await record_attempt(
            session,
            submission_id,
            DoorReading(text=work),
            served_by=ServedBy.NEW_PATH,
            config=_shipped(),
        )
        await session.commit()


async def _row(
    session_factory: async_sessionmaker[AsyncSession], submission_id: uuid.UUID
) -> HomeworkSubmission:
    async with session_factory() as session:
        submission = await session.get(HomeworkSubmission, submission_id)
    assert submission is not None
    return submission


async def _set_status(
    session_factory: async_sessionmaker[AsyncSession],
    submission_id: uuid.UUID,
    status: str,
) -> None:
    """Stand in for the review of a submission whose doors were walked for real."""
    async with session_factory() as session:
        await session.execute(
            update(HomeworkSubmission)
            .where(HomeworkSubmission.id == submission_id)
            .values(status=status)
        )
        await session.commit()


# ── а. The migration ────────────────────────────────────────────────────────


class TestLockAMigration:
    """а. One head; down and up; the existing rows keep NULL and false."""

    def test_one_head_and_it_comes_after_billing(self) -> None:
        script = ScriptDirectory.from_config(Config("alembic.ini"))
        heads = script.get_heads()

        assert len(heads) == 1
        assert script.get_revision(_REVISION).down_revision == _DOWN_REVISION
        chain = {rev.revision for rev in script.walk_revisions("base", heads[0])}
        assert _REVISION in chain

    async def test_down_up_keeps_existing_rows_null_and_false(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        task_world: dict[str, Any],
    ) -> None:
        cfg = Config("alembic.ini")
        engine = create_engine(get_settings().database_url)
        submission_id, verdict_id = uuid.uuid4(), uuid.uuid4()
        try:
            command.downgrade(cfg, _DOWN_REVISION)
            with engine.begin() as conn:
                columns = {
                    c["name"] for c in inspect(conn).get_columns("homework_submissions")
                }
                assert not set(_LEDGER_COLUMNS) & columns
                verdict_columns = {
                    c["name"]
                    for c in inspect(conn).get_columns("submission_criterion_verdicts")
                }
                assert "quote_from_base" not in verdict_columns
                # A row of each table written before the migration.
                conn.execute(
                    text(
                        "INSERT INTO homework_submissions (id, tenant_id, student_id,"
                        " course_node_id, node_id, authored_document_id, file_url,"
                        " file_type, status) VALUES (:id, :tenant, :student, :node,"
                        " :node, :doc, 's3://b/k', 'text/plain', 'delivered')"
                    ),
                    {
                        "id": submission_id,
                        "tenant": task_world["tenant_id"],
                        "student": task_world["student_id"],
                        "node": task_world["node_id"],
                        "doc": task_world["doc_id"],
                    },
                )
                conn.execute(
                    text(
                        "INSERT INTO submission_criterion_verdicts (id, tenant_id,"
                        " submission_id, criteria_layer, criteria_source_id, item_id,"
                        " item_kind, weight, verdict, model_verdict, missing) VALUES"
                        " (:id, :tenant, :submission, 'model', :source, 'c1',"
                        " 'criterion', 'must', 'not_met', 'not_met', 'nothing')"
                    ),
                    {
                        "id": verdict_id,
                        "tenant": task_world["tenant_id"],
                        "submission": submission_id,
                        "source": uuid.uuid4(),
                    },
                )
        finally:
            command.upgrade(cfg, "head")
        with engine.connect() as conn:
            ledger = conn.execute(
                text(
                    "SELECT attempt_no, previous_submission_id, task_version,"
                    " fingerprint, attempt_receipt FROM homework_submissions"
                    " WHERE id = :id"
                ),
                {"id": submission_id},
            ).one()
            quote_from_base = conn.scalar(
                text(
                    "SELECT quote_from_base FROM submission_criterion_verdicts"
                    " WHERE id = :id"
                ),
                {"id": verdict_id},
            )
            index = conn.scalar(
                text(
                    "SELECT indexdef FROM pg_indexes WHERE indexname ="
                    " 'ix_homework_submissions_attempt_fingerprint'"
                )
            )
        engine.dispose()

        assert tuple(ledger) == (None,) * len(_LEDGER_COLUMNS)
        assert quote_from_base is False
        assert index is not None
        assert "(student_id, authored_document_id, fingerprint)" in index
        assert "WHERE (fingerprint IS NOT NULL)" in index

    async def test_a_new_verdict_row_is_not_from_the_base(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        task_world: dict[str, Any],
    ) -> None:
        """The column's default holds for a row nobody sets it on."""
        submission_id = await _add(
            session_factory, task_world, status="delivered", minute=0
        )
        async with session_factory() as session:
            await session.execute(
                text(
                    "INSERT INTO submission_criterion_verdicts (id, tenant_id,"
                    " submission_id, criteria_layer, criteria_source_id, item_id,"
                    " item_kind, weight, verdict, model_verdict, missing) VALUES"
                    " (:id, :tenant, :submission, 'model', :source, 'c1',"
                    " 'criterion', 'must', 'not_met', 'not_met', 'nothing')"
                ),
                {
                    "id": uuid.uuid4(),
                    "tenant": task_world["tenant_id"],
                    "submission": submission_id,
                    "source": uuid.uuid4(),
                },
            )
            value = await session.scalar(
                text(
                    "SELECT quote_from_base FROM submission_criterion_verdicts"
                    " WHERE submission_id = :submission"
                ),
                {"submission": submission_id},
            )
            await session.rollback()

        assert value is False


# ── б–ґ. The rules, on the rows ──────────────────────────────────────────────


class TestLockBNumbering:
    """б. Attempt 1 with no previous; after its review, attempt 2 after it."""

    async def test_first_and_second(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        task_world: dict[str, Any],
    ) -> None:
        first = await _add(session_factory, task_world, status="received", minute=0)
        await _record(session_factory, first)
        await _set_status(session_factory, first, "delivered")
        second = await _add(session_factory, task_world, status="received", minute=1)
        await _record(session_factory, second)

        one, two = (
            await _row(session_factory, first),
            await _row(session_factory, second),
        )
        assert (one.attempt_no, one.previous_submission_id) == (1, None)
        assert (two.attempt_no, two.previous_submission_id) == (2, first)
        assert two.attempt_receipt == {
            "attempt_no": 2,
            "previous_submission_id": str(first),
            "task_version": _VERSION_A,
            "task_version_changed": False,
            "fingerprint": two.fingerprint,
            # The shipped file serves ``task`` on the new path, in test mode.
            "path": "new_path",
            "test_mode": True,
        }

    async def test_a_completed_review_counts_as_delivered_does(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        task_world: dict[str, Any],
    ) -> None:
        """``completed`` is the in-app terminal: a review the student reads."""
        first = await _add(session_factory, task_world, status="completed", minute=0)
        second = await _add(session_factory, task_world, status="received", minute=1)
        await _record(session_factory, second)

        two = await _row(session_factory, second)
        assert (two.attempt_no, two.previous_submission_id) == (2, first)

    async def test_the_previous_is_the_latest_and_another_student_is_not_counted(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        task_world: dict[str, Any],
    ) -> None:
        async with session_factory() as session:
            other = Student(
                tenant_id=task_world["tenant_id"], external_id="someone-else"
            )
            session.add(other)
            await session.commit()
        await _add(session_factory, task_world, status="delivered", minute=0)
        latest = await _add(session_factory, task_world, status="delivered", minute=5)
        await _add(
            session_factory,
            task_world,
            status="delivered",
            minute=6,
            student_id=other.id,
        )
        current = await _add(session_factory, task_world, status="received", minute=7)
        await _record(session_factory, current)

        row = await _row(session_factory, current)
        assert (row.attempt_no, row.previous_submission_id) == (3, latest)

    async def test_written_once(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        task_world: dict[str, Any],
    ) -> None:
        """Doors opened again (a continuation) keep the first record."""
        current = await _add(session_factory, task_world, status="received", minute=0)
        await _record(session_factory, current, "first reading\n")
        before = await _row(session_factory, current)
        await _add(session_factory, task_world, status="delivered", minute=1)
        await _record(session_factory, current, "second reading\n")

        after = await _row(session_factory, current)
        assert after.attempt_no == before.attempt_no == 1
        assert after.fingerprint == before.fingerprint
        assert after.attempt_receipt == before.attempt_receipt


class TestLockVRefusalsTakeNoNumber:
    """в. Refused, off-task and failed submissions are not attempts."""

    @pytest.mark.parametrize("not_an_attempt", ["rejected", "mismatch", "failed"])
    async def test_the_next_real_one_gets_the_same_number(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        task_world: dict[str, Any],
        not_an_attempt: str,
    ) -> None:
        reviewed = await _add(session_factory, task_world, status="delivered", minute=0)
        refused = await _add(session_factory, task_world, status="received", minute=1)
        await _record(session_factory, refused)
        await _set_status(session_factory, refused, not_an_attempt)
        current = await _add(session_factory, task_world, status="received", minute=2)
        await _record(session_factory, current)

        # Without the refused one the next would be attempt 2 after the first.
        row = await _row(session_factory, current)
        assert (row.attempt_no, row.previous_submission_id) == (2, reviewed)
        assert (await _row(session_factory, refused)).attempt_no == 2

    async def test_held_and_in_flight_are_not_attempts(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        task_world: dict[str, Any],
    ) -> None:
        for minute, status in enumerate(
            ("awaiting_funds", "awaiting_criteria", "safety_ok", "reviewing")
        ):
            await _add(session_factory, task_world, status=status, minute=minute)
        current = await _add(session_factory, task_world, status="received", minute=9)
        await _record(session_factory, current)

        row = await _row(session_factory, current)
        assert (row.attempt_no, row.previous_submission_id) == (1, None)


class TestLockGDeletedIsNotPrevious:
    """г. A soft-deleted attempt is neither counted nor previous."""

    async def test_the_only_attempt_deleted(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        task_world: dict[str, Any],
    ) -> None:
        await _add(
            session_factory, task_world, status="delivered", minute=0, deleted=True
        )
        current = await _add(session_factory, task_world, status="received", minute=1)
        await _record(session_factory, current)

        row = await _row(session_factory, current)
        assert (row.attempt_no, row.previous_submission_id) == (1, None)

    async def test_the_latest_deleted_the_earlier_stands(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        task_world: dict[str, Any],
    ) -> None:
        live = await _add(session_factory, task_world, status="delivered", minute=0)
        await _add(
            session_factory, task_world, status="delivered", minute=1, deleted=True
        )
        current = await _add(session_factory, task_world, status="received", minute=2)
        await _record(session_factory, current)

        row = await _row(session_factory, current)
        assert (row.attempt_no, row.previous_submission_id) == (2, live)


class TestLockGgTaskVersion:
    """ґ. A new version of the task between attempts is in the receipt."""

    async def test_changed_between_attempts(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        task_world: dict[str, Any],
    ) -> None:
        first = await _add(session_factory, task_world, status="received", minute=0)
        await _record(session_factory, first)
        await _set_status(session_factory, first, "delivered")
        async with session_factory() as session:
            await session.execute(
                update(AuthoredDocument)
                .where(AuthoredDocument.id == task_world["doc_id"])
                .values(content_hash=_VERSION_B)
            )
            await session.commit()
        second = await _add(session_factory, task_world, status="received", minute=1)
        await _record(session_factory, second)

        one, two = (
            await _row(session_factory, first),
            await _row(session_factory, second),
        )
        assert one.task_version == _VERSION_A
        assert one.attempt_receipt is not None
        assert one.attempt_receipt["task_version_changed"] is False
        assert two.task_version == _VERSION_B
        assert two.attempt_receipt is not None
        assert two.attempt_receipt["task_version"] == _VERSION_B
        assert two.attempt_receipt["task_version_changed"] is True

    async def test_same_version_is_not_changed(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        task_world: dict[str, Any],
    ) -> None:
        await _add(
            session_factory,
            task_world,
            status="delivered",
            minute=0,
            task_version=_VERSION_A,
        )
        current = await _add(session_factory, task_world, status="received", minute=1)
        await _record(session_factory, current)

        receipt = (await _row(session_factory, current)).attempt_receipt
        assert receipt is not None
        assert receipt["task_version_changed"] is False

    async def test_an_attempt_from_before_the_ledger_is_not_a_change(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        task_world: dict[str, Any],
    ) -> None:
        """Its version was never recorded: unknown is not different."""
        await _add(session_factory, task_world, status="delivered", minute=0)
        current = await _add(session_factory, task_world, status="received", minute=1)
        await _record(session_factory, current)

        receipt = (await _row(session_factory, current)).attempt_receipt
        assert receipt is not None
        assert receipt["attempt_no"] == 2
        assert receipt["task_version_changed"] is False


# ── д, е. Through the real doors of the new path ────────────────────────────


class _RefusingPort(_PortDouble):
    """Refuses at the first question: the body stops right after the doors."""

    async def check_and_reserve(
        self, context: SubmissionContext, ceiling_estimate_usd: float
    ) -> FundsAnswer:
        return FundsAnswer.refused(FundsRefusalReason.INSUFFICIENT_FUNDS)

    async def account_stage_cost(
        self, context: SubmissionContext, stage_cost_usd: float
    ) -> None:
        raise AssertionError("nothing is paid for after a refusal")

    async def release_remainder(
        self, context: SubmissionContext, outcome: SubmissionOutcome
    ) -> None:
        raise AssertionError("nothing is released after a refusal")

    async def check_and_reserve_for_version(
        self, context: VersionWorkContext, ceiling_estimate_usd: float
    ) -> FundsAnswer:
        raise AssertionError("the submission path must not use the version operations")


async def _through_the_doors(
    session_factory: async_sessionmaker[AsyncSession],
    world: dict[str, Any],
    raw: bytes,
    tmp_path: Path,
) -> uuid.UUID:
    """A submission of ``raw`` through the new path's doors, held for funds."""
    submission_id, job_id = await _submit(session_factory, world)
    download = tmp_path / f"{submission_id}.py"

    def _download(_key: str) -> Path:
        download.write_bytes(raw)
        return download

    s3 = MagicMock()
    s3.extract_key = MagicMock(return_value="homework/solution.py")
    s3.download_file = AsyncMock(side_effect=_download)
    ctx = {
        "session_factory": session_factory,
        "stage_router": MagicMock(),
        "s3_client": s3,
        "job_try": 1,
    }
    with patch(
        "course_supporter.homework.path_runner.get_path_config",
        return_value=_shipped(),
    ):
        assert await run_new_path_if_switched(
            ctx, job_id, submission_id, funds_port=_RefusingPort()
        )
    assert (await _row(session_factory, submission_id)).status == "awaiting_funds"
    return submission_id


class TestLockDFingerprintIsK1s:
    """д. The ledger's fingerprint is K1's for the same work, in any encoding."""

    async def test_utf8_and_cp1251(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        task_world: dict[str, Any],
        tmp_path: Path,
    ) -> None:
        expected = (
            attempt_snapshotter_for("task")
            .snapshot(AttemptWork(reading=DoorReading(text=_UKRAINIAN_WORK)))
            .fingerprint
        )
        in_utf8 = await _through_the_doors(
            session_factory, task_world, _UKRAINIAN_WORK.encode("utf-8"), tmp_path
        )
        in_cp1251 = await _through_the_doors(
            session_factory,
            task_world,
            _UKRAINIAN_WORK.replace("\n", "\r\n").encode("cp1251"),
            tmp_path,
        )

        utf8, cp1251 = (
            await _row(session_factory, in_utf8),
            await _row(session_factory, in_cp1251),
        )
        assert utf8.fingerprint == cp1251.fingerprint == expected
        assert utf8.attempt_receipt is not None
        assert utf8.attempt_receipt["fingerprint"] == expected
        # Neither was reviewed: both are the student's first attempt.
        assert utf8.attempt_no == cp1251.attempt_no == 1


async def _receipt_types(
    session_factory: async_sessionmaker[AsyncSession], submission_id: uuid.UUID
) -> tuple[str, str]:
    """The JSON types of ``path`` and ``test_mode`` as Postgres stores them."""
    async with session_factory() as session:
        row = (
            await session.execute(
                text(
                    "SELECT jsonb_typeof(attempt_receipt -> 'path'),"
                    " jsonb_typeof(attempt_receipt -> 'test_mode')"
                    " FROM homework_submissions WHERE id = :id"
                ),
                {"id": submission_id},
            )
        ).one()
    return row[0], row[1]


class TestLockGgPathAndTestModeInTheReceipt:
    """ґ (evaluator version). Which road served the submission, and test mode.

    The daily activity report reads ``path`` and ``test_mode`` from the
    receipt: the ``served_by`` of the road, and a JSON boolean — never the
    string ``"true"``. Today's Mentor: ``TestLockEProjectOnTodaysMentor``.
    """

    async def test_a_task_on_the_new_path_in_test_mode(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        task_world: dict[str, Any],
        tmp_path: Path,
    ) -> None:
        submission_id = await _through_the_doors(
            session_factory, task_world, _UKRAINIAN_WORK.encode("utf-8"), tmp_path
        )

        receipt = (await _row(session_factory, submission_id)).attempt_receipt
        assert receipt is not None
        assert receipt["path"] == "new_path"
        assert receipt["test_mode"] is True
        assert await _receipt_types(session_factory, submission_id) == (
            "string",
            "boolean",
        )

    async def test_a_test_on_the_new_path_is_charged(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        quiz_world: dict[str, Any],
        tmp_path: Path,
    ) -> None:
        answers = stored_answers(uuid.uuid4(), {"1": ["а"]}).encode("utf-8")
        submission_id = await _through_the_doors(
            session_factory, quiz_world, answers, tmp_path
        )

        receipt = (await _row(session_factory, submission_id)).attempt_receipt
        assert receipt is not None
        assert receipt["path"] == "new_path"
        assert receipt["test_mode"] is False
        assert await _receipt_types(session_factory, submission_id) == (
            "string",
            "boolean",
        )


class TestLockETestAndProject:
    """е. A test (new path) and a project (today's Mentor) are numbered too."""

    async def test_a_test_through_the_doors(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        quiz_world: dict[str, Any],
        tmp_path: Path,
    ) -> None:
        version_1, version_2 = uuid.uuid4(), uuid.uuid4()
        first_answers = stored_answers(version_1, {"1": ["а"], "2": ["б", "в"]})
        first = await _through_the_doors(
            session_factory, quiz_world, first_answers.encode("utf-8"), tmp_path
        )
        await _set_status(session_factory, first, "delivered")
        second = await _through_the_doors(
            session_factory,
            quiz_world,
            stored_answers(version_2, {"1": ["а"], "2": ["б", "в"]}).encode("utf-8"),
            tmp_path,
        )

        one, two = (
            await _row(session_factory, first),
            await _row(session_factory, second),
        )
        assert (one.attempt_no, one.previous_submission_id) == (1, None)
        assert one.task_version == str(version_1)
        assert one.fingerprint == (
            attempt_snapshotter_for("test")
            .snapshot(AttemptWork(reading=DoorReading(text=first_answers)))
            .fingerprint
        )
        assert (two.attempt_no, two.previous_submission_id) == (2, first)
        assert two.task_version == str(version_2)
        assert two.attempt_receipt is not None
        assert two.attempt_receipt["task_version_changed"] is True


@pytest.fixture()
async def s3_client() -> AsyncGenerator[S3Client]:
    s = get_settings()
    client = S3Client(
        endpoint_url=s.s3_endpoint,
        access_key=s.s3_access_key,
        secret_key=s.s3_secret_key.get_secret_value(),
        bucket=s.s3_bucket,
    )
    await client.open()
    try:
        await client.ensure_bucket()
        yield client
    finally:
        await client.close()


async def _another_project_submission(
    session_factory: async_sessionmaker[AsyncSession],
    ids: dict[str, Any],
    file_url: str,
) -> dict[str, Any]:
    """The same student's next submission at the same project, with its job."""
    async with session_factory() as session:
        first = await session.get(HomeworkSubmission, ids["submission_id"])
        assert first is not None
        submission = HomeworkSubmission(
            tenant_id=ids["tenant_id"],
            student_id=first.student_id,
            course_node_id=ids["node_id"],
            node_id=ids["node_id"],
            authored_document_id=ids["doc_id"],
            file_url=file_url,
            file_type="application/zip",
            original_filename="proj.zip",
            status="received",
        )
        session.add(submission)
        await session.flush()
        job = Job(
            tenant_id=ids["tenant_id"],
            course_node_id=ids["node_id"],
            job_type="homework_processing",
            input_params={"submission_id": str(submission.id)},
            status="queued",
        )
        session.add(job)
        await session.commit()
        return {**ids, "submission_id": submission.id, "job_id": job.id}


def _project_url(raw_key: str) -> str:
    s = get_settings()
    return f"{s.s3_endpoint}/{s.s3_bucket}/{raw_key}"


@pytest.mark.requires_redis
class TestLockEProjectOnTodaysMentor:
    """е. A project, reviewed by today's Mentor, is numbered; є on its stages."""

    async def test_two_attempts_and_the_stages_read_what_they_read(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        s3_client: S3Client,
    ) -> None:
        keys = [f"homework/it-k2/{uuid.uuid4().hex}/proj.zip" for _ in range(3)]
        first_zip = _project_zip({"app/main.py": b"def solve():\n    return 42\n"})
        second_zip = _project_zip({"app/main.py": b"def solve():\n    return 43\n"})
        ids = await _seed(session_factory, file_url=_project_url(keys[0]))
        # The same first work by another student, with the ledger left out: what
        # today's Mentor reviewed before K2.
        before_k2 = await _seed(session_factory, file_url=_project_url(keys[2]))
        try:
            stages = [
                (_safety_mock(), _RecordingSanityService(), _RecordingReviewService())
                for _ in range(3)
            ]
            await _run_full_worker(
                session_factory,
                s3_client,
                ids,
                keys[0],
                first_zip,
                safety=stages[0][0],
                sanity=stages[0][1],
                review=stages[0][2],
            )
            second = await _another_project_submission(
                session_factory, ids, _project_url(keys[1])
            )
            await _run_full_worker(
                session_factory,
                s3_client,
                second,
                keys[1],
                second_zip,
                safety=stages[1][0],
                sanity=stages[1][1],
                review=stages[1][2],
            )
            with patch(
                "course_supporter.homework.attempt_ledger.record_attempt",
                new=AsyncMock(return_value=None),
            ):
                await _run_full_worker(
                    session_factory,
                    s3_client,
                    before_k2,
                    keys[2],
                    first_zip,
                    safety=stages[2][0],
                    sanity=stages[2][1],
                    review=stages[2][2],
                )

            one = await _row(session_factory, ids["submission_id"])
            two = await _row(session_factory, second["submission_id"])
            unrecorded = await _row(session_factory, before_k2["submission_id"])
            assert one.status == two.status == "completed"
            assert (one.attempt_no, one.previous_submission_id) == (1, None)
            assert (two.attempt_no, two.previous_submission_id) == (2, one.id)
            # ґ (evaluator version): served by today's Mentor, charged as usual,
            # and no evaluator named — today's Mentor writes no version.
            for reviewed in (one, two):
                assert reviewed.attempt_receipt is not None
                assert reviewed.attempt_receipt["path"] == "todays_mentor"
                assert reviewed.attempt_receipt["test_mode"] is False
                assert "evaluator_version" not in reviewed.attempt_receipt
                assert await _receipt_types(session_factory, reviewed.id) == (
                    "string",
                    "boolean",
                )
            # The project's fingerprint IS the snapshot the normalizer stored.
            assert one.fingerprint == one.snapshot_hash is not None
            assert two.fingerprint == two.snapshot_hash != one.snapshot_hash
            assert unrecorded.attempt_no is None

            # є on today's Mentor: the ledger changes nothing the stages read.
            def _seen(index: int) -> tuple[str, list[str], list[str]]:
                safety, sanity, review = stages[index]
                assert safety.await_args is not None
                return (
                    safety.await_args.kwargs["submission_text"],
                    sanity.seen,
                    review.seen,
                )

            assert _seen(0) == _seen(2)
            assert (one.review_result, one.review_markdown, one.score) == (
                unrecorded.review_result,
                unrecorded.review_markdown,
                unrecorded.score,
            )
        finally:
            for key in keys:
                await _s3_purge(s3_client, key, _submission_snapshot_key(key))
            await _cleanup(session_factory, ids)
            await _cleanup(session_factory, before_k2)


# ── б, є. The text review through the real body ─────────────────────────────


class TestLockYeReviewUnchanged:
    """є. The same submission's review is the one written without the ledger."""

    async def test_structure_and_markdown_byte_for_byte(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        task_world: dict[str, Any],
        tmp_path: Path,
    ) -> None:
        source = _ListSource(task_world["in_force"])
        async with session_factory() as session:
            before_k2_student = Student(
                tenant_id=task_world["tenant_id"], external_id="before-k2"
            )
            session.add(before_k2_student)
            await session.commit()

        # The same work by two students: one with the ledger left out — the
        # review before K2 — and one with it.
        before, before_job = await _submit(
            session_factory, task_world, student_id=before_k2_student.id
        )
        with patch(
            "course_supporter.homework.path_runner.record_attempt",
            new=AsyncMock(return_value=None),
        ):
            await _run(
                session_factory,
                _RouterDouble(session_factory),
                source,
                _webhook(),
                tmp_path,
                submission_id=before,
                job_id=before_job,
            )
        after, after_job = await _submit(session_factory, task_world)
        await _run(
            session_factory,
            _RouterDouble(session_factory),
            source,
            _webhook(),
            tmp_path,
            submission_id=after,
            job_id=after_job,
        )

        unrecorded = await _row(session_factory, before)
        recorded = await _row(session_factory, after)
        assert unrecorded.status == recorded.status == "delivered"
        assert unrecorded.attempt_no is None
        assert recorded.attempt_no == 1
        assert recorded.review_result == unrecorded.review_result
        assert recorded.review_markdown == unrecorded.review_markdown
        assert recorded.score == unrecorded.score

    async def test_b_the_second_after_the_first_review(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        task_world: dict[str, Any],
        tmp_path: Path,
    ) -> None:
        """б through the body: the first review delivered, the next is attempt 2."""
        source = _ListSource(task_world["in_force"])
        walked = []
        for _ in range(2):
            submission_id, job_id = await _submit(session_factory, task_world)
            await _run(
                session_factory,
                _RouterDouble(session_factory),
                source,
                _webhook(),
                tmp_path,
                submission_id=submission_id,
                job_id=job_id,
            )
            walked.append(await _row(session_factory, submission_id))

        first, second = walked
        assert first.status == second.status == "delivered"
        assert (first.attempt_no, first.previous_submission_id) == (1, None)
        assert (second.attempt_no, second.previous_submission_id) == (2, first.id)
        # The same work twice: one fingerprint, the dedup's key (K3).
        assert first.fingerprint == second.fingerprint is not None
