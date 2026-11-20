"""The student's path through the replies, from the login to the Mentor's answer.

Task 12a, K6. The ends of K3 and K4 are pinned by ``test_reply_screen_e2e_db``
and ``test_reply_answers_e2e_db``; they put the replies in the table with a
helper and call the routes' functions directly. This module walks the same
road as the student does — over HTTP of the portal, on a real database, the
environment's S3 and the real body of the queue task — and proves what those
modules do not:

1. **The whole road over HTTP.** Login; the first submission of a ``task``;
   the queue task; the review read with its remarks; a reply on the work under
   one remark and one beside the work under another; the same work sent again;
   the queue task; the threads read — each reply answered by the Mentor and
   marked answered; the second review read — its section of replies has both.
2. **The stages chosen by the state.** The router double records the stages
   it is asked for: the first submission walks the list of ``first``, the
   second the list of ``repeat_with_replies`` in the order of the file, and
   each reply's ``reply_safety`` check is asked and registered before the first
   paid stage. The lists are read from the configuration the app sees, and the
   two lists are different. The state picks its list at
   ``homework/path_selection.py:164`` (``choose_path``:
   ``declared.paths[state]``).
3. **A known finding, as a strict expected failure.** A reply the screen could
   not judge (its safety check failed) is not picked up by the next submission
   — ``pending_replies`` (``homework/remark_comments.py``) reads the replies to
   the student's latest previous attempt alone — so it stays unanswered for
   good.

The switch of the replies is on for this module alone, and where the app
reads it: the loader's cache (``path_config._config``, which
``get_path_config`` returns to every reader — the comment routes, the
submission's state, the reply screen, the rule of the same work, the body of
the queue task) holds the shipped file with ``replies_enabled`` turned on for
``task`` (``shipped_with_replies_for``); ``monkeypatch`` puts back what was
there. The units' gate of the submission route is passed as every portal
submission test passes it: charging is off (``BILLING_CHARGING_ENABLED`` is
unset, its default is false), so ``_require_units`` refuses nothing.

No model is called: the router and the topic port are the doubles of
``test_reply_answers_e2e_db``. Run with ``--run-db --run-redis``.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from course_supporter.api.app import app
from course_supporter.api.deps import get_arq_redis, get_s3_client, get_session
from course_supporter.api.tasks import arq_process_homework
from course_supporter.auth.student_passwords import hash_password
from course_supporter.billing.charging import charging_enabled
from course_supporter.config import get_settings
from course_supporter.homework import path_config
from course_supporter.homework.path_config import (
    REPLY_SAFETY_ACTION,
    PathConfig,
    SubmissionState,
    model_stages,
)
from course_supporter.models.review_structure import ReviewStructureV1
from course_supporter.models.source import AssignmentType
from course_supporter.remark_kinds import CommentAuthor
from course_supporter.storage.orm import (
    DocumentSummary,
    HomeworkSubmission,
    Job,
    StudentCredential,
    StudentEnrollment,
)
from course_supporter.storage.s3 import S3Client
from tests._helpers.path_config import shipped_with_replies_for
from tests.integration.test_attempt_dedup_db import _UTF8_LF
from tests.integration.test_attempt_ledger_db import _drop_world, _make_world
from tests.integration.test_reply_answers_e2e_db import (
    _BESIDE,
    _ON_THE_WORK,
    _RepliesRouter,
    _Topics,
)
from tests.integration.test_reply_screen_e2e_db import _actions_of
from tests.integration.test_text_review_e2e_db import _ListSource, _PortDouble

pytestmark = pytest.mark.requires_db

_LOGIN = "replies-k6"
_PASSWORD = "correct horse 10"
_FILENAME = "answers.py"


# ── The switch, the world, the portal ───────────────────────────────────────


@pytest.fixture()
def replies_on(monkeypatch: pytest.MonkeyPatch) -> PathConfig:
    """The shipped file with replies on for ``task``, where the app reads it.

    Put in the loader's cache, so every reader that asks ``get_path_config``
    — the routes in the request, the body of the queue task — gets it.
    """
    config = shipped_with_replies_for(AssignmentType.TASK)
    monkeypatch.setattr(path_config, "_config", config)
    assert path_config.get_path_config() is config
    return config


@pytest.fixture()
async def world(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[dict[str, Any]]:
    """A ``task`` ready for submissions, and a student who can log in to it.

    The world of the ledger's tests, with the task's ready summary (the
    route's readiness gate), the student enrolled in its course (the access
    gate) and a portal login of theirs.
    """
    world = await _make_world(session_factory, task_type="task")
    async with session_factory() as session:
        session.add(
            DocumentSummary(
                authored_document_id=world["doc_id"],
                course_root_id=world["node_id"],
                title="Завдання",
                status="ready",
            )
        )
        session.add(
            StudentEnrollment(
                student_id=world["student_id"], course_node_id=world["node_id"]
            )
        )
        session.add(
            StudentCredential(
                student_id=world["student_id"],
                tenant_id=world["tenant_id"],
                login=_LOGIN,
                password_hash=hash_password(_PASSWORD),
                is_active=True,
            )
        )
        await session.commit()
    yield world
    async with session_factory() as session:
        await session.execute(
            delete(StudentEnrollment).where(
                StudentEnrollment.student_id == world["student_id"]
            )
        )
        await session.execute(
            delete(StudentCredential).where(
                StudentCredential.tenant_id == world["tenant_id"]
            )
        )
        await session.commit()
    await _drop_world(session_factory, world)


@pytest.fixture()
async def s3() -> AsyncGenerator[S3Client]:
    """The environment's S3 (SeaweedFS in the local stack), as the route uses it."""
    settings = get_settings()
    client = S3Client(
        endpoint_url=settings.s3_endpoint,
        access_key=settings.s3_access_key,
        secret_key=settings.s3_secret_key.get_secret_value(),
        bucket=settings.s3_bucket,
    )
    await client.open()
    try:
        await client.ensure_bucket()
        yield client
    finally:
        await client.close()


