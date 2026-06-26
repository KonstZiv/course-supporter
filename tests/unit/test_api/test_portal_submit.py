"""Unit tests for the portal session submission route (Phase 6 T2, mode-2).

``get_current_student`` is overridden to a fixed StudentContext; the route's
gates (anchor / tenant / enrollment / readiness) and the shared core are mocked.
The live bearer flow + curated-slice non-leak are covered by the integration /
live acceptance.
"""

from __future__ import annotations

import io
import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from course_supporter.api.app import app
from course_supporter.api.deps import (
    get_arq_redis,
    get_current_student,
    get_s3_client,
    get_session,
)
from course_supporter.auth.context import StudentContext
from course_supporter.storage.authored_document_repository import (
    AuthoredDocumentRepository,
)
from course_supporter.storage.course_node_repository import CourseNodeRepository
from course_supporter.storage.document_summary_repository import (
    DocumentSummaryRepository,
)
from course_supporter.storage.homework_repository import HomeworkRepository
from course_supporter.storage.student_enrollment_repository import (
    StudentEnrollmentRepository,
)

CREATE_JOB_FUNC = "course_supporter.homework.submission_core.create_homework_job"
DISPATCH_FUNC = "course_supporter.homework.submission_core.dispatch_homework"

STUB_TENANT_ID = uuid.uuid4()
STUB_STUDENT_ID = uuid.uuid4()
STUB_STUDENT = StudentContext(
    student_id=STUB_STUDENT_ID,
    tenant_id=STUB_TENANT_ID,
    login="alice",
    display_name="Alice",
)


def _mock_task_doc(
    *,
    course_root_id: uuid.UUID,
    course_node_id: uuid.UUID | None = None,
    task_type: str | None = "task",
    deleted_at: object | None = None,
) -> MagicMock:
    doc = MagicMock()
    doc.course_root_id = course_root_id
    doc.course_node_id = course_node_id or uuid.uuid4()
    doc.task_type = task_type
    doc.deleted_at = deleted_at
    return doc


def _mock_root_node(tenant_id: uuid.UUID) -> MagicMock:
    node = MagicMock()
    node.id = uuid.uuid4()
    node.tenant_id = tenant_id
    node.parent_id = None
    return node


def _mock_summary(status: str = "ready") -> MagicMock:
    summary = MagicMock()
    summary.status = status
    return summary


def _mock_student() -> MagicMock:
    s = MagicMock()
    s.id = STUB_STUDENT_ID
    return s


def _mock_submission(status: str = "received") -> MagicMock:
    sub = MagicMock()
    sub.id = uuid.uuid4()
    sub.status = status
    sub.job_id = uuid.uuid4()
    return sub


def _mock_job() -> MagicMock:
    job = MagicMock()
    job.id = uuid.uuid4()
    return job


@pytest.fixture()
def mock_session() -> AsyncMock:
    session = AsyncMock()
    session.add = MagicMock()
    session.get = AsyncMock(return_value=_mock_student())
    return session


@pytest.fixture()
def mock_s3() -> AsyncMock:
    s3 = AsyncMock()
    s3.upload_smart = AsyncMock(
        return_value=("http://localhost:9000/course-materials/key/file.py", 512)
    )
    s3.delete_object = AsyncMock()
    return s3


@pytest.fixture()
async def client(mock_session: AsyncMock, mock_s3: AsyncMock) -> AsyncClient:
    app.dependency_overrides[get_session] = lambda: mock_session
    app.dependency_overrides[get_current_student] = lambda: STUB_STUDENT
    app.dependency_overrides[get_arq_redis] = lambda: MagicMock()
    app.dependency_overrides[get_s3_client] = lambda: mock_s3
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as ac:
        yield ac  # type: ignore[misc]
    app.dependency_overrides.clear()


def _files() -> dict[str, tuple[str, io.BytesIO, str]]:
    return {"file": ("solution.py", io.BytesIO(b"print('hello')"), "text/x-python")}


def _url(authored_document_id: uuid.UUID) -> str:
    return f"/api/v1/portal/tasks/{authored_document_id}/submissions"


