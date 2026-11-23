"""BIL-E for the Methodist: a regeneration of node summaries is the author's processing.

The operator, 2026-11-23 (the author's guide): writing section descriptions is
paid with the author's units as a material's processing is, and the panel shows
the price before the action. The regeneration job (``arq_regenerate_node_summary``)
runs here through the real execution seam; only the orchestrator is a stub that
writes the register rows a run of the model would (``_Methodist``). The price
route is the real one.

Numbers, where E is fixed (``_priced``): K = 900 units a dollar, margin 0.20
(``config/cost_estimate.yaml``); E = $0.05 → P = 54, 2E = 90; the payer's call
of $0.04 is F = 36 units, a structural retry of $0.02 is the platform's.
Where E is not fixed, it is the real one on the real factors.

Requires ``docker compose up -d``; run with ``--run-db``.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from course_supporter.agents import methodist_factory
from course_supporter.api.app import app
from course_supporter.api.deps import get_current_tenant
from course_supporter.api.routes import billing_estimates
from course_supporter.api.tasks import arq_regenerate_node_summary
from course_supporter.auth.context import TenantContext
from course_supporter.billing import admin, charging, estimates, funds, ledger
from course_supporter.billing.accounts import get_or_create_author_account
from course_supporter.billing.kinds import ActionKind, MovementKind, ReservationState
from course_supporter.call_outcome import CallOutcome, RequestKind
from course_supporter.cost_estimate.actions import MaterialFactors, NodeSummaryFactors
from course_supporter.cost_estimate.node_summary import read_node_summary_factors
from course_supporter.cost_kinds import CostKind
from course_supporter.jobs.job_type import JOB_SUBJECT_TYPE, JobType
from course_supporter.security.exceptions import ErrorCategory
from course_supporter.storage.database import get_session
from course_supporter.storage.job_repository import JobRepository
from course_supporter.storage.orm import (
    ActionCostEstimate,
    AuthoredDocument,
    BillingMovement,
    BillingReservation,
    CourseNode,
    DocumentSegment,
    DocumentSummary,
    ExternalServiceCall,
    Job,
    NodeSummaryRaw,
    Tenant,
)
from tests._helpers.billing_cleanup import drop_billing_of_tenant
from tests._helpers.course_node_factory import make_root_course_node

pytestmark = pytest.mark.requires_db

E = Decimal("0.05")
P = 54
TWO_E = 90
F = 36
ACTOR = "test-admin"


# ── A course, its author, a regeneration job ──────────────────────────


@dataclass(frozen=True)
class World:
    tenant_id: uuid.UUID
    course_id: uuid.UUID
    section_id: uuid.UUID
    """The vertex of the regeneration: a section with a methodological note,
    no summary yet — one bottom-up and one top-down call."""
    material_id: uuid.UUID
    """A material of the section, for the processing it is compared with."""
    author: uuid.UUID


_WORLDS: list[uuid.UUID] = []


async def make_world(session_factory: async_sessionmaker[AsyncSession]) -> World:
    async with session_factory() as session:
        tenant = Tenant(name=f"bil-ns-{uuid.uuid4().hex[:8]}")
        session.add(tenant)
        await session.flush()
        course = make_root_course_node(tenant_id=tenant.id, title="Курс", order=0)
        session.add(course)
        await session.flush()
        section = CourseNode(
            tenant_id=tenant.id, parent_id=course.id, title="Розділ", order=0
        )
        session.add(section)
        await session.flush()
        material = AuthoredDocument(
            course_node_id=section.id,
            course_root_id=course.id,
            source_type="text",
            source_url=f"https://example.com/{uuid.uuid4().hex}",
            material_role="methodological",
        )
        session.add(material)
        await session.flush()
        summary = DocumentSummary(
            authored_document_id=material.id,
            course_root_id=course.id,
            title="Методичка",
            description="Як читати розділ.",
            main_concepts=["цикл"],
            secondary_concepts=["умова"],
            content_char_count=40,
        )
        session.add(summary)
        await session.flush()
        session.add(
            DocumentSegment(
                document_summary_id=summary.id,
                course_root_id=course.id,
                order=0,
                start_pos=0,
                end_pos=40,
                content="Текст методички розділу, який читає модель.",
                main_concepts=[],
                secondary_concepts=[],
            )
        )
        author = await get_or_create_author_account(session, tenant_id=tenant.id)
        await session.commit()
    _WORLDS.append(tenant.id)
    return World(tenant.id, course.id, section.id, material.id, author.id)


async def _drop_world(
    session_factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> None:
    await drop_billing_of_tenant(session_factory, tenant_id)
    async with session_factory() as session:
        jobs = select(Job.id).where(Job.tenant_id == tenant_id)
        nodes = select(CourseNode.id).where(CourseNode.tenant_id == tenant_id)
        await session.execute(
            delete(ExternalServiceCall).where(ExternalServiceCall.job_id.in_(jobs))
        )
        await session.execute(
            delete(ActionCostEstimate).where(ActionCostEstimate.job_id.in_(jobs))
        )
        await session.execute(delete(Job).where(Job.tenant_id == tenant_id))
        await session.execute(
            delete(DocumentSegment).where(DocumentSegment.course_root_id.in_(nodes))
        )
        await session.execute(
            delete(DocumentSummary).where(DocumentSummary.course_root_id.in_(nodes))
        )
        await session.execute(
            delete(AuthoredDocument).where(AuthoredDocument.course_root_id.in_(nodes))
        )
        await session.execute(
            delete(NodeSummaryRaw).where(NodeSummaryRaw.course_node_id.in_(nodes))
        )
        await session.execute(
            delete(CourseNode).where(
                CourseNode.tenant_id == tenant_id, CourseNode.parent_id.is_not(None)
            )
        )
        await session.execute(
            delete(CourseNode).where(CourseNode.tenant_id == tenant_id)
        )
        await session.execute(delete(Tenant).where(Tenant.id == tenant_id))
        await session.commit()


@pytest.fixture(autouse=True)
async def _drop_worlds(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[None]:
    yield
    while _WORLDS:
        await _drop_world(session_factory, _WORLDS.pop())


@pytest.fixture(autouse=True)
def _charging_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(charging, "charging_enabled", lambda settings=None: True)


@pytest.fixture()
def _priced(monkeypatch: pytest.MonkeyPatch) -> None:
    """E = $0.05 for a job that calls the model (the real 0 for one that does not)."""

    def fixed(factors: NodeSummaryFactors, **_: Any) -> Decimal:
        return E if factors.calls_model else Decimal(0)

    monkeypatch.setattr(funds, "node_summary_e_usd", fixed)
    monkeypatch.setattr(funds, "processing_e_usd", lambda **_: E)


async def new_job(
    session_factory: async_sessionmaker[AsyncSession],
    w: World,
    *,
    vertex: uuid.UUID | None = None,
    force: bool = False,
) -> uuid.UUID:
    vertex = vertex or w.section_id
    async with session_factory() as session:
        job = Job(
            tenant_id=w.tenant_id,
            course_node_id=vertex,
            job_type=JobType.NODE_SUMMARY_REGENERATION.value,
            subject_type=JOB_SUBJECT_TYPE[JobType.NODE_SUMMARY_REGENERATION],
            subject_id=vertex,
            status="queued",
            input_params={"force": force},
        )
        session.add(job)
        await session.commit()
        return job.id


# ── The Methodist, as far as billing sees it ──────────────────────────


@dataclass
class _Methodist:
    """A stub orchestrator: each run writes the register rows of its model calls.

    ``seen_reserve`` — the job's active reserve (units, or ``None``) at its first
    model call: the order "reserve, then the first paid step" is read from it.
    """

    session_factory: async_sessionmaker[AsyncSession]
    fail: bool = False
    runs: int = 0
    seen_reserve: list[int | None] = field(default_factory=list)

    def build(self, session: AsyncSession, stage_router: Any) -> _Methodist:
        return self

    async def run(
        self, *, job_id: uuid.UUID, vertex_node_id: uuid.UUID, force: bool = False
    ) -> None:
        self.runs += 1
        async with self.session_factory() as session:
            held = await charging.reservation_of(
                session, ActionKind.PROCESSING, str(job_id)
            )
            if held is None or held.state != ReservationState.ACTIVE.value:
                self.seen_reserve.append(None)
            else:
                self.seen_reserve.append(held.units)
            for kind, usd in (
                (RequestKind.FIRST, "0.04"),  # the payer's
                (RequestKind.STRUCTURAL_RETRY, "0.02"),  # the platform's
            ):
                session.add(_call(job_id, kind, usd))
            await session.commit()
        if self.fail:
            raise RuntimeError("the model broke on purpose")


def _call(job_id: uuid.UUID, kind: RequestKind, usd: str) -> ExternalServiceCall:
    return ExternalServiceCall(
        job_id=job_id,
        action="methodist_bottomup",
        provider="deepseek_thinking",
        model_id="deepseek-v4-pro",
        success=True,
        outcome=CallOutcome.SUCCESS.value,
        request_kind=kind.value,
        unit_type="tokens",
        unit_in=10,
        unit_out=10,
        cost_usd=Decimal(usd),
        cost_kind=CostKind.ESTIMATED.value,
        call_started_at=datetime.now(UTC),
        created_at=datetime.now(UTC),
    )


@pytest.fixture()
def methodist(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> _Methodist:
    stub = _Methodist(session_factory)
    monkeypatch.setattr(
        methodist_factory, "build_node_summary_orchestrator", stub.build
    )
    return stub


async def run_job(
    session_factory: async_sessionmaker[AsyncSession],
    job_id: uuid.UUID,
    vertex: uuid.UUID,
    *,
    force: bool = False,
) -> Job:
    """The worker's call, through the seam; the job as it ended."""
    ctx = {"session_factory": session_factory, "stage_router": MagicMock()}
    await arq_regenerate_node_summary(ctx, str(job_id), str(vertex), force)
    async with session_factory() as session:
        job = await session.get(Job, job_id)
        assert job is not None
        return job


