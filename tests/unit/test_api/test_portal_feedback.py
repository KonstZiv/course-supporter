"""The portal door of a touch (mentor-rebuild task 05).

``get_current_student`` is overridden to a fixed session; the repositories are
mocked. What is tested here is what belongs to the DOOR and to nothing else:
the student it names into the core comes from the SESSION, the submission is
resolved by the same ownership helper the detail route uses, and the detail
serves this session's own answer and nobody else's.

The refusals themselves belong to the core and are tested there
(``tests/unit/test_feedback_core.py``); the one exercised here is the end-to-end
guarantee that a foreign submission is refused through both layers at once.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from course_supporter.api.app import app
from course_supporter.api.deps import get_current_student, get_session
from course_supporter.auth.context import StudentContext
from course_supporter.feedback_kinds import (
    FeedbackKind,
    FeedbackTargetKind,
    FeedbackValue,
)
from course_supporter.homework.feedback_core import NO_REVIEW_CODE, SUBMISSION_NOT_FOUND
from course_supporter.storage.feedback_repository import FeedbackRepository
from course_supporter.storage.homework_repository import HomeworkRepository
from course_supporter.storage.orm import HomeworkSubmission, StudentFeedback

STUB_TENANT_ID = uuid.uuid4()
STUB_STUDENT_ID = uuid.uuid4()
STUB_STUDENT = StudentContext(
    student_id=STUB_STUDENT_ID,
    tenant_id=STUB_TENANT_ID,
    login="alice",
    display_name="Alice",
)
_OTHER_STUDENT_ID = uuid.uuid4()


def _submission(
    *,
    student_id: uuid.UUID = STUB_STUDENT_ID,
    tenant_id: uuid.UUID = STUB_TENANT_ID,
    status: str = "completed",
    review_markdown: str | None = "## Good work\nWell done.",
) -> HomeworkSubmission:
    submission = HomeworkSubmission(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        student_id=student_id,
        course_node_id=uuid.uuid4(),
        node_id=uuid.uuid4(),
        authored_document_id=uuid.uuid4(),
        file_url="s3://bucket/solution.py",
        file_type="text/plain",
        original_filename="solution.py",
        delivery_mode="in_app",
    )
    submission.status = status
    submission.review_markdown = review_markdown
    submission.review_result = None
    submission.safety_result = None
    submission.sanity_result = None
    submission.score = 87
    submission.created_at = datetime.now(UTC)
    submission.snapshot_manifest = None
    submission.base_id = None
    submission.deleted_at = None
    submission.recovered_encoding = None
    return submission


def _stored_touch(
    *,
    student_id: uuid.UUID = STUB_STUDENT_ID,
    value: FeedbackValue = FeedbackValue.HELPED,
) -> StudentFeedback:
    row = StudentFeedback(
        id=uuid.uuid4(),
        tenant_id=STUB_TENANT_ID,
        student_id=student_id,
        target_kind=FeedbackTargetKind.REVIEW.value,
        target_id=uuid.uuid4(),
        kind=FeedbackKind.TOUCH.value,
        value=value.value,
    )
    row.created_at = datetime.now(UTC)
    row.updated_at = datetime.now(UTC)
    return row


@pytest.fixture()
def mock_session() -> AsyncMock:
    session = AsyncMock()
    session.add = MagicMock()
    return session


@pytest.fixture()
async def client(mock_session: AsyncMock) -> AsyncGenerator[AsyncClient]:
    app.dependency_overrides[get_session] = lambda: mock_session
    app.dependency_overrides[get_current_student] = lambda: STUB_STUDENT
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as ac:
        yield ac
    app.dependency_overrides.clear()


def _url(submission_id: uuid.UUID) -> str:
    return f"/api/v1/portal/submissions/{submission_id}/feedback"


class TestTheDoorNamesTheStudentFromTheSession:
    async def test_the_touch_is_recorded_for_the_session_student(
        self, client: AsyncClient
    ) -> None:
        """The id handed to the core is the session's, not the body's or a guess.

        The body cannot name a student — there is no such field — so what this
        pins is the other half: the route reads the id off the authenticated
        session and hands THAT to the core, which then checks it against the
        submission's owner.
        """
        submission = _submission()
        with (
            patch.object(HomeworkRepository, "get_owned", return_value=submission),
            patch.object(
                FeedbackRepository,
                "record",
                new=AsyncMock(return_value=_stored_touch()),
            ) as record,
        ):
            resp = await client.post(
                _url(submission.id),
                json={"kind": "touch", "value": "helped"},
            )
        assert resp.status_code == 200
        assert resp.json()["value"] == "helped"
        assert record.await_args.kwargs["student_id"] == STUB_STUDENT_ID

    async def test_the_submission_is_looked_up_for_the_session_student(
        self, client: AsyncClient
    ) -> None:
        """The ownership helper is called with the session's student id."""
        submission = _submission()
        with (
            patch.object(
                HomeworkRepository, "get_owned", return_value=submission
            ) as get_owned,
            patch.object(
                FeedbackRepository,
                "record",
                new=AsyncMock(return_value=_stored_touch()),
            ),
        ):
            await client.post(
                _url(submission.id), json={"kind": "touch", "value": "not_helped"}
            )
        assert get_owned.await_args.args[1] == STUB_STUDENT_ID


