"""The author's three routes, against a live database (task 06, block D1).

Nothing is called directly: every assertion below goes through HTTP, with a key
context carrying exactly the scopes the route requires and nothing else, so a
scope check that stopped working would show up here rather than in a comment.
The database is live, because half of what these routes do is a write the next
read has to see.

The generation queue is the one double, and it counts: "sending the same key
twice costs one generation" is a number of requests, not the absence of a call
on a mock.

Requires ``docker compose up -d``; run with ``--run-db``.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator, Callable
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from course_supporter.api.app import app
from course_supporter.api.deps import get_arq_redis, get_current_tenant
from course_supporter.auth.context import TenantContext
from course_supporter.storage.database import get_session
from course_supporter.storage.orm import (
    AuthoredDocument,
    DocumentSegment,
    DocumentSummary,
    Tenant,
)
from tests._helpers.course_node_factory import make_root_course_node

pytestmark = pytest.mark.requires_db

_KEY: dict[str, list[str]] = {"1": ["б"], "2": ["в"]}
_TEXT = "1. Перше?\nа) так\nб) ні\n\n2. Друге?\nв) так\nг) ні"
_HASH = "1" * 64


class _CountingQueue:
    """The generation seam, counting what the routes asked for."""

    def __init__(self, **_: object) -> None:
        self.requests: list[uuid.UUID] = []

    async def request(
        self, *, authored_document_id: uuid.UUID, reference_id: uuid.UUID
    ) -> None:
        del authored_document_id
        self.requests.append(reference_id)


def _key_context(tenant_id: uuid.UUID, *scopes: str) -> TenantContext:
    """A key context carrying exactly the scopes named — no more."""
    return TenantContext(
        tenant_id=tenant_id,
        tenant_name="routes",
        scopes=list(scopes),
        plan_id="basic",
        key_prefix="cs_routes",
    )


async def _make_task(
    session: AsyncSession,
    tenant: Tenant,
    *,
    text: str = _TEXT,
    task_type: str | None = "test",
    language: str | None = "ukr",
    content_hash: str | None = _HASH,
) -> AuthoredDocument:
    node = make_root_course_node(tenant_id=tenant.id, title="Routes course", order=0)
    session.add(node)
    await session.flush()
    document = AuthoredDocument(
        course_node_id=node.id,
        course_root_id=node.id,
        source_type="text",
        source_url="file:///tmp/routes.md",
        task_type=task_type,
        language=language,
        content_hash=content_hash,
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
            content=text,
            description="the test",
            start_pos=0,
            end_pos=len(text),
        )
    )
    await session.flush()
    return document


@pytest.fixture()
async def world(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[dict[str, uuid.UUID]]:
    """Two tenants: one with a test task, and a stranger who owns nothing here."""
    async with session_factory() as session:
        owner = Tenant(name=f"owner-{uuid.uuid4().hex[:8]}")
        stranger = Tenant(name=f"stranger-{uuid.uuid4().hex[:8]}")
        session.add_all([owner, stranger])
        await session.flush()

        test_task = await _make_task(session, owner)
        project_task = await _make_task(session, owner, task_type="project")
        unprocessed = await _make_task(session, owner, content_hash=None)
        no_language = await _make_task(session, owner, language=None)
        no_numbers = await _make_task(session, owner, text="Тест\n\nа) так\nб) ні")
        await session.commit()
        ids = {
            "owner_id": owner.id,
            "stranger_id": stranger.id,
            "test_task": test_task.id,
            "project_task": project_task.id,
            "unprocessed": unprocessed.id,
            "no_language": no_language.id,
            "no_numbers": no_numbers.id,
        }

    yield ids

    async with session_factory() as session:
        for tenant_id in (ids["owner_id"], ids["stranger_id"]):
            await session.execute(
                Tenant.__table__.delete().where(Tenant.id == tenant_id)
            )
        await session.commit()


@pytest.fixture()
def queue() -> _CountingQueue:
    """A fresh counting queue per test — no state travels between them."""
    return _CountingQueue()


@pytest.fixture()
async def client(
    world: dict[str, uuid.UUID],
    queue: _CountingQueue,
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[tuple[AsyncClient, Callable[[TenantContext], None]]]:
    """A live client, a key-context switch, and the counting queue wired in."""

    async def _yield_session() -> AsyncGenerator[AsyncSession]:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = _yield_session
    # The routes take the queue from the request, and the queue takes Redis.
    # The queue itself is replaced below, so what this override supplies is
    # never used — but the dependency still has to resolve, and in a test there
    # is no application state to resolve it from.
    app.dependency_overrides[get_arq_redis] = lambda: None
    app.dependency_overrides[get_current_tenant] = lambda: _key_context(
        world["owner_id"], "prep"
    )

    def use_key(ctx: TenantContext) -> None:
        app.dependency_overrides[get_current_tenant] = lambda: ctx

    with patch(
        "course_supporter.api.routes.references.ArqExplanationQueue",
        return_value=queue,
    ):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac, use_key

    app.dependency_overrides.clear()


def _read(document_id: uuid.UUID) -> str:
    return f"/api/v1/documents/{document_id}/reference"


def _write(document_id: uuid.UUID) -> str:
    return f"/api/v1/documents/{document_id}/reference/override"


class TestTheKeyRoundTrip:
    async def test_put_then_get_shows_the_answers(
        self,
        client: tuple[AsyncClient, Callable[[TenantContext], None]],
        queue: _CountingQueue,
        world: dict[str, uuid.UUID],
    ) -> None:
        """What the author sent comes back, and the work was asked for once."""
        ac, _ = client
        put = await ac.put(_write(world["test_task"]), json={"answers": _KEY})

        assert put.status_code == 200, put.text
        body = put.json()
        assert body["status"] == "generating"
        assert body["answers"] == _KEY
        assert body["carried_over"] is False
        assert body["language"] == "ukr"
        assert body["version"] == 1
        assert len(queue.requests) == 1

        got = await ac.get(_read(world["test_task"]))
        assert got.status_code == 200
        assert got.json() == body

    async def test_the_same_key_again_asks_for_no_new_work(
        self,
        client: tuple[AsyncClient, Callable[[TenantContext], None]],
        queue: _CountingQueue,
        world: dict[str, uuid.UUID],
    ) -> None:
        ac, _ = client
        await ac.put(_write(world["test_task"]), json={"answers": _KEY})
        again = await ac.put(
            _write(world["test_task"]), json={"answers": {"2": ["в"], "1": ["б"]}}
        )

        assert again.status_code == 200
        assert again.json()["version"] == 1
        assert len(queue.requests) == 1, "a repeat must not buy a second generation"

    async def test_a_task_without_a_key_is_awaiting_one_not_a_404(
        self,
        client: tuple[AsyncClient, Callable[[TenantContext], None]],
        world: dict[str, uuid.UUID],
    ) -> None:
        """The absence of a key is a state of the task, not a missing thing."""
        ac, _ = client
        resp = await ac.get(_read(world["test_task"]))

        assert resp.status_code == 200
        assert resp.json() == {
            "status": "awaiting_key",
            "version": None,
            "answers": {},
            "explanations": {},
            "carried_over": False,
            "language": "ukr",
            "failure_reason": None,
        }

    async def test_delete_returns_the_task_to_awaiting_a_key(
        self,
        client: tuple[AsyncClient, Callable[[TenantContext], None]],
        world: dict[str, uuid.UUID],
    ) -> None:
        ac, _ = client
        await ac.put(_write(world["test_task"]), json={"answers": _KEY})

        cleared = await ac.delete(_write(world["test_task"]))
        assert cleared.status_code == 200
        assert cleared.json()["status"] == "awaiting_key"
        assert cleared.json()["answers"] == {}

        assert (await ac.get(_read(world["test_task"]))).json()["status"] == (
            "awaiting_key"
        )
        # Clearing twice is not an error: the state asked for is the state that
        # results either way.
        assert (await ac.delete(_write(world["test_task"]))).status_code == 200

    async def test_the_authors_own_explanation_comes_back(
        self,
        client: tuple[AsyncClient, Callable[[TenantContext], None]],
        world: dict[str, uuid.UUID],
    ) -> None:
        ac, _ = client
        resp = await ac.put(
            _write(world["test_task"]),
            json={"answers": _KEY, "author_explanations": {"1": "бо так"}},
        )

        assert resp.status_code == 200
        assert resp.json()["explanations"] == {"1": "бо так"}


class TestEveryRefusalCarriesItsOwnCode:
    @pytest.mark.parametrize(
        ("task", "code"),
        [
            ("project_task", "NOT_A_TEST_TASK"),
            ("unprocessed", "TASK_NOT_READY"),
            ("no_language", "TASK_LANGUAGE_UNSET"),
            ("no_numbers", "NO_QUESTION_NUMBERS"),
        ],
    )
    async def test_a_task_that_cannot_carry_a_key_says_which_way(
        self,
        client: tuple[AsyncClient, Callable[[TenantContext], None]],
        world: dict[str, uuid.UUID],
        task: str,
        code: str,
    ) -> None:
        """Four different reasons, four different codes — never one for all."""
        ac, _ = client
        resp = await ac.put(_write(world[task]), json={"answers": _KEY})

        assert resp.status_code == 422, resp.text
        assert resp.json()["detail"]["code"] == code
        assert resp.json()["detail"]["details"]

    async def test_a_key_that_misses_and_invents_says_both(
        self,
        client: tuple[AsyncClient, Callable[[TenantContext], None]],
        queue: _CountingQueue,
        world: dict[str, uuid.UUID],
    ) -> None:
        """One refusal naming both halves: a renumbered question is one mistake."""
        ac, _ = client
        resp = await ac.put(
            _write(world["test_task"]), json={"answers": {"1": ["б"], "9": ["а"]}}
        )

        assert resp.status_code == 422
        detail = resp.json()["detail"]
        assert detail["code"] == "KEY_DOES_NOT_MATCH_QUESTIONS"
        assert "missing: ['2']" in detail["details"]
        assert "unknown: ['9']" in detail["details"]
        assert not queue.requests, "a refused key costs nothing"

    async def test_an_empty_answer_is_refused_at_the_boundary(
        self,
        client: tuple[AsyncClient, Callable[[TenantContext], None]],
        world: dict[str, uuid.UUID],
    ) -> None:
        """A question mapped to nothing answers nothing — 422 before the service."""
        ac, _ = client

        assert (
            await ac.put(_write(world["test_task"]), json={"answers": {}})
        ).status_code == 422
        assert (
            await ac.put(_write(world["test_task"]), json={"answers": {"1": []}})
        ).status_code == 422
        assert (
            await ac.put(_write(world["test_task"]), json={"answers": {"1": ["  "]}})
        ).status_code == 422


class TestWhoMayKnock:
    async def test_a_check_key_can_neither_read_nor_write_the_key(
        self,
        client: tuple[AsyncClient, Callable[[TenantContext], None]],
        world: dict[str, uuid.UUID],
    ) -> None:
        """The reference is the author's side; the submission channel never sees it."""
        ac, use_key = client
        use_key(_key_context(world["owner_id"], "check"))

        assert (await ac.get(_read(world["test_task"]))).status_code == 403
        assert (
            await ac.put(_write(world["test_task"]), json={"answers": _KEY})
        ).status_code == 403
        assert (await ac.delete(_write(world["test_task"]))).status_code == 403

    async def test_a_foreign_task_and_a_missing_one_answer_identically(
        self,
        client: tuple[AsyncClient, Callable[[TenantContext], None]],
        world: dict[str, uuid.UUID],
    ) -> None:
        """Byte-identical, or a valid key could enumerate another tenant's ids."""
        ac, use_key = client
        use_key(_key_context(world["stranger_id"], "prep"))
        missing = uuid.uuid4()

        foreign_read = await ac.get(_read(world["test_task"]))
        missing_read = await ac.get(_read(missing))
        foreign_write = await ac.put(_write(world["test_task"]), json={"answers": _KEY})
        missing_write = await ac.put(_write(missing), json={"answers": _KEY})
        foreign_delete = await ac.delete(_write(world["test_task"]))
        missing_delete = await ac.delete(_write(missing))

        for foreign, absent in (
            (foreign_read, missing_read),
            (foreign_write, missing_write),
            (foreign_delete, missing_delete),
        ):
            assert foreign.status_code == absent.status_code == 404
            assert foreign.content == absent.content

    async def test_the_404_is_the_one_the_document_routes_give(
        self,
        client: tuple[AsyncClient, Callable[[TenantContext], None]],
        world: dict[str, uuid.UUID],
    ) -> None:
        """Across routes, not only within them.

        A refusal that reads differently from the sibling route's is a refusal
        that says WHICH door was knocked on. The prose of the constant claimed
        this agreement before anything checked it, and claimed it wrongly — the
        string carried a trailing full stop the document route does not have.
        Hence a test rather than a sentence.
        """
        ac, _ = client
        missing = uuid.uuid4()

        ours = await ac.get(_read(missing))
        theirs = await ac.get(f"/api/v1/documents/{missing}")

        assert ours.status_code == theirs.status_code == 404
        assert ours.content, "the comparison is over a real body"
        assert ours.content == theirs.content
