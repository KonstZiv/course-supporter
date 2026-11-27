"""The work that composes the criteria list at the author's request (DD-SP-DR).

Everything is real but the model: the ARQ task runs through the L2 seam, the
``Job`` row is the one the enqueue helper wrote, the list service is the one
the evaluation stage calls, the register is the real table. The composer is the
double, and it writes the register row a paid call would have written, under
the job the seam put in context — so what is charged is a real sum over a real
row.

* The charge is ``task_preparation`` on the tenant's account, by the very key
  a submission's composition uses.
* Funds that cannot pay (a ceiling) are not an error of the work: the list row
  says so and the job ends ``complete``.
* A defect in composing fails the job and records the list's failure.
* The work asked to run over a list already in force calls no model and charges
  nothing.

Requires ``docker compose up -d``; run with ``--run-db``.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from course_supporter.billing import charging, funds, ledger
from course_supporter.billing.accounts import get_or_create_author_account
from course_supporter.billing.kinds import ActionKind, MovementKind
from course_supporter.cost_estimate.shadow import preparation_key
from course_supporter.cost_kinds import CostKind
from course_supporter.criteria_list_state import CriteriaListState
from course_supporter.enqueue import enqueue_criteria_composition
from course_supporter.homework.criteria_edit_service import (
    CriteriaEditService,
    CriteriaReasonCode,
    CriteriaStatus,
)
from course_supporter.homework.criteria_form import (
    CriteriaComposition,
    CriterionDraft,
    compose_criteria,
)
from course_supporter.homework.criteria_list_service import CriteriaListService
from course_supporter.llm.error_categories import LadderExhaustedError, LadderStop
from course_supporter.service_logging import get_current_job_id
from course_supporter.storage.document_summary_repository import (
    DocumentSummaryRepository,
)
from course_supporter.storage.orm import (
    AuthoredDocument,
    BillingMovement,
    CourseNode,
    DocumentSegment,
    ExternalServiceCall,
    Job,
    TaskCriteriaList,
    Tenant,
)
from course_supporter.workers.criteria_compose import arq_compose_criteria
from tests._helpers.billing_cleanup import drop_billing_of_tenant
from tests._helpers.course_node_factory import make_root_course_node

pytestmark = pytest.mark.requires_db

_TASK_TEXT = "Write a recursive factorial function and handle n = 0."
_HASH = "1" * 64
E = Decimal("0.05")
FACT = "0.04"  # 36 units at 900 a dollar and a margin of 0.20
FACT_UNITS = 36


@pytest.fixture(autouse=True)
def _charging_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(charging, "charging_enabled", lambda settings=None: True)
    monkeypatch.setattr(funds, "in_test_mode", lambda *a, **k: False)
    monkeypatch.setattr(funds, "preparation_e_usd", lambda **_: E)


@pytest.fixture()
async def world(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[dict[str, Any]]:
    """A tenant with an author account, a course, and a processed task."""
    async with session_factory() as session:
        tenant = Tenant(name=f"criteria-work-{uuid.uuid4().hex[:8]}")
        session.add(tenant)
        await session.flush()
        root = make_root_course_node(tenant_id=tenant.id, title="Python", order=0)
        session.add(root)
        await session.flush()
        node = CourseNode(
            tenant_id=tenant.id, title="Recursion", order=0, parent_id=root.id
        )
        session.add(node)
        await session.flush()
        task = AuthoredDocument(
            course_node_id=node.id,
            course_root_id=root.id,
            source_type="web",
            source_url="https://example.com/task",
            task_type="task",
            language="ukr",
            content_hash=_HASH,
        )
        session.add(task)
        await session.flush()
        summary = await DocumentSummaryRepository(session).create(
            authored_document_id=task.id,
            title="The Task",
            description="Task description.",
            main_concepts=[],
            secondary_concepts=[],
            content_char_count=len(_TASK_TEXT),
        )
        session.add(
            DocumentSegment(
                document_summary_id=summary.id,
                course_root_id=summary.course_root_id,
                order=0,
                start_pos=0,
                end_pos=len(_TASK_TEXT),
                content=_TASK_TEXT,
            )
        )
        account = await get_or_create_author_account(session, tenant_id=tenant.id)
        await session.commit()
        ids: dict[str, Any] = {
            "tenant": tenant.id,
            "root": root.id,
            "node": node.id,
            "task": task.id,
            "author": account.id,
        }

    yield ids

    await drop_billing_of_tenant(session_factory, ids["tenant"])
    async with session_factory() as session:
        jobs = select(Job.id).where(Job.tenant_id == ids["tenant"])
        await session.execute(
            delete(ExternalServiceCall).where(ExternalServiceCall.job_id.in_(jobs))
        )
        await session.execute(delete(Job).where(Job.tenant_id == ids["tenant"]))
        await session.execute(
            delete(AuthoredDocument).where(
                AuthoredDocument.course_root_id == ids["root"]
            )
        )
        await session.execute(delete(CourseNode).where(CourseNode.id == ids["node"]))
        await session.execute(delete(CourseNode).where(CourseNode.id == ids["root"]))
        await session.execute(delete(Tenant).where(Tenant.id == ids["tenant"]))
        await session.commit()


class _PaidComposer:
    """Answers one criterion — and leaves the register row a paid call leaves."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        error: BaseException | None = None,
    ) -> None:
        self.calls = 0
        self._session_factory = session_factory
        self._error = error

    async def compose(self, **kwargs: Any) -> CriteriaComposition:
        self.calls += 1
        if self._error is not None:
            raise self._error
        job_id = get_current_job_id()
        assert job_id is not None, "the seam must put the job in context"
        async with self._session_factory() as session:
            session.add(
                ExternalServiceCall(
                    job_id=job_id,
                    action="criteria_decomposition",
                    provider="gemini",
                    model_id="gemini-2.5-flash",
                    success=True,
                    unit_type="tokens",
                    unit_in=10,
                    unit_out=10,
                    cost_usd=Decimal(FACT),
                    cost_kind=CostKind.ESTIMATED.value,
                    call_started_at=datetime.now(UTC),
                    created_at=datetime.now(UTC),
                )
            )
            await session.commit()
        return compose_criteria(
            [
                CriterionDraft(
                    text="Has a base case",
                    evidence="an explicit if-return for n = 0",
                    weight="must",
                    check_method="model_verdict",
                    concepts=(),
                )
            ],
            [],
            input_concepts=[],
        )

    def prompt_hash(self) -> str:
        return "p" * 64


