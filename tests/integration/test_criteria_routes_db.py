"""The author's three criteria routes, against a live database (task 08, K5).

Nothing is called directly: every assertion goes through HTTP, with a key
context carrying exactly the scopes the route requires, as the key's routes
are tested (``test_reference_routes_db.py``). No model is called either — a
route composes nothing (``TASK.md`` section 9, decision 1) — so a model's list
is seeded as a composition stores it: claimed, then marked ready.

The locks of ``TASK.md`` section 5 for the routes: the layers are read apart;
a replacement keeps the identifiers of what it edits; a reset is soft; an edit
of an earlier version is not in force; a foreign task is the same 404 as a
missing one; a test is refused with its code; a task without a list awaits the
first submission.

Requires ``docker compose up -d``; run with ``--run-db``.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator, Callable
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from course_supporter.api.app import app
from course_supporter.api.deps import get_current_tenant
from course_supporter.auth.context import TenantContext
from course_supporter.storage.database import get_session
from course_supporter.storage.orm import (
    AuthoredDocument,
    CourseNode,
    NodeSummaryFinal,
    TaskCriteriaOverride,
    Tenant,
)
from course_supporter.storage.task_criteria_list_repository import (
    TaskCriteriaListRepository,
)
from tests._helpers.course_node_factory import make_root_course_node

pytestmark = pytest.mark.requires_db

_HASH = "1" * 64
_NEW_HASH = "2" * 64

_MODEL: list[dict[str, Any]] = [
    {
        "id": "c1",
        "text": "Handles n = 0",
        "evidence": "factorial(0) returns 1",
        "weight": "must",
        "check_method": "model_verdict",
        "soft_descent": False,
        "concepts": ["Base Case"],
        "mandatory_points": [],
    },
    {
        "id": "c2",
        "text": "Uses recursion",
        "evidence": "the function calls itself",
        "weight": "should",
        "check_method": "mandatory_points",
        "soft_descent": False,
        "concepts": ["Recursion"],
        "mandatory_points": [
            {"id": "c2.p1", "text": "a recursive call"},
            {"id": "c2.p2", "text": "a smaller argument each time"},
        ],
    },
    {
        "id": "c3",
        "text": "Readable names",
        "evidence": "names say what they hold",
        "weight": "may",
        "check_method": "model_verdict",
        "soft_descent": False,
        "concepts": [],
        "mandatory_points": [],
    },
]
_CONTRADICTIONS = ["The node teaches loops; the task asks for recursion."]
_NODE_CONCEPTS = ["Recursion", "Base Case"]
_ROOT_CONCEPTS = ["Functions"]
_COURSE_CONCEPTS = [*_NODE_CONCEPTS, *_ROOT_CONCEPTS]
# The very sentence of decision 1, not the module's constant: a changed text
# must turn this red rather than travel along.
_AWAITING_MESSAGE = (
    "Перелік критеріїв буде складено після першої подачі роботи студентом на "
    "перевірку, після цього його можна поправити."
)


def _key_context(tenant_id: uuid.UUID, *scopes: str) -> TenantContext:
    """A key context carrying exactly the scopes named — no more."""
    return TenantContext(
        tenant_id=tenant_id,
        tenant_name="criteria-routes",
        scopes=list(scopes),
        plan_id="basic",
        key_prefix="cs_criteria",
    )


def _edited(criterion: dict[str, Any], **changes: Any) -> dict[str, Any]:
    """A stored criterion as the author sends it back: no ``soft_descent``."""
    sent = {key: value for key, value in criterion.items() if key != "soft_descent"}
    sent.update(changes)
    return sent


def _new(**fields: Any) -> dict[str, Any]:
    """A criterion the author adds: no ``id``."""
    criterion: dict[str, Any] = {
        "text": "Explains the base case",
        "evidence": "a comment says why n = 0 stops",
        "weight": "should",
        "check_method": "model_verdict",
    }
    criterion.update(fields)
    return criterion


async def _make_task(
    session: AsyncSession,
    node: CourseNode,
    root: CourseNode,
    *,
    task_type: str | None = "task",
    content_hash: str | None = _HASH,
) -> AuthoredDocument:
    document = AuthoredDocument(
        course_node_id=node.id,
        course_root_id=root.id,
        source_type="web",
        source_url="https://example.com/task",
        task_type=task_type,
        language="ukr",
        content_hash=content_hash,
    )
    session.add(document)
    await session.flush()
    return document


@pytest.fixture()
async def world(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[dict[str, uuid.UUID]]:
    """A course of one tenant with tasks of every kind, and a stranger."""
    async with session_factory() as session:
        owner = Tenant(name=f"owner-{uuid.uuid4().hex[:8]}")
        stranger = Tenant(name=f"stranger-{uuid.uuid4().hex[:8]}")
        session.add_all([owner, stranger])
        await session.flush()
        root = make_root_course_node(tenant_id=owner.id, title="Python", order=0)
        session.add(root)
        await session.flush()
        node = CourseNode(
            tenant_id=owner.id, title="Recursion", order=0, parent_id=root.id
        )
        session.add(node)
        await session.flush()
        session.add_all(
            [
                NodeSummaryFinal(course_node_id=node.id, main_concepts=_NODE_CONCEPTS),
                NodeSummaryFinal(course_node_id=root.id, main_concepts=_ROOT_CONCEPTS),
            ]
        )
        text_task = await _make_task(session, node, root)
        project_task = await _make_task(session, node, root, task_type="project")
        test_task = await _make_task(session, node, root, task_type="test")
        material = await _make_task(session, node, root, task_type=None)
        unprocessed = await _make_task(session, node, root, content_hash=None)
        await session.commit()
        ids = {
            "owner_id": owner.id,
            "stranger_id": stranger.id,
            "text_task": text_task.id,
            "project_task": project_task.id,
            "test_task": test_task.id,
            "material": material.id,
            "unprocessed": unprocessed.id,
        }

    yield ids

    async with session_factory() as session:
        # Nodes, tasks, their lists, edits and summaries go with the tenant.
        for tenant_id in (ids["owner_id"], ids["stranger_id"]):
            await session.execute(delete(Tenant).where(Tenant.id == tenant_id))
        await session.commit()


@pytest.fixture()
async def client(
    world: dict[str, uuid.UUID],
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[tuple[AsyncClient, Callable[[TenantContext], None]]]:
    """A live client and a switch of the key context it knocks with."""

    async def _yield_session() -> AsyncGenerator[AsyncSession]:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = _yield_session
    app.dependency_overrides[get_current_tenant] = lambda: _key_context(
        world["owner_id"], "prep"
    )

    def use_key(ctx: TenantContext) -> None:
        app.dependency_overrides[get_current_tenant] = lambda: ctx

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac, use_key

    app.dependency_overrides.clear()


def _read(document_id: uuid.UUID) -> str:
    return f"/api/v1/documents/{document_id}/criteria"


def _write(document_id: uuid.UUID) -> str:
    return f"/api/v1/documents/{document_id}/criteria/override"


async def _compose(
    session_factory: async_sessionmaker[AsyncSession],
    task_id: uuid.UUID,
    criteria: list[dict[str, Any]] = _MODEL,
    *,
    content_hash: str = _HASH,
    task_type: str = "task",
) -> uuid.UUID:
    """A model's list, stored as a composition stores it: claimed, then ready."""
    async with session_factory() as session:
        repo = TaskCriteriaListRepository(session)
        live = await repo.get_live(task_id)
        if live is not None:
            await repo.release(live.id)
        row = await repo.claim(
            authored_document_id=task_id,
            source_content_hash=content_hash,
            source_task_type=task_type,
            form_version=2,
        )
        assert row is not None
        assert await repo.mark_ready(
            row.id,
            claimed_at=row.claimed_at,
            criteria=criteria,
            contradictions=_CONTRADICTIONS,
            concepts_in_input=True,
            dropped_concept_count=0,
            input_fingerprint="f" * 64,
        )
        await session.commit()
        return row.id