class TestRefusalsReachThePortalUnchanged:
    async def test_a_submission_the_door_does_not_find_is_a_generic_404(
        self, client: AsyncClient
    ) -> None:
        """The two layers together: the helper returns None, the core refuses.

        This is the end-to-end guarantee, and it holds while EITHER layer does
        its job — which is why it stays green under a mutation of one of them
        alone, and red only under both.
        """
        with (
            patch.object(HomeworkRepository, "get_owned", return_value=None),
            patch.object(FeedbackRepository, "record", new=AsyncMock()) as record,
        ):
            resp = await client.post(
                _url(uuid.uuid4()), json={"kind": "touch", "value": "helped"}
            )
        assert resp.status_code == 404
        assert resp.json()["detail"] == SUBMISSION_NOT_FOUND
        record.assert_not_awaited()

    async def test_a_foreign_submission_is_refused_even_if_the_door_hands_it_over(
        self, client: AsyncClient
    ) -> None:
        """The through-guarantee, with the door's own filter taken away.

        The ownership helper is made to return a submission belonging to
        somebody else — the shape a rewritten or broken door filter would have.
        The answer must still be the generic 404 and nothing may be written.
        Green while EITHER layer does its job, so it stays green when one of
        them is mutated alone and goes red only when both are.
        """
        with (
            patch.object(
                HomeworkRepository,
                "get_owned",
                return_value=_submission(student_id=_OTHER_STUDENT_ID),
            ),
            patch.object(FeedbackRepository, "record", new=AsyncMock()) as record,
        ):
            resp = await client.post(
                _url(uuid.uuid4()), json={"kind": "touch", "value": "helped"}
            )
        assert resp.status_code == 404
        assert resp.json()["detail"] == SUBMISSION_NOT_FOUND
        record.assert_not_awaited()

    async def test_a_submission_without_a_review_carries_the_code_to_the_portal(
        self, client: AsyncClient
    ) -> None:
        """The 409 body reaches the client as {code, details}, for the phrase."""
        submission = _submission(status="received", review_markdown=None)
        with (
            patch.object(HomeworkRepository, "get_owned", return_value=submission),
            patch.object(FeedbackRepository, "record", new=AsyncMock()),
        ):
            resp = await client.post(
                _url(submission.id), json={"kind": "touch", "value": "helped"}
            )
        assert resp.status_code == 409
        assert resp.json()["detail"]["code"] == NO_REVIEW_CODE
        assert resp.json()["detail"]["details"]

    async def test_an_unknown_answer_is_refused_by_the_schema(
        self, client: AsyncClient
    ) -> None:
        """The closed vocabulary is a wall at the door, not only in the table."""
        with patch.object(FeedbackRepository, "record", new=AsyncMock()) as record:
            resp = await client.post(
                _url(uuid.uuid4()), json={"kind": "touch", "value": "maybe"}
            )
        assert resp.status_code == 422
        record.assert_not_awaited()


class TestTheDetailServesTheSessionsOwnAnswer:
    async def test_the_detail_carries_the_students_own_touch(
        self, client: AsyncClient
    ) -> None:
        """What was answered comes back on the next read of the submission."""
        submission = _submission()
        with (
            patch.object(HomeworkRepository, "get_owned", return_value=submission),
            patch.object(
                FeedbackRepository,
                "get_for_target",
                new=AsyncMock(return_value=_stored_touch(value=FeedbackValue.HELPED)),
            ),
        ):
            resp = await client.get(f"/api/v1/portal/submissions/{submission.id}")
        assert resp.status_code == 200
        assert resp.json()["own_feedback"]["value"] == "helped"
        assert resp.json()["own_feedback"]["kind"] == "touch"

    async def test_no_answer_yet_is_null_not_an_absent_key(
        self, client: AsyncClient
    ) -> None:
        """A student who has not answered reads a null, and the key is there."""
        submission = _submission()
        with (
            patch.object(HomeworkRepository, "get_owned", return_value=submission),
            patch.object(
                FeedbackRepository, "get_for_target", new=AsyncMock(return_value=None)
            ),
        ):
            resp = await client.get(f"/api/v1/portal/submissions/{submission.id}")
        assert resp.status_code == 200
        assert "own_feedback" in resp.json()
        assert resp.json()["own_feedback"] is None

    async def test_the_lookup_is_keyed_on_the_session_tenant_and_student(
        self, client: AsyncClient
    ) -> None:
        """Both keys come from the session — this is the door's own job.

        The repository's own filters are proved against a live database
        (``test_feedback_repository_db.py``); what is proved here is that the
        route hands it the session's values and not the submission's.

        The submission double disagrees with the session on both values on
        purpose. That state cannot occur in production — the ownership helper
        would not have returned such a row — and it is not asserted to be
        possible: it is the only way to ask WHICH of the two objects the route
        read, which is exactly what this test is for.
        """
        submission = _submission(student_id=_OTHER_STUDENT_ID, tenant_id=uuid.uuid4())
        with (
            patch.object(HomeworkRepository, "get_owned", return_value=submission),
            patch.object(
                FeedbackRepository, "get_for_target", new=AsyncMock(return_value=None)
            ) as lookup,
        ):
            await client.get(f"/api/v1/portal/submissions/{submission.id}")
        assert lookup.await_args.kwargs == {
            "tenant_id": STUB_TENANT_ID,
            "student_id": STUB_STUDENT_ID,
            "target_kind": FeedbackTargetKind.REVIEW,
            "target_id": submission.id,
        }