async def _ask(
    session_factory: async_sessionmaker[AsyncSession], world: dict[str, Any]
) -> Job:
    """What the route does: the Job row, then the queue."""
    arq = MagicMock()
    arq.enqueue_job = AsyncMock(return_value=MagicMock(job_id="arq-1"))
    async with session_factory() as session:
        return await enqueue_criteria_composition(
            redis=arq,
            session=session,
            tenant_id=world["tenant"],
            authored_document_id=world["task"],
        )


async def _run(
    session_factory: async_sessionmaker[AsyncSession],
    job: Job,
    world: dict[str, Any],
    composer: _PaidComposer,
) -> Job:
    ctx = {
        "session_factory": session_factory,
        "stage_router": MagicMock(),
        "redis": None,
    }

    def build(
        factory: Any, router: Any, *, shadow_estimate: bool = False
    ) -> CriteriaListService:
        """The production wiring with the composer swapped: the flag is the work's."""
        return CriteriaListService(factory, composer, shadow_estimate=shadow_estimate)

    with patch(
        "course_supporter.workers.criteria_compose.build_criteria_list_service",
        build,
    ):
        await arq_compose_criteria(ctx, str(job.id), str(world["task"]))
    async with session_factory() as session:
        done = await session.get(Job, job.id)
    assert done is not None
    return done


async def _buy(
    session_factory: async_sessionmaker[AsyncSession], world: dict[str, Any]
) -> None:
    async with session_factory() as session:
        await ledger.credit_purchase(
            session,
            account_id=world["author"],
            units=500,
            amount_paid_minor=500,
            currency="EUR",
            provider="fake",
        )
        await session.commit()


async def _moved(
    session_factory: async_sessionmaker[AsyncSession], world: dict[str, Any]
) -> dict[str, int]:
    async with session_factory() as session:
        # The version the composition is keyed by is the task's own hash, which
        # processing sets — not the one the seed wrote.
        version = await session.scalar(
            select(AuthoredDocument.content_hash).where(
                AuthoredDocument.id == world["task"]
            )
        )
        assert version is not None
        key = preparation_key(world["task"], version, "criteria_decomposition")
        rows = await session.execute(
            select(BillingMovement.kind, func.sum(BillingMovement.units))
            .where(
                BillingMovement.action_kind == ActionKind.TASK_PREPARATION.value,
                BillingMovement.action_key == key,
            )
            .group_by(BillingMovement.kind)
        )
        return {kind: int(units) for kind, units in rows.tuples()}


