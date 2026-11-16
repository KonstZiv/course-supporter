"""The two doors of a reply to a remark (task 12a, K3).

``get_current_student`` and ``get_current_tenant`` are overridden, the store of
comments is the in-memory double of the port (``remark_comments`` dependency),
and the facts the core reads about the review are a double too. What is tested
here belongs to the DOORS: the contract's shapes and status codes, the student
each door names into the core, the one 404 of the read path, the switch, and
that a refusal leaves both doors word for word alike. The decisions themselves
are the core's (``tests/unit/test_homework/test_reply_core.py``).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator, Iterator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from course_supporter.api.app import app
from course_supporter.api.deps import get_current_student, get_current_tenant
from course_supporter.api.routes._feedback_shared import remark_comments
from course_supporter.auth.context import StudentContext, TenantContext
from course_supporter.homework import path_config, reply_core
from course_supporter.homework.feedback_core import SUBMISSION_NOT_FOUND
from course_supporter.homework.remark_comments import ScreenMark, ThreadScope
from course_supporter.homework.reply_core import ReviewFacts
from course_supporter.models.source import AssignmentType
from course_supporter.reply_kinds import CommentAuthor, ReplyTopic
from course_supporter.security.schemas import ViolationCategory
from course_supporter.storage.database import get_session
from course_supporter.storage.homework_repository import HomeworkRepository
from course_supporter.storage.orm import HomeworkSubmission, Student
from course_supporter.storage.student_repository import StudentRepository
from tests._helpers.path_config import (
    shipped_with_replies_for,
    shipped_with_todays_mentor_for,
)
from tests._helpers.remark_comments import FakeRemarkComments, a_reply

_TENANT_ID = uuid.uuid4()
_STUDENT_ID = uuid.uuid4()
_EXTERNAL_ID = "school-student-42"
_VERSION = "content-hash"

_TENANT = TenantContext(
    tenant_id=_TENANT_ID,
    tenant_name="test-tenant",
    scopes=["check"],
    plan_id="basic",
    key_prefix="cs_test",
)
_SESSION_STUDENT = StudentContext(
    student_id=_STUDENT_ID,
    tenant_id=_TENANT_ID,
    login="alice",
    display_name="Alice",
)
_CONTRACT_KEYS = {
    "id",
    "submission_id",
    "item_id",
    "parent_id",
    "author",
    "kind",
    "body",
    "created_at",
    "answered_in_submission_id",
}


def _submission(*, tenant_id: uuid.UUID = _TENANT_ID) -> HomeworkSubmission:
    submission = HomeworkSubmission(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        student_id=_STUDENT_ID,
        course_node_id=uuid.uuid4(),
        node_id=uuid.uuid4(),
        authored_document_id=uuid.uuid4(),
        file_url="s3://bucket/solution.py",
        file_type="text/plain",
        original_filename="solution.py",
        delivery_mode="in_app",
    )
    submission.status = "delivered"
    submission.review_markdown = "## Рецензія"
    submission.task_version = _VERSION
    submission.created_at = datetime.now(UTC)
    submission.deleted_at = None
    return submission


def _scope(submission: HomeworkSubmission) -> ThreadScope:
    return ThreadScope(
        submission.tenant_id,
        submission.student_id,
        submission.authored_document_id,
        submission.task_version,
    )


_FACTS = ReviewFacts(
    task_type=AssignmentType.TASK,
    task_version=_VERSION,
    later_statuses=(),
    items=("c1", "c2"),
)


@pytest.fixture()
def replies_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """The switch on for ``task``, through the cached file the core reads."""
    monkeypatch.setattr(
        path_config, "_config", shipped_with_replies_for(AssignmentType.TASK)
    )


@pytest.fixture()
def replies_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """The shipped file: no type takes replies."""
    monkeypatch.setattr(path_config, "_config", shipped_with_todays_mentor_for())


@pytest.fixture(autouse=True)
def review_facts() -> Iterator[AsyncMock]:
    with patch.object(
        reply_core, "review_facts", new=AsyncMock(return_value=_FACTS)
    ) as read:
        yield read


@pytest.fixture()
def store() -> FakeRemarkComments:
    return FakeRemarkComments()


@pytest.fixture()
def mock_session() -> AsyncMock:
    session = AsyncMock()
    session.add = MagicMock()
    return session


@pytest.fixture()
async def client(
    mock_session: AsyncMock, store: FakeRemarkComments
) -> AsyncGenerator[AsyncClient]:
    app.dependency_overrides[get_session] = lambda: mock_session
    app.dependency_overrides[get_current_student] = lambda: _SESSION_STUDENT
    app.dependency_overrides[get_current_tenant] = lambda: _TENANT
    app.dependency_overrides[remark_comments] = lambda: store
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac
    app.dependency_overrides.clear()


def _portal(submission_id: uuid.UUID) -> str:
    return f"/api/v1/portal/submissions/{submission_id}/comments"


def _channel(submission_id: uuid.UUID) -> str:
    return f"/api/v1/feedback/submissions/{submission_id}/comments"


def _body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "item_id": "c1",
        "kind": "objection",
        "body": "Критерій 1 виконано: перевірка є в рядку 12.",
        "parent_id": None,
    }
    body.update(overrides)
    return body


def _owned(submission: HomeworkSubmission | None) -> Any:
    return patch.object(HomeworkRepository, "get_owned", return_value=submission)


def _known_student() -> Any:
    student = Student(id=_STUDENT_ID, tenant_id=_TENANT_ID, external_id=_EXTERNAL_ID)
    return patch.object(StudentRepository, "get_by_external_id", return_value=student)


@pytest.mark.usefixtures("replies_on")
class TestThePortalDoor:
    async def test_a_reply_is_created_in_the_contracts_shape(
        self, client: AsyncClient, store: FakeRemarkComments, mock_session: AsyncMock
    ) -> None:
        submission = _submission()
        with _owned(submission) as get_owned:
            resp = await client.post(_portal(submission.id), json=_body())

        assert resp.status_code == 201
        reply = resp.json()
        assert set(reply) == _CONTRACT_KEYS
        assert reply["submission_id"] == str(submission.id)
        assert reply["item_id"] == "c1"
        assert reply["parent_id"] is None
        assert reply["author"] == "student"
        assert reply["kind"] == "objection"
        assert reply["answered_in_submission_id"] is None
        assert get_owned.await_args.args == (submission.id, _STUDENT_ID)
        assert [r.submission_id for r in store.added] == [submission.id]
        mock_session.commit.assert_awaited_once()

    async def test_a_refusal_is_422_with_code_and_details_and_nothing_kept(
        self, client: AsyncClient, store: FakeRemarkComments, mock_session: AsyncMock
    ) -> None:
        submission = _submission()
        with _owned(submission):
            resp = await client.post(
                _portal(submission.id), json=_body(body="я" * 2_001)
            )

        assert resp.status_code == 422
        assert resp.json() == {
            "detail": {"code": "comment_too_long", "details": {"max": 2000}}
        }
        assert store.added == []
        mock_session.commit.assert_not_awaited()

    async def test_a_foreign_or_unknown_submission_is_the_read_paths_404(
        self, client: AsyncClient, store: FakeRemarkComments
    ) -> None:
        with _owned(None):
            post = await client.post(_portal(uuid.uuid4()), json=_body())
            read = await client.get(_portal(uuid.uuid4()))

        assert post.status_code == read.status_code == 404
        assert post.json() == read.json() == {"detail": SUBMISSION_NOT_FOUND}
        assert store.added == []

    @pytest.mark.parametrize(
        "body",
        [
            _body(kind="praise"),
            {**_body(), "student_external_id": "x"},
            {"kind": "question", "body": "Чому?"},
        ],
        ids=["unknown-kind", "unknown-field", "no-item"],
    )
    async def test_a_body_out_of_shape_is_the_schemas_422(
        self, client: AsyncClient, store: FakeRemarkComments, body: dict[str, Any]
    ) -> None:
        submission = _submission()
        with _owned(submission):
            resp = await client.post(_portal(submission.id), json=body)

        assert resp.status_code == 422
        assert isinstance(resp.json()["detail"], list), "FastAPI's own shape"
        assert store.added == []

    async def test_the_threads_without_the_screens_marks(
        self, client: AsyncClient, store: FakeRemarkComments
    ) -> None:
        """Topic and hostility are the platform's: never served."""
        submission = _submission()
        reply = a_reply(submission.id, item_id="c2")
        store.comments.append(reply)
        store.scopes[submission.id] = _scope(submission)
        await store.mark_screened(
            reply.id,
            ScreenMark(ReplyTopic.HOSTILE, 0.9, ViolationCategory.PROMPT_INJECTION),
        )
        with _owned(submission):
            resp = await client.get(_portal(submission.id))

        assert resp.status_code == 200
        (thread,) = resp.json()["threads"]
        assert thread["item_id"] == "c2"
        (comment,) = thread["comments"]
        assert set(comment) == _CONTRACT_KEYS
        assert "hostile" not in resp.text and "prompt_injection" not in resp.text


