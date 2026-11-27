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

Task 09b adds the reading of a task without a list — being composed, or not
composed and why, with how many students' works wait and what the author can
do — and the second entry of the continuation: a write that leaves a list in
force puts the waiting works back in the queue, once each (lock 6).

Requires ``docker compose up -d``; run with ``--run-db``.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator, Callable, Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from course_supporter.api.app import app
from course_supporter.api.deps import get_arq_redis, get_current_tenant
from course_supporter.auth.context import TenantContext
from course_supporter.billing import admin, charging, ledger
from course_supporter.billing.accounts import get_or_create_author_account
from course_supporter.homework.criteria_edit_service import (
    AWAITING_FIRST_SUBMISSION_MESSAGE,
    CriteriaReasonCode,
)
from course_supporter.homework.criteria_form import (
    CriteriaComposition,
    CriterionDraft,
    compose_criteria,
)
from course_supporter.homework.criteria_list_service import (
    CLAIM_SILENCE_LIMIT,
    CriteriaListService,
)
from course_supporter.homework.path_config import (
    PathConfig,
    ServedBy,
    load_path_config,
)
from course_supporter.jobs.job_type import JOB_SUBJECT_TYPE, JobType
from course_supporter.llm.error_categories import LadderExhaustedError, LadderStop
from course_supporter.models.source import AssignmentType
from course_supporter.storage.database import get_session
from course_supporter.storage.document_summary_repository import (
    DocumentSummaryRepository,
)
from course_supporter.storage.orm import (
    AuthoredDocument,
    CourseNode,
    DocumentSegment,
    HomeworkSubmission,
    Job,
    NodeSummaryFinal,
    Student,
    TaskCriteriaOverride,
    Tenant,
)
from course_supporter.storage.task_criteria_list_repository import (
    TaskCriteriaListRepository,
)
from course_supporter.workers.criteria_compose import arq_compose_criteria
from tests._helpers.billing_cleanup import drop_billing_of_tenant
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
    "перевірку, після цього його можна поправити. Ви можете скласти його й "
    "зараз, не чекаючи першої подачі."
)
# The sentences of task 09b, written out here for the same reason.
_COMPOSING_MESSAGE = (
    "Перелік критеріїв складається зараз. Роботи студентів, що чекають на нього, "
    "буде перевірено, щойно він буде готовий; після цього перелік можна поправити."
)
# Approved verbatim by the operator and vision-side (2026-10-02).
_ADVICE: dict[CriteriaReasonCode, str] = {
    CriteriaReasonCode.TASK_NOT_PROCESSED: (
        "Перелік критеріїв ще не складено: обробку тексту завдання не завершено. Якщо "
        "обробка ще триває — дочекайтеся її завершення; якщо вона завершилася з "
        "помилкою — запустіть її знову. Після цього перелік складеться автоматично, "
        "щойно хтось зі студентів подасть роботу на це завдання, і тоді ж буде "
        "перевірено роботи, що чекають. Якщо нових робіт найближчим часом не буде — "
        "зверніться до адміністратора системи, і він запустить повторну спробу."
    ),
    CriteriaReasonCode.MODELS_UNAVAILABLE: (
        "Перелік критеріїв не вдалося скласти: сервіс, що його складає, був тимчасово "
        "недоступний. Від вас нічого не потрібно: повторна спроба відбудеться "
        "автоматично, щойно хтось зі студентів подасть роботу на це завдання, і тоді ж "
        "буде перевірено роботи, що чекають. Якщо нових робіт найближчим часом не буде "
        "або стан не зміниться — зверніться до адміністратора системи."
    ),
    CriteriaReasonCode.LIMIT_REACHED: (
        "Перелік критеріїв не вдалося скласти: завдання завелике для обмежень, що "
        "зараз діють у системі. Зверніться до адміністратора системи: після зміни "
        "обмежень він запустить повторну спробу, і тоді ж буде перевірено роботи, що "
        "чекають. Не правте заради цього текст завдання: правка створить нову версію "
        "завдання й не допоможе роботам, що вже чекають."
    ),
    CriteriaReasonCode.COMPOSITION_ERROR: (
        "Перелік критеріїв не вдалося скласти через внутрішню помилку системи. "
        "Зверніться до адміністратора системи: після виправлення він запустить "
        "повторну спробу, і тоді ж буде перевірено роботи, що чекають."
    ),
    CriteriaReasonCode.NOT_ATTEMPTED: (
        "Перелік критеріїв для поточної версії завдання ще не складався, а роботи "
        "студентів уже чекають на нього. Він складеться автоматично, щойно хтось зі "
        "студентів подасть роботу на це завдання, і тоді ж буде перевірено роботи, що "
        "чекають. Якщо нових робіт найближчим часом не буде — зверніться до "
        "адміністратора системи, і він запустить повторну спробу."
    ),
}


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
            "root": root.id,
            "node": node.id,
            "text_task": text_task.id,
            "project_task": project_task.id,
            "test_task": test_task.id,
            "material": material.id,
            "unprocessed": unprocessed.id,
        }

    yield ids

    async with session_factory() as session:
        # Jobs outlive their tenant (SET NULL), so they go first, after the
        # works that point at them.
        await session.execute(
            delete(HomeworkSubmission).where(
                HomeworkSubmission.tenant_id == ids["owner_id"]
            )
        )
        await session.commit()
    await drop_billing_of_tenant(session_factory, ids["owner_id"])
    async with session_factory() as session:
        await session.execute(delete(Job).where(Job.tenant_id == ids["owner_id"]))
        # Nodes, tasks, their lists, edits and summaries go with the tenant.
        for tenant_id in (ids["owner_id"], ids["stranger_id"]):
            await session.execute(delete(Tenant).where(Tenant.id == tenant_id))
        await session.commit()