async def _edits(
    session_factory: async_sessionmaker[AsyncSession], task_id: uuid.UUID
) -> list[TaskCriteriaOverride]:
    """Every edit row of the task, live and snapshots, oldest first."""
    async with session_factory() as session:
        result = await session.execute(
            select(TaskCriteriaOverride)
            .where(TaskCriteriaOverride.authored_document_id == task_id)
            .order_by(TaskCriteriaOverride.created_at, TaskCriteriaOverride.id)
        )
        return list(result.scalars())


def _ids(criteria: list[dict[str, Any]]) -> list[str]:
    return [criterion["id"] for criterion in criteria]


class TestAwaitingTheFirstSubmission:
    async def test_a_task_without_a_list_awaits_the_first_submission(
        self,
        client: tuple[AsyncClient, Callable[[TenantContext], None]],
        world: dict[str, uuid.UUID],
    ) -> None:
        """200, not 404: no route composes a list — a submission does."""
        ac, _ = client
        resp = await ac.get(_read(world["text_task"]))

        assert resp.status_code == 200, resp.text
        assert resp.json() == {
            "status": "awaiting_first_submission",
            "message": _AWAITING_MESSAGE,
            "model": None,
            "author": None,
            "in_force": None,
            "contradictions": [],
            "concepts": _COURSE_CONCEPTS,
        }

    async def test_a_list_being_composed_is_still_awaited(
        self,
        client: tuple[AsyncClient, Callable[[TenantContext], None]],
        world: dict[str, uuid.UUID],
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        async with session_factory() as session:
            claim = await TaskCriteriaListRepository(session).claim(
                authored_document_id=world["text_task"],
                source_content_hash=_HASH,
                source_task_type="task",
                form_version=2,
            )
            assert claim is not None
            await session.commit()
        ac, _ = client

        body = (await ac.get(_read(world["text_task"]))).json()

        assert body["status"] == "awaiting_first_submission"
        assert body["model"] is None
        assert body["in_force"] is None

    async def test_an_edit_before_the_first_list_is_refused_with_its_code(
        self,
        client: tuple[AsyncClient, Callable[[TenantContext], None]],
        world: dict[str, uuid.UUID],
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        ac, _ = client
        resp = await ac.put(_write(world["text_task"]), json={"criteria": [_new()]})

        assert resp.status_code == 422, resp.text
        assert resp.json()["detail"]["code"] == "AWAITING_FIRST_SUBMISSION"
        assert resp.json()["detail"]["details"]
        assert await _edits(session_factory, world["text_task"]) == []


class TestTheLayersAreReadApart:
    async def test_the_models_list_alone_is_in_force(
        self,
        client: tuple[AsyncClient, Callable[[TenantContext], None]],
        world: dict[str, uuid.UUID],
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await _compose(session_factory, world["text_task"])
        ac, _ = client

        resp = await ac.get(_read(world["text_task"]))

        assert resp.status_code == 200, resp.text
        assert resp.json() == {
            "status": "ready",
            "message": None,
            "model": _MODEL,
            "author": None,
            "in_force": {"layer": "model", "criteria": _MODEL},
            "contradictions": _CONTRADICTIONS,
            "concepts": _COURSE_CONCEPTS,
        }

    async def test_an_edit_is_shown_beside_the_models_list_not_over_it(
        self,
        client: tuple[AsyncClient, Callable[[TenantContext], None]],
        world: dict[str, uuid.UUID],
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Each layer as stored; the one in force named, not merged into both."""
        await _compose(session_factory, world["text_task"])
        ac, _ = client
        put = await ac.put(
            _write(world["text_task"]),
            json={"criteria": [_edited(_MODEL[0], text="Handles n = 0 and n = 1")]},
        )
        assert put.status_code == 200, put.text

        body = (await ac.get(_read(world["text_task"]))).json()

        edit = [{**_MODEL[0], "text": "Handles n = 0 and n = 1"}]
        assert body["status"] == "ready"
        assert body["model"] == _MODEL
        assert body["author"] == edit
        assert body["in_force"] == {"layer": "author", "criteria": edit}
        assert body["contradictions"] == _CONTRADICTIONS
        assert put.json() == body


class TestAReplacementKeepsIdentifiers:
    async def test_what_is_edited_keeps_its_id_and_what_is_new_gets_one(
        self,
        client: tuple[AsyncClient, Callable[[TenantContext], None]],
        world: dict[str, uuid.UUID],
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await _compose(session_factory, world["text_task"])
        ac, _ = client

        resp = await ac.put(
            _write(world["text_task"]),
            json={
                "criteria": [
                    _edited(_MODEL[2], text="Names say what they hold"),
                    _new(),
                    _edited(_MODEL[0], weight="may"),
                ]
            },
        )

        assert resp.status_code == 200, resp.text
        author = resp.json()["author"]
        # c2 is gone from the edit, yet the new criterion does not take its id.
        assert _ids(author) == ["c3", "c4", "c1"]
        assert author[0]["text"] == "Names say what they hold"
        assert author[2]["weight"] == "may"
        assert resp.json()["in_force"] == {"layer": "author", "criteria": author}
        assert (await ac.get(_read(world["text_task"]))).json()["author"] == author

    async def test_a_kept_point_keeps_its_id_and_a_new_one_follows(
        self,
        client: tuple[AsyncClient, Callable[[TenantContext], None]],
        world: dict[str, uuid.UUID],
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await _compose(session_factory, world["text_task"])
        ac, _ = client
        points = [
            {"id": "c2.p2", "text": "a smaller argument"},
            {"text": "no loop at all"},
        ]

        resp = await ac.put(
            _write(world["text_task"]),
            json={"criteria": [_edited(_MODEL[1], mandatory_points=points)]},
        )

        assert resp.status_code == 200, resp.text
        [criterion] = resp.json()["author"]
        assert criterion["id"] == "c2"
        assert criterion["mandatory_points"] == [
            {"id": "c2.p2", "text": "a smaller argument"},
            {"id": "c2.p3", "text": "no loop at all"},
        ]

    async def test_a_new_criterion_never_takes_an_id_the_model_gave(
        self,
        client: tuple[AsyncClient, Callable[[TenantContext], None]],
        world: dict[str, uuid.UUID],
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Even when the edit it is made on no longer has that criterion."""
        await _compose(session_factory, world["text_task"])
        ac, _ = client
        first = await ac.put(
            _write(world["text_task"]),
            json={"criteria": [_edited(_MODEL[0]), _edited(_MODEL[1])]},
        )
        assert _ids(first.json()["in_force"]["criteria"]) == ["c1", "c2"]

        second = await ac.put(
            _write(world["text_task"]),
            json={"criteria": [_edited(_MODEL[0]), _edited(_MODEL[1]), _new()]},
        )

        assert second.status_code == 200, second.text
        assert _ids(second.json()["author"]) == ["c1", "c2", "c4"]

    async def test_an_id_the_list_does_not_have_is_refused_with_its_code(
        self,
        client: tuple[AsyncClient, Callable[[TenantContext], None]],
        world: dict[str, uuid.UUID],
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await _compose(session_factory, world["text_task"])
        ac, _ = client
        resp = await ac.put(
            _write(world["text_task"]),
            json={"criteria": [_edited(_MODEL[0], id="c9")]},
        )

        assert resp.status_code == 422, resp.text
        assert resp.json()["detail"]["code"] == "UNKNOWN_CRITERION_ID"
        assert "c9" in resp.json()["detail"]["details"]
        assert await _edits(session_factory, world["text_task"]) == []

    async def test_every_replacement_leaves_the_edit_it_replaced_as_a_snapshot(
        self,
        client: tuple[AsyncClient, Callable[[TenantContext], None]],
        world: dict[str, uuid.UUID],
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await _compose(session_factory, world["text_task"])
        ac, _ = client
        for text in ("First wording", "Second wording"):
            resp = await ac.put(
                _write(world["text_task"]),
                json={"criteria": [_edited(_MODEL[0], text=text)]},
            )
            assert resp.status_code == 200, resp.text

        first, second = await _edits(session_factory, world["text_task"])

        assert first.deleted_at is not None
        assert first.criteria[0]["text"] == "First wording"
        assert second.deleted_at is None
        assert second.criteria[0]["text"] == "Second wording"


class TestAResetIsSoft:
    async def test_the_edit_stays_as_a_snapshot_and_the_model_is_in_force(
        self,
        client: tuple[AsyncClient, Callable[[TenantContext], None]],
        world: dict[str, uuid.UUID],
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await _compose(session_factory, world["text_task"])
        ac, _ = client
        edit = [_edited(_MODEL[0], text="The author's wording")]
        assert (
            await ac.put(_write(world["text_task"]), json={"criteria": edit})
        ).status_code == 200

        reset = await ac.delete(_write(world["text_task"]))

        assert reset.status_code == 200, reset.text
        assert reset.json()["author"] is None
        assert reset.json()["in_force"] == {"layer": "model", "criteria": _MODEL}
        rows = await _edits(session_factory, world["text_task"])
        assert len(rows) == 1, "the edit is a snapshot, not gone"
        assert rows[0].deleted_at is not None
        assert rows[0].criteria[0]["text"] == "The author's wording"
        # Resetting again is not an error: the state asked for is the result.
        again = await ac.delete(_write(world["text_task"]))
        assert again.status_code == 200
        assert again.json() == reset.json()


class TestANewVersionOfTheTask:
    async def test_an_edit_of_an_earlier_version_is_neither_in_force_nor_shown(
        self,
        client: tuple[AsyncClient, Callable[[TenantContext], None]],
        world: dict[str, uuid.UUID],
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await _compose(session_factory, world["text_task"])
        ac, _ = client
        put = await ac.put(
            _write(world["text_task"]),
            json={"criteria": [_edited(_MODEL[0], text="Old version's wording")]},
        )
        assert put.json()["in_force"]["layer"] == "author"
        async with session_factory() as session:
            document = await session.get(AuthoredDocument, world["text_task"])
            assert document is not None
            document.content_hash = _NEW_HASH
            await session.commit()
        # The premise: the edit is still the live row — only its version keeps
        # it out of force.
        [live] = await _edits(session_factory, world["text_task"])
        assert live.deleted_at is None
        assert live.source_content_hash == _HASH

        stale = (await ac.get(_read(world["text_task"]))).json()
        await _compose(session_factory, world["text_task"], content_hash=_NEW_HASH)
        fresh = (await ac.get(_read(world["text_task"]))).json()

        assert stale["status"] == "awaiting_first_submission"
        assert stale["author"] is None
        assert stale["model"] is None
        assert stale["in_force"] is None
        assert fresh["author"] is None
        assert fresh["in_force"] == {"layer": "model", "criteria": _MODEL}

    async def test_an_edit_after_a_new_version_is_made_on_the_new_list(
        self,
        client: tuple[AsyncClient, Callable[[TenantContext], None]],
        world: dict[str, uuid.UUID],
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await _compose(session_factory, world["text_task"])
        ac, _ = client
        await ac.put(
            _write(world["text_task"]),
            json={"criteria": [_edited(_MODEL[0]), _edited(_MODEL[1])]},
        )
        async with session_factory() as session:
            document = await session.get(AuthoredDocument, world["text_task"])
            assert document is not None
            document.content_hash = _NEW_HASH
            await session.commit()
        await _compose(
            session_factory, world["text_task"], _MODEL[:1], content_hash=_NEW_HASH
        )

        resp = await ac.put(
            _write(world["text_task"]), json={"criteria": [_edited(_MODEL[0]), _new()]}
        )

        assert resp.status_code == 200, resp.text
        assert _ids(resp.json()["author"]) == ["c1", "c2"]
        old, new = await _edits(session_factory, world["text_task"])
        assert old.deleted_at is not None
        assert new.source_content_hash == _NEW_HASH


class TestTheFormOfAnEdit:
    async def test_code_test_is_refused_in_a_task_and_marked_in_a_project(
        self,
        client: tuple[AsyncClient, Callable[[TenantContext], None]],
        world: dict[str, uuid.UUID],
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await _compose(session_factory, world["text_task"])
        await _compose(session_factory, world["project_task"], task_type="project")
        ac, _ = client
        by_code = [_edited(_MODEL[0], check_method="code_test")]

        refused = await ac.put(_write(world["text_task"]), json={"criteria": by_code})
        taken = await ac.put(_write(world["project_task"]), json={"criteria": by_code})

        assert refused.status_code == 422, refused.text
        assert refused.json()["detail"]["code"] == "CHECK_METHOD_NOT_ADMITTED"
        assert await _edits(session_factory, world["text_task"]) == []
        assert taken.status_code == 200, taken.text
        [criterion] = taken.json()["author"]
        assert criterion["check_method"] == "code_test"
        assert criterion["soft_descent"] is True

    async def test_a_concept_outside_the_course_is_refused_with_its_code(
        self,
        client: tuple[AsyncClient, Callable[[TenantContext], None]],
        world: dict[str, uuid.UUID],
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await _compose(session_factory, world["text_task"])
        ac, _ = client
        resp = await ac.put(
            _write(world["text_task"]),
            json={"criteria": [_edited(_MODEL[0], concepts=["Monads"])]},
        )

        assert resp.status_code == 422, resp.text
        assert resp.json()["detail"]["code"] == "UNKNOWN_CONCEPT"
        assert "Monads" in resp.json()["detail"]["details"]
        assert await _edits(session_factory, world["text_task"]) == []

    async def test_a_concept_is_kept_in_the_courses_spelling(
        self,
        client: tuple[AsyncClient, Callable[[TenantContext], None]],
        world: dict[str, uuid.UUID],
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await _compose(session_factory, world["text_task"])
        ac, _ = client
        resp = await ac.put(
            _write(world["text_task"]),
            json={
                "criteria": [_edited(_MODEL[0], concepts=["base-cases", "function"])]
            },
        )

        assert resp.status_code == 200, resp.text
        assert resp.json()["author"][0]["concepts"] == ["Base Case", "Functions"]

    async def test_a_concept_the_list_names_beyond_the_course_can_be_sent_back(
        self,
        client: tuple[AsyncClient, Callable[[TenantContext], None]],
        world: dict[str, uuid.UUID],
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """The course's summaries may change after a list is composed."""
        await _compose(
            session_factory,
            world["text_task"],
            [{**_MODEL[0], "concepts": ["Base Case", "Loops"]}],
        )
        ac, _ = client
        read = (await ac.get(_read(world["text_task"]))).json()
        resp = await ac.put(
            _write(world["text_task"]),
            json={"criteria": [_edited(_MODEL[0], concepts=["Loops"])]},
        )

        assert read["concepts"] == [*_COURSE_CONCEPTS, "Loops"]
        assert resp.status_code == 200, resp.text
        assert resp.json()["author"][0]["concepts"] == ["Loops"]

    @pytest.mark.parametrize(
        "criteria",
        [
            pytest.param([], id="no-criteria"),
            pytest.param([_new()] * 61, id="61-criteria"),
            pytest.param([_new(text="x" * 601)], id="text-601"),
            pytest.param([{**_new(), "soft_descent": False}], id="soft-descent-sent"),
            pytest.param([_edited(_MODEL[0]), _edited(_MODEL[0])], id="id-twice"),
        ],
    )
    async def test_an_edit_outside_the_form_is_refused_at_the_boundary(
        self,
        client: tuple[AsyncClient, Callable[[TenantContext], None]],
        world: dict[str, uuid.UUID],
        session_factory: async_sessionmaker[AsyncSession],
        criteria: list[dict[str, Any]],
    ) -> None:
        await _compose(session_factory, world["text_task"])
        ac, _ = client
        resp = await ac.put(_write(world["text_task"]), json={"criteria": criteria})

        assert resp.status_code == 422, resp.text
        assert await _edits(session_factory, world["text_task"]) == []


class TestNotEveryDocumentHasCriteria:
    @pytest.mark.parametrize("method", ["get", "put", "delete"])
    @pytest.mark.parametrize("task", ["test_task", "material"])
    async def test_a_test_or_a_material_is_refused_with_its_code(
        self,
        client: tuple[AsyncClient, Callable[[TenantContext], None]],
        world: dict[str, uuid.UUID],
        session_factory: async_sessionmaker[AsyncSession],
        method: str,
        task: str,
    ) -> None:
        """A test is checked by its key; these routes do not serve it."""
        ac, _ = client
        resp = await _knock(ac, method, world[task])

        assert resp.status_code == 422, resp.text
        assert resp.json()["detail"]["code"] == "NOT_A_TEXT_TASK"
        assert resp.json()["detail"]["details"]
        assert await _edits(session_factory, world[task]) == []

    @pytest.mark.parametrize("method", ["get", "put", "delete"])
    async def test_a_task_not_processed_yet_is_refused_with_its_code(
        self,
        client: tuple[AsyncClient, Callable[[TenantContext], None]],
        world: dict[str, uuid.UUID],
        method: str,
    ) -> None:
        ac, _ = client
        resp = await _knock(ac, method, world["unprocessed"])

        assert resp.status_code == 422, resp.text
        assert resp.json()["detail"]["code"] == "TASK_NOT_READY"


class TestWhoMayKnock:
    async def test_a_check_key_can_neither_read_nor_write_the_criteria(
        self,
        client: tuple[AsyncClient, Callable[[TenantContext], None]],
        world: dict[str, uuid.UUID],
    ) -> None:
        """The criteria are the author's side; the submission channel is not."""
        ac, use_key = client
        use_key(_key_context(world["owner_id"], "check"))

        for method in ("get", "put", "delete"):
            resp = await _knock(ac, method, world["text_task"])
            assert resp.status_code == 403, (method, resp.text)

    async def test_a_foreign_task_and_a_missing_one_answer_identically(
        self,
        client: tuple[AsyncClient, Callable[[TenantContext], None]],
        world: dict[str, uuid.UUID],
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Byte-identical, or a valid key could enumerate another tenant's ids."""
        await _compose(session_factory, world["text_task"])
        ac, use_key = client
        use_key(_key_context(world["stranger_id"], "prep"))
        missing = uuid.uuid4()

        for method in ("get", "put", "delete"):
            foreign = await _knock(ac, method, world["text_task"])
            absent = await _knock(ac, method, missing)
            assert foreign.status_code == absent.status_code == 404, method
            assert foreign.content == absent.content, method
        assert await _edits(session_factory, world["text_task"]) == []

    async def test_the_404_is_the_one_the_document_routes_give(
        self,
        client: tuple[AsyncClient, Callable[[TenantContext], None]],
    ) -> None:
        """Across routes too: a refusal must not say which door was knocked on."""
        ac, _ = client
        missing = uuid.uuid4()

        ours = await ac.get(_read(missing))
        theirs = await ac.get(f"/api/v1/documents/{missing}")

        assert ours.status_code == theirs.status_code == 404
        assert ours.content, "the comparison is over a real body"
        assert ours.content == theirs.content


async def _knock(ac: AsyncClient, method: str, document_id: uuid.UUID) -> Response:
    """One of the three routes; a PUT carries a well-formed edit."""
    if method == "get":
        return await ac.get(_read(document_id))
    if method == "put":
        return await ac.put(
            _write(document_id), json={"criteria": [_edited(_MODEL[0])]}
        )
    return await ac.delete(_write(document_id))
