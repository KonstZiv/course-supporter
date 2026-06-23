"""Tests for homework submission API endpoint."""

from __future__ import annotations

import io
import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient

from course_supporter.api.app import app
from course_supporter.api.deps import get_arq_redis, get_current_tenant, get_s3_client
from course_supporter.auth.context import TenantContext
from course_supporter.storage.authored_document_repository import (
    AuthoredDocumentRepository,
)
from course_supporter.storage.course_node_repository import CourseNodeRepository
from course_supporter.storage.database import get_session
from course_supporter.storage.homework_repository import HomeworkRepository
from course_supporter.storage.student_repository import StudentRepository

STUB_TENANT = TenantContext(
    tenant_id=uuid.uuid4(),
    tenant_name="test-tenant",
    scopes=["prep", "check"],
    plan_id="basic",
    key_prefix="cs_test",
)

ENQUEUE_FUNC = "course_supporter.api.routes.homework.enqueue_homework"


def _mock_node(
    *,
    node_id: uuid.UUID | None = None,
    tenant_id: uuid.UUID | None = None,
    parent_id: uuid.UUID | None = None,
) -> MagicMock:
    """Create a mock CourseNode."""
    node = MagicMock()
    node.id = node_id or uuid.uuid4()
    node.tenant_id = tenant_id or STUB_TENANT.tenant_id
    node.parent_id = parent_id
    return node


def _mock_student(student_id: uuid.UUID | None = None) -> MagicMock:
    """Create a mock Student."""
    student = MagicMock()
    student.id = student_id or uuid.uuid4()
    return student


def _mock_task_doc(
    *,
    course_root_id: uuid.UUID,
    task_type: str | None = "task",
    deleted_at: datetime | None = None,
) -> MagicMock:
    """Create a mock AuthoredDocument that is a valid task by default."""
    doc = MagicMock()
    doc.course_root_id = course_root_id
    doc.task_type = task_type
    doc.deleted_at = deleted_at
    return doc


def _mock_submission(
    submission_id: uuid.UUID | None = None,
    student_id: uuid.UUID | None = None,
) -> MagicMock:
    """Create a mock HomeworkSubmission."""
    sub = MagicMock()
    sub.id = submission_id or uuid.uuid4()
    sub.student_id = student_id or uuid.uuid4()
    sub.status = "received"
    sub.created_at = datetime.now(UTC)
    return sub


def _mock_job(job_id: uuid.UUID | None = None) -> MagicMock:
    """Create a mock Job."""
    job = MagicMock()
    job.id = job_id or uuid.uuid4()
    return job


@pytest.fixture()
def mock_session() -> AsyncMock:
    session = AsyncMock()
    session.add = MagicMock()
    return session


@pytest.fixture()
def mock_arq() -> MagicMock:
    return MagicMock()


@pytest.fixture()
def mock_s3() -> AsyncMock:
    s3 = AsyncMock()
    s3.upload_smart = AsyncMock(
        return_value=("http://localhost:9000/course-materials/key/file.py", 512)
    )
    s3.delete_object = AsyncMock()
    return s3


@pytest.fixture()
def course_node_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture()
def node_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture()
def authored_document_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture()
async def client(
    mock_session: AsyncMock, mock_arq: MagicMock, mock_s3: AsyncMock
) -> AsyncClient:
    app.dependency_overrides[get_session] = lambda: mock_session
    app.dependency_overrides[get_current_tenant] = lambda: STUB_TENANT
    app.dependency_overrides[get_arq_redis] = lambda: mock_arq
    app.dependency_overrides[get_s3_client] = lambda: mock_s3
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as ac:
        yield ac  # type: ignore[misc]
    app.dependency_overrides.clear()