@pytest.fixture()
def queue() -> MagicMock:
    """The queue double the write routes put waiting works back in."""
    arq = MagicMock()
    arq.enqueue_job = AsyncMock(return_value=MagicMock(job_id="arq-criteria"))
    return arq


@pytest.fixture()
def task_on_the_new_path() -> Iterator[None]:
    """The shipped configuration with ``task`` switched to the new path.

    A continuation of a type left to today's Mentor lifts the hold with its
    job (decided 2026-10-02); these tests are about the new path's.
    """
    shipped = load_path_config(
        Path(__file__).resolve().parents[2] / "config" / "submission_paths.yaml"
    )
    task_types = dict(shipped.task_types)
    task_types[AssignmentType.TASK] = task_types[AssignmentType.TASK].model_copy(
        update={"served_by": ServedBy.NEW_PATH}
    )
    config: PathConfig = shipped.model_copy(update={"task_types": task_types})
    with patch(
        "course_supporter.homework.path_continuation.get_path_config",
        return_value=config,
    ):
        yield


@pytest.fixture()
async def client(
    world: dict[str, uuid.UUID],
    session_factory: async_sessionmaker[AsyncSession],
    queue: MagicMock,
) -> AsyncGenerator[tuple[AsyncClient, Callable[[TenantContext], None]]]:
    """A live client and a switch of the key context it knocks with."""

    async def _yield_session() -> AsyncGenerator[AsyncSession]:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = _yield_session
    app.dependency_overrides[get_arq_redis] = lambda: queue
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


