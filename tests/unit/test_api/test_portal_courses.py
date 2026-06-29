"""Unit tests for the portal course-listing route (Phase 6 T4a c1, KD17).

``get_current_student`` is overridden to a fixed StudentContext; the
enrollment JOIN is mocked at the repository. Visibility (publish-gate A) and
the active-only / oldest-first ordering are exercised at the repository level
by the live acceptance + integration; here we pin the route contract: bare
id+title, empty list for no enrollments, no extra fields.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from course_supporter.api.app import app
from course_supporter.api.deps import get_current_student, get_session
from course_supporter.auth.context import StudentContext
from course_supporter.storage.course_node_repository import CourseNodeRepository
from course_supporter.storage.homework_repository import HomeworkRepository
from course_supporter.storage.orm import MaterialState
from course_supporter.storage.student_enrollment_repository import (
    StudentEnrollmentRepository,
)

STUB_TENANT_ID = uuid.uuid4()
STUB_STUDENT_ID = uuid.uuid4()
STUB_STUDENT = StudentContext(
    student_id=STUB_STUDENT_ID,
    tenant_id=STUB_TENANT_ID,
    login="alice",
    display_name="Alice",
)

_COURSES_URL = "/api/v1/portal/courses"


def _mock_course(title: str) -> MagicMock:
    course = MagicMock()
    course.id = uuid.uuid4()
    course.title = title
    return course


@pytest.fixture()
def mock_session() -> AsyncMock:
    return AsyncMock()


@pytest.fixture()
async def client(mock_session: AsyncMock) -> AsyncClient:
    app.dependency_overrides[get_session] = lambda: mock_session
    app.dependency_overrides[get_current_student] = lambda: STUB_STUDENT
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as ac:
        yield ac  # type: ignore[misc]
    app.dependency_overrides.clear()


class TestPortalCourseListing:
    async def test_lists_enrolled_courses_bare(self, client: AsyncClient) -> None:
        """Enrolled courses → list of {id, title} in repository order."""
        courses = [_mock_course("Python 101"), _mock_course("Algorithms")]
        with patch.object(
            StudentEnrollmentRepository,
            "list_enrolled_courses",
            return_value=courses,
        ):
            resp = await client.get(_COURSES_URL)
        assert resp.status_code == 200
        body = resp.json()
        assert body == [
            {"id": str(courses[0].id), "title": "Python 101"},
            {"id": str(courses[1].id), "title": "Algorithms"},
        ]

    async def test_empty_when_no_enrollments(self, client: AsyncClient) -> None:
        """No enrollments → empty list, never an error (nothing to leak)."""
        with patch.object(
            StudentEnrollmentRepository,
            "list_enrolled_courses",
            return_value=[],
        ):
            resp = await client.get(_COURSES_URL)
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_no_extra_fields_leaked(self, client: AsyncClient) -> None:
        """The card is bare — no counts, no tree, no tenant on the wire."""
        course = _mock_course("Solo")
        with patch.object(
            StudentEnrollmentRepository,
            "list_enrolled_courses",
            return_value=[course],
        ):
            resp = await client.get(_COURSES_URL)
        assert resp.status_code == 200
        assert set(resp.json()[0].keys()) == {"id", "title"}


# --- Endpoint 2: GET /portal/courses/{root_id}/materials (c2) ---


def _doc(
    *,
    task_type: str | None = None,
    filename: str | None = "f.pdf",
    source_type: str = "text",
    order: int = 0,
    state: MaterialState = MaterialState.READY,
    deleted_at: object | None = None,
    doc_id: uuid.UUID | None = None,
) -> MagicMock:
    doc = MagicMock()
    doc.id = doc_id or uuid.uuid4()
    doc.task_type = task_type
    doc.filename = filename
    doc.source_type = source_type
    doc.order = order
    doc.state = state
    doc.deleted_at = deleted_at
    doc.created_at = datetime.now(UTC)
    return doc


def _node(
    *,
    title: str = "Root",
    order: int = 0,
    parent_id: uuid.UUID | None = None,
    tenant_id: uuid.UUID = STUB_TENANT_ID,
    deleted_at: object | None = None,
    documents: list[MagicMock] | None = None,
    children: list[MagicMock] | None = None,
    node_id: uuid.UUID | None = None,
) -> MagicMock:
    node = MagicMock()
    node.id = node_id or uuid.uuid4()
    node.title = title
    node.order = order
    node.parent_id = parent_id
    node.tenant_id = tenant_id
    node.deleted_at = deleted_at
    node.documents = documents or []
    node.children = children or []
    return node


def _sub(
    *,
    authored_document_id: uuid.UUID,
    status: str,
    score: int | None = None,
    review_result: dict[str, object] | None = None,
) -> MagicMock:
    sub = MagicMock()
    sub.authored_document_id = authored_document_id
    sub.status = status
    sub.score = score
    sub.review_result = review_result
    sub.created_at = datetime.now(UTC)
    return sub


def _materials_url(root_id: uuid.UUID) -> str:
    return f"/api/v1/portal/courses/{root_id}/materials"


def _gate_ok(root: MagicMock, subtree: list[MagicMock], submissions: list[MagicMock]):
    """Patch the gate + sources to resolve `root`/`subtree`/`submissions`."""
    return (
        patch.object(CourseNodeRepository, "get_by_id", return_value=root),
        patch.object(StudentEnrollmentRepository, "is_enrolled", return_value=True),
        patch.object(CourseNodeRepository, "get_subtree", return_value=subtree),
        patch.object(
            HomeworkRepository,
            "list_active_submissions_in_course",
            return_value=submissions,
        ),
    )


class TestPortalMaterialsTree:
    async def test_material_and_task_with_overlay(self, client: AsyncClient) -> None:
        """Tree carries materials (no overlay) and tasks (with overlay)."""
        task = _doc(task_type="task", filename="hw1.py", source_type="text", order=1)
        material = _doc(task_type=None, filename="intro.pdf", order=0)
        root = _node(documents=[material, task])
        sub = _sub(
            authored_document_id=task.id,
            status="completed",
            score=80,
            review_result={"verdict": {"passed": True, "correctness": "correct"}},
        )
        get_by_id, enrolled, subtree, subs = _gate_ok(root, [root], [sub])
        with get_by_id, enrolled, subtree, subs:
            resp = await client.get(_materials_url(root.id))
        assert resp.status_code == 200
        body = resp.json()
        assert body["title"] == "Root"
        docs = body["documents"]
        assert [d["kind"] for d in docs] == ["material", "task"]
        assert docs[0]["label"] == "intro.pdf"
        assert docs[0]["overlay"] is None
        task_item = docs[1]
        assert task_item["label"] == "hw1.py"
        assert task_item["overlay"]["submission_status"] == "reviewed"
        assert task_item["overlay"]["last"] == {
            "score": 80,
            "verdict": {"passed": True, "correctness": "correct"},
        }
        assert task_item["overlay"]["best"]["score"] == 80

    async def test_task_no_attempts_status_none(self, client: AsyncClient) -> None:
        """A task with no submissions → status none, last/best absent."""
        task = _doc(task_type="project", filename="proj.zip")
        root = _node(documents=[task])
        get_by_id, enrolled, subtree, subs = _gate_ok(root, [root], [])
        with get_by_id, enrolled, subtree, subs:
            resp = await client.get(_materials_url(root.id))
        assert resp.status_code == 200
        overlay = resp.json()["documents"][0]["overlay"]
        assert overlay == {"submission_status": "none", "last": None, "best": None}

    async def test_best_is_max_reviewed_score(self, client: AsyncClient) -> None:
        """best = max(score) among reviewed; latest pending drives status."""
        task = _doc(task_type="task")
        root = _node(documents=[task])
        # Newest-first (as the repo returns): latest is an in-flight retry.
        latest = _sub(authored_document_id=task.id, status="reviewing")
        high = _sub(authored_document_id=task.id, status="completed", score=90)
        low = _sub(authored_document_id=task.id, status="delivered", score=55)
        get_by_id, enrolled, subtree, subs = _gate_ok(root, [root], [latest, high, low])
        with get_by_id, enrolled, subtree, subs:
            resp = await client.get(_materials_url(root.id))
        overlay = resp.json()["documents"][0]["overlay"]
        assert overlay["submission_status"] == "pending"  # latest is reviewing
        assert overlay["last"] == {"score": None, "verdict": None}
        assert overlay["best"]["score"] == 90  # max reviewed score

    async def test_all_null_scores_best_absent(self, client: AsyncClient) -> None:
        """No reviewed/non-null score → best absent, last still present."""
        task = _doc(task_type="task")
        root = _node(documents=[task])
        failed = _sub(authored_document_id=task.id, status="failed")
        get_by_id, enrolled, subtree, subs = _gate_ok(root, [root], [failed])
        with get_by_id, enrolled, subtree, subs:
            resp = await client.get(_materials_url(root.id))
        overlay = resp.json()["documents"][0]["overlay"]
        # Error terminal surfaces as "error" (DD-6-D de-collapse); a failed
        # attempt has no usable score, so best is absent.
        assert overlay["submission_status"] == "error"
        assert overlay["last"] == {"score": None, "verdict": None}
        assert overlay["best"] is None

    @pytest.mark.parametrize("terminal", ["rejected", "mismatch", "failed"])
    async def test_error_terminals_surface_as_error(
        self, client: AsyncClient, terminal: str
    ) -> None:
        """Each error terminal → submission_status 'error' (DD-6-D de-collapse).

        The coarse overlay carries one 'error' bucket; the precise terminal
        (rejected / mismatch / failed) lives in the read-path detail. The
        internal trace never reaches the overlay — last is the curated shell
        (score / verdict only), best absent (no usable score).
        """
        task = _doc(task_type="task")
        root = _node(documents=[task])
        attempt = _sub(authored_document_id=task.id, status=terminal)
        get_by_id, enrolled, subtree, subs = _gate_ok(root, [root], [attempt])
        with get_by_id, enrolled, subtree, subs:
            resp = await client.get(_materials_url(root.id))
        overlay = resp.json()["documents"][0]["overlay"]
        assert overlay["submission_status"] == "error"
        assert overlay["last"] == {"score": None, "verdict": None}
        assert overlay["best"] is None

    async def test_error_terminal_does_not_win_best(self, client: AsyncClient) -> None:
        """An error terminal never competes for best; a prior reviewed score does.

        Latest is an error terminal (status 'error', no score); an earlier
        reviewed attempt holds the best score. The error attempt must not
        suppress or win best — best stays the reviewed max.
        """
        task = _doc(task_type="task")
        root = _node(documents=[task])
        # Newest-first: latest is a terminal error, earlier one was reviewed.
        latest = _sub(authored_document_id=task.id, status="rejected")
        reviewed = _sub(
            authored_document_id=task.id,
            status="completed",
            score=70,
            review_result={"verdict": {"passed": True, "correctness": "correct"}},
        )
        get_by_id, enrolled, subtree, subs = _gate_ok(root, [root], [latest, reviewed])
        with get_by_id, enrolled, subtree, subs:
            resp = await client.get(_materials_url(root.id))
        overlay = resp.json()["documents"][0]["overlay"]
        assert overlay["submission_status"] == "error"  # latest drives status
        assert overlay["last"] == {"score": None, "verdict": None}
        assert overlay["best"]["score"] == 70  # reviewed attempt still wins best

    async def test_soft_deleted_and_non_ready_filtered(
        self, client: AsyncClient
    ) -> None:
        """Soft-deleted + non-READY documents are dropped; child nested."""
        visible = _doc(task_type=None, filename="ok.pdf", order=0)
        gone = _doc(task_type=None, filename="gone.pdf", order=1, deleted_at=object())
        pending_doc = _doc(
            task_type=None,
            filename="ingesting.pdf",
            order=2,
            state=MaterialState.PENDING,
        )
        child = _node(title="Section 1", order=0, parent_id=uuid.uuid4())
        root = _node(documents=[visible, gone, pending_doc], children=[child])
        get_by_id, enrolled, subtree, subs = _gate_ok(root, [root], [])
        with get_by_id, enrolled, subtree, subs:
            resp = await client.get(_materials_url(root.id))
        body = resp.json()
        labels = [d["label"] for d in body["documents"]]
        assert labels == ["ok.pdf"]
        assert [c["title"] for c in body["children"]] == ["Section 1"]

    async def test_derived_label_fallback(self, client: AsyncClient) -> None:
        """A document with no filename → '{source_type} #{order}'."""
        doc = _doc(task_type=None, filename=None, source_type="web", order=3)
        root = _node(documents=[doc])
        get_by_id, enrolled, subtree, subs = _gate_ok(root, [root], [])
        with get_by_id, enrolled, subtree, subs:
            resp = await client.get(_materials_url(root.id))
        assert resp.json()["documents"][0]["label"] == "web #3"

    async def test_no_internal_trace_leaked(self, client: AsyncClient) -> None:
        """review_result (beyond verdict) / safety / sanity never serialized."""
        task = _doc(task_type="task")
        root = _node(documents=[task])
        sub = _sub(
            authored_document_id=task.id,
            status="completed",
            score=70,
            review_result={
                "verdict": {"passed": True, "correctness": "correct"},
                "layers": ["SECRET INTERNAL TRACE"],
                "denoised_score": 70,
            },
        )
        get_by_id, enrolled, subtree, subs = _gate_ok(root, [root], [sub])
        with get_by_id, enrolled, subtree, subs:
            resp = await client.get(_materials_url(root.id))
        assert "SECRET INTERNAL TRACE" not in resp.text
        assert "denoised_score" not in resp.text
        assert "safety_result" not in resp.text
        assert "sanity_result" not in resp.text


class TestPortalMaterialsGate:
    async def test_unknown_root_404(self, client: AsyncClient) -> None:
        with patch.object(CourseNodeRepository, "get_by_id", return_value=None):
            resp = await client.get(_materials_url(uuid.uuid4()))
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Course not found."

    async def test_non_root_id_404(self, client: AsyncClient) -> None:
        """A non-root node id (parent_id set) → 404."""
        node = _node(parent_id=uuid.uuid4())
        with patch.object(CourseNodeRepository, "get_by_id", return_value=node):
            resp = await client.get(_materials_url(node.id))
        assert resp.status_code == 404

    async def test_foreign_tenant_404(self, client: AsyncClient) -> None:
        node = _node(tenant_id=uuid.uuid4())
        with patch.object(CourseNodeRepository, "get_by_id", return_value=node):
            resp = await client.get(_materials_url(node.id))
        assert resp.status_code == 404

    async def test_soft_deleted_root_404(self, client: AsyncClient) -> None:
        node = _node(deleted_at=object())
        with patch.object(CourseNodeRepository, "get_by_id", return_value=node):
            resp = await client.get(_materials_url(node.id))
        assert resp.status_code == 404

    async def test_not_enrolled_404(self, client: AsyncClient) -> None:
        node = _node()
        with (
            patch.object(CourseNodeRepository, "get_by_id", return_value=node),
            patch.object(
                StudentEnrollmentRepository, "is_enrolled", return_value=False
            ),
        ):
            resp = await client.get(_materials_url(node.id))
        assert resp.status_code == 404
