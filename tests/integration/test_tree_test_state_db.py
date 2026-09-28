"""The author's tree marks each written test: draft, published, changed (live DB).

``GET /nodes/{id}/detail`` carries ``test_state`` on every document:

- ``null`` for a document that is not a test written in the system — a
  material, a test written as a file;
- ``draft`` for a test never published;
- ``published`` when the draft equals the latest version by the full digest;
- ``changed`` when it does not — the pass mark alone included, which the
  visible digest does not see.

The rule is the draft read's ``unpublished_changes``; a tree of tests in every
state is checked against it test by test. And the state is batched: the route
asks as many queries of a tree with one test as of a tree with five — one more
than of a tree with none.

Requires ``docker compose up -d``; run with ``--run-db``.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator, Generator
from dataclasses import dataclass
from typing import Any, Literal

import pytest
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import delete, event
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from course_supporter.api.app import app
from course_supporter.api.deps import get_current_tenant
from course_supporter.auth.context import TenantContext
from course_supporter.homework.test_object import (
    DraftBody,
    DraftOption,
    DraftQuestion,
    published_form,
    version_digests,
)
from course_supporter.storage.content_hash import compute_content_hash
from course_supporter.storage.database import get_session
from course_supporter.storage.orm import AuthoredDocument, CourseNode, Tenant
from course_supporter.storage.test_object_repository import TestObjectRepository
from tests._helpers.course_node_factory import make_root_course_node

pytestmark = pytest.mark.requires_db

_LANGUAGE = "ukr"
_DRAFT = DraftBody(
    pass_threshold=80,
    questions=(
        DraftQuestion(
            text="Перше?",
            options=(
                DraftOption(text="так", correct=True),
                DraftOption(text="ні", correct=False),
            ),
        ),
    ),
)

History = Literal["draft", "published", "pass-mark-edited", "question-edited"]
"""What happened to a test: never published; published as it stands; published
and then only its pass mark edited; published and then a question edited."""

_EXPECTED: dict[History, str] = {
    "draft": "draft",
    "published": "published",
    "pass-mark-edited": "changed",
    "question-edited": "changed",
}


@dataclass(frozen=True)
class Course:
    """A course root with one module below it; nothing in either yet."""

    tenant: uuid.UUID
    root: uuid.UUID
    module: uuid.UUID


@pytest.fixture()
async def course(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[Course]:
    async with session_factory() as session:
        tenant = Tenant(name=f"tree-state-{uuid.uuid4().hex[:8]}")
        session.add(tenant)
        await session.flush()
        root = make_root_course_node(
            tenant_id=tenant.id, title="Курс", order=0, default_language=_LANGUAGE
        )
        session.add(root)
        await session.flush()
        module = CourseNode(
            tenant_id=tenant.id, parent_id=root.id, title="Модуль", order=0
        )
        session.add(module)
        await session.commit()
        built = Course(tenant=tenant.id, root=root.id, module=module.id)

    yield built

    async with session_factory() as session:
        await session.execute(delete(Tenant).where(Tenant.id == built.tenant))
        await session.commit()


@pytest.fixture()
def routes(
    course: Course, session_factory: async_sessionmaker[AsyncSession]
) -> Generator[None]:
    """The app on the live database, with the author's key of the course."""

    async def _session() -> Any:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_current_tenant] = lambda: TenantContext(
        tenant_id=course.tenant,
        tenant_name="tree-state",
        scopes=["prep", "check"],
        plan_id="basic",
        key_prefix="cs_tree",
    )
    yield
    app.dependency_overrides.clear()


async def _get(path: str) -> Response:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        return await client.get(path)


async def _material(
    session_factory: async_sessionmaker[AsyncSession],
    course: Course,
    node: uuid.UUID,
    order: int,
    **fields: Any,
) -> uuid.UUID:
    async with session_factory() as session:
        document = AuthoredDocument(
            course_node_id=node, course_root_id=course.root, order=order, **fields
        )
        session.add(document)
        await session.commit()
        return document.id


async def _test(
    session_factory: async_sessionmaker[AsyncSession],
    course: Course,
    node: uuid.UUID,
    order: int,
    history: History,
) -> uuid.UUID:
    """A test written in the system with ``history`` behind it."""
    async with session_factory() as session:
        document = AuthoredDocument(
            course_node_id=node,
            course_root_id=course.root,
            source_type="test_object",
            source_url="test-object:",
            task_type="test",
            order=order,
            language=_LANGUAGE,
            content_hash=compute_content_hash(b"", []),
            title=f"Тест {order}",
        )
        session.add(document)
        await session.flush()
        repo = TestObjectRepository(session)
        await repo.replace_draft(document.id, _DRAFT.to_jsonb())
        if history != "draft":
            # A publication as the service makes it, without asking for
            # explanations.
            body = published_form(_DRAFT, _LANGUAGE)
            digests = version_digests(body, _LANGUAGE)
            await repo.publish(
                document.id,
                language=_LANGUAGE,
                body=body.to_jsonb(),
                content_digest=digests.content_digest,
                answers_digest=digests.answers_digest,
                publication_digest=digests.publication_digest,
            )
        if history == "pass-mark-edited":
            edited = _DRAFT.model_copy(update={"pass_threshold": 90})
            await repo.replace_draft(document.id, edited.to_jsonb())
        if history == "question-edited":
            question = _DRAFT.questions[0].model_copy(update={"text": "Друге?"})
            edited = _DRAFT.model_copy(update={"questions": (question,)})
            await repo.replace_draft(document.id, edited.to_jsonb())
        await session.commit()
        return document.id


