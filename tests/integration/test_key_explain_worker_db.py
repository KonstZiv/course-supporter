"""The generation job, end to end below the model (task 06, block G2).

Everything here is the real thing except the model: the ARQ task runs through
the L2 seam, the Job row is real, the version and the author's layer are real
rows, the register is the real table, and the funds port is either the shipped
implementation or a double that refuses. The ROUTER is the one double, because
the alternative is a paid call — and it writes the register row a real call
would have written, so the cost the body reads back is a real sum over real
rows.

Four of the five tests are about NOT spending: a key that moved, a port that
refuses, a model that answers badly. The fifth is the one that costs money, and
it checks that what the port is told afterwards equals what the register says.

Requires ``docker compose up -d``; run with ``--run-db``.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from course_supporter.agents.key_explainer import STAGE_NAME
from course_supporter.call_outcome import FundsDecision
from course_supporter.funds_port import (
    FUNDS_PORT_ACTION,
    FundsAnswer,
    FundsRefusalReason,
    SubmissionContext,
    SubmissionOutcome,
    VersionWorkContext,
)
from course_supporter.homework.reference_key import answers_digest
from course_supporter.jobs import JOB_SUBJECT_TYPE, JobType
from course_supporter.llm.error_categories import LadderExhaustedError
from course_supporter.reference_kinds import ReferenceKind, ReferenceState
from course_supporter.service_logging import get_current_job_id
from course_supporter.storage.orm import (
    AuthoredDocument,
    DocumentSegment,
    DocumentSummary,
    ExternalServiceCall,
    Job,
    Tenant,
)
from course_supporter.storage.task_reference_repository import TaskReferenceRepository
from course_supporter.workers.key_explain import arq_explain_key
from tests._helpers.course_node_factory import make_root_course_node

pytestmark = pytest.mark.requires_db

_KEY: dict[str, list[str]] = {"1": ["б"], "2": ["в"]}
_TEXT = "1. Перше?\nа) так\nб) ні\n\n2. Друге?\nв) так\nг) ні"
_HASH = "e" * 64
_GOOD = '{"explanations": {"1": "Бо так, і ось чому.", "2": "Бо саме так."}}'
_BAD = '{"explanations": {"1": "Лише перше."}}'
_COST = 0.0231


class _RouterDouble:
    """Runs the validator over canned content and writes the register row.

    The row is what makes the cost real: the body sums the register for its own
    job, so a double that only returned a string would let every cost assertion
    pass over an empty table.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        content: str,
        cost_usd: float = _COST,
    ) -> None:
        self._session_factory = session_factory
        self._content = content
        self._cost = cost_usd
        self.calls = 0
        self.raise_instead: Exception | None = None

    async def execute_for_stage(
        self,
        stage_name: str,
        *,
        response_validator: Any = None,
        expects_json: bool = False,
        **render_context: Any,
    ) -> Any:
        del expects_json, render_context
        self.calls += 1
        from course_supporter.service_logging import _persist

        await _persist(
            self._session_factory,
            action=stage_name,
            strategy="default",
            provider="deepseek_thinking",
            model_id="deepseek-v4-pro",
            success=True,
            cost_usd=self._cost,
            unit_type="tokens",
            unit_in=1200,
            unit_out=400,
        )
        if self.raise_instead is not None:
            raise self.raise_instead
        if response_validator is not None:
            response_validator(self._content)
        return None


class _RefusingPort:
    """A port that refuses the work; the submission operations must not be used."""

    def __init__(self) -> None:
        self.accounted: list[float] = []

    async def check_and_reserve(
        self, context: SubmissionContext, ceiling_estimate_usd: float
    ) -> FundsAnswer:
        raise AssertionError("a generation is not a submission")

    async def account_stage_cost(
        self, context: SubmissionContext, stage_cost_usd: float
    ) -> None:
        raise AssertionError("a generation is not a submission")

    async def release_remainder(
        self, context: SubmissionContext, outcome: SubmissionOutcome
    ) -> None:
        raise AssertionError("a generation is not a submission")

    async def check_and_reserve_for_version(
        self, context: VersionWorkContext, ceiling_estimate_usd: float
    ) -> FundsAnswer:
        return FundsAnswer.refused(FundsRefusalReason.INSUFFICIENT_FUNDS)

    async def account_version_work_cost(
        self, context: VersionWorkContext, actual_usd: float
    ) -> None:
        self.accounted.append(actual_usd)


