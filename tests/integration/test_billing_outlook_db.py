"""The student's reminder against the doors (BIL-OUTLOOK-N), on a live database.

``reviews_left`` is how many reviews in a row the doors let through: in while
``available >= P``, then the fact F is charged. P — the median of the course's
latest review estimates, each priced again from its ``e_usd``; F — the
student's own average charge, else the median fact of the course's finished
reviews, else P.

Numbers: K = 900, margin 0.20 (``config/cost_estimate.yaml``): E = $0.25 is
P = ceil(0.25 x 1.2 x 900) = 270; a call of $0.10 is F = 90.

Requires ``docker compose up -d``; run with ``--run-db``.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from decimal import Decimal

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from course_supporter.billing import admin, ledger, outlook
from course_supporter.billing.kinds import ActionKind, PayerSource, RuleScope
from course_supporter.call_outcome import RequestKind
from course_supporter.config import get_settings
from course_supporter.jobs.job_type import JobType
from course_supporter.storage.orm import (
    ActionCostEstimate,
    AuthoredDocument,
    HomeworkSubmission,
)
from tests._helpers.billing_cleanup import drop_billing_of_tenant
from tests.integration import test_billing_charging_db as bil
from tests.integration.test_billing_payer_fact_db import attempt

pytestmark = pytest.mark.requires_db

P_USD = "0.25"  # P = 270


@pytest.fixture(autouse=True)
async def _drop_billing(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[None]:
    """The billing rows and the estimates of every world a test made go."""
    yield
    while bil._WORLDS:
        tenant_id = bil._WORLDS.pop()
        async with session_factory() as session:
            await session.execute(
                delete(ActionCostEstimate).where(
                    ActionCostEstimate.tenant_id == tenant_id
                )
            )
            await session.commit()
        await drop_billing_of_tenant(session_factory, tenant_id)


async def task(
    session_factory: async_sessionmaker[AsyncSession], w: bil.World, task_type: str
) -> uuid.UUID:
    """Another task of the world's course."""
    async with session_factory() as session:
        doc = AuthoredDocument(
            course_node_id=w.course_id,
            course_root_id=w.course_id,
            source_type="text",
            source_url=f"https://example.com/{uuid.uuid4().hex}",
            task_type=task_type,
        )
        session.add(doc)
        await session.commit()
        return doc.id


async def estimate(
    session_factory: async_sessionmaker[AsyncSession],
    w: bil.World,
    submission_id: uuid.UUID,
    e_usd: str,
    *,
    p_units: int,
) -> None:
    """A review estimate whose stored P is ``p_units`` (any rate)."""
    async with session_factory() as session:
        session.add(
            ActionCostEstimate(
                action_kind=ActionKind.REVIEW.value,
                action_key=str(submission_id),
                tenant_id=w.tenant_id,
                submission_id=submission_id,
                e_usd=Decimal(e_usd),
                p_units=p_units,
                reserve_units=1,
                margin=Decimal("0.2"),
                units_per_usd=Decimal(900),
                breakdown=[],
                inputs={},
                uncalibrated=True,
            )
        )
        await session.commit()


async def review(
    session_factory: async_sessionmaker[AsyncSession],
    w: bil.World,
    calls: list[tuple[str, RequestKind]],
    *,
    status: str = "completed",
    task_id: uuid.UUID | None = None,
    e_usd: str | None = None,
) -> uuid.UUID:
    """A review of the course in ``status`` with its job's calls (and estimate)."""
    async with session_factory() as session:
        submission = HomeworkSubmission(
            tenant_id=w.tenant_id,
            student_id=w.student_id,
            course_node_id=w.course_id,
            node_id=w.course_id,
            authored_document_id=task_id or w.task_id,
            file_url="s3://bucket/work.py",
            file_type="text/plain",
            status=status,
        )
        session.add(submission)
        await session.flush()
        job = bil._job(
            w.tenant_id, w.course_id, JobType.HOMEWORK_PROCESSING, submission.id
        )
        session.add(job)
        await session.commit()
    for usd, kind in calls:
        await attempt(session_factory, job.id, usd, kind)
    if e_usd is not None:
        await estimate(session_factory, w, submission.id, e_usd, p_units=1)
    return submission.id


async def charged(
    session_factory: async_sessionmaker[AsyncSession], w: bil.World, units: list[int]
) -> None:
    """Reviews charged to the student's account."""
    async with session_factory() as session:
        for charge in units:
            await ledger.settle(
                session,
                action_kind=ActionKind.REVIEW,
                action_key=str(uuid.uuid4()),
                fact_units=charge,
                beneficiary_account_id=w.student,
                payer_source=PayerSource.DEFAULT,
                account_id=w.student,
            )
        await session.commit()


