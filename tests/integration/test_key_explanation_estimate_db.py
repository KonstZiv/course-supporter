"""BIL-B4: the price of a key's explanations is the E the worker reserves.

The route ``POST /documents/{id}/reference/estimate`` and the worker
``arq_explain_key`` (step 2) price the same version on the same characters:
``billing.estimates.key_explanation_task_chars`` over the text and the key the
worker shows the model. Each test asks the route, then runs the real worker on
the version the author's action asked for, with a funds port that records E and
refuses — so no model is called — and compares the two numbers.

Requires ``docker compose up -d``; run with ``--run-db``.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncGenerator, Iterator
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from course_supporter.api.app import app
from course_supporter.api.deps import get_arq_redis, get_current_tenant, get_s3_client
from course_supporter.api.routes import billing_estimates
from course_supporter.auth.context import TenantContext
from course_supporter.billing import charging, quotes
from course_supporter.billing.estimates import preparation_e_usd
from course_supporter.funds_port import (
    FundsAnswer,
    FundsRefusalReason,
    VersionWorkContext,
)
from course_supporter.homework.reference_key import AnswerKey, answers_digest
from course_supporter.homework.task_context import load_task_context
from course_supporter.homework.test_object import (
    DraftBody,
    DraftOption,
    DraftQuestion,
    published_form,
    render_for_prompt,
)
from course_supporter.homework.test_object_service import TestObjectService
from course_supporter.jobs import JOB_SUBJECT_TYPE, JobType
from course_supporter.reference_kinds import ReferenceKind
from course_supporter.storage.database import get_session
from course_supporter.storage.orm import (
    AuthoredDocument,
    DocumentSegment,
    DocumentSummary,
    Job,
    Tenant,
)
from course_supporter.storage.task_reference_repository import TaskReferenceRepository
from course_supporter.workers.key_explain import arq_explain_key
from tests._helpers.course_node_factory import make_root_course_node

pytestmark = pytest.mark.requires_db

_KIND = "key_explanation"
_HASH = "b4" * 32
_TEXT = (
    "1. Перше питання тесту?\nа) так\nб) ні\n\n2. Друге питання тесту?\nв) так\nг) ні"
)
_KEY: AnswerKey = {"1": ["б"], "2": ["в"]}
_DRAFT = DraftBody(
    questions=tuple(
        DraftQuestion(
            text=f"Питання {n}: що виведе print({n} ** 3) у Python? " * 4,
            options=(
                DraftOption(text=f"{n**3}", correct=True),
                DraftOption(text=f"{n * 3}", correct=False),
                DraftOption(text="Помилку TypeError", correct=False),
            ),
        )
        for n in range(1, 6)
    )
)


@dataclass(frozen=True)
class Course:
    tenant_id: uuid.UUID
    root_id: uuid.UUID


class _Asked:
    """The queue of a check or a publication: records the version it asks for."""

    def __init__(self) -> None:
        self.asked: list[uuid.UUID] = []

    async def request(
        self, *, authored_document_id: uuid.UUID, reference_id: uuid.UUID
    ) -> None:
        del authored_document_id
        self.asked.append(reference_id)

    async def in_flight(self, *, authored_document_id: uuid.UUID) -> bool:
        del authored_document_id
        return False


class _RecordingPort:
    """Records the E the worker reserves for, and refuses: no model is called."""

    def __init__(self) -> None:
        self.expected: list[Decimal | None] = []

    async def check_and_reserve_for_version(
        self, context: VersionWorkContext, ceiling_estimate_usd: float
    ) -> FundsAnswer:
        del ceiling_estimate_usd
        self.expected.append(context.expected_usd)
        return FundsAnswer.refused(FundsRefusalReason.INSUFFICIENT_FUNDS)

    async def account_version_work_cost(self, *_: Any, **__: Any) -> None:
        raise AssertionError("a refused work spends nothing")


@pytest.fixture()
async def course(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[Course]:
    async with session_factory() as session:
        tenant = Tenant(name=f"bil-b4-{uuid.uuid4().hex[:8]}")
        session.add(tenant)
        await session.flush()
        root = make_root_course_node(tenant_id=tenant.id, title="Курс", order=0)
        session.add(root)
        await session.commit()
        made = Course(tenant.id, root.id)
    yield made
    async with session_factory() as session:
        await session.execute(delete(Job).where(Job.tenant_id == made.tenant_id))
        await session.execute(delete(Tenant).where(Tenant.id == made.tenant_id))
        await session.commit()


@pytest.fixture()
def route_e(
    session_factory: async_sessionmaker[AsyncSession],
    course: Course,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[list[Decimal | None]]:
    """The author signed in; each E the route prices with is recorded."""
    seen: list[Decimal | None] = []

    def recorded(**kwargs: Any) -> Decimal | None:
        e_usd = preparation_e_usd(**kwargs)
        seen.append(e_usd)
        return e_usd

    async def _session() -> Any:
        async with session_factory() as session:
            yield session

    monkeypatch.setattr(billing_estimates, "preparation_e_usd", recorded)
    monkeypatch.setattr(charging, "charging_enabled", lambda settings=None: True)
    monkeypatch.setattr(quotes, "in_test_mode", lambda *a, **k: False)
    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_current_tenant] = lambda: TenantContext(
        tenant_id=course.tenant_id,
        tenant_name="t",
        scopes=["prep"],
        plan_id="basic",
        key_prefix="cs_test",
    )
    app.dependency_overrides[get_s3_client] = lambda: MagicMock()
    app.dependency_overrides[get_arq_redis] = lambda: AsyncMock()
    yield seen
    app.dependency_overrides.clear()


async def _estimate(document_id: uuid.UUID) -> dict[str, Any]:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/api/v1/documents/{document_id}/reference/estimate"
        )
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    assert body["price_units"] is not None
    assert body["unpriced_reason"] is None
    return body


async def _worker_e(
    session_factory: async_sessionmaker[AsyncSession],
    course: Course,
    document_id: uuid.UUID,
    version_id: uuid.UUID,
) -> Decimal | None:
    """Run the real worker on ``version_id``; the E it reserves for (step 2)."""
    async with session_factory() as session:
        job = Job(
            tenant_id=course.tenant_id,
            course_node_id=course.root_id,
            job_type=JobType.KEY_EXPLANATION.value,
            subject_type=JOB_SUBJECT_TYPE[JobType.KEY_EXPLANATION],
            subject_id=document_id,
        )
        session.add(job)
        await session.commit()
        job_id = job.id
    port = _RecordingPort()
    await arq_explain_key(
        {
            "session_factory": session_factory,
            "stage_router": None,
            "funds_port": port,
            "job_try": 1,
        },
        str(job_id),
        str(version_id),
    )
    assert len(port.expected) == 1, "the worker reached the funds port once"
    return port.expected[0]


def _old_worker_e(text: str, answers: AnswerKey) -> Decimal | None:
    """E by the worker's line before BIL-B4, written out: its number is kept."""
    return preparation_e_usd(
        kind=_KIND,
        task_chars=len(text) + len(json.dumps(answers, default=str)),
    )


