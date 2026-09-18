"""The caller's-channel door of a touch (mentor-rebuild task 05).

``get_current_tenant`` is overridden to a fixed CHECK key; the repositories are
mocked. What is tested here belongs to THIS door and to nothing else: the
student is resolved by external identifier within the key's tenant, that
resolved student is the one handed to the core, and no touch ever creates a
student. The refusals themselves are the core's and are tested there; what this
file adds about them is that they leave this door WORD FOR WORD as they leave
the portal one.

The repository double returns a real row even in the tests that expect a
refusal: a double that cannot be projected would turn "the door failed to
refuse" into a ``ValueError`` somewhere in serialization, and a red from a
broken double proves only that the code ran. With a working one, a missing
refusal shows up as the status assertion it is.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from course_supporter.api.app import app
from course_supporter.api.deps import get_current_student, get_current_tenant
from course_supporter.auth.context import StudentContext, TenantContext
from course_supporter.feedback_kinds import (
    FeedbackKind,
    FeedbackTargetKind,
    FeedbackValue,
)
from course_supporter.homework.feedback_core import NO_REVIEW_CODE, SUBMISSION_NOT_FOUND
from course_supporter.storage.database import get_session
from course_supporter.storage.feedback_repository import FeedbackRepository
from course_supporter.storage.homework_repository import HomeworkRepository
from course_supporter.storage.orm import HomeworkSubmission, Student, StudentFeedback
from course_supporter.storage.student_repository import StudentRepository

STUB_TENANT_ID = uuid.uuid4()
STUB_STUDENT_ID = uuid.uuid4()
_OTHER_STUDENT_ID = uuid.uuid4()
_EXTERNAL_ID = "portal-f0f0f0"

STUB_TENANT = TenantContext(
    tenant_id=STUB_TENANT_ID,
    tenant_name="test-tenant",
    scopes=["check", "prep"],
    plan_id="basic",
    key_prefix="cs_test",
)
STUB_STUDENT_SESSION = StudentContext(
    student_id=STUB_STUDENT_ID,
    tenant_id=STUB_TENANT_ID,
    login="alice",
    display_name="Alice",
)


def _student(student_id: uuid.UUID = STUB_STUDENT_ID) -> Student:
    student = Student(id=student_id, tenant_id=STUB_TENANT_ID, external_id=_EXTERNAL_ID)
    return student


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
        delivery_mode="webhook",
    )
    submission.status = status
    submission.review_markdown = review_markdown
    submission.deleted_at = None
    return submission


def _stored_touch(value: FeedbackValue = FeedbackValue.HELPED) -> StudentFeedback:
    row = StudentFeedback(
        id=uuid.uuid4(),
        tenant_id=STUB_TENANT_ID,
        student_id=STUB_STUDENT_ID,
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
    app.dependency_overrides[get_current_tenant] = lambda: STUB_TENANT
    app.dependency_overrides[get_current_student] = lambda: STUB_STUDENT_SESSION
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as ac:
        yield ac
    app.dependency_overrides.clear()


def _url(submission_id: uuid.UUID) -> str:
    return f"/api/v1/feedback/submissions/{submission_id}"


def _body(value: str = "helped", external_id: str = _EXTERNAL_ID) -> dict[str, str]:
    return {"kind": "touch", "value": value, "student_external_id": external_id}


class TestTheDoorResolvesTheStudentItself:
    async def test_the_external_id_is_resolved_within_the_keys_tenant(
        self, client: AsyncClient
    ) -> None:
        """Both arguments of the lookup come from this request and nowhere else."""
        submission = _submission()
        with (
            patch.object(
                StudentRepository, "get_by_external_id", return_value=_student()
            ) as lookup,
            patch.object(HomeworkRepository, "get_owned", return_value=submission),
            patch.object(
                FeedbackRepository,
                "record",
                new=AsyncMock(return_value=_stored_touch()),
            ),
        ):
            resp = await client.post(_url(submission.id), json=_body())
        assert resp.status_code == 200
        assert lookup.await_args.args == (STUB_TENANT_ID, _EXTERNAL_ID)

    async def test_the_resolved_student_is_the_one_handed_to_the_core(
        self, client: AsyncClient
    ) -> None:
        """Not the submission's owner: the door names the student it resolved.

        The submission double is owned by somebody else on purpose — the shape
        a broken ownership filter would have. The core then refuses, which is
        the through-guarantee; what this pins is that the id the door passes is
        the resolved one, so the refusal happens at all.
        """
        with (
            patch.object(
                StudentRepository, "get_by_external_id", return_value=_student()
            ),
            patch.object(
                HomeworkRepository,
                "get_owned",
                return_value=_submission(student_id=_OTHER_STUDENT_ID),
            ),
            patch.object(
                FeedbackRepository,
                "record",
                new=AsyncMock(return_value=_stored_touch()),
            ) as record,
        ):
            resp = await client.post(_url(uuid.uuid4()), json=_body())
        assert resp.status_code == 404
        assert resp.json()["detail"] == SUBMISSION_NOT_FOUND
        record.assert_not_awaited()

    async def test_the_submission_is_looked_up_for_the_resolved_student(
        self, client: AsyncClient
    ) -> None:
        """The ownership helper is keyed on the resolved student's id."""
        submission = _submission()
        with (
            patch.object(
                StudentRepository, "get_by_external_id", return_value=_student()
            ),
            patch.object(
                HomeworkRepository, "get_owned", return_value=submission
            ) as get_owned,
            patch.object(
                FeedbackRepository,
                "record",
                new=AsyncMock(return_value=_stored_touch()),
            ),
        ):
            await client.post(_url(submission.id), json=_body("not_helped"))
        assert get_owned.await_args.args == (submission.id, STUB_STUDENT_ID)

    async def test_a_touch_never_creates_a_student(self, client: AsyncClient) -> None:
        """Unlike a submission: an unknown external id is a generic 404."""
        with (
            patch.object(StudentRepository, "get_by_external_id", return_value=None),
            patch.object(StudentRepository, "get_or_create", new=AsyncMock()) as create,
            patch.object(
                FeedbackRepository,
                "record",
                new=AsyncMock(return_value=_stored_touch()),
            ) as record,
        ):
            resp = await client.post(_url(uuid.uuid4()), json=_body())
        assert resp.status_code == 404
        assert resp.json()["detail"] == SUBMISSION_NOT_FOUND
        create.assert_not_awaited()
        record.assert_not_awaited()