@pytest.mark.usefixtures("replies_off")
class TestTheSwitchOff:
    """No type takes replies: no route for a reply, no threads to read."""

    async def test_post_is_the_same_404_and_get_is_empty(
        self, client: AsyncClient, store: FakeRemarkComments
    ) -> None:
        submission = _submission()
        with _owned(submission), _known_student():
            portal_post = await client.post(_portal(submission.id), json=_body())
            portal_get = await client.get(_portal(submission.id))
            channel_post = await client.post(
                _channel(submission.id),
                json={**_body(), "student_external_id": _EXTERNAL_ID},
            )
            channel_get = await client.get(
                _channel(submission.id), params={"student_external_id": _EXTERNAL_ID}
            )

        assert portal_post.status_code == channel_post.status_code == 404
        assert (
            portal_post.json()
            == channel_post.json()
            == {"detail": SUBMISSION_NOT_FOUND}
        )
        assert portal_get.status_code == channel_get.status_code == 200
        assert portal_get.json() == channel_get.json() == {"threads": []}
        assert store.added == [] and store.locked == []


@pytest.mark.usefixtures("replies_on")
class TestTheChannelDoor:
    async def test_the_student_is_resolved_within_the_keys_tenant(
        self, client: AsyncClient, store: FakeRemarkComments
    ) -> None:
        submission = _submission()
        with _known_student() as lookup, _owned(submission) as get_owned:
            resp = await client.post(
                _channel(submission.id),
                json={**_body(), "student_external_id": _EXTERNAL_ID},
            )

        assert resp.status_code == 201
        assert set(resp.json()) == _CONTRACT_KEYS
        assert lookup.await_args.args == (_TENANT_ID, _EXTERNAL_ID)
        assert get_owned.await_args.args == (submission.id, _STUDENT_ID)

    async def test_an_unknown_student_is_the_404_and_never_created(
        self, client: AsyncClient, store: FakeRemarkComments
    ) -> None:
        with (
            patch.object(StudentRepository, "get_by_external_id", return_value=None),
            patch.object(StudentRepository, "create", new=AsyncMock()) as create,
        ):
            post = await client.post(
                _channel(uuid.uuid4()),
                json={**_body(), "student_external_id": "nobody"},
            )
            read = await client.get(
                _channel(uuid.uuid4()), params={"student_external_id": "nobody"}
            )

        assert post.status_code == read.status_code == 404
        assert post.json() == read.json() == {"detail": SUBMISSION_NOT_FOUND}
        create.assert_not_awaited()
        assert store.added == []

    async def test_a_submission_of_another_tenant_is_the_404(
        self, client: AsyncClient, store: FakeRemarkComments
    ) -> None:
        stranger = _submission(tenant_id=uuid.uuid4())
        with _known_student(), _owned(stranger):
            resp = await client.post(
                _channel(stranger.id),
                json={**_body(), "student_external_id": _EXTERNAL_ID},
            )

        assert resp.status_code == 404
        assert store.added == []

    async def test_the_read_names_the_student_in_the_query(
        self, client: AsyncClient, store: FakeRemarkComments
    ) -> None:
        submission = _submission()
        store.comments.append(a_reply(submission.id, item_id="c1"))
        store.scopes[submission.id] = _scope(submission)
        with _known_student(), _owned(submission):
            named = await client.get(
                _channel(submission.id), params={"student_external_id": _EXTERNAL_ID}
            )
            unnamed = await client.get(_channel(submission.id))

        assert named.status_code == 200
        assert [t["item_id"] for t in named.json()["threads"]] == ["c1"]
        assert unnamed.status_code == 422

    async def test_a_key_without_check_is_refused(
        self, client: AsyncClient, store: FakeRemarkComments
    ) -> None:
        prep_only = TenantContext(
            tenant_id=_TENANT_ID,
            tenant_name="test-tenant",
            scopes=["prep"],
            plan_id="basic",
            key_prefix="cs_test",
        )
        app.dependency_overrides[get_current_tenant] = lambda: prep_only
        resp = await client.post(
            _channel(uuid.uuid4()),
            json={**_body(), "student_external_id": _EXTERNAL_ID},
        )

        assert resp.status_code == 403
        assert store.added == []