async def _written_test(
    session_factory: async_sessionmaker[AsyncSession], course: Course
) -> uuid.UUID:
    """A test written in the system, with its draft; nothing asked for yet."""
    async with session_factory() as session:
        document = await TestObjectService(session, _Asked()).create(
            course.root_id, _DRAFT, title="Тест у системі", language="ukr"
        )
        await session.commit()
        return document.id


async def _press(
    session_factory: async_sessionmaker[AsyncSession],
    document_id: uuid.UUID,
    action: Literal["check", "publish"],
) -> uuid.UUID:
    """The author presses ``action``; the version of explanations it asks for."""
    queue = _Asked()
    async with session_factory() as session:
        service = TestObjectService(session, queue)
        if action == "check":
            await service.check(document_id)
        else:
            await service.publish(document_id)
        await session.commit()
    assert len(queue.asked) == 1, f"the {action} asked for one version"
    return queue.asked[0]


async def _file_test(
    session_factory: async_sessionmaker[AsyncSession],
    course: Course,
    *,
    key: AnswerKey | None,
) -> tuple[uuid.UUID, uuid.UUID | None]:
    """A test written as a file; with ``key``, the author's key and its version."""
    async with session_factory() as session:
        document = AuthoredDocument(
            course_node_id=course.root_id,
            course_root_id=course.root_id,
            source_type="text",
            source_url=f"file:///tmp/bil-b4-{uuid.uuid4().hex}.md",
            task_type="test",
            language="ukr",
            content_hash=_HASH,
        )
        session.add(document)
        await session.flush()
        summary = DocumentSummary(
            authored_document_id=document.id,
            course_root_id=course.root_id,
            title="Тест із файла",
            description="Опис тесту, який стара оцінка теж рахувала.",
            status="ready",
        )
        session.add(summary)
        await session.flush()
        middle = _TEXT.index("\n\n")
        for order, (start, end) in enumerate(((0, middle), (middle, len(_TEXT)))):
            session.add(
                DocumentSegment(
                    document_summary_id=summary.id,
                    course_root_id=course.root_id,
                    order=order,
                    content=_TEXT[start:end],
                    description="the test",
                    start_pos=start,
                    end_pos=end,
                )
            )
        version_id = None
        if key is not None:
            repo = TaskReferenceRepository(session)
            await repo.replace_override(
                authored_document_id=document.id,
                kind=ReferenceKind.TEST_KEY,
                answers=key,
                source_content_hash=_HASH,
            )
            version, _ = await repo.create_version(
                authored_document_id=document.id,
                kind=ReferenceKind.TEST_KEY,
                source_content_hash=_HASH,
                source_task_type="test",
                answers_hash=answers_digest(key),
                language="ukr",
            )
            version_id = version.id
        await session.commit()
        return document.id, version_id