async def student_outlook(
    session_factory: async_sessionmaker[AsyncSession],
    w: bil.World,
    *,
    low_reviews: int = 2,
) -> outlook.StudentOutlook:
    settings = get_settings().model_copy(
        update={"billing_reminder_low_reviews": low_reviews}
    )
    async with session_factory() as session:
        return await outlook.student_outlook(
            session,
            tenant_id=w.tenant_id,
            student_id=w.student_id,
            settings=settings,
            course_id=w.course_id,
        )


FIRST = RequestKind.FIRST


class TestReviewsLeft:
    async def test_without_own_history_the_courses_price_and_fact(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """P: median of 216, 270, 324 (stored P at another rate); F: of 45, 90, 135."""
        w = await bil.make_world(session_factory)
        for e_usd, fact in (("0.2", "0.05"), (P_USD, "0.10"), ("0.3", "0.15")):
            await review(session_factory, w, [(fact, FIRST)], e_usd=e_usd)
        await bil.buy(session_factory, w.student, 500)

        found = await student_outlook(session_factory, w)

        assert (found.reviews_left, found.warning) == (3, None)  # 1 + 230 // 90

    async def test_own_history_is_the_fact_the_course_the_price(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        w = await bil.make_world(session_factory)
        await review(session_factory, w, [("0.10", FIRST)], e_usd=P_USD)
        await bil.buy(session_factory, w.student, 620)
        await charged(session_factory, w, [60, 60])

        found = await student_outlook(session_factory, w)

        assert found.reviews_left == 4  # 1 + (500 - 270) // 60; the course's: 3

    async def test_the_price_is_todays_price_of_e_not_the_stored_p(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """Written at 1 700 units a dollar: stored P = 510; today's is 270."""
        w = await bil.make_world(session_factory)
        done = await review(session_factory, w, [("0.10", FIRST)])
        await estimate(session_factory, w, done, P_USD, p_units=510)
        await bil.buy(session_factory, w.student, 540)

        found = await student_outlook(session_factory, w)

        assert found.reviews_left == 4  # 1 + (540 - 270) // 90; at 510: 1

    async def test_zero_facts_tests_and_unfinished_reviews_are_not_the_fact(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """F = median(90, 135) = 112.5: a copy (no calls), a review whose only
        call is the platform's, a test, a failed review — left out."""
        w = await bil.make_world(session_factory)
        short = await task(session_factory, w, "short_task")
        test = await task(session_factory, w, "test")
        await review(session_factory, w, [("0.10", FIRST)], e_usd=P_USD)
        await review(
            session_factory, w, [("0.15", FIRST)], status="delivered", task_id=short
        )
        await review(session_factory, w, [])
        await review(session_factory, w, [("0.50", RequestKind.STRUCTURAL_RETRY)])
        await review(session_factory, w, [("0.50", FIRST)], task_id=test)
        await review(session_factory, w, [("0.50", FIRST)], status="failed")
        await bil.buy(session_factory, w.student, 500)

        found = await student_outlook(session_factory, w)

        # 1 + 230 // 112.5; with the zeros (F = 45): 6; with the rest (292.5): 1
        assert found.reviews_left == 3

    async def test_a_course_without_finished_reviews_takes_the_price_as_the_fact(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        w = await bil.make_world(session_factory)
        await estimate(session_factory, w, w.submission_id, P_USD, p_units=270)
        await bil.buy(session_factory, w.student, 809)

        found = await student_outlook(session_factory, w)

        assert found.reviews_left == 809 // 270

    @pytest.mark.parametrize(
        ("units", "left", "warning"),
        [(269, 0, "next_may_fail"), (270, 1, "low"), (540, 2, None)],
    )
    async def test_the_warning_follows_the_same_number(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        units: int,
        left: int,
        warning: str | None,
    ) -> None:
        w = await bil.make_world(session_factory)
        await estimate(session_factory, w, w.submission_id, P_USD, p_units=270)
        await bil.buy(session_factory, w.student, units)

        found = await student_outlook(session_factory, w, low_reviews=2)

        assert (found.reviews_left, found.warning) == (left, warning)

    async def test_the_author_paying_says_nothing_about_units(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        w = await bil.make_world(session_factory)
        await review(session_factory, w, [("0.10", FIRST)], e_usd=P_USD)
        await bil.buy(session_factory, w.student, 500)
        async with session_factory() as session:
            await admin.create_payment_rule(
                session,
                actor="test-admin",
                tenant_id=w.tenant_id,
                scope=RuleScope.COURSE,
                action_kind=ActionKind.REVIEW,
                course_node_id=w.course_id,
            )
            await session.commit()

        found = await student_outlook(session_factory, w)

        assert (found.reviews_left, found.warning, found.paid_by) == (
            None,
            None,
            "author",
        )
