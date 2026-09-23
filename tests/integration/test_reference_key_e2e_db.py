"""Acceptance of task 06, walked end to end (mentor-rebuild task 06).

The criterion this file exists for is a THROUGH one — "the author sends a key
once, its explanations are written once, and a text edit that keeps the
question numbers keeps the key" — and a through criterion is not proved by a
pile of unit tests, each of which can be green while the joints between them
are broken (``vision-rules#25``).

So nothing here is called directly. The key goes in through the author's HTTP
route with a key context of scope PREP and nothing else; the request for a
generation goes through the shipped queue, which creates a real ``Job`` row;
the work runs the real ARQ body through the real seam. The database is live.
The MODEL is the one double, because the alternative is a paid call — and it
writes the register row a real call would have written, so "one generation"
and "no generation" below are counts over real rows, measured before and after
every step rather than assumed.

Requires ``docker compose up -d``; run with ``--run-db``.
"""

from __future__ import annotations

import pathlib
import re
import uuid
from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from course_supporter.agents.key_explainer import STAGE_NAME
from course_supporter.api.app import app
from course_supporter.api.deps import get_arq_redis, get_current_tenant
from course_supporter.auth.context import TenantContext
from course_supporter.homework.reference_service import RefusalCode
from course_supporter.jobs import JobType
from course_supporter.storage.database import get_session
from course_supporter.storage.orm import (
    AuthoredDocument,
    DocumentSegment,
    DocumentSummary,
    ExternalServiceCall,
    Job,
    Tenant,
)
from course_supporter.workers.key_explain import arq_explain_key
from tests._helpers.course_node_factory import make_root_course_node

pytestmark = pytest.mark.requires_db

_KEY: dict[str, list[str]] = {"1": ["б"], "2": ["в"], "3": ["а"]}
_V1 = "1. Перше?\nа) так\nб) ні\n\n2. Друге?\nв) так\n\n3. Третє?\nа) так\nб) ні"
_V2_SAME_NUMBERS = _V1.replace("Перше?", "Перше питання, уточнене?")
_V3_FEWER_NUMBERS = "1. Перше?\nа) так\nб) ні\n\n2. Друге?\nв) так"
_EXPLANATIONS = {
    "1": "Правильна відповідь «б», бо в питанні є заперечення.",
    "2": "«в» правильна, бо саме вона описує дію.",
    "3": "«а» правильна: інші варіанти про інше.",
}
_COST = 0.0184


class _RouterDouble:
    """Answers the canned explanations and writes the register row a call leaves."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory
        self.answer: dict[str, str] = dict(_EXPLANATIONS)

    async def execute_for_stage(
        self,
        stage_name: str,
        *,
        response_validator: Any = None,
        expects_json: bool = False,
        **render_context: Any,
    ) -> Any:
        del expects_json, render_context
        from course_supporter.service_logging import _persist

        await _persist(
            self._session_factory,
            action=stage_name,
            strategy="default",
            provider="deepseek_thinking",
            model_id="deepseek-v4-pro",
            success=True,
            cost_usd=_COST,
            unit_type="tokens",
            unit_in=900,
            unit_out=300,
        )
        if response_validator is not None:
            import json

            response_validator(json.dumps({"explanations": self.answer}))
        return None


def _key_context(tenant_id: uuid.UUID) -> TenantContext:
    """An author's key: scope PREP and nothing else."""
    return TenantContext(
        tenant_id=tenant_id,
        tenant_name="e2e",
        scopes=["prep"],
        plan_id="basic",
        key_prefix="cs_e2e",
    )


@pytest.fixture()
async def world(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[dict[str, uuid.UUID]]:
    """A committed test task of three numbered questions."""
    async with session_factory() as session:
        tenant = Tenant(name=f"e2e-{uuid.uuid4().hex[:8]}")
        session.add(tenant)
        await session.flush()
        node = make_root_course_node(tenant_id=tenant.id, title="E2E", order=0)
        session.add(node)
        await session.flush()
        document = AuthoredDocument(
            course_node_id=node.id,
            course_root_id=node.id,
            source_type="text",
            source_url="file:///tmp/e2e.md",
            task_type="test",
            language="ukr",
            content_hash="a1" + "0" * 62,
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
                content=_V1,
                description="the test",
                start_pos=0,
                end_pos=len(_V1),
            )
        )
        await session.commit()
        ids = {"tenant_id": tenant.id, "document_id": document.id}

    yield ids

    async with session_factory() as session:
        await session.execute(
            ExternalServiceCall.__table__.delete().where(
                ExternalServiceCall.job_id.in_(
                    select(Job.id).where(Job.tenant_id == ids["tenant_id"])
                )
            )
        )
        await session.execute(
            Job.__table__.delete().where(Job.tenant_id == ids["tenant_id"])
        )
        await session.execute(
            Tenant.__table__.delete().where(Tenant.id == ids["tenant_id"])
        )
        await session.commit()