class TestPortalSubmitSuccess:
    async def test_submit_in_app_returns_202(self, client: AsyncClient) -> None:
        """A valid, enrolled, ready task accepts an in_app submission (202)."""
        adoc = uuid.uuid4()
        root = uuid.uuid4()
        submission = _mock_submission()

        with (
            patch.object(
                AuthoredDocumentRepository,
                "get_by_id",
                return_value=_mock_task_doc(course_root_id=root),
            ),
            patch.object(
                CourseNodeRepository,
                "get_by_id",
                return_value=_mock_root_node(STUB_TENANT_ID),
            ),
            patch.object(StudentEnrollmentRepository, "is_enrolled", return_value=True),
            patch.object(
                DocumentSummaryRepository,
                "get_by_authored_document_id",
                return_value=_mock_summary("ready"),
            ),
            patch.object(HomeworkRepository, "find_duplicate", return_value=None),
            patch.object(
                HomeworkRepository, "create", return_value=submission
            ) as mock_create,
            patch.object(HomeworkRepository, "set_job_id", return_value=None),
            patch(CREATE_JOB_FUNC, new_callable=AsyncMock, return_value=_mock_job()),
            patch(DISPATCH_FUNC, new_callable=AsyncMock, return_value=None),
        ):
            resp = await client.post(_url(adoc), files=_files())

        assert resp.status_code == 202
        data = resp.json()
        assert data["submission_id"] == str(submission.id)
        assert data["status"] == "received"
        assert data["duplicate"] is False

        # delivery_mode='in_app' + the derived node-context flow into create.
        kwargs = mock_create.await_args.kwargs
        assert kwargs["delivery_mode"] == "in_app"
        assert kwargs["course_node_id"] == root
        assert kwargs["tenant_id"] == STUB_TENANT_ID

    async def test_duplicate_returns_existing(self, client: AsyncClient) -> None:
        """An identical file returns the existing submission, duplicate=True."""
        adoc = uuid.uuid4()
        root = uuid.uuid4()
        existing = _mock_submission(status="completed")

        with (
            patch.object(
                AuthoredDocumentRepository,
                "get_by_id",
                return_value=_mock_task_doc(course_root_id=root),
            ),
            patch.object(
                CourseNodeRepository,
                "get_by_id",
                return_value=_mock_root_node(STUB_TENANT_ID),
            ),
            patch.object(StudentEnrollmentRepository, "is_enrolled", return_value=True),
            patch.object(
                DocumentSummaryRepository,
                "get_by_authored_document_id",
                return_value=_mock_summary("ready"),
            ),
            patch.object(HomeworkRepository, "find_duplicate", return_value=existing),
        ):
            resp = await client.post(_url(adoc), files=_files())

        assert resp.status_code == 202
        data = resp.json()
        assert data["submission_id"] == str(existing.id)
        assert data["status"] == "completed"
        assert data["duplicate"] is True


class TestPortalSubmitGates:
    async def test_unknown_task_404(self, client: AsyncClient) -> None:
        with patch.object(AuthoredDocumentRepository, "get_by_id", return_value=None):
            resp = await client.post(_url(uuid.uuid4()), files=_files())
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Task not found."

    async def test_non_task_document_404(self, client: AsyncClient) -> None:
        """A document with no task_type is not a task → generic 404."""
        with patch.object(
            AuthoredDocumentRepository,
            "get_by_id",
            return_value=_mock_task_doc(course_root_id=uuid.uuid4(), task_type=None),
        ):
            resp = await client.post(_url(uuid.uuid4()), files=_files())
        assert resp.status_code == 404

    async def test_foreign_tenant_404(self, client: AsyncClient) -> None:
        """A task whose course is in another tenant → generic 404."""
        root = uuid.uuid4()
        with (
            patch.object(
                AuthoredDocumentRepository,
                "get_by_id",
                return_value=_mock_task_doc(course_root_id=root),
            ),
            patch.object(
                CourseNodeRepository,
                "get_by_id",
                return_value=_mock_root_node(uuid.uuid4()),  # different tenant
            ),
        ):
            resp = await client.post(_url(uuid.uuid4()), files=_files())
        assert resp.status_code == 404

    async def test_not_enrolled_404(self, client: AsyncClient) -> None:
        """Enrollment gate (Q1): not enrolled in the task's course → 404."""
        root = uuid.uuid4()
        with (
            patch.object(
                AuthoredDocumentRepository,
                "get_by_id",
                return_value=_mock_task_doc(course_root_id=root),
            ),
            patch.object(
                CourseNodeRepository,
                "get_by_id",
                return_value=_mock_root_node(STUB_TENANT_ID),
            ),
            patch.object(
                StudentEnrollmentRepository, "is_enrolled", return_value=False
            ),
        ):
            resp = await client.post(_url(uuid.uuid4()), files=_files())
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Task not found."

    async def test_not_ready_409(self, client: AsyncClient) -> None:
        """An enrolled, valid task that is not ready → 409 (exists, not ready)."""
        root = uuid.uuid4()
        with (
            patch.object(
                AuthoredDocumentRepository,
                "get_by_id",
                return_value=_mock_task_doc(course_root_id=root),
            ),
            patch.object(
                CourseNodeRepository,
                "get_by_id",
                return_value=_mock_root_node(STUB_TENANT_ID),
            ),
            patch.object(StudentEnrollmentRepository, "is_enrolled", return_value=True),
            patch.object(
                DocumentSummaryRepository,
                "get_by_authored_document_id",
                return_value=_mock_summary("processing"),
            ),
        ):
            resp = await client.post(_url(uuid.uuid4()), files=_files())
        assert resp.status_code == 409

    async def test_bad_extension_422(self, client: AsyncClient) -> None:
        """File validation runs first — a disallowed extension → 422."""
        resp = await client.post(
            _url(uuid.uuid4()),
            files={"file": ("evil.exe", io.BytesIO(b"x"), "application/octet-stream")},
        )
        assert resp.status_code == 422