@pytest.fixture()
async def portal(
    replies_on: PathConfig,
    world: dict[str, Any],
    session_factory: async_sessionmaker[AsyncSession],
    s3: S3Client,
) -> AsyncGenerator[AsyncClient]:
    """The app over HTTP: the live database and S3; the queue a double.

    The student is NOT stood in for: the bearer of the login authenticates
    every request, as in the portal.
    """
    # The units' gate refuses nothing while charging is off — the way every
    # portal submission test passes it; a stack that turned it on would be
    # another test.
    assert not charging_enabled()

    async def _session() -> AsyncGenerator[AsyncSession]:
        async with session_factory() as session:
            yield session

    arq = AsyncMock()
    arq.enqueue_job = AsyncMock(return_value=MagicMock(job_id="arq:hw:replies-k6"))
    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_s3_client] = lambda: s3
    app.dependency_overrides[get_arq_redis] = lambda: arq
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            yield client
    finally:
        app.dependency_overrides.clear()


# ── The student's steps, over HTTP ──────────────────────────────────────────


async def _login(client: AsyncClient, world: dict[str, Any]) -> dict[str, str]:
    response = await client.post(
        "/api/v1/portal/login",
        json={
            "tenant_id": str(world["tenant_id"]),
            "login": _LOGIN,
            "password": _PASSWORD,
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["student_id"] == str(world["student_id"])
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def _submit(
    client: AsyncClient, auth: dict[str, str], world: dict[str, Any]
) -> uuid.UUID:
    """The same work every time, sent to the task from the portal."""
    response = await client.post(
        f"/api/v1/portal/tasks/{world['doc_id']}/submissions",
        files={"file": (_FILENAME, _UTF8_LF, "text/plain")},
        headers=auth,
    )
    assert response.status_code == 202, response.text
    body = response.json()
    assert (body["status"], body["duplicate"]) == ("received", False)
    return uuid.UUID(body["submission_id"])


async def _read_review(
    client: AsyncClient, auth: dict[str, str], submission_id: uuid.UUID
) -> ReviewStructureV1:
    response = await client.get(
        f"/api/v1/portal/submissions/{submission_id}", headers=auth
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "completed"  # in_app: the read path delivers
    assert body["structure"] is not None
    return ReviewStructureV1.model_validate(body["structure"])


def _item_ids(review: ReviewStructureV1) -> list[str]:
    """The remarks a reply can be tied to, in the order the review shows them."""
    remarks = [*review.new_remarks, *review.open, *review.broken, *review.fixed]
    return [r.item_id for r in remarks if r.item_id is not None]


async def _reply(
    client: AsyncClient,
    auth: dict[str, str],
    submission_id: uuid.UUID,
    *,
    item_id: str,
    kind: str,
    body: str,
) -> uuid.UUID:
    response = await client.post(
        f"/api/v1/portal/submissions/{submission_id}/comments",
        json={"item_id": item_id, "kind": kind, "body": body},
        headers=auth,
    )
    assert response.status_code == 201, response.text
    stored = response.json()
    assert (stored["item_id"], stored["body"]) == (item_id, body)
    assert stored["answered_in_submission_id"] is None
    return uuid.UUID(stored["id"])


async def _threads(
    client: AsyncClient, auth: dict[str, str], submission_id: uuid.UUID
) -> dict[str, Any]:
    response = await client.get(
        f"/api/v1/portal/submissions/{submission_id}/comments", headers=auth
    )
    assert response.status_code == 200, response.text
    threads: dict[str, Any] = response.json()
    return threads


def _comments(threads: dict[str, Any]) -> list[dict[str, Any]]:
    return [c for thread in threads["threads"] for c in thread["comments"]]


# ── The queue task, run by the real body ───────────────────────────────────


@contextmanager
def _body_doubles(world: dict[str, Any]) -> Iterator[None]:
    """What the body reaches outside: funds, the criteria source, the topic port.

    The submission path itself is NOT patched: the body reads the loader's
    cache, as the routes do.
    """
    with (
        patch(
            "course_supporter.homework.path_runner.build_funds_port",
            return_value=_PortDouble(),
        ),
        patch(
            "course_supporter.homework.criteria_evaluation.build_criteria_list_service",
            return_value=_ListSource(world["in_force"]),
        ),
        patch(
            "course_supporter.homework.reply_screen.build_reply_topic_classifier",
            return_value=_Topics(),
        ),
        patch(
            "course_supporter.homework.webhook.deliver_webhook",
            new=AsyncMock(return_value=True),
        ),
    ):
        yield


async def _job_of(
    session_factory: async_sessionmaker[AsyncSession], submission_id: uuid.UUID
) -> Job:
    async with session_factory() as session:
        job = await session.scalar(select(Job).where(Job.subject_id == submission_id))
    assert job is not None, "the route dispatched no job"
    return job


async def _run_queue_task(
    session_factory: async_sessionmaker[AsyncSession],
    world: dict[str, Any],
    s3: S3Client,
    submission_id: uuid.UUID,
    router: _RepliesRouter,
) -> Job:
    """The queue task the route dispatched, as the walks of the new path run it."""
    job = await _job_of(session_factory, submission_id)
    ctx = {
        "session_factory": session_factory,
        "stage_router": router,
        "s3_client": s3,
        "job_try": 1,
    }
    with _body_doubles(world):
        await arq_process_homework(ctx, str(job.id), str(submission_id))
    async with session_factory() as session:
        submission = await session.get(HomeworkSubmission, submission_id)
    assert submission is not None
    assert submission.status == "completed", submission.status
    return await _job_of(session_factory, submission_id)


def _state_of(job: Job) -> str:
    assert job.stage_progress is not None
    state: str = job.stage_progress["submission_state"]
    return state


# ── The road ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Road:
    """What the student's road left behind, for the assertions."""

    first_id: uuid.UUID
    second_id: uuid.UUID
    first_router: _RepliesRouter
    second_router: _RepliesRouter
    first_job: Job
    second_job: Job
    on_work: uuid.UUID
    beside: uuid.UUID
    on_work_item: str
    beside_item: str
    threads: dict[str, Any]
    second_review: ReviewStructureV1


async def _the_students_road(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    world: dict[str, Any],
    s3: S3Client,
) -> _Road:
    """Login → first submission → review → two replies → the same work → answers."""
    auth = await _login(client, world)

    first_id = await _submit(client, auth, world)
    first_router = _RepliesRouter(session_factory)
    first_job = await _run_queue_task(
        session_factory, world, s3, first_id, first_router
    )

    first_review = await _read_review(client, auth, first_id)
    items = _item_ids(first_review)
    assert len(set(items)) >= 2, items
    on_work_item, beside_item = items[0], next(i for i in items if i != items[0])
    on_work = await _reply(
        client,
        auth,
        first_id,
        item_id=on_work_item,
        kind="question",
        body=_ON_THE_WORK,
    )
    beside = await _reply(
        client, auth, first_id, item_id=beside_item, kind="comment", body=_BESIDE
    )

    second_id = await _submit(client, auth, world)
    assert second_id != first_id
    second_router = _RepliesRouter(session_factory)
    second_job = await _run_queue_task(
        session_factory, world, s3, second_id, second_router
    )

    return _Road(
        first_id=first_id,
        second_id=second_id,
        first_router=first_router,
        second_router=second_router,
        first_job=first_job,
        second_job=second_job,
        on_work=on_work,
        beside=beside,
        on_work_item=on_work_item,
        beside_item=beside_item,
        threads=await _threads(client, auth, second_id),
        second_review=await _read_review(client, auth, second_id),
    )


def _model_stages_of(config: PathConfig, state: SubmissionState) -> list[str]:
    """The stages of ``task``'s list of ``state`` a model answers, in its order.

    A code stage (``course_link_lookup``) asks no router, so the router's
    record holds the rest of the list.
    """
    asked = model_stages(config)
    paths = config.task_types[AssignmentType.TASK].paths
    return [name for name in paths[state] if name in asked]


# ── 1. The whole road over HTTP ─────────────────────────────────────────────


class TestTheStudentsRoadOverHttp:
    async def test_from_the_login_to_the_mentors_answers(
        self,
        portal: AsyncClient,
        session_factory: async_sessionmaker[AsyncSession],
        world: dict[str, Any],
        s3: S3Client,
    ) -> None:
        road = await _the_students_road(portal, session_factory, world, s3)

        # The threads: each reply under its remark, a Mentor's answer under
        # each, attached to the second review; each reply marked answered.
        comments = _comments(road.threads)
        by_id = {uuid.UUID(c["id"]): c for c in comments}
        assert by_id[road.on_work]["item_id"] == road.on_work_item
        assert by_id[road.beside]["item_id"] == road.beside_item
        answers = {
            uuid.UUID(c["parent_id"]): c
            for c in comments
            if c["author"] == CommentAuthor.MENTOR.value
        }
        assert set(answers) == {road.on_work, road.beside}
        for reply_id in (road.on_work, road.beside):
            answer = answers[reply_id]
            assert answer["kind"] == "answer"
            assert answer["submission_id"] == str(road.second_id)
            assert answer["item_id"] == by_id[reply_id]["item_id"]
            assert answer["body"]
            assert by_id[reply_id]["answered_in_submission_id"] == str(road.second_id)

        # The second review's section of replies: both, with the same answers.
        assert [r.said for r in road.second_review.replies] == [_ON_THE_WORK, _BESIDE]
        assert [r.answer for r in road.second_review.replies] == [
            answers[road.on_work]["body"],
            answers[road.beside]["body"],
        ]


# ── 2. The stages chosen by the state ──────────────────────────────────────


class TestTheStateChoosesTheStages:
    """The state picks its list at ``homework/path_selection.py:164``."""

    def test_the_two_states_have_different_lists(self, replies_on: PathConfig) -> None:
        seen = path_config.get_path_config()
        paths = seen.task_types[AssignmentType.TASK].paths
        assert (
            paths[SubmissionState.FIRST] != paths[SubmissionState.REPEAT_WITH_REPLIES]
        )
        assert _model_stages_of(seen, SubmissionState.FIRST) != _model_stages_of(
            seen, SubmissionState.REPEAT_WITH_REPLIES
        )

    async def test_each_submission_walks_the_list_of_its_state(
        self,
        portal: AsyncClient,
        session_factory: async_sessionmaker[AsyncSession],
        world: dict[str, Any],
        s3: S3Client,
    ) -> None:
        road = await _the_students_road(portal, session_factory, world, s3)
        # The lists as the app reads them, not written out here.
        seen = path_config.get_path_config()
        first = _model_stages_of(seen, SubmissionState.FIRST)
        with_replies = _model_stages_of(seen, SubmissionState.REPEAT_WITH_REPLIES)

        assert _state_of(road.first_job) == SubmissionState.FIRST.value
        assert road.first_router.calls == first

        assert _state_of(road.second_job) == (SubmissionState.REPEAT_WITH_REPLIES.value)
        replies = len([road.on_work, road.beside])
        screens = road.second_router.calls[:replies]
        assert screens == [REPLY_SAFETY_ACTION] * replies
        assert road.second_router.calls[replies:] == with_replies
        # Each reply its own check, before the first paid stage — asked and
        # registered so.
        assert sorted(road.second_router.screened) == sorted([_ON_THE_WORK, _BESIDE])
        actions = await _actions_of(session_factory, road.second_job.id)
        assert actions[:replies] == [REPLY_SAFETY_ACTION] * replies
        assert actions.count(REPLY_SAFETY_ACTION) == replies
        # The register keeps more than the stages (the review's metrics row).
        assert [a for a in actions if a in with_replies] == with_replies
        assert actions.index(with_replies[0]) == replies


# ── 3. The known finding: a reply the screen could not judge ───────────────


async def _a_reply_the_screen_could_not_judge(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    world: dict[str, Any],
    s3: S3Client,
) -> tuple[dict[str, str], uuid.UUID, uuid.UUID]:
    """Two submissions of the same work; the second's screen of the reply fails.

    Returns the student's auth, the reply and the second submission. The
    reply is left unjudged and unanswered (as ``test_reply_answers_e2e_db``
    pins for a reply put in the table): asserted here.
    """
    auth = await _login(client, world)
    first_id = await _submit(client, auth, world)
    await _run_queue_task(
        session_factory, world, s3, first_id, _RepliesRouter(session_factory)
    )
    item = _item_ids(await _read_review(client, auth, first_id))[0]
    reply = await _reply(
        client, auth, first_id, item_id=item, kind="question", body=_ON_THE_WORK
    )

    second_id = await _submit(client, auth, world)
    failing = _RepliesRouter(session_factory, screen=lambda _text: "")
    second_job = await _run_queue_task(session_factory, world, s3, second_id, failing)
    assert _state_of(second_job) == SubmissionState.REPEAT_WITH_REPLIES.value
    assert failing.calls[0] == REPLY_SAFETY_ACTION
    assert failing.screened == [_ON_THE_WORK]

    comments = _comments(await _threads(client, auth, second_id))
    (stored,) = [c for c in comments if uuid.UUID(c["id"]) == reply]
    assert stored["answered_in_submission_id"] is None
    assert not [c for c in comments if c["author"] == CommentAuthor.MENTOR.value]
    assert (await _read_review(client, auth, second_id)).replies == []
    return auth, reply, second_id


class TestAReplyTheScreenCouldNotJudge:
    async def test_the_next_submission_answers_nothing(
        self,
        portal: AsyncClient,
        session_factory: async_sessionmaker[AsyncSession],
        world: dict[str, Any],
        s3: S3Client,
    ) -> None:
        """The precondition of the finding, red on its own if it breaks."""
        await _a_reply_the_screen_could_not_judge(portal, session_factory, world, s3)

    @pytest.mark.xfail(
        strict=True,
        raises=AssertionError,
        reason=(
            "знахідка гейту №686: репліку, яку екран не оцінив, наступна подача "
            "не підхоплює; виправлення — наступним словом агентові №686"
        ),
    )
    async def test_the_submission_after_it_judges_and_answers_the_reply(
        self,
        portal: AsyncClient,
        session_factory: async_sessionmaker[AsyncSession],
        world: dict[str, Any],
        s3: S3Client,
    ) -> None:
        auth, reply, _ = await _a_reply_the_screen_could_not_judge(
            portal, session_factory, world, s3
        )

        third_id = await _submit(portal, auth, world)
        router = _RepliesRouter(session_factory)
        await _run_queue_task(session_factory, world, s3, third_id, router)

        # The finding: the reply is not judged again — the third submission
        # does not see it (it reads the replies to the second review alone).
        assert router.screened == [_ON_THE_WORK], router.calls
        comments = _comments(await _threads(portal, auth, third_id))
        (stored,) = [c for c in comments if uuid.UUID(c["id"]) == reply]
        assert stored["answered_in_submission_id"] == str(third_id)
        assert [
            c
            for c in comments
            if c["author"] == CommentAuthor.MENTOR.value
            and c["parent_id"] == str(reply)
        ]