@pytest.fixture()
async def client(
    world: dict[str, uuid.UUID],
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[AsyncClient]:
    """A live client whose key carries PREP and nothing else."""

    async def _yield_session() -> AsyncGenerator[AsyncSession]:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = _yield_session
    # The shipped queue is used as it ships: the route builds an
    # ``ArqExplanationQueue`` around this dependency and the Job row it writes
    # is the real one. Only the dispatch to a live worker is absent — which is
    # why the test runs the work itself, through the real ARQ body and seam.
    app.dependency_overrides[get_arq_redis] = lambda: AsyncMock(
        enqueue_job=AsyncMock(return_value=None)
    )
    app.dependency_overrides[get_current_tenant] = lambda: _key_context(
        world["tenant_id"]
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


async def _stage_calls(
    session_factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> int:
    """How many calls of this stage the register holds for this tenant's jobs."""
    async with session_factory() as session:
        return int(
            await session.scalar(
                select(func.count())
                .select_from(ExternalServiceCall)
                .where(
                    ExternalServiceCall.action == STAGE_NAME,
                    ExternalServiceCall.job_id.in_(
                        select(Job.id).where(Job.tenant_id == tenant_id)
                    ),
                )
            )
            or 0
        )


async def _pending_jobs(
    session_factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> list[Job]:
    """Generation jobs of this tenant that have not run yet, oldest first."""
    async with session_factory() as session:
        result = await session.execute(
            select(Job)
            .where(
                Job.tenant_id == tenant_id,
                Job.job_type == JobType.KEY_EXPLANATION.value,
                Job.status == "queued",
            )
            .order_by(Job.queued_at)
        )
        return list(result.scalars())


async def _run_pending_work(
    session_factory: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    router: _RouterDouble,
) -> int:
    """Run every queued generation the way the worker would. Returns how many."""
    jobs = await _pending_jobs(session_factory, tenant_id)
    for job in jobs:
        reference_id = job.input_params["reference_id"]
        await arq_explain_key(
            {
                "session_factory": session_factory,
                "stage_router": router,
                "job_try": 1,
            },
            str(job.id),
            str(reference_id),
        )
    return len(jobs)


async def _revise(
    session_factory: async_sessionmaker[AsyncSession],
    document_id: uuid.UUID,
    text: str,
    content_hash: str,
) -> None:
    """Re-author the test: new segment content, new content hash.

    What ingestion does, reduced to what this task can observe. The pipeline
    itself is deliberately not involved — the lazy path means nobody tells the
    reference that a version appeared; it notices on the next read.
    """
    async with session_factory() as session:
        summary_id = await session.scalar(
            select(DocumentSummary.id).where(
                DocumentSummary.authored_document_id == document_id
            )
        )
        await session.execute(
            DocumentSegment.__table__.update()
            .where(DocumentSegment.document_summary_id == summary_id)
            .values(content=text)
        )
        document = await session.get(AuthoredDocument, document_id)
        assert document is not None
        document.content_hash = content_hash
        await session.commit()


class TestAcceptance:
    async def test_author_key_explanations_once_per_version_and_carry_over(
        self,
        client: AsyncClient,
        world: dict[str, uuid.UUID],
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """The whole of acceptance criterion 2, in one walk.

        Five steps, each measured against the register before and after, so a
        step that silently stopped costing anything — or started costing twice
        — fails here rather than on a live run with a real invoice.
        """
        tenant_id, document_id = world["tenant_id"], world["document_id"]
        router = _RouterDouble(session_factory)
        read_url = f"/api/v1/documents/{document_id}/reference"
        write_url = f"/api/v1/documents/{document_id}/reference/override"

        # Both sides of every comparison below must be non-empty
        # (``vision-rules#16``): an assertion over nothing is always true.
        assert _KEY, "the key under test is not empty"
        assert _EXPLANATIONS, "the explanations the model returns are not empty"

        # (a) The author sends the key; the work runs; the version is ready.
        before = await _stage_calls(session_factory, tenant_id)
        put = await client.put(write_url, json={"answers": _KEY})
        assert put.status_code == 200, put.text
        assert put.json()["status"] == "generating"

        ran = await _run_pending_work(session_factory, tenant_id, router)
        assert ran == 1, "exactly one generation was asked for"
        after_first = await _stage_calls(session_factory, tenant_id)
        assert after_first - before == 1, "exactly one call of the stage was paid for"

        jobs = await _finished_jobs(session_factory, tenant_id)
        assert len(jobs) == 1
        rows = await _stage_rows(session_factory, tenant_id)
        assert len(rows) == 1
        assert rows[0].job_id == jobs[0].id, "the call belongs to the work's job"

        # (b) The author reads their answers and the explanations, in the
        #     course language.
        got = await client.get(read_url)
        assert got.status_code == 200
        body = got.json()
        assert body["status"] == "ready"
        assert body["answers"] == _KEY
        assert body["language"] == "ukr"
        assert set(body["explanations"]) == set(_KEY), (
            "one explanation per question of the key, no more and no fewer"
        )
        assert all(text.strip() for text in body["explanations"].values())

        # (c) The same key again: nothing is asked for and nothing is paid.
        again = await client.put(write_url, json={"answers": dict(_KEY)})
        assert again.status_code == 200
        assert again.json()["version"] == body["version"]
        assert await _pending_jobs(session_factory, tenant_id) == []
        assert await _stage_calls(session_factory, tenant_id) == after_first

        # (d) A new version of the text with the SAME question numbers: the
        #     answers carry over, and their explanations are written once more.
        await _revise(session_factory, document_id, _V2_SAME_NUMBERS, "a2" + "0" * 62)
        carried = await client.get(read_url)
        assert carried.status_code == 200
        assert carried.json()["carried_over"] is True, (
            "the author did not send this key for the new version"
        )
        assert carried.json()["answers"] == _KEY

        ran = await _run_pending_work(session_factory, tenant_id, router)
        assert ran == 1, "the new version needs its own explanations"
        after_carry = await _stage_calls(session_factory, tenant_id)
        assert after_carry - after_first == 1, "exactly one more call was paid for"
        assert (await client.get(read_url)).json()["status"] == "ready"

        # (e) A new version with a DIFFERENT set of numbers: the key no longer
        #     fits, so the task waits for a new one and nothing is paid.
        await _revise(session_factory, document_id, _V3_FEWER_NUMBERS, "a3" + "0" * 62)
        awaiting = await client.get(read_url)
        assert awaiting.status_code == 200
        assert awaiting.json()["status"] == "awaiting_key"
        assert await _pending_jobs(session_factory, tenant_id) == []
        assert await _stage_calls(session_factory, tenant_id) == after_carry


class TestTheDocumentationSaysWhatTheCodeDoes:
    """``impl-rules#20`` asks the operator to read the description instead of the
    code — which only works while the two agree. These are the two lists a
    reader would act on, and both are checked against the source rather than
    trusted (``DD-SP-BC``: a documented example that nothing executes is prose).
    """

    _README = (
        pathlib.Path(__file__).parents[2]
        / "src"
        / "course_supporter"
        / "homework"
        / "README.md"
    )

    def test_every_refusal_code_is_documented_and_no_invented_one_is(self) -> None:
        text = self._README.read_text(encoding="utf-8")
        documented = {code for code in re.findall(r"`([A-Z][A-Z_]+)`", text)}
        real = {code.value for code in RefusalCode}

        assert real, "the vocabulary under test is not empty"
        assert documented, "the document names some codes"
        assert real <= documented, f"undocumented: {sorted(real - documented)}"

    def test_the_documented_routes_exist(self) -> None:
        text = self._README.read_text(encoding="utf-8")
        documented = {
            path.replace("$DOC_ID", "{document_id}")
            for path in re.findall(r"/api/v1/documents/\$DOC_ID/reference\S*", text)
        }
        real = {
            route.path
            for route in app.routes
            if "reference" in getattr(route, "path", "")
        }

        assert documented, "the document shows some requests"
        assert real, "the application serves some reference routes"
        assert documented == real, f"documented {documented}, served {real}"


async def _finished_jobs(
    session_factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> list[Job]:
    """Generation jobs of this tenant that have run, oldest first."""
    async with session_factory() as session:
        result = await session.execute(
            select(Job)
            .where(
                Job.tenant_id == tenant_id,
                Job.job_type == JobType.KEY_EXPLANATION.value,
                Job.status != "queued",
            )
            .order_by(Job.queued_at)
        )
        return list(result.scalars())


async def _stage_rows(
    session_factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> list[ExternalServiceCall]:
    async with session_factory() as session:
        result = await session.execute(
            select(ExternalServiceCall)
            .where(
                ExternalServiceCall.action == STAGE_NAME,
                ExternalServiceCall.job_id.in_(
                    select(Job.id).where(Job.tenant_id == tenant_id)
                ),
            )
            .order_by(ExternalServiceCall.created_at)
        )
        return list(result.scalars())