class _CountingPort(_RefusingPort):
    """Allows, and records what it was told the work actually cost."""

    async def check_and_reserve_for_version(
        self, context: VersionWorkContext, ceiling_estimate_usd: float
    ) -> FundsAnswer:
        self.estimate = ceiling_estimate_usd
        return FundsAnswer.allowed()


@pytest.fixture()
async def seeded(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[dict[str, uuid.UUID]]:
    """A committed test task with a key, a pending version and its Job."""
    async with session_factory() as session:
        tenant = Tenant(name=f"key-tenant-{uuid.uuid4().hex[:8]}")
        session.add(tenant)
        await session.flush()
        node = make_root_course_node(tenant_id=tenant.id, title="Key course", order=0)
        session.add(node)
        await session.flush()
        document = AuthoredDocument(
            course_node_id=node.id,
            course_root_id=node.id,
            source_type="text",
            source_url="file:///tmp/key-test.md",
            task_type="test",
            language="ukr",
            content_hash=_HASH,
        )
        session.add(document)
        await session.flush()
        summary = DocumentSummary(
            authored_document_id=document.id,
            course_root_id=node.id,
            title="Тест",
            status="ready",
        )
        session.add(summary)
        await session.flush()
        session.add(
            DocumentSegment(
                document_summary_id=summary.id,
                course_root_id=node.id,
                order=0,
                content=_TEXT,
                description="the test",
                start_pos=0,
                end_pos=len(_TEXT),
            )
        )
        repo = TaskReferenceRepository(session)
        await repo.replace_override(
            authored_document_id=document.id,
            kind=ReferenceKind.TEST_KEY,
            answers=_KEY,
            source_content_hash=_HASH,
        )
        version, _ = await repo.create_version(
            authored_document_id=document.id,
            kind=ReferenceKind.TEST_KEY,
            source_content_hash=_HASH,
            source_task_type="test",
            answers_hash=answers_digest(_KEY),
            language="ukr",
        )
        job = Job(
            tenant_id=tenant.id,
            course_node_id=node.id,
            job_type=JobType.KEY_EXPLANATION.value,
            subject_type=JOB_SUBJECT_TYPE[JobType.KEY_EXPLANATION],
            subject_id=document.id,
        )
        session.add(job)
        await session.commit()
        ids = {
            "tenant_id": tenant.id,
            "document_id": document.id,
            "version_id": version.id,
            "job_id": job.id,
        }

    yield ids

    async with session_factory() as session:
        await session.execute(
            ExternalServiceCall.__table__.delete().where(
                ExternalServiceCall.job_id == ids["job_id"]
            )
        )
        await session.execute(Job.__table__.delete().where(Job.id == ids["job_id"]))
        await session.execute(
            Tenant.__table__.delete().where(Tenant.id == ids["tenant_id"])
        )
        await session.commit()


def _ctx(
    session_factory: async_sessionmaker[AsyncSession],
    router: Any,
    port: Any,
) -> dict[str, Any]:
    return {
        "session_factory": session_factory,
        "stage_router": router,
        "funds_port": port,
        "job_try": 1,
    }


async def _version_state(
    session_factory: async_sessionmaker[AsyncSession], version_id: uuid.UUID
) -> tuple[str, str | None, dict[str, str] | None]:
    async with session_factory() as session:
        version = await TaskReferenceRepository(session).get_by_id(version_id)
        assert version is not None
        return version.state, version.failure_reason, version.explanations


async def _job_status(
    session_factory: async_sessionmaker[AsyncSession], job_id: uuid.UUID
) -> str:
    """The seam's verdict on the job, read from the row it wrote."""
    async with session_factory() as session:
        job = await session.get(Job, job_id)
        assert job is not None
        return str(job.status)


async def _stage_rows(
    session_factory: async_sessionmaker[AsyncSession], job_id: uuid.UUID
) -> list[ExternalServiceCall]:
    async with session_factory() as session:
        result = await session.execute(
            select(ExternalServiceCall).where(
                ExternalServiceCall.job_id == job_id,
                ExternalServiceCall.action == STAGE_NAME,
            )
        )
        return list(result.scalars())


class TestTheWorkThatSucceeds:
    async def test_it_writes_the_explanations_and_pays_once(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        seeded: dict[str, uuid.UUID],
    ) -> None:
        """Ready, one register row on this job, and the port told the real sum."""
        router = _RouterDouble(session_factory, content=_GOOD)
        port = _CountingPort()

        await arq_explain_key(
            _ctx(session_factory, router, port),
            str(seeded["job_id"]),
            str(seeded["version_id"]),
        )

        state, reason, explanations = await _version_state(
            session_factory, seeded["version_id"]
        )
        assert state == ReferenceState.READY.value
        assert reason is None
        assert explanations is not None and set(explanations) == {"1", "2"}

        rows = await _stage_rows(session_factory, seeded["job_id"])
        assert len(rows) == 1, "exactly one call of the stage, on this job"
        assert rows[0].job_id == seeded["job_id"]

        assert port.accounted == [pytest.approx(_COST)], (
            "what the port is told must be the register's sum, not the estimate"
        )
        assert port.estimate > _COST, "the estimate is a ceiling, not the price"

    async def test_the_accounted_sum_equals_the_register(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        seeded: dict[str, uuid.UUID],
    ) -> None:
        """Measured against the table, not against the constant the double used."""
        router = _RouterDouble(session_factory, content=_GOOD, cost_usd=0.0177)
        port = _CountingPort()

        await arq_explain_key(
            _ctx(session_factory, router, port),
            str(seeded["job_id"]),
            str(seeded["version_id"]),
        )

        async with session_factory() as session:
            registered = await session.scalar(
                select(
                    func.coalesce(func.sum(ExternalServiceCall.cost_usd), 0.0)
                ).where(
                    ExternalServiceCall.job_id == seeded["job_id"],
                    ExternalServiceCall.action == STAGE_NAME,
                )
            )
        assert port.accounted and port.accounted[0] > 0, (
            "the work paid, so its sum is above zero; two zeros agree over no rows"
        )
        assert port.accounted == [pytest.approx(registered)]


class TestTheWorkThatSpendsNothing:
    async def test_a_key_replaced_before_the_run_fails_without_a_call(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        seeded: dict[str, uuid.UUID],
    ) -> None:
        """The author moved on; explaining the old answers would be paid waste."""
        async with session_factory() as session:
            await TaskReferenceRepository(session).replace_override(
                authored_document_id=seeded["document_id"],
                kind=ReferenceKind.TEST_KEY,
                answers={"1": ["а"], "2": ["г"]},
                source_content_hash=_HASH,
            )
            await session.commit()

        router = _RouterDouble(session_factory, content=_GOOD)
        port = _CountingPort()

        await arq_explain_key(
            _ctx(session_factory, router, port),
            str(seeded["job_id"]),
            str(seeded["version_id"]),
        )

        state, reason, _ = await _version_state(session_factory, seeded["version_id"])
        assert state == ReferenceState.FAILED.value
        assert reason is not None and "changed" in reason
        assert router.calls == 0, "no model was called"
        assert await _stage_rows(session_factory, seeded["job_id"]) == []
        assert port.accounted == [], "nothing was spent, so nothing is accounted"

    async def test_a_retyped_task_fails_without_a_call(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        seeded: dict[str, uuid.UUID],
    ) -> None:
        """The other axis: the text moved while the key stayed."""
        async with session_factory() as session:
            document = await session.get(AuthoredDocument, seeded["document_id"])
            assert document is not None
            document.content_hash = "f" * 64
            await session.commit()

        router = _RouterDouble(session_factory, content=_GOOD)
        port = _CountingPort()

        await arq_explain_key(
            _ctx(session_factory, router, port),
            str(seeded["job_id"]),
            str(seeded["version_id"]),
        )

        state, reason, _ = await _version_state(session_factory, seeded["version_id"])
        assert state == ReferenceState.FAILED.value
        assert reason is not None and "changed" in reason
        assert router.calls == 0

    async def test_a_refused_port_fails_the_version_without_a_call(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        seeded: dict[str, uuid.UUID],
    ) -> None:
        """Money is asked before models, so a refusal costs nothing at all."""
        router = _RouterDouble(session_factory, content=_GOOD)
        port = _RefusingPort()

        await arq_explain_key(
            _ctx(session_factory, router, port),
            str(seeded["job_id"]),
            str(seeded["version_id"]),
        )

        state, reason, _ = await _version_state(session_factory, seeded["version_id"])
        assert state == ReferenceState.FAILED.value
        assert reason is not None
        assert FundsDecision.REFUSED.value in reason or "refused" in reason
        assert router.calls == 0, "the model was never reached"
        assert await _stage_rows(session_factory, seeded["job_id"]) == []


class TestTheWorkThatFailsLate:
    async def test_an_invalid_answer_fails_the_version_with_a_reason(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        seeded: dict[str, uuid.UUID],
    ) -> None:
        """A model that skips a question must not leave the version "writing".

        The double answers badly on every rung and does not pretend to be the
        real router's retry machinery, so what reaches the body is the
        validator's own refusal. The version must still end FAILED with a
        reason — a body that only understood ladder exhaustion would leave it
        ``pending`` forever, which reads to the author as work in progress.
        The re-raise is caught by the SEAM, which is what turns the Job itself
        ``failed``; that is asserted here rather than a propagating exception.
        """
        router = _RouterDouble(session_factory, content=_BAD)
        port = _CountingPort()

        await arq_explain_key(
            _ctx(session_factory, router, port),
            str(seeded["job_id"]),
            str(seeded["version_id"]),
        )

        state, reason, explanations = await _version_state(
            session_factory, seeded["version_id"]
        )
        assert state == ReferenceState.FAILED.value
        assert reason is not None and reason.startswith("generation failed")
        assert not explanations
        assert port.accounted == [pytest.approx(_COST)], (
            "a failed generation that already paid is still accounted"
        )
        assert await _job_status(session_factory, seeded["job_id"]) == "failed"

    async def test_ladder_exhaustion_fails_the_version_too(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        seeded: dict[str, uuid.UUID],
    ) -> None:
        """The expected member of the same family, named separately."""
        router = _RouterDouble(session_factory, content=_GOOD)
        router.raise_instead = LadderExhaustedError(stage_name=STAGE_NAME, attempts=[])
        port = _CountingPort()

        await arq_explain_key(
            _ctx(session_factory, router, port),
            str(seeded["job_id"]),
            str(seeded["version_id"]),
        )

        state, reason, _ = await _version_state(session_factory, seeded["version_id"])
        assert state == ReferenceState.FAILED.value
        assert reason is not None and reason.startswith("generation failed:")
        assert await _job_status(session_factory, seeded["job_id"]) == "failed"


class TestTheWorkThatIsWrittenDown:
    async def test_the_entry_alone_writes_both_rows_under_its_own_job(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        seeded: dict[str, uuid.UUID],
    ) -> None:
        """Called as ARQ calls it, the work still reaches the register.

        Production paid for a generation on 2026-09-23 and the register got no
        row of it: nothing on the worker's path put the job in context, and
        every test here supplied it with ``job_scope`` (hotfix 4). So nothing
        wraps the call below, the funds port is the shipped one, and both rows
        the work owes the register are required under THIS job — the port's
        decision and the stage's call.
        """
        assert get_current_job_id() is None, "premise: no job in context yet"
        router = _RouterDouble(session_factory, content=_GOOD)

        await arq_explain_key(
            {"session_factory": session_factory, "stage_router": router, "job_try": 1},
            str(seeded["job_id"]),
            str(seeded["version_id"]),
        )

        async with session_factory() as session:
            result = await session.execute(
                select(ExternalServiceCall.action).where(
                    ExternalServiceCall.job_id == seeded["job_id"]
                )
            )
            actions = sorted(result.scalars())
        assert actions == sorted([FUNDS_PORT_ACTION, STAGE_NAME]), (
            "one funds decision and one stage call, both under this job"
        )