def _submit_form(
    *,
    course_node_id: uuid.UUID,
    node_id: uuid.UUID,
    authored_document_id: uuid.UUID | None = None,
    filename: str = "solution.py",
    content: bytes = b"print('hello')",
    content_type: str = "text/x-python",
    student_external_id: str = "ext-student-1",
    webhook_url: str | None = None,
    student_note: str | None = None,
) -> dict[str, object]:
    """Build form data + files for submit_homework."""
    data: dict[str, object] = {
        "student_external_id": student_external_id,
        "course_node_id": str(course_node_id),
        "node_id": str(node_id),
        "authored_document_id": str(authored_document_id or uuid.uuid4()),
    }
    if webhook_url is not None:
        data["webhook_url"] = webhook_url
    if student_note is not None:
        data["student_note"] = student_note
    return data


class TestSubmitHomework:
    """POST /api/v1/homework/submit"""

    async def test_returns_202(
        self,
        client: AsyncClient,
        course_node_id: uuid.UUID,
        node_id: uuid.UUID,
        authored_document_id: uuid.UUID,
    ) -> None:
        """Successful submission returns 202 with IDs."""
        student = _mock_student()
        submission = _mock_submission(student_id=student.id)
        job = _mock_job()

        def get_node_by_id(requested_id: uuid.UUID) -> MagicMock | None:
            if requested_id == course_node_id:
                return _mock_node(node_id=course_node_id)
            if requested_id == node_id:
                return _mock_node(
                    node_id=node_id,
                    parent_id=course_node_id,
                )
            return None

        with (
            patch.object(
                CourseNodeRepository,
                "get_by_id",
                side_effect=get_node_by_id,
            ),
            patch.object(
                AuthoredDocumentRepository,
                "get_by_id",
                return_value=_mock_task_doc(course_root_id=course_node_id),
            ),
            patch.object(
                StudentRepository,
                "get_or_create",
                return_value=(student, True),
            ),
            patch.object(
                HomeworkRepository,
                "find_duplicate",
                return_value=None,
            ),
            patch.object(HomeworkRepository, "create", return_value=submission),
            patch.object(HomeworkRepository, "set_job_id", return_value=None),
            patch(ENQUEUE_FUNC, new_callable=AsyncMock, return_value=job),
        ):
            resp = await client.post(
                "/api/v1/homework/submit",
                data=_submit_form(
                    course_node_id=course_node_id,
                    node_id=node_id,
                    authored_document_id=authored_document_id,
                ),
                files={
                    "file": (
                        "solution.py",
                        io.BytesIO(b"print('hello')"),
                        "text/x-python",
                    )
                },
            )

        assert resp.status_code == 202
        data = resp.json()
        assert data["submission_id"] == str(submission.id)
        assert data["student_id"] == str(student.id)
        assert data["status"] == "received"
        assert data["job_id"] == str(job.id)

    async def test_student_note_threaded_to_create(
        self,
        client: AsyncClient,
        course_node_id: uuid.UUID,
        node_id: uuid.UUID,
        authored_document_id: uuid.UUID,
    ) -> None:
        """D7-local student_note flows from the form into repo.create (KD15)."""
        student = _mock_student()
        submission = _mock_submission(student_id=student.id)
        job = _mock_job()

        def get_node_by_id(requested_id: uuid.UUID) -> MagicMock | None:
            if requested_id == course_node_id:
                return _mock_node(node_id=course_node_id)
            if requested_id == node_id:
                return _mock_node(node_id=node_id, parent_id=course_node_id)
            return None

        with (
            patch.object(CourseNodeRepository, "get_by_id", side_effect=get_node_by_id),
            patch.object(
                AuthoredDocumentRepository,
                "get_by_id",
                return_value=_mock_task_doc(course_root_id=course_node_id),
            ),
            patch.object(
                StudentRepository, "get_or_create", return_value=(student, True)
            ),
            patch.object(HomeworkRepository, "find_duplicate", return_value=None),
            patch.object(
                HomeworkRepository, "create", return_value=submission
            ) as mock_create,
            patch.object(HomeworkRepository, "set_job_id", return_value=None),
            patch(ENQUEUE_FUNC, new_callable=AsyncMock, return_value=job),
        ):
            resp = await client.post(
                "/api/v1/homework/submit",
                data=_submit_form(
                    course_node_id=course_node_id,
                    node_id=node_id,
                    authored_document_id=authored_document_id,
                    student_note="Я не певен щодо рекурсії — чи коректна база?",
                ),
                files={
                    "file": (
                        "solution.py",
                        io.BytesIO(b"print('hello')"),
                        "text/x-python",
                    )
                },
            )

        assert resp.status_code == 202
        assert (
            mock_create.call_args.kwargs["student_note"]
            == "Я не певен щодо рекурсії — чи коректна база?"
        )

    async def test_invalid_extension_returns_422(
        self,
        client: AsyncClient,
        course_node_id: uuid.UUID,
        node_id: uuid.UUID,
    ) -> None:
        """File with disallowed extension returns 422."""
        resp = await client.post(
            "/api/v1/homework/submit",
            data=_submit_form(course_node_id=course_node_id, node_id=node_id),
            files={
                "file": ("malware.exe", io.BytesIO(b"\x00"), "application/octet-stream")
            },
        )
        assert resp.status_code == 422
        assert "'.exe' is not allowed" in resp.json()["detail"]

    async def test_course_node_not_found_returns_404(
        self,
        client: AsyncClient,
        course_node_id: uuid.UUID,
        node_id: uuid.UUID,
    ) -> None:
        """Non-existent course node returns 404."""
        with patch.object(CourseNodeRepository, "get_by_id", return_value=None):
            resp = await client.post(
                "/api/v1/homework/submit",
                data=_submit_form(course_node_id=course_node_id, node_id=node_id),
                files={"file": ("solution.py", io.BytesIO(b"x=1"), "text/x-python")},
            )
        assert resp.status_code == 404
        assert "Course node" in resp.json()["detail"]

    async def test_non_root_course_node_returns_422(
        self,
        client: AsyncClient,
        course_node_id: uuid.UUID,
        node_id: uuid.UUID,
    ) -> None:
        """course_node_id that is not a root node returns 422."""
        with patch.object(
            CourseNodeRepository,
            "get_by_id",
            return_value=_mock_node(
                node_id=course_node_id,
                parent_id=uuid.uuid4(),  # Not root
            ),
        ):
            resp = await client.post(
                "/api/v1/homework/submit",
                data=_submit_form(course_node_id=course_node_id, node_id=node_id),
                files={"file": ("solution.py", io.BytesIO(b"x=1"), "text/x-python")},
            )
        assert resp.status_code == 422
        assert "root node" in resp.json()["detail"]

    async def test_wrong_tenant_returns_404(
        self,
        client: AsyncClient,
        course_node_id: uuid.UUID,
        node_id: uuid.UUID,
    ) -> None:
        """Course node belonging to another tenant returns 404."""
        other_tenant = uuid.uuid4()
        with patch.object(
            CourseNodeRepository,
            "get_by_id",
            return_value=_mock_node(node_id=course_node_id, tenant_id=other_tenant),
        ):
            resp = await client.post(
                "/api/v1/homework/submit",
                data=_submit_form(course_node_id=course_node_id, node_id=node_id),
                files={"file": ("solution.py", io.BytesIO(b"x=1"), "text/x-python")},
            )
        assert resp.status_code == 404

    async def test_unknown_task_returns_404(
        self,
        client: AsyncClient,
        course_node_id: uuid.UUID,
        node_id: uuid.UUID,
    ) -> None:
        """authored_document_id that does not resolve returns 404."""

        def get_node_by_id(requested_id: uuid.UUID) -> MagicMock | None:
            if requested_id == course_node_id:
                return _mock_node(node_id=course_node_id)
            if requested_id == node_id:
                return _mock_node(node_id=node_id, parent_id=course_node_id)
            return None

        with (
            patch.object(CourseNodeRepository, "get_by_id", side_effect=get_node_by_id),
            patch.object(AuthoredDocumentRepository, "get_by_id", return_value=None),
        ):
            resp = await client.post(
                "/api/v1/homework/submit",
                data=_submit_form(course_node_id=course_node_id, node_id=node_id),
                files={"file": ("solution.py", io.BytesIO(b"x=1"), "text/x-python")},
            )
        assert resp.status_code == 404
        assert "Task not found" in resp.json()["detail"]

    async def test_task_from_other_course_returns_404(
        self,
        client: AsyncClient,
        course_node_id: uuid.UUID,
        node_id: uuid.UUID,
    ) -> None:
        """Task whose course root differs from course_node_id returns 404."""

        def get_node_by_id(requested_id: uuid.UUID) -> MagicMock | None:
            if requested_id == course_node_id:
                return _mock_node(node_id=course_node_id)
            if requested_id == node_id:
                return _mock_node(node_id=node_id, parent_id=course_node_id)
            return None

        with (
            patch.object(CourseNodeRepository, "get_by_id", side_effect=get_node_by_id),
            patch.object(
                AuthoredDocumentRepository,
                "get_by_id",
                return_value=_mock_task_doc(course_root_id=uuid.uuid4()),
            ),
        ):
            resp = await client.post(
                "/api/v1/homework/submit",
                data=_submit_form(course_node_id=course_node_id, node_id=node_id),
                files={"file": ("solution.py", io.BytesIO(b"x=1"), "text/x-python")},
            )
        assert resp.status_code == 404
        assert "Task not found" in resp.json()["detail"]

    async def test_non_task_document_returns_422(
        self,
        client: AsyncClient,
        course_node_id: uuid.UUID,
        node_id: uuid.UUID,
    ) -> None:
        """authored_document_id pointing at a non-task document returns 422."""

        def get_node_by_id(requested_id: uuid.UUID) -> MagicMock | None:
            if requested_id == course_node_id:
                return _mock_node(node_id=course_node_id)
            if requested_id == node_id:
                return _mock_node(node_id=node_id, parent_id=course_node_id)
            return None

        with (
            patch.object(CourseNodeRepository, "get_by_id", side_effect=get_node_by_id),
            patch.object(
                AuthoredDocumentRepository,
                "get_by_id",
                return_value=_mock_task_doc(
                    course_root_id=course_node_id, task_type=None
                ),
            ),
        ):
            resp = await client.post(
                "/api/v1/homework/submit",
                data=_submit_form(course_node_id=course_node_id, node_id=node_id),
                files={"file": ("solution.py", io.BytesIO(b"x=1"), "text/x-python")},
            )
        assert resp.status_code == 422
        assert "must reference a task" in resp.json()["detail"]

    async def test_webhook_ssrf_rejected(
        self,
        client: AsyncClient,
        course_node_id: uuid.UUID,
        node_id: uuid.UUID,
    ) -> None:
        """Webhook URL targeting private IP rejected (mocked validator)."""
        with patch(
            "course_supporter.api.routes.homework.validate_webhook_url",
            side_effect=HTTPException(status_code=422, detail="SSRF: not allowed"),
        ):
            resp = await client.post(
                "/api/v1/homework/submit",
                data=_submit_form(
                    course_node_id=course_node_id,
                    node_id=node_id,
                    webhook_url="https://evil.internal/hook",
                ),
                files={
                    "file": (
                        "solution.py",
                        io.BytesIO(b"x=1"),
                        "text/x-python",
                    )
                },
            )
        assert resp.status_code == 422
        assert "not allowed" in resp.json()["detail"]

    async def test_oversized_file_after_upload_returns_422(
        self,
        client: AsyncClient,
        course_node_id: uuid.UUID,
        node_id: uuid.UUID,
        mock_s3: AsyncMock,
    ) -> None:
        """File exceeding 10 MB after upload returns 422 and cleans up S3."""
        oversized_bytes = 11 * 1024 * 1024
        mock_s3.upload_smart = AsyncMock(
            return_value=("http://s3/key/big.py", oversized_bytes)
        )

        with (
            patch.object(
                CourseNodeRepository,
                "get_by_id",
                side_effect=[
                    _mock_node(node_id=course_node_id),
                    _mock_node(
                        node_id=node_id,
                        parent_id=course_node_id,
                    ),
                ],
            ),
            patch.object(
                AuthoredDocumentRepository,
                "get_by_id",
                return_value=_mock_task_doc(course_root_id=course_node_id),
            ),
        ):
            resp = await client.post(
                "/api/v1/homework/submit",
                data=_submit_form(course_node_id=course_node_id, node_id=node_id),
                files={
                    "file": ("big.py", io.BytesIO(b"x"), "text/x-python"),
                },
            )
        assert resp.status_code == 422
        assert "too large" in resp.json()["detail"].lower()
        mock_s3.delete_object.assert_awaited_once()

    async def test_duplicate_file_returns_cached(
        self,
        client: AsyncClient,
        course_node_id: uuid.UUID,
        node_id: uuid.UUID,
    ) -> None:
        """Identical file already reviewed returns cached result."""
        student = _mock_student()
        existing = _mock_submission(student_id=student.id)
        existing.status = "completed"
        existing.job_id = uuid.uuid4()

        def get_node_by_id(requested_id: uuid.UUID) -> MagicMock | None:
            if requested_id == course_node_id:
                return _mock_node(node_id=course_node_id)
            if requested_id == node_id:
                return _mock_node(
                    node_id=node_id,
                    parent_id=course_node_id,
                )
            return None

        with (
            patch.object(
                CourseNodeRepository,
                "get_by_id",
                side_effect=get_node_by_id,
            ),
            patch.object(
                AuthoredDocumentRepository,
                "get_by_id",
                return_value=_mock_task_doc(course_root_id=course_node_id),
            ),
            patch.object(
                StudentRepository,
                "get_or_create",
                return_value=(student, True),
            ),
            patch.object(
                HomeworkRepository,
                "find_duplicate",
                return_value=existing,
            ),
        ):
            resp = await client.post(
                "/api/v1/homework/submit",
                data=_submit_form(
                    course_node_id=course_node_id,
                    node_id=node_id,
                ),
                files={
                    "file": (
                        "solution.py",
                        io.BytesIO(b"print('hello')"),
                        "text/x-python",
                    )
                },
            )

        assert resp.status_code == 202
        data = resp.json()
        assert data["submission_id"] == str(existing.id)
        assert data["duplicate"] is True
        assert data["status"] == "completed"


class TestSubmitHomeworkOpenAPI:
    """Regression: ensure submit endpoint accepts multipart/form-data."""

    async def test_openapi_content_type_is_multipart(self, client: AsyncClient) -> None:
        """Homework submit endpoint declares multipart/form-data."""
        resp = await client.get("/openapi.json")
        schema = resp.json()
        path = "/api/v1/homework/submit"
        content_types = list(
            schema["paths"][path]["post"]["requestBody"]["content"].keys()
        )
        assert "multipart/form-data" in content_types

    async def test_openapi_submit_form_exposes_student_note(
        self, client: AsyncClient
    ) -> None:
        """The submission input contract surfaces student_note (D7-local, KD15)."""
        resp = await client.get("/openapi.json")
        schema = resp.json()
        path = "/api/v1/homework/submit"
        form = schema["paths"][path]["post"]["requestBody"]["content"][
            "multipart/form-data"
        ]["schema"]
        body_model = form["$ref"].split("/")[-1]
        props = schema["components"]["schemas"][body_model]["properties"]
        assert "student_note" in props