def _compose_path(document_id: uuid.UUID) -> str:
    return f"/api/v1/documents/{document_id}/criteria/compose"


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
            "reason_code": None,
            "waiting_submissions": 0,
            "message": _AWAITING_MESSAGE,
            "model": None,
            "author": None,
            "in_force": None,
            "contradictions": [],
            "concepts": _COURSE_CONCEPTS,
        }

    async def test_a_list_being_composed_is_said_to_be_composed(
        self,
        client: tuple[AsyncClient, Callable[[TenantContext], None]],
        world: dict[str, uuid.UUID],
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Not "after the first submission": one has come, and it is composing."""
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

        assert body["status"] == "composing"
        assert body["message"] == _COMPOSING_MESSAGE
        assert body["reason_code"] is None
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
            "reason_code": None,
            "waiting_submissions": 0,
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

        # The model's "must", kept by an author who corrected a word: not
        # made mandatory (2026-11-17).
        edit = [
            {**_MODEL[0], "text": "Handles n = 0 and n = 1", "must_by_author": False}
        ]
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


class TestMustByAuthor:
    """The author's own "must" (2026-11-17), through the routes.

    The server settles ``must_by_author`` once, when the edit is stored, and
    every answer shows it on the author's criteria; the model's list has none.
    """

    async def test_the_answers_carry_the_mark_on_the_authors_criteria_alone(
        self,
        client: tuple[AsyncClient, Callable[[TenantContext], None]],
        world: dict[str, uuid.UUID],
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await _compose(session_factory, world["text_task"])
        ac, _ = client

        put = await ac.put(
            _write(world["text_task"]),
            json={
                "criteria": [
                    _edited(_MODEL[0]),
                    _edited(_MODEL[2], weight="must"),
                    _new(weight="must"),
                    _new(weight="must", must_by_author=False),
                ]
            },
        )

        assert put.status_code == 200, put.text
        body = put.json()
        assert [c["must_by_author"] for c in body["author"]] == [
            False,
            True,
            True,
            False,
        ]
        assert body["in_force"] == {"layer": "author", "criteria": body["author"]}
        assert all("must_by_author" not in c for c in body["model"])
        assert (await ac.get(_read(world["text_task"]))).json() == body
        [row] = await _edits(session_factory, world["text_task"])
        assert [c["must_by_author"] for c in row.criteria] == [False, True, True, False]

    async def test_the_models_list_in_force_has_no_mark(
        self,
        client: tuple[AsyncClient, Callable[[TenantContext], None]],
        world: dict[str, uuid.UUID],
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await _compose(session_factory, world["text_task"])
        ac, _ = client

        body = (await ac.get(_read(world["text_task"]))).json()

        assert body["in_force"]["layer"] == "model"
        assert all("must_by_author" not in c for c in body["in_force"]["criteria"])

    async def test_true_on_a_criterion_that_is_not_a_must_is_refused_with_its_code(
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
                    _edited(_MODEL[0]),
                    _edited(_MODEL[2], must_by_author=True),
                ]
            },
        )

        assert resp.status_code == 422, resp.text
        assert resp.json()["detail"]["code"] == "MUST_BY_AUTHOR_WITHOUT_MUST"
        assert "criteria.1" in resp.json()["detail"]["details"]
        assert await _edits(session_factory, world["text_task"]) == []

    async def test_a_second_edit_without_the_field_keeps_the_marks(
        self,
        client: tuple[AsyncClient, Callable[[TenantContext], None]],
        world: dict[str, uuid.UUID],
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await _compose(session_factory, world["text_task"])
        ac, _ = client
        first = await ac.put(
            _write(world["text_task"]),
            json={
                "criteria": [
                    _edited(_MODEL[0], must_by_author=True),
                    _edited(_MODEL[1]),
                ]
            },
        )
        assert first.status_code == 200, first.text

        second = await ac.put(
            _write(world["text_task"]),
            json={
                "criteria": [
                    _edited(_MODEL[0], text="Handles n = 0 and n = 1"),
                    _edited(_MODEL[1]),
                ]
            },
        )

        assert second.status_code == 200, second.text
        assert [c["must_by_author"] for c in second.json()["author"]] == [True, False]

    async def test_an_edit_after_a_reset_inherits_no_mark(
        self,
        client: tuple[AsyncClient, Callable[[TenantContext], None]],
        world: dict[str, uuid.UUID],
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await _compose(session_factory, world["text_task"])
        ac, _ = client
        marked = await ac.put(
            _write(world["text_task"]),
            json={"criteria": [_edited(_MODEL[0], must_by_author=True)]},
        )
        assert marked.json()["author"][0]["must_by_author"] is True
        assert (await ac.delete(_write(world["text_task"]))).status_code == 200

        after = await ac.put(
            _write(world["text_task"]), json={"criteria": [_edited(_MODEL[0])]}
        )

        assert after.status_code == 200, after.text
        assert after.json()["author"][0]["must_by_author"] is False

    async def test_an_edit_stored_before_the_mark_reads_false_and_is_not_rewritten(
        self,
        client: tuple[AsyncClient, Callable[[TenantContext], None]],
        world: dict[str, uuid.UUID],
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await _compose(session_factory, world["text_task"])
        async with session_factory() as session:
            session.add(
                TaskCriteriaOverride(
                    authored_document_id=world["text_task"],
                    source_content_hash=_HASH,
                    source_task_type="task",
                    criteria=_MODEL,
                )
            )
            await session.commit()
        ac, _ = client

        body = (await ac.get(_read(world["text_task"]))).json()

        assert [c["must_by_author"] for c in body["author"]] == [False, False, False]
        assert body["in_force"]["layer"] == "author"
        [row] = await _edits(session_factory, world["text_task"])
        assert row.criteria == _MODEL


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

        for method in ("get", "put", "delete", "post"):
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
    """One of the four routes; a PUT carries a well-formed edit."""
    if method == "get":
        return await ac.get(_read(document_id))
    if method == "post":
        return await ac.post(_compose_path(document_id))
    if method == "put":
        return await ac.put(
            _write(document_id), json={"criteria": [_edited(_MODEL[0])]}
        )
    return await ac.delete(_write(document_id))


# ── Task 09b: why there is no list, and the works that wait for one ───


async def _waiting(
    session_factory: async_sessionmaker[AsyncSession],
    world: dict[str, uuid.UUID],
    task_id: uuid.UUID,
    *,
    count: int = 1,
    status: str = "awaiting_criteria",
    deleted: bool = False,
) -> list[uuid.UUID]:
    """Students' works of the task, held — by default — for its criteria list."""
    async with session_factory() as session:
        ids: list[uuid.UUID] = []
        for _ in range(count):
            student = Student(
                tenant_id=world["owner_id"], external_id=f"s-{uuid.uuid4().hex[:8]}"
            )
            session.add(student)
            await session.flush()
            submission = HomeworkSubmission(
                tenant_id=world["owner_id"],
                student_id=student.id,
                course_node_id=world["node"],
                node_id=world["node"],
                authored_document_id=task_id,
                file_url="s3://bucket/work.py",
                file_type="text/plain",
                original_filename="work.py",
                status=status,
            )
            session.add(submission)
            await session.flush()
            if deleted:
                await session.execute(
                    update(HomeworkSubmission)
                    .where(HomeworkSubmission.id == submission.id)
                    .values(deleted_at=submission.created_at)
                )
            ids.append(submission.id)
        await session.commit()
        return ids


async def _failed_attempt(
    session_factory: async_sessionmaker[AsyncSession],
    task_id: uuid.UUID,
    failure_reason: str,
    *,
    content_hash: str = _HASH,
) -> None:
    """A composition of the version that gave up, as the service records one."""
    async with session_factory() as session:
        repo = TaskCriteriaListRepository(session)
        row = await repo.claim(
            authored_document_id=task_id,
            source_content_hash=content_hash,
            source_task_type="task",
            form_version=2,
        )
        assert row is not None
        assert await repo.mark_failed(
            row.id, claimed_at=row.claimed_at, failure_reason=failure_reason
        )
        await session.commit()


async def _ready_summary(
    session_factory: async_sessionmaker[AsyncSession], task_id: uuid.UUID
) -> None:
    """The task's text processed: the one thing a composition needs first."""
    async with session_factory() as session:
        await DocumentSummaryRepository(session).create(
            authored_document_id=task_id,
            title="The Task",
            description="Task description.",
            main_concepts=[],
            secondary_concepts=[],
            content_char_count=10,
        )
        await session.commit()


async def _jobs_of(
    session_factory: async_sessionmaker[AsyncSession], submission_id: uuid.UUID
) -> list[Job]:
    async with session_factory() as session:
        rows = await session.execute(select(Job).where(Job.subject_id == submission_id))
        return list(rows.scalars())


class TestWhyThereIsNoList:
    """Task 09b, ``PRE-FLIGHT.md`` 9.2: the state, the waiting works, the advice."""

    @pytest.mark.parametrize(
        ("failure_reason", "code"),
        [
            ("exhausted: every rung had a bad minute", "models_unavailable"),
            ("output_ceiling: the answer was cut", "limit_reached"),
            ("money_ceiling: no rung could pay", "limit_reached"),
            ("error: KeyError", "composition_error"),
        ],
    )
    async def test_a_failed_composition_says_why_and_what_to_do(
        self,
        client: tuple[AsyncClient, Callable[[TenantContext], None]],
        world: dict[str, uuid.UUID],
        session_factory: async_sessionmaker[AsyncSession],
        failure_reason: str,
        code: str,
    ) -> None:
        """Not "after the first submission" — the old answer here was untrue."""
        await _failed_attempt(session_factory, world["text_task"], failure_reason)
        await _waiting(session_factory, world, world["text_task"], count=2)
        ac, _ = client

        body = (await ac.get(_read(world["text_task"]))).json()

        assert body["status"] == "not_composed"
        assert body["reason_code"] == code
        assert body["waiting_submissions"] == 2
        assert body["message"] == _ADVICE[CriteriaReasonCode(code)]
        assert body["in_force"] is None

    async def test_works_waiting_on_an_unprocessed_task_say_so(
        self,
        client: tuple[AsyncClient, Callable[[TenantContext], None]],
        world: dict[str, uuid.UUID],
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """The task's text has no ready summary: nothing to compose from yet."""
        await _waiting(session_factory, world, world["text_task"])
        ac, _ = client

        body = (await ac.get(_read(world["text_task"]))).json()

        assert body["status"] == "not_composed"
        assert body["reason_code"] == "task_not_processed"
        assert body["waiting_submissions"] == 1
        assert body["message"] == _ADVICE[CriteriaReasonCode.TASK_NOT_PROCESSED]

    async def test_works_waiting_with_no_attempt_for_the_version_say_so(
        self,
        client: tuple[AsyncClient, Callable[[TenantContext], None]],
        world: dict[str, uuid.UUID],
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """An attempt of an earlier version is not this version's."""
        await _ready_summary(session_factory, world["text_task"])
        await _failed_attempt(
            session_factory,
            world["text_task"],
            "exhausted: long ago",
            content_hash=_NEW_HASH,
        )
        await _waiting(session_factory, world, world["text_task"])
        ac, _ = client

        body = (await ac.get(_read(world["text_task"]))).json()

        assert body["status"] == "not_composed"
        assert body["reason_code"] == "not_attempted"
        assert body["message"] == _ADVICE[CriteriaReasonCode.NOT_ATTEMPTED]

    async def test_only_this_tasks_live_waiting_works_are_counted(
        self,
        client: tuple[AsyncClient, Callable[[TenantContext], None]],
        world: dict[str, uuid.UUID],
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await _waiting(session_factory, world, world["text_task"], count=2)
        await _waiting(session_factory, world, world["text_task"], status="received")
        await _waiting(session_factory, world, world["text_task"], deleted=True)
        await _waiting(session_factory, world, world["project_task"])
        ac, _ = client

        body = (await ac.get(_read(world["text_task"]))).json()

        assert body["waiting_submissions"] == 2

    async def test_a_list_in_force_has_no_reason_and_no_message(
        self,
        client: tuple[AsyncClient, Callable[[TenantContext], None]],
        world: dict[str, uuid.UUID],
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await _compose(session_factory, world["text_task"])
        ac, _ = client

        body = (await ac.get(_read(world["text_task"]))).json()

        assert body["status"] == "ready"
        assert body["reason_code"] is None
        assert body["message"] is None
        assert body["waiting_submissions"] == 0


@pytest.mark.usefixtures("task_on_the_new_path")
class TestAWriteContinuesTheWaitingWorks:
    """Entry 2 of the continuation (``PRE-FLIGHT.md`` 9.2); lock 6."""

    async def test_a_replacement_puts_each_waiting_work_back_once(
        self,
        client: tuple[AsyncClient, Callable[[TenantContext], None]],
        world: dict[str, uuid.UUID],
        session_factory: async_sessionmaker[AsyncSession],
        queue: MagicMock,
    ) -> None:
        """Two writes in a row: one job per work — the second finds it in flight."""
        await _compose(session_factory, world["text_task"])
        waiting = await _waiting(session_factory, world, world["text_task"], count=2)
        ac, _ = client
        edit = {"criteria": [_edited(_MODEL[0])]}

        first = await ac.put(_write(world["text_task"]), json=edit)
        second = await ac.put(_write(world["text_task"]), json=edit)

        assert first.status_code == 200, first.text
        assert second.status_code == 200, second.text
        assert queue.enqueue_job.await_count == 2
        for submission_id in waiting:
            assert len(await _jobs_of(session_factory, submission_id)) == 1
        # Still waiting until its run starts: the run, not the entry, lifts it.
        assert second.json()["waiting_submissions"] == 2

    async def test_a_reset_puts_the_waiting_works_back(
        self,
        client: tuple[AsyncClient, Callable[[TenantContext], None]],
        world: dict[str, uuid.UUID],
        session_factory: async_sessionmaker[AsyncSession],
        queue: MagicMock,
    ) -> None:
        await _compose(session_factory, world["text_task"])
        [work] = await _waiting(session_factory, world, world["text_task"])
        ac, _ = client

        resp = await ac.delete(_write(world["text_task"]))

        assert resp.status_code == 200, resp.text
        queue.enqueue_job.assert_awaited_once()
        assert len(await _jobs_of(session_factory, work)) == 1

    async def test_a_refused_write_queues_nothing(
        self,
        client: tuple[AsyncClient, Callable[[TenantContext], None]],
        world: dict[str, uuid.UUID],
        session_factory: async_sessionmaker[AsyncSession],
        queue: MagicMock,
    ) -> None:
        [work] = await _waiting(session_factory, world, world["text_task"])
        ac, _ = client

        resp = await ac.put(_write(world["text_task"]), json={"criteria": [_new()]})

        assert resp.status_code == 422, resp.text
        queue.enqueue_job.assert_not_awaited()
        assert await _jobs_of(session_factory, work) == []

    async def test_a_queue_that_refuses_does_not_undo_the_edit(
        self,
        client: tuple[AsyncClient, Callable[[TenantContext], None]],
        world: dict[str, uuid.UUID],
        session_factory: async_sessionmaker[AsyncSession],
        queue: MagicMock,
    ) -> None:
        await _compose(session_factory, world["text_task"])
        await _waiting(session_factory, world, world["text_task"])
        queue.enqueue_job.side_effect = ConnectionError("the queue is away")
        ac, _ = client

        resp = await ac.put(
            _write(world["text_task"]), json={"criteria": [_edited(_MODEL[0])]}
        )

        assert resp.status_code == 200, resp.text
        assert len(await _edits(session_factory, world["text_task"])) == 1


# ── DD-SP-DR: the author composes the list before the first submission ─


_TASK_TEXT = "Write a recursive factorial function and handle n = 0."
_BUSY_DETAILS = (
    "Завдання ще обробляється — спробуйте скласти перелік, коли обробку буде завершено"
)


async def _processed(
    session_factory: async_sessionmaker[AsyncSession], task_id: uuid.UUID
) -> None:
    """The task's text processed: a ready summary and its text."""
    async with session_factory() as session:
        summary = await DocumentSummaryRepository(session).create(
            authored_document_id=task_id,
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
        await session.commit()


async def _composition_jobs(
    session_factory: async_sessionmaker[AsyncSession], task_id: uuid.UUID
) -> list[Job]:
    async with session_factory() as session:
        rows = await session.execute(
            select(Job).where(
                Job.subject_id == task_id,
                Job.job_type == JobType.CRITERIA_COMPOSITION.value,
            )
        )
        return list(rows.scalars())


async def _other_job(
    session_factory: async_sessionmaker[AsyncSession],
    world: dict[str, uuid.UUID],
    task_id: uuid.UUID,
    job_type: JobType = JobType.DOCUMENT_PROCESSING,
    status: str = "active",
) -> None:
    async with session_factory() as session:
        session.add(
            Job(
                tenant_id=world["owner_id"],
                course_node_id=world["node"],
                job_type=job_type.value,
                subject_type=JOB_SUBJECT_TYPE[job_type],
                subject_id=task_id,
                status=status,
            )
        )
        await session.commit()


class _Composer:
    """The model's place in the composition: one criterion, or the ladder's end."""

    def __init__(self, *, error: BaseException | None = None) -> None:
        self.calls = 0
        self._error = error

    async def compose(self, **kwargs: Any) -> CriteriaComposition:
        self.calls += 1
        if self._error is not None:
            raise self._error
        return compose_criteria(
            [
                CriterionDraft(
                    text="Has a base case",
                    evidence="an explicit if-return for n = 0",
                    weight="must",
                    check_method="model_verdict",
                    concepts=("Recursion",),
                )
            ],
            [],
            input_concepts=[*kwargs["node_concepts"], *kwargs["root_concepts"]],
        )

    def prompt_hash(self) -> str:
        return "p" * 64


async def _run_the_work(
    session_factory: async_sessionmaker[AsyncSession],
    queue: MagicMock,
    task_id: uuid.UUID,
    composer: _Composer,
) -> Job:
    """The queued composition job, run by the real task body and the real seam."""
    [job] = await _composition_jobs(session_factory, task_id)
    ctx = {
        "session_factory": session_factory,
        "stage_router": MagicMock(),
        "redis": queue,
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
        await arq_compose_criteria(ctx, str(job.id), str(task_id))
    async with session_factory() as session:
        done = await session.get(Job, job.id)
    assert done is not None
    return done


class TestTheAuthorMayComposeNow:
    async def test_the_message_of_a_task_without_a_list_says_it_may_be_composed_now(
        self,
        client: tuple[AsyncClient, Callable[[TenantContext], None]],
        world: dict[str, uuid.UUID],
    ) -> None:
        ac, _ = client
        body = (await ac.get(_read(world["text_task"]))).json()

        assert body["message"] == AWAITING_FIRST_SUBMISSION_MESSAGE
        assert body["message"].endswith(
            "Ви можете скласти його й зараз, не чекаючи першої подачі."
        )

    async def test_a_request_queues_one_job_and_the_answer_is_composing(
        self,
        client: tuple[AsyncClient, Callable[[TenantContext], None]],
        world: dict[str, uuid.UUID],
        session_factory: async_sessionmaker[AsyncSession],
        queue: MagicMock,
    ) -> None:
        await _processed(session_factory, world["text_task"])
        ac, _ = client

        resp = await ac.post(_compose_path(world["text_task"]))

        assert resp.status_code == 202, resp.text
        body = resp.json()
        assert body["status"] == "composing"
        assert body["message"] == _COMPOSING_MESSAGE
        assert body["in_force"] is None
        queue.enqueue_job.assert_awaited_once()
        assert queue.enqueue_job.await_args is not None
        assert queue.enqueue_job.await_args.args[0] == "arq_compose_criteria"
        [job] = await _composition_jobs(session_factory, world["text_task"])
        assert job.status == "queued"
        assert job.tenant_id == world["owner_id"]
        assert job.subject_type == "authored_document"
        assert queue.enqueue_job.await_args.args[1:] == (
            str(job.id),
            str(world["text_task"]),
        )
        # No claim of the list exists yet; the reading says composing all the
        # same, because the queued job is a row and the reading counts it.
        assert (await ac.get(_read(world["text_task"]))).json()["status"] == "composing"

    async def test_a_second_request_while_composing_queues_nothing_more(
        self,
        client: tuple[AsyncClient, Callable[[TenantContext], None]],
        world: dict[str, uuid.UUID],
        session_factory: async_sessionmaker[AsyncSession],
        queue: MagicMock,
    ) -> None:
        await _processed(session_factory, world["text_task"])
        ac, _ = client
        first = await ac.post(_compose_path(world["text_task"]))

        second = await ac.post(_compose_path(world["text_task"]))

        assert (first.status_code, second.status_code) == (202, 202)
        assert second.json()["status"] == "composing"
        queue.enqueue_job.assert_awaited_once()
        assert len(await _composition_jobs(session_factory, world["text_task"])) == 1

    async def test_a_list_in_force_answers_200_and_queues_nothing(
        self,
        client: tuple[AsyncClient, Callable[[TenantContext], None]],
        world: dict[str, uuid.UUID],
        session_factory: async_sessionmaker[AsyncSession],
        queue: MagicMock,
    ) -> None:
        await _compose(session_factory, world["text_task"])
        ac, _ = client

        resp = await ac.post(_compose_path(world["text_task"]))

        assert resp.status_code == 200, resp.text
        assert resp.json() == (await ac.get(_read(world["text_task"]))).json()
        assert resp.json()["status"] == "ready"
        queue.enqueue_job.assert_not_awaited()
        assert await _composition_jobs(session_factory, world["text_task"]) == []

    async def test_a_list_being_composed_by_a_submission_queues_nothing(
        self,
        client: tuple[AsyncClient, Callable[[TenantContext], None]],
        world: dict[str, uuid.UUID],
        session_factory: async_sessionmaker[AsyncSession],
        queue: MagicMock,
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

        resp = await ac.post(_compose_path(world["text_task"]))

        assert resp.status_code == 202, resp.text
        assert resp.json()["status"] == "composing"
        queue.enqueue_job.assert_not_awaited()
        assert await _composition_jobs(session_factory, world["text_task"]) == []

    @pytest.mark.parametrize(
        ("job_type", "status"),
        [
            (JobType.DOCUMENT_PROCESSING, "active"),
            (JobType.DOCUMENT_PROCESSING, "queued"),
            (JobType.KEY_EXPLANATION, "queued"),
        ],
    )
    async def test_another_job_of_the_task_in_flight_is_a_409_with_its_reason(
        self,
        client: tuple[AsyncClient, Callable[[TenantContext], None]],
        world: dict[str, uuid.UUID],
        session_factory: async_sessionmaker[AsyncSession],
        queue: MagicMock,
        job_type: JobType,
        status: str,
    ) -> None:
        await _processed(session_factory, world["text_task"])
        await _other_job(session_factory, world, world["text_task"], job_type, status)
        ac, _ = client

        resp = await ac.post(_compose_path(world["text_task"]))

        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"] == {
            "code": "GENERATION_IN_PROGRESS",
            "details": _BUSY_DETAILS,
        }
        queue.enqueue_job.assert_not_awaited()
        assert await _composition_jobs(session_factory, world["text_task"]) == []

    @pytest.mark.parametrize(
        ("rival", "expected"),
        [(JobType.CRITERIA_COMPOSITION, 202), (JobType.DOCUMENT_PROCESSING, 409)],
    )
    async def test_a_request_that_loses_the_race_for_the_slot_is_answered_by_the_winner(
        self,
        client: tuple[AsyncClient, Callable[[TenantContext], None]],
        world: dict[str, uuid.UUID],
        session_factory: async_sessionmaker[AsyncSession],
        queue: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
        rival: JobType,
        expected: int,
    ) -> None:
        """The index judges: another request took the slot after this one read."""
        await _processed(session_factory, world["text_task"])

        async def lost(**_: Any) -> None:
            await _other_job(
                session_factory, world, world["text_task"], rival, "queued"
            )
            raise IntegrityError("insert", {}, Exception("uq_jobs_subject_in_flight"))

        monkeypatch.setattr(
            "course_supporter.api.routes.criteria.enqueue_criteria_composition", lost
        )
        ac, _ = client

        resp = await ac.post(_compose_path(world["text_task"]))

        assert resp.status_code == expected, resp.text
        queue.enqueue_job.assert_not_awaited()
        if expected == 202:
            assert resp.json()["status"] == "composing"

    @pytest.mark.parametrize(
        ("task", "code"),
        [
            ("test_task", "NOT_A_TEXT_TASK"),
            ("material", "NOT_A_TEXT_TASK"),
            ("unprocessed", "TASK_NOT_READY"),
            ("text_task", "TASK_NOT_READY"),  # a version, but no ready summary
        ],
    )
    async def test_a_task_that_cannot_have_a_list_is_refused_as_the_other_routes_do(
        self,
        client: tuple[AsyncClient, Callable[[TenantContext], None]],
        world: dict[str, uuid.UUID],
        session_factory: async_sessionmaker[AsyncSession],
        queue: MagicMock,
        task: str,
        code: str,
    ) -> None:
        ac, _ = client

        resp = await ac.post(_compose_path(world[task]))

        assert resp.status_code == 422, resp.text
        assert resp.json()["detail"]["code"] == code
        assert resp.json()["detail"]["details"]
        queue.enqueue_job.assert_not_awaited()
        assert await _composition_jobs(session_factory, world[task]) == []

    async def test_a_foreign_task_is_the_same_404_as_on_the_other_routes(
        self,
        client: tuple[AsyncClient, Callable[[TenantContext], None]],
        world: dict[str, uuid.UUID],
        session_factory: async_sessionmaker[AsyncSession],
        queue: MagicMock,
    ) -> None:
        await _processed(session_factory, world["text_task"])
        ac, use_key = client
        use_key(_key_context(world["stranger_id"], "prep"))

        foreign = await ac.post(_compose_path(world["text_task"]))
        missing = await ac.post(_compose_path(uuid.uuid4()))
        read_foreign = await ac.get(_read(world["text_task"]))

        assert foreign.status_code == missing.status_code == 404
        assert foreign.content == missing.content == read_foreign.content
        queue.enqueue_job.assert_not_awaited()
        assert await _composition_jobs(session_factory, world["text_task"]) == []

    async def test_the_work_composes_the_list_and_the_reading_says_ready(
        self,
        client: tuple[AsyncClient, Callable[[TenantContext], None]],
        world: dict[str, uuid.UUID],
        session_factory: async_sessionmaker[AsyncSession],
        queue: MagicMock,
        task_on_the_new_path: None,
    ) -> None:
        await _processed(session_factory, world["text_task"])
        [waiting] = await _waiting(session_factory, world, world["text_task"])
        ac, _ = client
        assert (await ac.post(_compose_path(world["text_task"]))).status_code == 202
        composer = _Composer()

        job = await _run_the_work(session_factory, queue, world["text_task"], composer)

        assert composer.calls == 1
        assert job.status == "complete"
        assert (job.result_data or {})["state"] == "ready"
        body = (await ac.get(_read(world["text_task"]))).json()
        assert body["status"] == "ready"
        assert body["in_force"]["layer"] == "model"
        assert [c["text"] for c in body["model"]] == ["Has a base case"]
        # The waiting work is put back in the queue, as after the author's edit.
        assert len(await _jobs_of(session_factory, waiting)) == 1
        # And asking again now is a 200 that queues nothing.
        again = await ac.post(_compose_path(world["text_task"]))
        assert again.status_code == 200
        assert len(await _composition_jobs(session_factory, world["text_task"])) == 1

    @pytest.mark.parametrize(
        ("stop", "code"),
        [
            (LadderStop.MONEY_CEILING, "limit_reached"),
            (LadderStop.OUTPUT_CEILING, "limit_reached"),
            (LadderStop.EXHAUSTED, "models_unavailable"),
        ],
    )
    async def test_a_composition_that_gave_up_is_read_as_a_submissions_is(
        self,
        client: tuple[AsyncClient, Callable[[TenantContext], None]],
        world: dict[str, uuid.UUID],
        session_factory: async_sessionmaker[AsyncSession],
        queue: MagicMock,
        stop: LadderStop,
        code: str,
    ) -> None:
        await _processed(session_factory, world["text_task"])
        ac, _ = client
        await ac.post(_compose_path(world["text_task"]))
        composer = _Composer(
            error=LadderExhaustedError("criteria_decomposition", [], stop=stop)
        )

        job = await _run_the_work(session_factory, queue, world["text_task"], composer)

        assert job.status == "complete"
        assert (job.result_data or {})["state"] == "unavailable"
        body = (await ac.get(_read(world["text_task"]))).json()
        assert body["status"] == "not_composed"
        assert body["reason_code"] == code
        assert body["message"] == _ADVICE[CriteriaReasonCode(code)]
        # The author may ask again: a failed attempt is history, not a slot.
        retry = await ac.post(_compose_path(world["text_task"]))
        assert retry.status_code == 202
        assert retry.json()["status"] == "composing"
        assert len(await _composition_jobs(session_factory, world["text_task"])) == 2


_PRICE_USD = Decimal("0.05")


async def _author_account(
    session_factory: async_sessionmaker[AsyncSession], world: dict[str, uuid.UUID]
) -> uuid.UUID:
    async with session_factory() as session:
        account = await get_or_create_author_account(
            session, tenant_id=world["owner_id"]
        )
        await session.commit()
        return account.id


async def _units(
    session_factory: async_sessionmaker[AsyncSession],
    account: uuid.UUID,
    units: int,
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


class TestTheAuthorsThreshold:
    """Composing from the button is the author's own action: ``available >= P``."""

    @pytest.fixture(autouse=True)
    def _priced(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(charging, "charging_enabled", lambda settings=None: True)
        monkeypatch.setattr(
            "course_supporter.homework.criteria_edit_service.preparation_e_usd",
            lambda **_: _PRICE_USD,
        )

    async def test_an_author_without_credit_and_without_enough_units_is_refused(
        self,
        client: tuple[AsyncClient, Callable[[TenantContext], None]],
        world: dict[str, uuid.UUID],
        session_factory: async_sessionmaker[AsyncSession],
        queue: MagicMock,
    ) -> None:
        await _processed(session_factory, world["text_task"])
        price = charging.price_of(_PRICE_USD)
        await _units(
            session_factory, await _author_account(session_factory, world), price - 1
        )
        ac, _ = client

        resp = await ac.post(_compose_path(world["text_task"]))

        assert resp.status_code == 402, resp.text
        assert resp.json()["detail"] == {
            "code": "insufficient_units",
            "details": {
                "price_units": price,
                "available_units": price - 1,
                "missing_units": 1,
            },
        }
        queue.enqueue_job.assert_not_awaited()
        assert await _composition_jobs(session_factory, world["text_task"]) == []

    async def test_an_author_with_enough_units_is_queued(
        self,
        client: tuple[AsyncClient, Callable[[TenantContext], None]],
        world: dict[str, uuid.UUID],
        session_factory: async_sessionmaker[AsyncSession],
        queue: MagicMock,
    ) -> None:
        await _processed(session_factory, world["text_task"])
        await _units(
            session_factory,
            await _author_account(session_factory, world),
            charging.price_of(_PRICE_USD),
        )
        ac, _ = client

        resp = await ac.post(_compose_path(world["text_task"]))

        assert resp.status_code == 202, resp.text
        queue.enqueue_job.assert_awaited_once()

    async def test_an_author_with_credit_is_queued_at_zero(
        self,
        client: tuple[AsyncClient, Callable[[TenantContext], None]],
        world: dict[str, uuid.UUID],
        session_factory: async_sessionmaker[AsyncSession],
        queue: MagicMock,
    ) -> None:
        await _processed(session_factory, world["text_task"])
        async with session_factory() as session:
            await admin.update_author_settings(
                session, world["owner_id"], actor="test-admin", credit_allowed=True
            )
            await session.commit()
        ac, _ = client

        resp = await ac.post(_compose_path(world["text_task"]))

        assert resp.status_code == 202, resp.text
        queue.enqueue_job.assert_awaited_once()

    async def test_with_charging_off_nothing_is_checked(
        self,
        client: tuple[AsyncClient, Callable[[TenantContext], None]],
        world: dict[str, uuid.UUID],
        session_factory: async_sessionmaker[AsyncSession],
        queue: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(charging, "charging_enabled", lambda settings=None: False)
        await _processed(session_factory, world["text_task"])
        ac, _ = client

        resp = await ac.post(_compose_path(world["text_task"]))

        assert resp.status_code == 202, resp.text
        queue.enqueue_job.assert_awaited_once()

    async def test_the_threshold_is_asked_only_where_a_job_would_be_queued(
        self,
        client: tuple[AsyncClient, Callable[[TenantContext], None]],
        world: dict[str, uuid.UUID],
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """A list in force is a 200 for an author with no units at all."""
        await _compose(session_factory, world["text_task"])
        ac, _ = client

        resp = await ac.post(_compose_path(world["text_task"]))

        assert resp.status_code == 200, resp.text


_GONE = CLAIM_SILENCE_LIMIT + timedelta(minutes=1)
_ALIVE = CLAIM_SILENCE_LIMIT - timedelta(minutes=1)


async def _claim_silent_for(
    session_factory: async_sessionmaker[AsyncSession],
    task_id: uuid.UUID,
    silence: timedelta,
) -> None:
    """A pending claim whose claimer last spoke ``silence`` ago."""
    async with session_factory() as session:
        # The version is the task's own hash, which processing set: a claim of
        # any other is a stale one, and a stale claim is nobody's to wait for.
        task = await session.get(AuthoredDocument, task_id)
        assert task is not None
        assert task.content_hash is not None
        repo = TaskCriteriaListRepository(session)
        row = await repo.claim(
            authored_document_id=task_id,
            source_content_hash=task.content_hash,
            source_task_type="task",
            form_version=2,
        )
        assert row is not None
        assert await repo.take_over(
            row.id,
            seen_claimed_at=row.claimed_at,
            now=datetime.now(UTC) - silence,
        )
        await session.commit()


class TestAClaimWhoseClaimerIsGone:
    """The one rule of ``claim_is_abandoned``: silent for the limit, a submission
    takes it over — and the author's request does not take it for a live one."""

    async def test_an_abandoned_claim_does_not_hold_the_button(
        self,
        client: tuple[AsyncClient, Callable[[TenantContext], None]],
        world: dict[str, uuid.UUID],
        session_factory: async_sessionmaker[AsyncSession],
        queue: MagicMock,
    ) -> None:
        await _processed(session_factory, world["text_task"])
        await _claim_silent_for(
            session_factory,
            world["text_task"],
            CLAIM_SILENCE_LIMIT + timedelta(minutes=1),
        )
        ac, _ = client

        resp = await ac.post(_compose_path(world["text_task"]))

        assert resp.status_code == 202, resp.text
        queue.enqueue_job.assert_awaited_once()
        assert len(await _composition_jobs(session_factory, world["text_task"])) == 1
        # And a second press finds the work in flight, as for any request.
        assert (await ac.post(_compose_path(world["text_task"]))).status_code == 202
        queue.enqueue_job.assert_awaited_once()

    async def test_the_work_takes_the_abandoned_claim_over_and_the_list_is_ready(
        self,
        client: tuple[AsyncClient, Callable[[TenantContext], None]],
        world: dict[str, uuid.UUID],
        session_factory: async_sessionmaker[AsyncSession],
        queue: MagicMock,
    ) -> None:
        await _processed(session_factory, world["text_task"])
        await _claim_silent_for(
            session_factory,
            world["text_task"],
            CLAIM_SILENCE_LIMIT + timedelta(minutes=1),
        )
        ac, _ = client
        await ac.post(_compose_path(world["text_task"]))
        composer = _Composer()

        job = await _run_the_work(session_factory, queue, world["text_task"], composer)

        assert composer.calls == 1
        assert job.status == "complete"
        body = (await ac.get(_read(world["text_task"]))).json()
        assert body["status"] == "ready"
        assert body["in_force"]["layer"] == "model"

    async def test_a_live_claim_still_holds_the_button(
        self,
        client: tuple[AsyncClient, Callable[[TenantContext], None]],
        world: dict[str, uuid.UUID],
        session_factory: async_sessionmaker[AsyncSession],
        queue: MagicMock,
    ) -> None:
        await _processed(session_factory, world["text_task"])
        await _claim_silent_for(
            session_factory,
            world["text_task"],
            CLAIM_SILENCE_LIMIT - timedelta(minutes=1),
        )
        ac, _ = client

        resp = await ac.post(_compose_path(world["text_task"]))

        assert resp.status_code == 202, resp.text
        assert resp.json()["status"] == "composing"
        queue.enqueue_job.assert_not_awaited()
        assert await _composition_jobs(session_factory, world["text_task"]) == []
