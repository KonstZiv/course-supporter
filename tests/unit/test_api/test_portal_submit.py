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
from course_supporter.models.source import MaterialRole
from course_supporter.storage.authored_document_repository import (
    AuthoredDocumentRepository,
)
from course_supporter.storage.course_node_repository import CourseNodeRepository
from course_supporter.storage.document_summary_repository import (
    DocumentSummaryRepository,
)
from course_supporter.storage.homework_repository import HomeworkRepository
from course_supporter.storage.project_base_repository import ProjectBaseRepository
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
    material_role: str = MaterialRole.EDUCATIONAL.value,
) -> MagicMock:
    doc = MagicMock()
    doc.course_root_id = course_root_id
    doc.course_node_id = course_node_id or uuid.uuid4()
    doc.task_type = task_type
    doc.deleted_at = deleted_at
    doc.material_role = material_role
    return doc


def _mock_root_node(tenant_id: uuid.UUID) -> MagicMock:
    node = MagicMock()
    node.id = uuid.uuid4()
    node.tenant_id = tenant_id
    node.parent_id = None
    return node


def _mock_base(archive_key: str = "bases/original.zip") -> MagicMock:
    base = MagicMock()
    base.archive_key = archive_key
    base.state = "ready"
    return base


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

    async def test_methodological_project_task_404(self, client: AsyncClient) -> None:
        """A methodological document (even a PROJECT task) is invisible in the
        student's tree, so submit collapses to the same generic 404 (step A, P2)."""
        root = uuid.uuid4()
        with (
            patch.object(
                AuthoredDocumentRepository,
                "get_by_id",
                return_value=_mock_task_doc(
                    course_root_id=root,
                    task_type="project",
                    material_role=MaterialRole.METHODOLOGICAL.value,
                ),
            ),
            patch.object(
                CourseNodeRepository,
                "get_by_id",
                return_value=_mock_root_node(STUB_TENANT_ID),
            ),
            patch.object(StudentEnrollmentRepository, "is_enrolled", return_value=True),
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
    # Non-project submission → no snapshot manifest, so the detail delta is None.
    sub.snapshot_manifest = None
    sub.base_id = None
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
        # List item is light — no review_markdown, no internal trace. The
        # ``rejection`` / ``not_opened`` / ``recovered_encoding`` trio is the
        # reason code, the skipped files and how the file was read, all derived
        # on read; none carries the trace (gates §1.7, step Г2 §1.2). All three
        # ride the LIST row because the error and pending branches of the review
        # detail do not fetch (DD-6-D).
        assert set(item) == {
            "id",
            "status",
            "score",
            "verdict",
            "created_at",
            "original_filename",
            "rejection",
            "not_opened",
            "recovered_encoding",
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

    async def test_methodological_project_task_no_attempts_404(
        self, client: AsyncClient
    ) -> None:
        """No own attempts + a methodological PROJECT task (invisible in the
        tree) → generic 404, even for an enrolled student (step A, P2)."""
        root = uuid.uuid4()
        with (
            patch.object(
                HomeworkRepository, "list_for_student_and_task", return_value=[]
            ),
            patch.object(
                AuthoredDocumentRepository,
                "get_by_id",
                return_value=_mock_task_doc(
                    course_root_id=root,
                    task_type="project",
                    material_role=MaterialRole.METHODOLOGICAL.value,
                ),
            ),
            patch.object(StudentEnrollmentRepository, "is_enrolled", return_value=True),
        ):
            resp = await client.get(_url(uuid.uuid4()))
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Task not found."


class TestPortalReadDetail:
    async def test_owned_returns_curated_slice(self, client: AsyncClient) -> None:
        """Detail returns the curated slice and NEVER the internal trace."""
        sub = _mock_reviewed_submission()
        with patch.object(HomeworkRepository, "get_owned", return_value=sub):
            resp = await client.get(f"/api/v1/portal/submissions/{sub.id}")
        assert resp.status_code == 200
        data = resp.json()

        # Exactly the curated fields — and nothing else. A non-project
        # submission carries delta=None (KD18 P5 nested-nullable).
        assert set(data) == {
            "id",
            "status",
            "score",
            "verdict",
            "review_markdown",
            "created_at",
            "original_filename",
            "delta",
            "rejection",
            "not_opened",
            "recovered_encoding",
        }
        assert data["status"] == "completed"
        assert data["score"] == 87
        assert data["verdict"] == {"passed": True, "correctness": "correct"}
        assert data["review_markdown"] == "## Good work\nWell done."
        assert data["delta"] is None

        # The internal trace must NOT appear anywhere in the response body.
        body = resp.text
        assert "review_result" not in data
        assert "safety_result" not in data
        assert "sanity_result" not in data
        assert "error_message" not in data
        assert "INTERNAL" not in body
        assert "denoised_score" not in body
        assert "internal_flags" not in body
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
        sub.snapshot_manifest = None  # non-project → delta None
        sub.base_id = None
        with patch.object(HomeworkRepository, "get_owned", return_value=sub):
            resp = await client.get(f"/api/v1/portal/submissions/{sub.id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["verdict"] is None
        assert data["score"] is None
        assert data["review_markdown"] is None
        assert data["delta"] is None

    async def test_not_owned_404(self, client: AsyncClient) -> None:
        """A non-owned / unknown / soft-deleted submission → generic 404."""
        with patch.object(HomeworkRepository, "get_owned", return_value=None):
            resp = await client.get(f"/api/v1/portal/submissions/{uuid.uuid4()}")
        assert resp.status_code == 404


class TestPortalSubmissionDelta:
    """KD18 P5: the I2 delta receipt (counters + staleness), derived on read.

    ``manifest_from_jsonb`` / ``compute_delta`` are patched at the route module
    to pin the receipt WIRING (row -> counts + staleness) without constructing a
    full normalizer manifest; the byte-level delta itself is the normalizer's
    own tested concern.
    """

    _MOD = "course_supporter.api.routes.portal_submissions"

    def _project_sub(
        self, *, base_id: uuid.UUID | None, snapshot_manifest: dict | None
    ) -> MagicMock:
        sub = MagicMock()
        sub.id = uuid.uuid4()
        sub.status = "completed"
        sub.score = 90
        sub.review_markdown = "ok"
        sub.review_result = None
        sub.created_at = datetime.now(UTC)
        sub.original_filename = "proj.zip"
        sub.authored_document_id = uuid.uuid4()
        sub.base_id = base_id
        sub.snapshot_manifest = snapshot_manifest
        return sub

    async def test_base_set_counts_and_stale(self, client: AsyncClient) -> None:
        """base_id set → compute_delta counts + staleness (base v1 < latest v3)."""
        sub = self._project_sub(base_id=uuid.uuid4(), snapshot_manifest={"schema": 1})
        base = MagicMock()
        base.manifest = {"schema": 1}
        base.version = 1
        latest = MagicMock()
        latest.version = 3
        fake_delta = MagicMock()
        fake_delta.changed = ("a.py", "b.py")
        fake_delta.new = ("c.py",)
        fake_delta.deleted = ()
        with (
            patch.object(HomeworkRepository, "get_owned", return_value=sub),
            patch.object(ProjectBaseRepository, "get_by_id", return_value=base),
            patch.object(
                ProjectBaseRepository, "get_latest_ready", return_value=latest
            ),
            patch(f"{self._MOD}.manifest_from_jsonb", return_value=MagicMock()),
            patch(f"{self._MOD}.compute_delta", return_value=fake_delta),
        ):
            resp = await client.get(f"/api/v1/portal/submissions/{sub.id}")
        assert resp.status_code == 200
        assert resp.json()["delta"] == {
            "changed": 2,
            "new": 1,
            "deleted": 0,
            "base_version": 1,
            "latest_version": 3,
            "is_stale": True,
        }

    async def test_base_set_not_stale_when_built_on_latest(
        self, client: AsyncClient
    ) -> None:
        """base v2 == latest v2 → is_stale False."""
        sub = self._project_sub(base_id=uuid.uuid4(), snapshot_manifest={"schema": 1})
        base = MagicMock()
        base.manifest = {"schema": 1}
        base.version = 2
        latest = MagicMock()
        latest.version = 2
        fake_delta = MagicMock()
        fake_delta.changed = ()
        fake_delta.new = ()
        fake_delta.deleted = ("gone.py",)
        with (
            patch.object(HomeworkRepository, "get_owned", return_value=sub),
            patch.object(ProjectBaseRepository, "get_by_id", return_value=base),
            patch.object(
                ProjectBaseRepository, "get_latest_ready", return_value=latest
            ),
            patch(f"{self._MOD}.manifest_from_jsonb", return_value=MagicMock()),
            patch(f"{self._MOD}.compute_delta", return_value=fake_delta),
        ):
            resp = await client.get(f"/api/v1/portal/submissions/{sub.id}")
        delta = resp.json()["delta"]
        assert delta["deleted"] == 1
        assert delta["base_version"] == 2
        assert delta["latest_version"] == 2
        assert delta["is_stale"] is False

    async def test_no_base_all_new_null_staleness(self, client: AsyncClient) -> None:
        """Project submission, base_id None → all-new, null staleness."""
        sub = self._project_sub(base_id=None, snapshot_manifest={"schema": 1})
        sub_manifest = MagicMock()
        sub_manifest.included = ("a.py", "b.py", "c.py")  # 3 new
        with (
            patch.object(HomeworkRepository, "get_owned", return_value=sub),
            patch(f"{self._MOD}.manifest_from_jsonb", return_value=sub_manifest),
        ):
            resp = await client.get(f"/api/v1/portal/submissions/{sub.id}")
        assert resp.json()["delta"] == {
            "changed": 0,
            "new": 3,
            "deleted": 0,
            "base_version": None,
            "latest_version": None,
            "is_stale": False,
        }

    async def test_base_row_gone_falls_back_to_all_new(
        self, client: AsyncClient
    ) -> None:
        """base_id set but the base row vanished → defensive all-new, no error."""
        sub = self._project_sub(base_id=uuid.uuid4(), snapshot_manifest={"schema": 1})
        sub_manifest = MagicMock()
        sub_manifest.included = ("only.py",)
        with (
            patch.object(HomeworkRepository, "get_owned", return_value=sub),
            patch.object(ProjectBaseRepository, "get_by_id", return_value=None),
            patch(f"{self._MOD}.manifest_from_jsonb", return_value=sub_manifest),
        ):
            resp = await client.get(f"/api/v1/portal/submissions/{sub.id}")
        assert resp.status_code == 200
        assert resp.json()["delta"] == {
            "changed": 0,
            "new": 1,
            "deleted": 0,
            "base_version": None,
            "latest_version": None,
            "is_stale": False,
        }


def _base_url(authored_document_id: uuid.UUID) -> str:
    return f"/api/v1/portal/tasks/{authored_document_id}/base"


class TestPortalTaskBaseDownload:
    """KD18 P5: presigned download of a project task's active base ORIGINAL.

    Bearer-session + enrollment-scoped counterpart of the mode-1 API-key route;
    access failures collapse to a generic 404, a visible task with no READY base
    yet returns a DISTINCT 404.
    """

    async def test_ready_base_returns_presigned_original(
        self, client: AsyncClient, mock_s3: AsyncMock
    ) -> None:
        """An enrolled student, project task, READY base → 200 + presigned URL."""
        root = uuid.uuid4()
        url = "https://s3.local/course-materials/bases/original.zip?sig=abc"
        mock_s3.generate_presigned_get_url = AsyncMock(return_value=url)
        with (
            patch.object(
                AuthoredDocumentRepository,
                "get_by_id",
                return_value=_mock_task_doc(course_root_id=root, task_type="project"),
            ),
            patch.object(
                CourseNodeRepository,
                "get_by_id",
                return_value=_mock_root_node(STUB_TENANT_ID),
            ),
            patch.object(StudentEnrollmentRepository, "is_enrolled", return_value=True),
            patch.object(
                ProjectBaseRepository,
                "get_latest_ready",
                return_value=_mock_base("bases/original.zip"),
            ),
        ):
            resp = await client.get(_base_url(uuid.uuid4()))
        assert resp.status_code == 200
        assert resp.json() == {"original_url": url}
        # Presigns the ORIGINAL archive_key (never the normalized snapshot, KD17).
        mock_s3.generate_presigned_get_url.assert_awaited_once_with(
            "bases/original.zip"
        )

    async def test_no_ready_base_distinct_404(self, client: AsyncClient) -> None:
        """Visible project task but no READY base → DISTINCT 404 (not generic)."""
        root = uuid.uuid4()
        with (
            patch.object(
                AuthoredDocumentRepository,
                "get_by_id",
                return_value=_mock_task_doc(course_root_id=root, task_type="project"),
            ),
            patch.object(
                CourseNodeRepository,
                "get_by_id",
                return_value=_mock_root_node(STUB_TENANT_ID),
            ),
            patch.object(StudentEnrollmentRepository, "is_enrolled", return_value=True),
            patch.object(ProjectBaseRepository, "get_latest_ready", return_value=None),
        ):
            resp = await client.get(_base_url(uuid.uuid4()))
        assert resp.status_code == 404
        assert resp.json()["detail"] == "No base is available for this task yet."

    async def test_not_enrolled_generic_404(self, client: AsyncClient) -> None:
        """Not enrolled → generic 'Task not found.' (never leaks existence)."""
        root = uuid.uuid4()
        with (
            patch.object(
                AuthoredDocumentRepository,
                "get_by_id",
                return_value=_mock_task_doc(course_root_id=root, task_type="project"),
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
            resp = await client.get(_base_url(uuid.uuid4()))
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Task not found."

    async def test_non_project_task_404(self, client: AsyncClient) -> None:
        """A non-project task carries no base → generic 404."""
        with patch.object(
            AuthoredDocumentRepository,
            "get_by_id",
            return_value=_mock_task_doc(course_root_id=uuid.uuid4(), task_type="task"),
        ):
            resp = await client.get(_base_url(uuid.uuid4()))
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Task not found."

    async def test_methodological_project_task_generic_404(
        self, client: AsyncClient
    ) -> None:
        """A methodological PROJECT task is invisible in the tree → generic
        'Task not found.' (collapses before the base-availability check; step A,
        P2). Tenant/enrollment are mocked as passing, so only the role gate can
        produce this 404."""
        root = uuid.uuid4()
        with (
            patch.object(
                AuthoredDocumentRepository,
                "get_by_id",
                return_value=_mock_task_doc(
                    course_root_id=root,
                    task_type="project",
                    material_role=MaterialRole.METHODOLOGICAL.value,
                ),
            ),
            patch.object(
                CourseNodeRepository,
                "get_by_id",
                return_value=_mock_root_node(STUB_TENANT_ID),
            ),
            patch.object(StudentEnrollmentRepository, "is_enrolled", return_value=True),
        ):
            resp = await client.get(_base_url(uuid.uuid4()))
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Task not found."

    async def test_unknown_task_404(self, client: AsyncClient) -> None:
        with patch.object(AuthoredDocumentRepository, "get_by_id", return_value=None):
            resp = await client.get(_base_url(uuid.uuid4()))
        assert resp.status_code == 404

    async def test_foreign_tenant_404(self, client: AsyncClient) -> None:
        """A task whose course is in another tenant → generic 404."""
        root = uuid.uuid4()
        with (
            patch.object(
                AuthoredDocumentRepository,
                "get_by_id",
                return_value=_mock_task_doc(course_root_id=root, task_type="project"),
            ),
            patch.object(
                CourseNodeRepository,
                "get_by_id",
                return_value=_mock_root_node(uuid.uuid4()),  # different tenant
            ),
        ):
            resp = await client.get(_base_url(uuid.uuid4()))
        assert resp.status_code == 404


def _mock_terminal_submission(
    *,
    status: str,
    safety_result: object = None,
    error_message: str | None = None,
) -> MagicMock:
    sub = MagicMock()
    sub.id = uuid.uuid4()
    sub.status = status
    sub.score = None
    sub.review_markdown = None
    sub.review_result = None
    sub.safety_result = safety_result
    sub.sanity_result = None
    sub.error_message = error_message
    sub.created_at = datetime.now(UTC)
    sub.original_filename = "submission.zip"
    sub.snapshot_manifest = None
    sub.base_id = None
    return sub


class TestCuratedRejection:
    """One code per outcome, read from whichever source decided it.

    Three sources write a terminal outcome in three shapes (DD-SP-Q records
    that the two families store it differently and why). The read path derives
    the code rather than reading a fourth column that would have to be kept in
    step — and never reads ``error_message``, which is a developer string.
    """

    def test_stage1_refusal_yields_its_category(self) -> None:
        from course_supporter.api.routes._portal_shared import curated_rejection

        sub = _mock_terminal_submission(
            status="rejected",
            safety_result={
                "source": "stage1",
                "is_safe": False,
                "category": "magic_mismatch",
                "detail": "extension 'zip' expects ('application/zip',) but …",
            },
            error_message="extension 'zip' expects ('application/zip',) but …",
        )
        rejection = curated_rejection(sub)
        assert rejection is not None
        assert rejection.code == "magic_mismatch"
        # The curated detail is the filename, never the raiser's message.
        assert rejection.details == "submission.zip"

    def test_sanity_mismatch_yields_the_verdict(self) -> None:
        from course_supporter.api.routes._portal_shared import curated_rejection

        rejection = curated_rejection(_mock_terminal_submission(status="mismatch"))
        assert rejection is not None
        assert rejection.code == "mismatch"

    def test_stage2_refusal_yields_stage2_rejected(self) -> None:
        from course_supporter.api.routes._portal_shared import curated_rejection

        sub = _mock_terminal_submission(
            status="rejected",
            safety_result={
                "source": "stage2",
                "is_safe": False,
                "violations": ["harmful_content"],
                "reasoning": "INTERNAL classifier reasoning",
            },
        )
        rejection = curated_rejection(sub)
        assert rejection is not None
        assert rejection.code == "stage2_rejected"

    def test_stage2_pass_is_not_a_rejection(self) -> None:
        from course_supporter.api.routes._portal_shared import curated_rejection

        sub = _mock_terminal_submission(
            status="completed",
            safety_result={"source": "stage2", "is_safe": True, "violations": []},
        )
        assert curated_rejection(sub) is None

    def test_unknown_source_falls_back_to_the_status_phrase(self) -> None:
        # A normalizer rejection (DD-6-Z): no honest code to derive, so none is
        # invented and the interface keeps using its status phrase.
        from course_supporter.api.routes._portal_shared import curated_rejection

        sub = _mock_terminal_submission(
            status="rejected",
            safety_result={"source": "normalizer", "reason": "unpack failed"},
        )
        assert curated_rejection(sub) is None


class TestCuratedNotOpened:
    def test_listed_on_a_passing_submission(self) -> None:
        # The whole point: a review that rested on part of the work must say so
        # even when it passed.
        from course_supporter.api.routes._portal_shared import curated_not_opened

        sub = _mock_terminal_submission(
            status="completed",
            safety_result={
                "source": "stage2",
                "is_safe": True,
                "violations": [],
                "not_opened": [
                    {"arcname": ".gitignore", "reason": "forbidden_type", "size": 40},
                    {"arcname": "logo.png", "reason": "forbidden_type", "size": 2048},
                ],
            },
        )
        entries = curated_not_opened(sub)
        assert [e.path for e in entries] == [".gitignore", "logo.png"]
        assert entries[1].size == 2048

    def test_empty_when_everything_was_read(self) -> None:
        from course_supporter.api.routes._portal_shared import curated_not_opened

        sub = _mock_terminal_submission(
            status="completed",
            safety_result={"source": "stage2", "is_safe": True, "violations": []},
        )
        assert curated_not_opened(sub) == []

    def test_malformed_records_are_skipped_not_crashed_on(self) -> None:
        # ``safety_result`` is free-form JSONB written by older code too; a row
        # that does not match the shape must not take the read path down.
        from course_supporter.api.routes._portal_shared import curated_not_opened

        sub = _mock_terminal_submission(
            status="completed",
            safety_result={
                "source": "stage2",
                "not_opened": ["a string", {"arcname": "ok.png"}, None],
            },
        )
        assert curated_not_opened(sub) == []


class TestCuratedRecoveredEncoding:
    """Three values, three different facts — the reader must keep them apart.

    ``utf-8`` and ``None`` both mean "nothing to tell the student", but for
    different reasons, and the projection is not allowed to collapse them:
    the interface decides what to show, and a server that answered ``None``
    for a direct decode would make "was it an archive?" unanswerable.
    """

    @staticmethod
    def _read(safety_result: object) -> str | None:
        from course_supporter.api.routes._portal_shared import (
            curated_recovered_encoding,
        )

        return curated_recovered_encoding(
            _mock_terminal_submission(status="completed", safety_result=safety_result)
        )

    def test_names_the_recovered_encoding(self) -> None:
        assert (
            self._read(
                {"source": "stage2", "is_safe": True, "recovered_encoding": "cp1251"}
            )
            == "cp1251"
        )

    def test_utf8_is_carried_through_not_flattened(self) -> None:
        assert (
            self._read(
                {"source": "stage2", "is_safe": True, "recovered_encoding": "utf-8"}
            )
            == "utf-8"
        )

    def test_absent_for_an_archive_or_document(self) -> None:
        # Neither branch writes the key: an archive recovers its members one
        # by one, a document arrives decoded from the extractor.
        assert self._read({"source": "stage2", "is_safe": True}) is None

    def test_no_safety_result_at_all(self) -> None:
        assert self._read(None) is None

    def test_malformed_values_do_not_crash_the_read_path(self) -> None:
        # Same reason as ``not_opened``: this column is free-form JSONB with
        # older writers behind it.
        assert self._read({"recovered_encoding": 1251}) is None
        assert self._read({"recovered_encoding": ""}) is None
        assert self._read("not a dict at all") is None


class TestRecoveredEncodingOnTheWire:
    """The encoding reaches BOTH read surfaces, list as well as detail.

    Two assertions rather than one because the two projections are separate
    functions: the review detail's error and pending branches render from the
    list row without fetching (DD-6-D), so a file that was refused would never
    show how it was read if only the detail carried the field.
    """

    async def test_list_row_carries_it(self, client: AsyncClient) -> None:
        sub = _mock_reviewed_submission()
        sub.safety_result = {"source": "stage2", "recovered_encoding": "cp1251"}
        with patch.object(
            HomeworkRepository, "list_for_student_and_task", return_value=[sub]
        ):
            resp = await client.get(_url(uuid.uuid4()))
        assert resp.status_code == 200
        assert resp.json()[0]["recovered_encoding"] == "cp1251"

    async def test_detail_carries_it(self, client: AsyncClient) -> None:
        sub = _mock_reviewed_submission()
        sub.safety_result = {"source": "stage2", "recovered_encoding": "cp1251"}
        with patch.object(HomeworkRepository, "get_owned", return_value=sub):
            resp = await client.get(f"/api/v1/portal/submissions/{sub.id}")
        assert resp.status_code == 200
        assert resp.json()["recovered_encoding"] == "cp1251"

    async def test_null_when_the_column_never_carried_one(
        self, client: AsyncClient
    ) -> None:
        sub = _mock_reviewed_submission()  # safety_result without the key
        with patch.object(HomeworkRepository, "get_owned", return_value=sub):
            resp = await client.get(f"/api/v1/portal/submissions/{sub.id}")
        assert resp.status_code == 200
        assert resp.json()["recovered_encoding"] is None
