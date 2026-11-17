"""The payer's fact (operator 2026-11-17) on a live database.

Which register rows the payer pays for — ``billing.charging.PAID_BY_PAYER``:
the first request of a stage and an item repeat; structural and infrastructure
retries, and an attempt that itself failed on the infrastructure, are the
platform's. Here: the facts the journal settles (a review, a processing, a
task preparation), the movements they settle into, and the platform's share
the cost report shows.

Numbers as in ``test_billing_charging_db``: K = 900 units a dollar, E = $0.05,
2E = 90 units; a call of $0.04 is F = 36 units.

Requires ``docker compose up -d``; run with ``--run-db``.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from course_supporter.billing import charging, costs, funds
from course_supporter.billing.kinds import ActionKind, MovementKind
from course_supporter.call_outcome import CallOutcome, RequestKind
from course_supporter.cost_kinds import CostKind
from course_supporter.funds_port import SubmissionOutcome
from course_supporter.storage.orm import ExternalServiceCall
from tests._helpers.billing_cleanup import drop_billing_of_tenant
from tests.integration import test_billing_charging_db as bil

pytestmark = pytest.mark.requires_db

FIRST = RequestKind.FIRST
ITEM_REPEAT = RequestKind.ITEM_REPEAT
STRUCTURAL = RequestKind.STRUCTURAL_RETRY
INFRA = RequestKind.INFRA_RETRY
FAILED = CallOutcome.TRANSPORT_ERROR


@pytest.fixture(autouse=True)
def _charging_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """Charging on, and the path's test mode off."""
    monkeypatch.setattr(charging, "charging_enabled", lambda settings=None: True)
    monkeypatch.setattr(funds, "in_test_mode", lambda *a, **k: False)