def _states(node: dict[str, Any]) -> dict[str, str | None]:
    """Every document of the author's tree, at any depth: id → ``test_state``."""
    found = {item["id"]: item["test_state"] for item in node["authored_documents"]}
    for child in node["children"]:
        found |= _states(child)
    return found


async def _tree(node: uuid.UUID) -> dict[str, str | None]:
    detail = await _get(f"/api/v1/nodes/{node}/detail")
    assert detail.status_code == 200, detail.text
    return _states(detail.json())


class TestTheAuthorsTreeMarksItsTests:
    async def test_a_document_that_is_not_a_written_test_is_null(
        self,
        routes: None,
        course: Course,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """A material, and a test written as a file."""
        material = await _material(
            session_factory,
            course,
            course.root,
            0,
            source_type="web",
            source_url="https://example.test/lecture",
        )
        text_test = await _material(
            session_factory,
            course,
            course.root,
            1,
            source_type="text",
            source_url="https://example.test/test.md",
            filename="test.md",
            task_type="test",
        )

        states = await _tree(course.root)

        assert states == {str(material): None, str(text_test): None}

    @pytest.mark.parametrize("history", list(_EXPECTED))
    async def test_a_written_test_is_marked_by_its_history(
        self,
        routes: None,
        course: Course,
        session_factory: async_sessionmaker[AsyncSession],
        history: History,
    ) -> None:
        """Never published → draft; as published → published; edited → changed.

        ``pass-mark-edited`` is the case the visible digest cannot tell: the
        student would see the same test, yet a publication would be a new
        version.
        """
        test = await _test(session_factory, course, course.root, 0, history)

        states = await _tree(course.root)

        assert states == {str(test): _EXPECTED[history]}

    async def test_a_tree_of_tests_in_every_state_agrees_with_each_draft(
        self,
        routes: None,
        course: Course,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """At both levels, beside a material; each as its own draft read says."""
        material = await _material(
            session_factory,
            course,
            course.root,
            0,
            source_type="web",
            source_url="https://example.test/lecture",
        )
        placed: list[tuple[uuid.UUID, History]] = [
            (course.root, "draft"),
            (course.root, "published"),
            (course.module, "pass-mark-edited"),
            (course.module, "question-edited"),
            (course.module, "published"),
        ]
        tests = {
            await _test(session_factory, course, node, order, history): history
            for order, (node, history) in enumerate(placed, start=1)
        }

        states = await _tree(course.root)

        assert states == {str(material): None} | {
            str(test): _EXPECTED[history] for test, history in tests.items()
        }
        for test in tests:
            draft = await _get(f"/api/v1/tests/{test}/draft")
            assert draft.status_code == 200, draft.text
            unpublished = draft.json()["unpublished_changes"]
            assert (states[str(test)] == "published") is not unpublished

    async def test_a_subtree_below_the_root_is_marked_too(
        self,
        routes: None,
        course: Course,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Read from a module: the course language comes from the root anyway."""
        published = await _test(session_factory, course, course.module, 0, "published")
        edited = await _test(
            session_factory, course, course.module, 1, "pass-mark-edited"
        )

        states = await _tree(course.module)

        assert states == {str(published): "published", str(edited): "changed"}


class TestTheQueryCount:
    """The state is batched: a test more is not a query more."""

    @staticmethod
    async def _queries(engine: AsyncEngine, node: uuid.UUID) -> int:
        count = 0

        def _on_execute(*_: object) -> None:
            nonlocal count
            count += 1

        event.listen(engine.sync_engine, "before_cursor_execute", _on_execute)
        try:
            detail = await _get(f"/api/v1/nodes/{node}/detail")
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", _on_execute)
        assert detail.status_code == 200, detail.text
        return count

    async def test_one_test_and_five_ask_as_many_queries(
        self,
        routes: None,
        course: Course,
        session_factory: async_sessionmaker[AsyncSession],
        async_engine: AsyncEngine,
    ) -> None:
        """Three trees under one tenant: no test, one test, five in every state.

        Equal counts for one and five tests lock out a query per test; one
        query more than a tree without a test is the batch itself.
        """
        async with session_factory() as session:
            roots = [
                make_root_course_node(
                    tenant_id=course.tenant, title=f"Курс {n}", order=n + 1
                )
                for n in range(3)
            ]
            session.add_all(roots)
            await session.commit()
            none, one, five = (Course(course.tenant, r.id, r.id) for r in roots)

        await _material(
            session_factory,
            none,
            none.root,
            0,
            source_type="web",
            source_url="https://example.test/lecture",
        )
        await _test(session_factory, one, one.root, 0, "published")
        histories: list[History] = [
            "draft",
            "published",
            "pass-mark-edited",
            "question-edited",
            "published",
        ]
        for order, history in enumerate(histories):
            await _test(session_factory, five, five.root, order, history)

        q_none = await self._queries(async_engine, none.root)
        q_one = await self._queries(async_engine, one.root)
        q_five = await self._queries(async_engine, five.root)

        assert q_one == q_five, (q_none, q_one, q_five)
        assert q_one == q_none + 1, (q_none, q_one, q_five)