@pytest.mark.usefixtures("replies_on")
class TestBothDoorsAnswerAlike:
    @pytest.mark.parametrize(
        ("body", "code"),
        [
            (_body(body="   "), "comment_empty"),
            (_body(item_id="c9"), "unknown_item"),
            (_body(parent_id=str(uuid.uuid4())), "parent_not_in_thread"),
            (_body(body="Чому?‮"), "comment_rejected"),
        ],
    )
    async def test_a_refusal_word_for_word(
        self, client: AsyncClient, body: dict[str, Any], code: str
    ) -> None:
        submission = _submission()
        with _known_student(), _owned(submission):
            portal = await client.post(_portal(submission.id), json=body)
            channel = await client.post(
                _channel(submission.id),
                json={**body, "student_external_id": _EXTERNAL_ID},
            )

        assert portal.status_code == channel.status_code == 422
        assert portal.json() == channel.json()
        assert portal.json()["detail"]["code"] == code

    async def test_the_limit_word_for_word(
        self, client: AsyncClient, store: FakeRemarkComments
    ) -> None:
        submission = _submission()
        store.comments.extend(
            a_reply(submission.id, item_id="c1", minute=m) for m in range(5)
        )
        with _known_student(), _owned(submission):
            portal = await client.post(_portal(submission.id), json=_body())
            channel = await client.post(
                _channel(submission.id),
                json={**_body(), "student_external_id": _EXTERNAL_ID},
            )

        assert (
            portal.json()
            == channel.json()
            == {"detail": {"code": "comment_limit_reached", "details": {"max": 5}}}
        )

    async def test_the_mentors_answer_is_served_as_a_comment(
        self, client: AsyncClient, store: FakeRemarkComments
    ) -> None:
        submission = _submission()
        question = a_reply(submission.id, item_id="c1", answered_in=submission.id)
        answer = a_reply(
            submission.id,
            item_id="c1",
            author=CommentAuthor.MENTOR,
            kind="answer",
            parent_id=question.id,
            minute=1,
        )
        store.comments.extend([answer, question])
        store.scopes[submission.id] = _scope(submission)
        with _known_student(), _owned(submission):
            portal = await client.get(_portal(submission.id))
            channel = await client.get(
                _channel(submission.id), params={"student_external_id": _EXTERNAL_ID}
            )

        assert portal.json() == channel.json()
        (thread,) = portal.json()["threads"]
        assert [c["author"] for c in thread["comments"]] == ["student", "mentor"]
        assert thread["comments"][0]["answered_in_submission_id"] == str(submission.id)
        assert thread["comments"][1]["parent_id"] == str(question.id)