@pytest.fixture(autouse=True)
async def _drop_billing(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[None]:
    """The billing rows of every world a test made go."""
    yield
    while bil._WORLDS:
        await drop_billing_of_tenant(session_factory, bil._WORLDS.pop())


async def attempt(
    session_factory: async_sessionmaker[AsyncSession],
    job_id: uuid.UUID,
    usd: str,
    kind: RequestKind | None,
    *,
    outcome: CallOutcome | None = CallOutcome.SUCCESS,
    action: str = "criteria_evaluation",
) -> None:
    """One priced row of the register; ``kind=None`` — a row without a kind
    (speech-to-text, or written before the column)."""
    async with session_factory() as session:
        session.add(
            ExternalServiceCall(
                job_id=job_id,
                action=action,
                provider="gemini",
                model_id="gemini-2.5-flash",
                success=outcome is not FAILED,
                outcome=None if outcome is None else outcome.value,
                request_kind=None if kind is None else kind.value,
                unit_type="tokens",
                unit_in=10,
                unit_out=10,
                cost_usd=Decimal(usd),
                cost_kind=CostKind.ESTIMATED.value,
                call_started_at=datetime.now(UTC),
                created_at=datetime.now(UTC),
            )
        )
        await session.commit()


async def review_fact(
    session_factory: async_sessionmaker[AsyncSession], w: bil.World
) -> Decimal:
    async with session_factory() as session:
        return await charging.review_fact_usd(session, w.submission_id)


async def job_fact(
    session_factory: async_sessionmaker[AsyncSession],
    job_id: uuid.UUID,
    *,
    action: str | None = None,
) -> Decimal:
    async with session_factory() as session:
        return await charging.job_fact_usd(session, job_id, action=action)


async def report(
    session_factory: async_sessionmaker[AsyncSession], w: bil.World
) -> costs.CostReport:
    today = datetime.now(UTC).date()
    async with session_factory() as session:
        return await costs.build_cost_report(
            session, from_date=today, to_date=today, tenant_id=w.tenant_id
        )


class TestWhoPaysWhichCall:
    async def test_the_fact_is_first_item_repeat_and_rows_without_a_kind(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """Every kind once: the payer's three, the platform's two."""
        w = await bil.make_world(session_factory)
        job = w.homework_job_id
        await attempt(session_factory, job, "0.04", FIRST)
        await attempt(session_factory, job, "0.02", ITEM_REPEAT)
        await attempt(session_factory, job, "0.01", None, outcome=None)
        # A failed row without a kind stays the payer's, as before the decision.
        await attempt(session_factory, job, "0.005", None, outcome=FAILED)
        await attempt(session_factory, job, "0.03", STRUCTURAL)
        await attempt(session_factory, job, "0.05", INFRA)

        assert await review_fact(session_factory, w) == Decimal("0.075")
        found = await report(session_factory, w)
        (student,) = found.students
        (author,) = found.authors
        for spend in (student.spend, author.spend):
            assert spend.known_usd == Decimal("0.155"), "the dollars stay whole"
            assert spend.platform_usd == Decimal("0.08"), "structural + infra"

    async def test_a_first_attempt_that_failed_on_the_infrastructure_is_the_platforms(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """A read timeout billed as $0.02, then the same request again."""
        w = await bil.make_world(session_factory)
        job = w.homework_job_id
        await attempt(session_factory, job, "0.02", FIRST, outcome=FAILED)
        await attempt(session_factory, job, "0.04", INFRA)
        await attempt(session_factory, job, "0.03", FIRST, action="criteria_review")

        assert await review_fact(session_factory, w) == Decimal("0.03")
        (student,) = (await report(session_factory, w)).students
        assert student.spend.platform_usd == Decimal("0.06")

    async def test_a_first_attempt_whose_answer_was_refused_is_the_payers(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """Only an infrastructure failure moves a first request to the platform."""
        w = await bil.make_world(session_factory)
        job = w.homework_job_id
        await attempt(
            session_factory, job, "0.04", FIRST, outcome=CallOutcome.INVALID_CONTENT
        )
        await attempt(session_factory, job, "0.04", STRUCTURAL)

        assert await review_fact(session_factory, w) == Decimal("0.04")

    async def test_processing_and_preparation_by_the_same_rule(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        w = await bil.make_world(session_factory)
        processing = w.processing_job_id
        await attempt(session_factory, processing, "0.04", FIRST)
        await attempt(session_factory, processing, "0.02", FIRST, outcome=FAILED)
        await attempt(session_factory, processing, "0.05", INFRA)
        await attempt(session_factory, processing, "0.01", STRUCTURAL)
        prep = "criteria_decomposition"
        job = w.homework_job_id
        await attempt(session_factory, job, "0.03", FIRST, action=prep)
        await attempt(session_factory, job, "0.02", STRUCTURAL, action=prep)
        await attempt(session_factory, job, "0.04", FIRST)

        assert await job_fact(session_factory, processing) == Decimal("0.04")
        assert await job_fact(session_factory, job, action=prep) == Decimal("0.03")
        assert await review_fact(session_factory, w) == Decimal("0.04"), (
            "the preparation stays out of the review"
        )

    async def test_first_requests_alone_the_fact_is_as_before(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """Regression: without a repeat, the fact is every priced call, as before."""
        w = await bil.make_world(session_factory)
        job = w.homework_job_id
        for usd, stage in (("0.04", "criteria_evaluation"), ("0.01", "safety")):
            await attempt(session_factory, job, usd, FIRST, action=stage)
        async with session_factory() as session:
            every_priced_call = await session.scalar(
                select(func.sum(ExternalServiceCall.cost_usd)).where(
                    ExternalServiceCall.job_id == job,
                    ExternalServiceCall.unit_type.is_not(None),
                )
            )

        assert await review_fact(session_factory, w) == every_priced_call
        assert every_priced_call == Decimal("0.05")
        (student,) = (await report(session_factory, w)).students
        assert student.spend.platform_usd == 0


class TestTheJournal:
    async def test_the_review_charges_the_payers_fact_in_the_same_movements(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """As ``test_reserve_at_start_fact_at_end_rest_released``, with retries
        that cost more than the reserve: none of it reaches the journal."""
        w = await bil.make_world(session_factory)
        await bil.buy(session_factory, w.student, 500)
        port = funds.LedgerFundsPort(session_factory)
        await port.check_and_reserve(bil.review(w), 1.0)
        job = w.homework_job_id
        await attempt(session_factory, job, "0.04", FIRST)
        await attempt(session_factory, job, "0.10", STRUCTURAL)
        await attempt(session_factory, job, "0.10", INFRA)

        await port.release_remainder(bil.review(w), SubmissionOutcome.COMPLETED)

        moved = await bil.sums(session_factory, ActionKind.REVIEW, bil.key_of(w))
        assert moved == {
            MovementKind.RESERVE.value: bil.TWO_E,
            MovementKind.DEBIT.value: 36,
            MovementKind.RESERVE_RELEASE.value: bil.TWO_E - 36,
        }
        after = await bil.balance(session_factory, w.student)
        assert (after.balance, after.available, after.difference) == (464, 464, 0)
        async with session_factory() as session:
            assert await funds.billing_note(session, w.submission_id) is None

    async def test_the_processing_charges_the_payers_fact(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(funds, "processing_e_usd", lambda **_: bil.E)
        w = await bil.make_world(session_factory)
        await bil.buy(session_factory, w.author, 500)
        await bil.start_processing(session_factory, w)
        job = w.processing_job_id
        await attempt(session_factory, job, "0.04", FIRST)
        await attempt(session_factory, job, "0.30", INFRA)

        await funds.job_ended(
            session_factory,
            job_id=job,
            authored_document_id=w.task_id,
            completed=True,
        )

        moved = await bil.sums(session_factory, ActionKind.PROCESSING, str(job))
        assert moved == {
            MovementKind.RESERVE.value: bil.TWO_E,
            MovementKind.DEBIT.value: 36,
            MovementKind.RESERVE_RELEASE.value: bil.TWO_E - 36,
        }