async def _reading(
    session_factory: async_sessionmaker[AsyncSession], world: dict[str, Any]
) -> Any:
    async with session_factory() as session:
        return await CriteriaEditService(session).read(world["task"])


class TestTheWork:
    async def test_it_composes_the_list_and_takes_task_preparation_from_the_account(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        world: dict[str, Any],
    ) -> None:
        await _buy(session_factory, world)
        job = await _ask(session_factory, world)
        composer = _PaidComposer(session_factory)

        done = await _run(session_factory, job, world, composer)

        assert composer.calls == 1
        assert done.status == "complete"
        assert (done.result_data or {})["state"] == "ready"
        moved = await _moved(session_factory, world)
        assert moved[MovementKind.DEBIT.value] == FACT_UNITS
        # What was held is gone: debited or released, nothing left reserved.
        held = moved.get("reserve", 0) - moved.get("reserve_release", 0)
        assert held == FACT_UNITS
        async with session_factory() as session:
            assert (await ledger.balance(session, world["author"])).available == (
                500 - FACT_UNITS
            )
        reading = await _reading(session_factory, world)
        assert reading.status is CriteriaStatus.READY
        assert reading.in_force is not None
        assert [c.text for c in reading.in_force.criteria] == ["Has a base case"]

    async def test_the_call_is_in_the_register_under_this_job(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        world: dict[str, Any],
    ) -> None:
        job = await _ask(session_factory, world)

        await _run(session_factory, job, world, _PaidComposer(session_factory))

        async with session_factory() as session:
            rows = (
                await session.scalars(
                    select(ExternalServiceCall).where(
                        ExternalServiceCall.job_id == job.id
                    )
                )
            ).all()
        assert [row.action for row in rows] == ["criteria_decomposition"]

    @pytest.mark.parametrize(
        ("stop", "reason"),
        [
            (LadderStop.MONEY_CEILING, CriteriaReasonCode.LIMIT_REACHED),
            (LadderStop.EXHAUSTED, CriteriaReasonCode.MODELS_UNAVAILABLE),
        ],
    )
    async def test_a_composition_that_gave_up_charges_nothing_and_is_read_why(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        world: dict[str, Any],
        stop: LadderStop,
        reason: CriteriaReasonCode,
    ) -> None:
        await _buy(session_factory, world)
        job = await _ask(session_factory, world)
        composer = _PaidComposer(
            session_factory,
            error=LadderExhaustedError("criteria_decomposition", [], stop=stop),
        )

        done = await _run(session_factory, job, world, composer)

        assert done.status == "complete"
        assert (done.result_data or {})["state"] == "unavailable"
        assert MovementKind.DEBIT.value not in await _moved(session_factory, world)
        async with session_factory() as session:
            assert (await ledger.balance(session, world["author"])).available == 500
        reading = await _reading(session_factory, world)
        assert reading.status is CriteriaStatus.NOT_COMPOSED
        assert reading.reason_code is reason

    async def test_a_defect_fails_the_job_and_records_the_failure_of_the_list(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        world: dict[str, Any],
    ) -> None:
        job = await _ask(session_factory, world)
        composer = _PaidComposer(session_factory, error=RuntimeError("a defect"))

        done = await _run(session_factory, job, world, composer)

        assert done.status == "failed"
        async with session_factory() as session:
            row = await session.scalar(
                select(TaskCriteriaList).where(
                    TaskCriteriaList.authored_document_id == world["task"]
                )
            )
        assert row is not None
        assert row.state == CriteriaListState.FAILED.value
        assert row.failure_reason == "error: RuntimeError"
        reading = await _reading(session_factory, world)
        assert reading.reason_code is CriteriaReasonCode.COMPOSITION_ERROR

    async def test_a_list_already_in_force_calls_no_model_and_charges_nothing(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        world: dict[str, Any],
    ) -> None:
        await _buy(session_factory, world)
        first = await _ask(session_factory, world)
        await _run(session_factory, first, world, _PaidComposer(session_factory))
        moved = await _moved(session_factory, world)
        second = await _ask(session_factory, world)
        composer = _PaidComposer(session_factory)

        done = await _run(session_factory, second, world, composer)

        assert composer.calls == 0
        assert done.status == "complete"
        assert (done.result_data or {})["state"] == "ready"
        assert await _moved(session_factory, world) == moved