# ── Reading the journal ───────────────────────────────────────────────


async def buy(
    session_factory: async_sessionmaker[AsyncSession], account: uuid.UUID, units: int
) -> None:
    async with session_factory() as session:
        await ledger.credit_purchase(
            session,
            account_id=account,
            units=units,
            amount_paid_minor=500,
            currency="EUR",
            provider="fake",
        )
        await session.commit()


async def available(
    session_factory: async_sessionmaker[AsyncSession], account: uuid.UUID
) -> int:
    async with session_factory() as session:
        return (await ledger.balance(session, account)).available


async def sums(
    session_factory: async_sessionmaker[AsyncSession], key: uuid.UUID
) -> dict[str, int]:
    async with session_factory() as session:
        rows = await session.execute(
            select(BillingMovement.kind, func.sum(BillingMovement.units))
            .where(
                BillingMovement.action_kind == ActionKind.PROCESSING.value,
                BillingMovement.action_key == str(key),
            )
            .group_by(BillingMovement.kind)
        )
        return {k: int(u) for k, u in rows.tuples()}


async def reservations(
    session_factory: async_sessionmaker[AsyncSession], key: uuid.UUID
) -> list[BillingReservation]:
    async with session_factory() as session:
        rows = await session.execute(
            select(BillingReservation).where(
                BillingReservation.action_kind == ActionKind.PROCESSING.value,
                BillingReservation.action_key == str(key),
            )
        )
        return list(rows.scalars())


