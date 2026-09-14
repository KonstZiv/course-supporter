"""Schema-acceptance + FK enforcement for ExternalServiceCall (KD5).

Mirrors the catalog-level pattern from ``test_job_redesign_db.py``
(0.3) — `inspect()` + `pg_constraint` queries verify the migration's
shape stuck. FK enforcement tests verify the runtime behavior (NULL
rejected, NO ACTION blocks Job DELETE).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator, Generator
from typing import Any

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from course_supporter.call_outcome import CallOutcome, SkipReason
from course_supporter.config import get_settings
from course_supporter.llm.finish_reason import FinishReason
from course_supporter.service_logging import _persist, job_scope
from course_supporter.storage.orm import (
    ExternalServiceCall,
    Job,
    Tenant,
)
from tests._helpers.course_node_factory import make_root_course_node

pytestmark = pytest.mark.requires_db


@pytest.fixture()
def sync_engine() -> Generator[Engine]:
    """Sync engine for catalog inspection."""
    engine = create_engine(get_settings().database_url)
    yield engine
    engine.dispose()


class TestSchemaShape:
    """Migration ``f3a4b5c6d7e8`` shipped the KD5 schema."""

    def test_tenant_id_column_dropped(self, sync_engine: Engine) -> None:
        col_names = {
            c["name"]
            for c in inspect(sync_engine).get_columns("external_service_calls")
        }
        assert "tenant_id" not in col_names

    def test_job_id_column_is_not_null(self, sync_engine: Engine) -> None:
        cols = {
            c["name"]: c
            for c in inspect(sync_engine).get_columns("external_service_calls")
        }
        assert "job_id" in cols
        assert cols["job_id"]["nullable"] is False

    def test_unit_out_reasoning_column_nullable(self, sync_engine: Engine) -> None:
        """Migration ``esc_reasoning_tokens`` added the nullable column (P5/P6)."""
        cols = {
            c["name"]: c
            for c in inspect(sync_engine).get_columns("external_service_calls")
        }
        assert "unit_out_reasoning" in cols
        assert cols["unit_out_reasoning"]["nullable"] is True

    def test_tenant_id_index_dropped(self, sync_engine: Engine) -> None:
        with sync_engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT indexname FROM pg_indexes "
                    "WHERE tablename = 'external_service_calls'"
                )
            ).all()
        names = {r[0] for r in rows}
        assert "ix_external_service_calls_tenant_id" not in names

    def test_fk_ondelete_is_no_action(self, sync_engine: Engine) -> None:
        """FK ``external_service_calls_job_id_fkey`` has ``ON DELETE NO ACTION``."""
        with sync_engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT confdeltype FROM pg_constraint "
                    "WHERE conname = 'external_service_calls_job_id_fkey'"
                )
            ).first()
        assert row is not None, "FK constraint not found"
        # PostgreSQL confdeltype: 'a' = NO ACTION, 'r' = RESTRICT,
        # 'c' = CASCADE, 'n' = SET NULL, 'd' = SET DEFAULT.
        assert row[0] == "a"


class TestForeignKeyEnforcement:
    """Runtime FK behavior."""

    async def test_null_job_id_rejected(self, db_session: AsyncSession) -> None:
        """INSERT with NULL job_id violates the NOT NULL constraint."""
        esc = ExternalServiceCall(
            job_id=None,  # type: ignore[arg-type]
            provider="anthropic",
            model_id="claude-sonnet-4",
        )
        db_session.add(esc)
        with pytest.raises(IntegrityError):
            await db_session.flush()

    async def test_no_action_blocks_job_hard_delete(
        self, db_session: AsyncSession
    ) -> None:
        """DELETE Job with attached ESC raises IntegrityError (NO ACTION)."""
        tenant = Tenant(name=f"esc-{uuid.uuid4().hex[:6]}")
        db_session.add(tenant)
        await db_session.flush()
        node = make_root_course_node(tenant_id=tenant.id, title="course", order=0)
        db_session.add(node)
        await db_session.flush()
        job = Job(
            tenant_id=tenant.id, course_node_id=node.id, job_type="document_processing"
        )
        db_session.add(job)
        await db_session.flush()
        esc = ExternalServiceCall(
            job_id=job.id,
            provider="anthropic",
            model_id="claude-sonnet-4",
            cost_usd=1.0,
        )
        db_session.add(esc)
        await db_session.flush()

        # Raw DELETE triggers the FK check inside ``execute`` — not at
        # flush time as ORM-tracked changes do.
        with pytest.raises(IntegrityError):
            await db_session.execute(Job.__table__.delete().where(Job.id == job.id))

    async def test_valid_job_id_accepted(self, db_session: AsyncSession) -> None:
        """ESC with a real Job FK persists cleanly."""
        tenant = Tenant(name=f"esc-{uuid.uuid4().hex[:6]}")
        db_session.add(tenant)
        await db_session.flush()
        job = Job(tenant_id=tenant.id, job_type="document_processing")
        db_session.add(job)
        await db_session.flush()
        esc = ExternalServiceCall(
            job_id=job.id,
            provider="anthropic",
            model_id="claude-sonnet-4",
            cost_usd=2.5,
        )
        db_session.add(esc)
        await db_session.flush()
        await db_session.refresh(esc)
        assert esc.cost_usd == pytest.approx(2.5)


# ── mentor-rebuild 01: migration ``esc_call_outcome`` ─────────────────

_REGISTER_COLUMNS = (
    "outcome",
    "finish_reason",
    "skip_reason",
    "prompt_hash",
    "input_hash",
    "input_text",
    "output_text",
    "authenticity",
    "completeness",
)


class TestCallOutcomeSchemaShape:
    """The register gained its columns; rows without a call are admissible."""

    def test_new_columns_exist_and_are_nullable(self, sync_engine: Engine) -> None:
        cols = {
            c["name"]: c
            for c in inspect(sync_engine).get_columns("external_service_calls")
        }
        for name in _REGISTER_COLUMNS:
            assert name in cols, name
            assert cols[name]["nullable"] is True, name

    @pytest.mark.parametrize("column", ["provider", "model_id", "success"])
    def test_call_columns_admit_no_call(self, sync_engine: Engine, column: str) -> None:
        cols = {
            c["name"]: c
            for c in inspect(sync_engine).get_columns("external_service_calls")
        }
        assert cols[column]["nullable"] is True

    def test_vocabulary_check_constraints_exist(self, sync_engine: Engine) -> None:
        names = {
            c["name"]
            for c in inspect(sync_engine).get_check_constraints(
                "external_service_calls"
            )
        }
        assert {"ck_esc_outcome", "ck_esc_finish_reason", "ck_esc_skip_reason"} <= names


async def _job_id(db_session: AsyncSession) -> uuid.UUID:
    tenant = Tenant(name=f"esc-{uuid.uuid4().hex[:6]}")
    db_session.add(tenant)
    await db_session.flush()
    job = Job(tenant_id=tenant.id, job_type="document_processing")
    db_session.add(job)
    await db_session.flush()
    return job.id


class TestCallOutcomeVocabulary:
    """The database admits exactly the enum values — the migration's literal
    CHECK lists and the Python enums cannot drift apart unnoticed."""

    @pytest.mark.parametrize(
        ("column", "value"),
        [("outcome", m.value) for m in CallOutcome]
        + [("finish_reason", m.value) for m in FinishReason]
        + [("skip_reason", m.value) for m in SkipReason],
    )
    async def test_every_enum_member_is_accepted(
        self, db_session: AsyncSession, column: str, value: str
    ) -> None:
        esc = ExternalServiceCall(job_id=await _job_id(db_session), **{column: value})
        db_session.add(esc)
        await db_session.flush()

    @pytest.mark.parametrize("column", ["outcome", "finish_reason", "skip_reason"])
    async def test_unlisted_value_is_refused(
        self, db_session: AsyncSession, column: str
    ) -> None:
        esc = ExternalServiceCall(
            job_id=await _job_id(db_session), **{column: "not_a_value"}
        )
        db_session.add(esc)
        with pytest.raises(IntegrityError, match=f"ck_esc_{column}"):
            await db_session.flush()


@pytest.fixture()
async def committed_register_job(
    session_factory: async_sessionmaker[AsyncSession],
    committed_seeds: dict[str, uuid.UUID],
) -> AsyncGenerator[uuid.UUID]:
    """A committed Job for rows written through the real ``_persist``."""
    async with session_factory() as session:
        job = Job(
            tenant_id=committed_seeds["tenant_id"],
            course_node_id=committed_seeds["course_node_id"],
            job_type="document_processing",
        )
        session.add(job)
        await session.commit()
        job_id = job.id

    yield job_id

    async with session_factory() as session:
        await session.execute(
            ExternalServiceCall.__table__.delete().where(
                ExternalServiceCall.job_id == job_id
            )
        )
        await session.execute(Job.__table__.delete().where(Job.id == job_id))
        await session.commit()


class TestCanonicalFailureMeasure:
    """``NOT success OR error_message IS NOT NULL`` counts failed calls only.

    Rows that record no call — the per-review metrics row and the ladder
    traces of a skipped or abandoned rung — carry ``success = NULL`` and no
    ``error_message``, so the expression evaluates to NULL for them and the
    ``WHERE`` leaves them out. That is the intended behaviour, not an
    accident: the controls below prove the same query does count the failed
    calls written next to them.
    """

    async def test_rows_without_a_call_stay_out_of_the_failure_count(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        committed_register_job: uuid.UUID,
    ) -> None:
        rows: dict[str, dict[str, Any]] = {
            "metrics": {
                "action": "review_metrics",
                "provider": None,
                "model_id": None,
                "success": None,
                "authenticity": 0.5,
                "completeness": 1.0,
            },
            "skipped": {
                "provider": "anthropic",
                "model_id": "claude-x",
                "success": None,
                "outcome": CallOutcome.SKIPPED,
                "skip_reason": SkipReason.INPUT_BUDGET_EXCEEDED,
            },
            "abandoned": {
                "provider": "anthropic",
                "model_id": "claude-x",
                "success": None,
                "outcome": CallOutcome.ABANDONED,
            },
            # Controls: a clean call and two failed calls.
            "success": {
                "provider": "anthropic",
                "model_id": "claude-x",
                "success": True,
                "outcome": CallOutcome.SUCCESS,
            },
            "transport_error": {
                "provider": "anthropic",
                "model_id": "claude-x",
                "success": False,
                "error_message": "503",
                "outcome": CallOutcome.TRANSPORT_ERROR,
            },
            "invalid_content": {
                "provider": "anthropic",
                "model_id": "claude-x",
                "success": True,
                "error_message": "schema mismatch",
                "outcome": CallOutcome.INVALID_CONTENT,
            },
        }
        with job_scope(committed_register_job):
            for label, fields in rows.items():
                await _persist(
                    session_factory,
                    **{"action": label, "strategy": "default", **fields},
                )

        async with session_factory() as session:
            written = (
                await session.execute(
                    text(
                        "SELECT count(*) FROM external_service_calls "
                        "WHERE job_id = :job"
                    ),
                    {"job": committed_register_job},
                )
            ).scalar_one()
            failed = (
                await session.execute(
                    text(
                        "SELECT action FROM external_service_calls "
                        "WHERE job_id = :job "
                        "AND (NOT success OR error_message IS NOT NULL)"
                    ),
                    {"job": committed_register_job},
                )
            ).scalars()
            failed_actions = set(failed)
            # Guard against a false green: had the ORM default turned an
            # explicit None into success = true, the rows would drop out of
            # the count for the wrong reason.
            no_call = (
                await session.execute(
                    text(
                        "SELECT action FROM external_service_calls "
                        "WHERE job_id = :job AND success IS NULL"
                    ),
                    {"job": committed_register_job},
                )
            ).scalars()
            no_call_actions = set(no_call)

        assert written == len(rows)
        assert no_call_actions == {"review_metrics", "skipped", "abandoned"}
        assert failed_actions == {"transport_error", "invalid_content"}

    async def test_abandoned_rungs_are_counted_by_field_not_by_text(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        committed_register_job: uuid.UUID,
    ) -> None:
        """DD-SP-AC: abandoned rungs come from ``outcome``, no text parsing."""
        with job_scope(committed_register_job):
            for provider in ("deepseek_thinking", "dashscope"):
                await _persist(
                    session_factory,
                    action="criteria_decomposition",
                    strategy="default",
                    provider=provider,
                    model_id="m",
                    success=None,
                    outcome=CallOutcome.ABANDONED,
                )
            await _persist(
                session_factory,
                action="criteria_decomposition",
                strategy="default",
                provider="deepseek",
                model_id="m",
                success=True,
                outcome=CallOutcome.SUCCESS,
            )

        async with session_factory() as session:
            abandoned = (
                await session.execute(
                    text(
                        "SELECT count(*) FROM external_service_calls "
                        "WHERE job_id = :job AND outcome = 'abandoned'"
                    ),
                    {"job": committed_register_job},
                )
            ).scalar_one()
        assert abandoned == 2