def _mock_reviewed_submission() -> MagicMock:
    """A fully reviewed submission whose review_result carries internal trace."""
    sub = MagicMock()
    sub.id = uuid.uuid4()
    sub.status = "completed"
    sub.score = 87
    sub.review_markdown = "## Good work\nWell done."
    # review_result carries the verdict AND internal trace that must NEVER leak.
    sub.review_result = {
        "verdict": {"passed": True, "correctness": "correct"},
        "layers": {"node": "INTERNAL — secret layered judgment"},
        "denoised_score": 87,
    }
    sub.safety_result = {"safe": True, "internal_flags": ["x"]}
    sub.sanity_result = {"verdict": "match", "confidence": 0.9}
    sub.created_at = datetime.now(UTC)
    sub.original_filename = "solution.py"
    return sub


class TestPortalReadList:
    async def test_owns_attempts_returns_items(self, client: AsyncClient) -> None:
        """A student who owns attempts sees them (newest-first list)."""
        sub = _mock_reviewed_submission()
        with patch.object(
            HomeworkRepository, "list_for_student_and_task", return_value=[sub]
        ):
            resp = await client.get(_url(uuid.uuid4()))
        assert resp.status_code == 200
        items = resp.json()
        assert len(items) == 1
        item = items[0]
        # List item is light — no review_markdown, no internal trace.
        assert set(item) == {
            "id",
            "status",
            "score",
            "verdict",
            "created_at",
            "original_filename",
        }
        assert item["verdict"] == {"passed": True, "correctness": "correct"}
        assert "review_markdown" not in item

    async def test_enrolled_no_attempts_empty(self, client: AsyncClient) -> None:
        """Enrolled, no attempts yet → 200 empty list."""
        root = uuid.uuid4()
        with (
            patch.object(
                HomeworkRepository, "list_for_student_and_task", return_value=[]
            ),
            patch.object(
                AuthoredDocumentRepository,
                "get_by_id",
                return_value=_mock_task_doc(course_root_id=root),
            ),
            patch.object(StudentEnrollmentRepository, "is_enrolled", return_value=True),
        ):
            resp = await client.get(_url(uuid.uuid4()))
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_not_enrolled_no_attempts_404(self, client: AsyncClient) -> None:
        """Neither enrolled nor any own attempts → generic 404."""
        root = uuid.uuid4()
        with (
            patch.object(
                HomeworkRepository, "list_for_student_and_task", return_value=[]
            ),
            patch.object(
                AuthoredDocumentRepository,
                "get_by_id",
                return_value=_mock_task_doc(course_root_id=root),
            ),
            patch.object(
                StudentEnrollmentRepository, "is_enrolled", return_value=False
            ),
        ):
            resp = await client.get(_url(uuid.uuid4()))
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Task not found."

    async def test_unknown_task_no_attempts_404(self, client: AsyncClient) -> None:
        """No attempts + unknown task → generic 404."""
        with (
            patch.object(
                HomeworkRepository, "list_for_student_and_task", return_value=[]
            ),
            patch.object(AuthoredDocumentRepository, "get_by_id", return_value=None),
        ):
            resp = await client.get(_url(uuid.uuid4()))
        assert resp.status_code == 404


class TestPortalReadDetail:
    async def test_owned_returns_curated_slice(self, client: AsyncClient) -> None:
        """Detail returns the curated slice and NEVER the internal trace."""
        sub = _mock_reviewed_submission()
        with patch.object(HomeworkRepository, "get_owned", return_value=sub):
            resp = await client.get(f"/api/v1/portal/submissions/{sub.id}")
        assert resp.status_code == 200
        data = resp.json()

        # Exactly the curated fields — and nothing else.
        assert set(data) == {
            "id",
            "status",
            "score",
            "verdict",
            "review_markdown",
            "created_at",
            "original_filename",
        }
        assert data["status"] == "completed"
        assert data["score"] == 87
        assert data["verdict"] == {"passed": True, "correctness": "correct"}
        assert data["review_markdown"] == "## Good work\nWell done."

        # The internal trace must NOT appear anywhere in the response body.
        body = resp.text
        assert "review_result" not in data
        assert "safety_result" not in data
        assert "sanity_result" not in data
        assert "INTERNAL" not in body
        assert "denoised_score" not in body
        assert "confidence" not in body

    async def test_verdict_none_until_reviewed(self, client: AsyncClient) -> None:
        """verdict is None when review_result has no verdict yet."""
        sub = MagicMock()
        sub.id = uuid.uuid4()
        sub.status = "reviewing"
        sub.score = None
        sub.review_markdown = None
        sub.review_result = None
        sub.created_at = datetime.now(UTC)
        sub.original_filename = "solution.py"
        with patch.object(HomeworkRepository, "get_owned", return_value=sub):
            resp = await client.get(f"/api/v1/portal/submissions/{sub.id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["verdict"] is None
        assert data["score"] is None
        assert data["review_markdown"] is None

    async def test_not_owned_404(self, client: AsyncClient) -> None:
        """A non-owned / unknown / soft-deleted submission → generic 404."""
        with patch.object(HomeworkRepository, "get_owned", return_value=None):
            resp = await client.get(f"/api/v1/portal/submissions/{uuid.uuid4()}")
        assert resp.status_code == 404