class TestTheRoutePricesWhatTheWorkerReserves:
    @pytest.mark.parametrize("action", ["check", "publish"])
    async def test_a_written_test_its_draft_as_the_worker_reads_it(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        course: Course,
        route_e: list[Decimal | None],
        action: Literal["check", "publish"],
    ) -> None:
        """Priced before the button; the worker explains what the button asked.

        A check's row is read from the draft, a publication's from the version
        it published (``TestObjectService.body_with_axes``): the same E.
        """
        document_id = await _written_test(session_factory, course)

        await _estimate(document_id)
        version_id = await _press(session_factory, document_id, action)
        worker = await _worker_e(session_factory, course, document_id, version_id)

        shown = published_form(_DRAFT, "ukr")
        assert route_e == [worker]
        assert worker is not None
        assert worker == _old_worker_e(render_for_prompt(shown), shown.answer_key())

    async def test_a_file_test_its_source_text_and_the_key_in_force(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        course: Course,
        route_e: list[Decimal | None],
    ) -> None:
        document_id, version_id = await _file_test(session_factory, course, key=_KEY)
        assert version_id is not None

        await _estimate(document_id)
        worker = await _worker_e(session_factory, course, document_id, version_id)

        assert route_e == [worker]
        assert worker is not None
        assert worker == _old_worker_e(_TEXT, _KEY)

    async def test_a_file_test_without_a_key_is_priced_on_its_text_alone(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        course: Course,
        route_e: list[Decimal | None],
    ) -> None:
        """No key yet: the source text and an empty key — as before, a price."""
        document_id, _ = await _file_test(session_factory, course, key=None)

        await _estimate(document_id)

        assert route_e == [_old_worker_e(_TEXT, {})]


class TestTheOldRoutePriceWasLower:
    async def test_a_written_test_was_priced_on_nothing_of_its_body(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        course: Course,
        route_e: list[Decimal | None],
    ) -> None:
        """The route's formula before BIL-B4 (title, description and stitched
        text) reads none of a written test's body: its E was well below the
        worker's, and the equality above would have caught it."""
        document_id = await _written_test(session_factory, course)
        await _estimate(document_id)
        async with session_factory() as session:
            title, description, text = await load_task_context(session, document_id)
        old_chars = sum(len(part or "") for part in (title, description, text))
        old = preparation_e_usd(kind=_KIND, task_chars=old_chars)

        (new,) = route_e
        assert old is not None and new is not None
        assert new > old