class TestTheFourMismatchesAnswerAsThePortalDoes:
    @pytest.mark.parametrize(
        ("case", "student", "submission"),
        [
            ("foreign tenant — its students are invisible to this key", None, None),
            ("unknown external id", None, None),
            ("unknown submission id", _student(), None),
            (
                "a submission of another student",
                _student(),
                _submission(student_id=_OTHER_STUDENT_ID),
            ),
            (
                "a soft-deleted submission — the helper drops it",
                _student(),
                None,
            ),
        ],
    )
    async def test_every_mismatch_is_the_same_404(
        self,
        client: AsyncClient,
        case: str,
        student: Student | None,
        submission: HomeworkSubmission | None,
    ) -> None:
        """One answer, whatever failed to resolve."""
        with (
            patch.object(StudentRepository, "get_by_external_id", return_value=student),
            patch.object(HomeworkRepository, "get_owned", return_value=submission),
            patch.object(
                FeedbackRepository,
                "record",
                new=AsyncMock(return_value=_stored_touch()),
            ) as record,
        ):
            resp = await client.post(_url(uuid.uuid4()), json=_body())
        assert resp.status_code == 404, case
        assert resp.json() == {"detail": SUBMISSION_NOT_FOUND}, case
        record.assert_not_awaited()

    async def test_the_two_doors_refuse_with_the_same_status_and_body(
        self, client: AsyncClient
    ) -> None:
        """Acceptance 3, measured rather than asserted by eye.

        The same unresolvable situation is posted to both doors and the two
        responses are compared whole — status and body. They are equal because
        they come from one constant in the core, not because two sentences were
        kept in step by hand.
        """
        submission_id = uuid.uuid4()
        with (
            patch.object(StudentRepository, "get_by_external_id", return_value=None),
            patch.object(HomeworkRepository, "get_owned", return_value=None),
            patch.object(
                FeedbackRepository,
                "record",
                new=AsyncMock(return_value=_stored_touch()),
            ),
        ):
            channel = await client.post(_url(submission_id), json=_body())
            portal = await client.post(
                f"/api/v1/portal/submissions/{submission_id}/feedback",
                json={"kind": "touch", "value": "helped"},
            )
        assert (channel.status_code, channel.json()) == (
            portal.status_code,
            portal.json(),
        )

    async def test_no_review_is_the_same_409_on_both_doors(
        self, client: AsyncClient
    ) -> None:
        """The refusal that is NOT generic travels identically too."""
        submission = _submission(status="received", review_markdown=None)
        with (
            patch.object(
                StudentRepository, "get_by_external_id", return_value=_student()
            ),
            patch.object(HomeworkRepository, "get_owned", return_value=submission),
            patch.object(
                FeedbackRepository,
                "record",
                new=AsyncMock(return_value=_stored_touch()),
            ),
        ):
            channel = await client.post(_url(submission.id), json=_body())
            portal = await client.post(
                f"/api/v1/portal/submissions/{submission.id}/feedback",
                json={"kind": "touch", "value": "helped"},
            )
        assert channel.status_code == 409
        assert channel.json()["detail"]["code"] == NO_REVIEW_CODE
        assert (channel.status_code, channel.json()) == (
            portal.status_code,
            portal.json(),
        )


class TestTheScopeIsTheOneSubmissionsTravelOn:
    async def test_a_key_without_the_check_scope_is_refused(
        self, client: AsyncClient
    ) -> None:
        """PREP alone does not answer for a student."""
        app.dependency_overrides[get_current_tenant] = lambda: TenantContext(
            tenant_id=STUB_TENANT_ID,
            tenant_name="prep-only",
            scopes=["prep"],
            plan_id="basic",
            key_prefix="cs_prep",
        )
        with patch.object(
            FeedbackRepository,
            "record",
            new=AsyncMock(return_value=_stored_touch()),
        ) as record:
            resp = await client.post(_url(uuid.uuid4()), json=_body())
        assert resp.status_code == 403
        record.assert_not_awaited()