async def movements_of(
    session_factory: async_sessionmaker[AsyncSession], account: uuid.UUID
) -> int:
    async with session_factory() as session:
        return int(
            await session.scalar(
                select(func.count(BillingMovement.id)).where(
                    BillingMovement.account_id == account,
                    BillingMovement.kind != MovementKind.CREDIT.value,
                )
            )
            or 0
        )


async def calls_of(
    session_factory: async_sessionmaker[AsyncSession], job_id: uuid.UUID
) -> int:
    async with session_factory() as session:
        return int(
            await session.scalar(
                select(func.count(ExternalServiceCall.id)).where(
                    ExternalServiceCall.job_id == job_id
                )
            )
            or 0
        )


async def set_credit(
    session_factory: async_sessionmaker[AsyncSession], w: World
) -> None:
    async with session_factory() as session:
        await admin.update_author_settings(
            session, w.tenant_id, actor=ACTOR, credit_allowed=True
        )
        await session.commit()


# ── charging off ──────────────────────────────────────────────────


@pytest.mark.usefixtures("_priced")
async def test_off_the_journal_stays_empty_and_the_job_runs_as_before(
    session_factory: async_sessionmaker[AsyncSession],
    methodist: _Methodist,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(charging, "charging_enabled", lambda settings=None: False)
    w = await make_world(session_factory)  # an author with no units at all
    job_id = await new_job(session_factory, w)

    job = await run_job(session_factory, job_id, w.section_id)

    assert job.status == "complete"
    assert methodist.seen_reserve == [None]
    assert await reservations(session_factory, job_id) == []
    assert await movements_of(session_factory, w.author) == 0


# ── reserve before the first model call, the fact at the end ──────


@pytest.mark.usefixtures("_priced")
class TestReserveAndFact:
    async def test_the_reserve_precedes_the_first_call_and_the_fact_is_charged(
        self, session_factory: async_sessionmaker[AsyncSession], methodist: _Methodist
    ) -> None:
        w = await make_world(session_factory)
        await buy(session_factory, w.author, 500)
        job_id = await new_job(session_factory, w)

        job = await run_job(session_factory, job_id, w.section_id)

        assert job.status == "complete"
        assert methodist.seen_reserve == [TWO_E], "held before the first model call"
        moved = await sums(session_factory, job_id)
        assert moved == {
            MovementKind.RESERVE.value: TWO_E,
            MovementKind.DEBIT.value: F,  # the payer's call alone (PAID_BY_PAYER)
            MovementKind.RESERVE_RELEASE.value: TWO_E - F,
        }
        assert await available(session_factory, w.author) == 500 - F

    async def test_a_failed_run_releases_the_reserve_and_charges_nothing(
        self, session_factory: async_sessionmaker[AsyncSession], methodist: _Methodist
    ) -> None:
        w = await make_world(session_factory)
        await buy(session_factory, w.author, 500)
        job_id = await new_job(session_factory, w)
        methodist.fail = True

        job = await run_job(session_factory, job_id, w.section_id)

        assert job.status == "failed"
        assert await sums(session_factory, job_id) == {
            MovementKind.RESERVE.value: TWO_E,
            MovementKind.RESERVE_RELEASE.value: TWO_E,
        }
        assert await available(session_factory, w.author) == 500

    async def test_an_obsolete_job_releases_what_its_first_attempt_held(
        self, session_factory: async_sessionmaker[AsyncSession], methodist: _Methodist
    ) -> None:
        """An attempt reserved and the worker died; the vertex is gone before
        ARQ's next attempt: the seam ends the job ``obsolete`` — released."""
        w = await make_world(session_factory)
        await buy(session_factory, w.author, 500)
        job_id = await new_job(session_factory, w)
        async with session_factory() as session:
            factors = await read_node_summary_factors(session, w.section_id)
        await funds.node_summary_regeneration_started(
            factors=factors,
            job_id=job_id,
            vertex_node_id=w.section_id,
            session_factory=session_factory,
        )
        async with session_factory() as session:
            await session.execute(
                update(CourseNode)
                .where(CourseNode.id == w.section_id)
                .values(deleted_at=datetime.now(UTC))
            )
            await session.commit()

        job = await run_job(session_factory, job_id, w.section_id)

        assert job.status == "obsolete"
        assert methodist.runs == 0
        assert await sums(session_factory, job_id) == {
            MovementKind.RESERVE.value: TWO_E,
            MovementKind.RESERVE_RELEASE.value: TWO_E,
        }


# ── short of the price ────────────────────────────────────────────


@pytest.mark.usefixtures("_priced")
class TestShortOfThePrice:
    async def test_without_credit_the_job_fails_before_any_model_call(
        self, session_factory: async_sessionmaker[AsyncSession], methodist: _Methodist
    ) -> None:
        w = await make_world(session_factory)
        await buy(session_factory, w.author, P - 1)
        job_id = await new_job(session_factory, w)

        job = await run_job(session_factory, job_id, w.section_id)

        assert job.status == "failed"
        assert job.error_category == funds.InsufficientUnitsError.category.value
        assert job.error_category == ErrorCategory.INSUFFICIENT_UNITS.value
        assert (job.error_message or "").startswith("insufficient_units")
        assert methodist.runs == 0
        assert await calls_of(session_factory, job_id) == 0
        assert await reservations(session_factory, job_id) == []
        assert await available(session_factory, w.author) == P - 1

    async def test_the_refusal_is_the_processings(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """The same gate refuses a material's processing with the same error."""
        w = await make_world(session_factory)
        await buy(session_factory, w.author, P - 1)

        with pytest.raises(funds.InsufficientUnitsError) as processing:
            await funds.processing_started(
                kind="text",
                factors=MaterialFactors(text_chars=1000, safety_chars=1000),
                job_id=uuid.uuid4(),
                tenant_id=w.tenant_id,
                authored_document_id=w.material_id,
                session_factory=session_factory,
            )
        async with session_factory() as session:
            factors = await read_node_summary_factors(session, w.section_id)
        with pytest.raises(funds.InsufficientUnitsError) as regeneration:
            await funds.node_summary_regeneration_started(
                factors=factors,
                job_id=uuid.uuid4(),
                vertex_node_id=w.section_id,
                session_factory=session_factory,
            )

        assert str(regeneration.value) == str(processing.value)
        assert regeneration.value.category is processing.value.category

    async def test_with_credit_it_goes_on_and_owes(
        self, session_factory: async_sessionmaker[AsyncSession], methodist: _Methodist
    ) -> None:
        w = await make_world(session_factory)
        await set_credit(session_factory, w)
        job_id = await new_job(session_factory, w)

        job = await run_job(session_factory, job_id, w.section_id)

        assert job.status == "complete"
        assert methodist.runs == 1
        assert await sums(session_factory, job_id) == {
            MovementKind.DIFFERENCE_ARISEN.value: F
        }


# ── every summary fresh ───────────────────────────────────────────


async def _all_fresh(
    session_factory: async_sessionmaker[AsyncSession], w: World
) -> None:
    """The course root alone, its summary current: nothing to regenerate."""
    async with session_factory() as session:
        root = await session.get(CourseNode, w.course_id)
        assert root is not None
        await session.execute(
            update(CourseNode)
            .where(CourseNode.id == w.section_id)
            .values(deleted_at=datetime.now(UTC))
        )
        session.add(
            NodeSummaryRaw(
                course_node_id=root.id,
                source_content_hash=root.content_hash,
                compressed_summary="Підсумок курсу.",
            )
        )
        await session.commit()


class TestNothingToDo:
    async def test_no_reserve_and_the_job_runs(
        self, session_factory: async_sessionmaker[AsyncSession], methodist: _Methodist
    ) -> None:
        w = await make_world(session_factory)
        await _all_fresh(session_factory, w)
        job_id = await new_job(session_factory, w, vertex=w.course_id)

        job = await run_job(session_factory, job_id, w.course_id)

        assert job.status == "complete"
        assert methodist.seen_reserve == [None]
        assert await reservations(session_factory, job_id) == []
        assert await movements_of(session_factory, w.author) == 0

    @pytest.mark.usefixtures("signed_in")
    async def test_the_price_is_0_with_nothing_to_do(
        self, session_factory: async_sessionmaker[AsyncSession], world: World
    ) -> None:
        await _all_fresh(session_factory, world)

        async with _client() as client:
            response = await client.post(
                f"/api/v1/nodes/{world.course_id}/summary/estimate", json={}
            )

        assert response.status_code == 200, response.text
        assert response.json() == {
            "price_units": 0,
            "available_units": 0,
            "enough": True,
            "missing_units": 0,
            "charging_enabled": True,
            "unpriced_reason": "nothing_to_do",
            "missing_features": None,
        }


# ── the price shown is the E the job reserves ─────────────────────


@pytest.fixture()
async def world(session_factory: async_sessionmaker[AsyncSession]) -> World:
    return await make_world(session_factory)


@pytest.fixture()
def signed_in(
    session_factory: async_sessionmaker[AsyncSession], world: World
) -> Iterator[None]:
    async def _session() -> Any:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_current_tenant] = lambda: TenantContext(
        tenant_id=world.tenant_id,
        tenant_name="t",
        scopes=["prep"],
        plan_id="basic",
        key_prefix="cs_test",
    )
    yield
    app.dependency_overrides.clear()


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.usefixtures("signed_in")
class TestThePriceBeforeTheAction:
    @pytest.mark.parametrize("force", [False, True])
    async def test_the_price_is_p_of_the_e_the_job_reserves(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        world: World,
        methodist: _Methodist,
        monkeypatch: pytest.MonkeyPatch,
        force: bool,
    ) -> None:
        """One function, the same factors: what the route prices, the job holds."""
        seen: dict[str, tuple[NodeSummaryFactors, Decimal | None]] = {}

        def spy(where: str) -> Any:
            def e_usd(factors: NodeSummaryFactors, **kwargs: Any) -> Decimal | None:
                e = estimates.node_summary_e_usd(factors, **kwargs)
                seen[where] = (factors, e)
                return e

            return e_usd

        monkeypatch.setattr(billing_estimates, "node_summary_e_usd", spy("route"))
        monkeypatch.setattr(funds, "node_summary_e_usd", spy("job"))
        await buy(session_factory, world.author, 10_000)

        async with _client() as client:
            response = await client.post(
                f"/api/v1/nodes/{world.section_id}/summary/estimate",
                json={"force": force},
            )
        job_id = await new_job(session_factory, world, force=force)
        job = await run_job(session_factory, job_id, world.section_id, force=force)

        assert response.status_code == 200, response.text
        assert job.status == "complete"
        route_factors, e = seen["route"]
        assert seen["job"] == (route_factors, e), "the same factors, the same E"
        assert route_factors.calls_model
        assert e is not None and e > 0
        body = response.json()
        assert body["price_units"] == charging.price_of(e) > 0
        assert body["unpriced_reason"] is None
        assert methodist.seen_reserve == [charging.reserve_request(e, None)]

    async def test_another_tenants_node_is_not_found(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        other = await make_world(session_factory)

        async with _client() as client:
            response = await client.post(
                f"/api/v1/nodes/{other.section_id}/summary/estimate", json={}
            )

        assert response.status_code == 404

    async def test_charging_off_the_price_is_shown_and_never_short(
        self,
        world: World,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(charging, "charging_enabled", lambda settings=None: False)

        async with _client() as client:
            response = await client.post(
                f"/api/v1/nodes/{world.section_id}/summary/estimate", json={}
            )

        body = response.json()
        assert body["price_units"] > 0
        assert (body["enough"], body["missing_units"]) == (True, 0)
        assert body["charging_enabled"] is False


# ── a reactivated job: as a material's processing ─────────────────


@pytest.mark.usefixtures("_priced")
async def test_a_reactivated_job_is_charged_as_a_reactivated_processing(
    session_factory: async_sessionmaker[AsyncSession], methodist: _Methodist
) -> None:
    """A failed job reactivated (``POST /jobs/{id}/reactivate``, the same job id)
    goes through the same key twice — exactly as a material's processing does.

    What the key does the second time (``charging.reserve_for`` → the ledger
    returns the reservation there is, whatever its state; ``charging.finish``
    on a released reservation settles nothing): no second reserve, nothing
    charged twice — and, today, nothing charged for the second run either.
    That last part is the processing's own behaviour, pinned here as it is and
    reported (B-5, item 3), not changed.
    """
    w = await make_world(session_factory)
    await buy(session_factory, w.author, 500)

    # A material's processing: a run that fails, then the same job again.
    processing = uuid.uuid4()
    for completed in (False, True):
        await funds.processing_started(
            kind="text",
            factors=MaterialFactors(text_chars=1000, safety_chars=1000),
            job_id=processing,
            tenant_id=w.tenant_id,
            authored_document_id=w.material_id,
            session_factory=session_factory,
        )
        await funds.job_ended(
            session_factory,
            job_id=processing,
            authored_document_id=w.material_id,
            completed=completed,
        )

    # The regeneration: the same through the seam and the reactivation.
    job_id = await new_job(session_factory, w)
    methodist.fail = True
    first = await run_job(session_factory, job_id, w.section_id)
    async with session_factory() as session:
        await JobRepository(session).reactivate(job_id)
        await session.commit()
    methodist.fail = False
    second = await run_job(session_factory, job_id, w.section_id)

    assert (first.status, second.status) == ("failed", "complete")
    assert methodist.seen_reserve == [TWO_E, None], "the second run holds nothing"
    assert len(await reservations(session_factory, job_id)) == 1
    regeneration = await sums(session_factory, job_id)
    assert regeneration == await sums(session_factory, processing)
    assert regeneration == {
        MovementKind.RESERVE.value: TWO_E,
        MovementKind.RESERVE_RELEASE.value: TWO_E,
    }, "one reserve, no debit twice (today: none for the second run)"
    assert await available(session_factory, w.author) == 500
