"""Tests for GET /courses/{id} with nested structure."""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from course_supporter.api.app import app
from course_supporter.api.deps import get_current_tenant
from course_supporter.auth.context import TenantContext
from course_supporter.storage.database import get_session
from course_supporter.storage.material_node_repository import MaterialNodeRepository
from course_supporter.storage.repositories import CourseRepository

STUB_TENANT = TenantContext(
    tenant_id=uuid.uuid4(),
    tenant_name="test-tenant",
    scopes=["prep"],
    rate_limit_prep=100,
    rate_limit_check=1000,
    key_prefix="cs_test",
)


def _make_concept_mock(*, title: str = "Variables") -> MagicMock:
    """Create a mock Concept ORM object."""
    concept = MagicMock()
    concept.id = uuid.uuid4()
    concept.title = title
    concept.definition = "A named storage location"
    concept.examples = ["x = 1"]
    concept.timecodes = ["00:05:00"]
    concept.slide_references = [1, 2]
    concept.web_references = [{"url": "https://example.com", "title": "Ref"}]
    return concept


def _make_exercise_mock(*, description: str = "Write a loop") -> MagicMock:
    """Create a mock Exercise ORM object."""
    exercise = MagicMock()
    exercise.id = uuid.uuid4()
    exercise.description = description
    exercise.reference_solution = "for i in range(10): print(i)"
    exercise.grading_criteria = "Correct output"
    exercise.difficulty_level = 3
    return exercise


def _make_lesson_mock(
    *,
    title: str = "Intro",
    concepts: list[MagicMock] | None = None,
    exercises: list[MagicMock] | None = None,
) -> MagicMock:
    """Create a mock Lesson ORM object."""
    lesson = MagicMock()
    lesson.id = uuid.uuid4()
    lesson.title = title
    lesson.order = 0
    lesson.video_start_timecode = "00:00:00"
    lesson.video_end_timecode = "00:10:00"
    lesson.slide_range = {"start": 1, "end": 5}
    lesson.concepts = concepts or []
    lesson.exercises = exercises or []
    return lesson


def _make_module_mock(
    *, title: str = "Basics", lessons: list[MagicMock] | None = None
) -> MagicMock:
    """Create a mock Module ORM object."""
    module = MagicMock()
    module.id = uuid.uuid4()
    module.title = title
    module.description = "Fundamentals"
    module.learning_goal = "Learn basics"
    module.difficulty = "easy"
    module.order = 0
    module.lessons = lessons or []
    return module


def _make_source_material_mock(
    *, source_type: str = "video", status: str = "done"
) -> MagicMock:
    """Create a mock SourceMaterial ORM object."""
    sm = MagicMock()
    sm.id = uuid.uuid4()
    sm.source_type = source_type
    sm.source_url = "https://example.com/video.mp4"
    sm.filename = "video.mp4"
    sm.status = status
    sm.created_at = datetime.now(UTC)
    return sm


def _make_course_mock(
    *,
    modules: list[MagicMock] | None = None,
    source_materials: list[MagicMock] | None = None,
) -> MagicMock:
    """Create a mock Course ORM object with nested structure."""
    course = MagicMock()
    course.id = uuid.uuid4()
    course.title = "Python 101"
    course.description = "Intro to Python"
    course.learning_goal = "Learn Python basics"
    course.created_at = datetime.now(UTC)
    course.updated_at = datetime.now(UTC)
    course.modules = modules or []
    course.source_materials = source_materials or []
    return course


@pytest.fixture()
def mock_session() -> AsyncMock:
    session = AsyncMock()
    session.add = MagicMock()
    return session


@pytest.fixture()
async def client(mock_session: AsyncMock) -> AsyncClient:
    app.dependency_overrides[get_session] = lambda: mock_session
    app.dependency_overrides[get_current_tenant] = lambda: STUB_TENANT
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as ac:
        yield ac  # type: ignore[misc]
    app.dependency_overrides.clear()


def _mock_node(
    *,
    node_id: uuid.UUID | None = None,
    course_id: uuid.UUID | None = None,
    title: str = "Module 1",
    description: str | None = None,
    order: int = 0,
    node_fingerprint: str | None = None,
    children: list[object] | None = None,
    materials: list[object] | None = None,
) -> MagicMock:
    """Create a mock MaterialNode for tree tests."""
    node = MagicMock()
    node.id = node_id or uuid.uuid4()
    node.course_id = course_id or uuid.uuid4()
    node.parent_id = None
    node.title = title
    node.description = description
    node.order = order
    node.node_fingerprint = node_fingerprint
    node.children = children or []
    node.materials = materials or []
    node.created_at = datetime.now(UTC)
    node.updated_at = datetime.now(UTC)
    return node


def _mock_entry(
    *,
    source_type: str = "text",
    state: str = "ready",
    error_message: str | None = None,
) -> MagicMock:
    """Create a mock MaterialEntry for tree tests."""
    entry = MagicMock()
    entry.id = uuid.uuid4()
    entry.source_type = source_type
    entry.source_url = "https://example.com/doc.md"
    entry.filename = "doc.md"
    entry.order = 0
    entry.state = state
    entry.error_message = error_message
    entry.created_at = datetime.now(UTC)
    return entry


def _get_course_patches(
    course: MagicMock, tree: list[MagicMock] | None = None
) -> tuple[object, object]:
    """Return context managers for patching get_with_structure and get_tree."""
    return (
        patch.object(CourseRepository, "get_with_structure", return_value=course),
        patch.object(MaterialNodeRepository, "get_tree", return_value=tree or []),
    )


class TestGetCourseDetailAPI:
    @pytest.mark.asyncio
    async def test_get_course_returns_200(self, client: AsyncClient) -> None:
        """GET /api/v1/courses/{id} returns 200 for existing course."""
        course = _make_course_mock()
        p_course, p_tree = _get_course_patches(course)
        with p_course, p_tree:
            response = await client.get(f"/api/v1/courses/{course.id}")
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_get_course_not_found_returns_404(self, client: AsyncClient) -> None:
        """GET /api/v1/courses/{id} returns 404 for missing course."""
        with patch.object(CourseRepository, "get_with_structure", return_value=None):
            response = await client.get(f"/api/v1/courses/{uuid.uuid4()}")
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_get_course_includes_basic_fields(self, client: AsyncClient) -> None:
        """Response includes id, title, description, learning_goal."""
        course = _make_course_mock()
        p_course, p_tree = _get_course_patches(course)
        with p_course, p_tree:
            response = await client.get(f"/api/v1/courses/{course.id}")
        data = response.json()
        assert data["id"] == str(course.id)
        assert data["title"] == "Python 101"
        assert data["description"] == "Intro to Python"
        assert data["learning_goal"] == "Learn Python basics"

    @pytest.mark.asyncio
    async def test_get_course_empty_structure(self, client: AsyncClient) -> None:
        """Course with no modules or materials returns empty lists."""
        course = _make_course_mock()
        p_course, p_tree = _get_course_patches(course)
        with p_course, p_tree:
            response = await client.get(f"/api/v1/courses/{course.id}")
        data = response.json()
        assert data["modules"] == []
        assert data["source_materials"] == []
        assert data["material_tree"] == []

    @pytest.mark.asyncio
    async def test_get_course_includes_source_materials(
        self, client: AsyncClient
    ) -> None:
        """Response includes source materials."""
        sm = _make_source_material_mock()
        course = _make_course_mock(source_materials=[sm])
        p_course, p_tree = _get_course_patches(course)
        with p_course, p_tree:
            response = await client.get(f"/api/v1/courses/{course.id}")
        data = response.json()
        assert len(data["source_materials"]) == 1
        assert data["source_materials"][0]["source_type"] == "video"
        assert data["source_materials"][0]["status"] == "done"

    @pytest.mark.asyncio
    async def test_get_course_nested_modules_lessons_concepts(
        self, client: AsyncClient
    ) -> None:
        """Response includes full nested structure."""
        concept = _make_concept_mock()
        exercise = _make_exercise_mock()
        lesson = _make_lesson_mock(concepts=[concept], exercises=[exercise])
        module = _make_module_mock(lessons=[lesson])
        course = _make_course_mock(modules=[module])
        p_course, p_tree = _get_course_patches(course)
        with p_course, p_tree:
            response = await client.get(f"/api/v1/courses/{course.id}")
        data = response.json()
        assert len(data["modules"]) == 1
        mod = data["modules"][0]
        assert mod["title"] == "Basics"
        assert mod["difficulty"] == "easy"
        assert len(mod["lessons"]) == 1
        les = mod["lessons"][0]
        assert les["title"] == "Intro"
        assert les["slide_range"] == {"start": 1, "end": 5}
        assert len(les["concepts"]) == 1
        assert les["concepts"][0]["title"] == "Variables"
        assert len(les["exercises"]) == 1
        assert les["exercises"][0]["description"] == "Write a loop"

    @pytest.mark.asyncio
    async def test_get_course_includes_material_tree(self, client: AsyncClient) -> None:
        """Response includes material_tree with nodes and entries."""
        entry = _mock_entry(source_type="video", state="ready")
        child = _mock_node(title="Lesson 1.1", order=0, materials=[entry])
        root = _mock_node(title="Module 1", order=0, children=[child])
        course = _make_course_mock()
        p_course, p_tree = _get_course_patches(course, tree=[root])
        with p_course, p_tree:
            response = await client.get(f"/api/v1/courses/{course.id}")
        data = response.json()
        tree = data["material_tree"]
        assert len(tree) == 1
        assert tree[0]["title"] == "Module 1"
        assert len(tree[0]["children"]) == 1
        child_data = tree[0]["children"][0]
        assert child_data["title"] == "Lesson 1.1"
        assert len(child_data["materials"]) == 1
        assert child_data["materials"][0]["source_type"] == "video"
        assert child_data["materials"][0]["state"] == "ready"

    @pytest.mark.asyncio
    async def test_get_course_material_tree_includes_states(
        self, client: AsyncClient
    ) -> None:
        """Material entries in tree include derived state and error_message."""
        entry_ok = _mock_entry(state="ready")
        entry_err = _mock_entry(state="error", error_message="Timeout")
        node = _mock_node(materials=[entry_ok, entry_err])
        course = _make_course_mock()
        p_course, p_tree = _get_course_patches(course, tree=[node])
        with p_course, p_tree:
            response = await client.get(f"/api/v1/courses/{course.id}")
        materials = response.json()["material_tree"][0]["materials"]
        assert materials[0]["state"] == "ready"
        assert materials[0]["error_message"] is None
        assert materials[1]["state"] == "error"
        assert materials[1]["error_message"] == "Timeout"

    @pytest.mark.asyncio
    async def test_get_course_material_tree_empty(self, client: AsyncClient) -> None:
        """Course with no tree returns empty material_tree."""
        course = _make_course_mock()
        p_course, p_tree = _get_course_patches(course, tree=[])
        with p_course, p_tree:
            response = await client.get(f"/api/v1/courses/{course.id}")
        assert response.json()["material_tree"] == []


class TestCourseRepositoryGetWithStructure:
    @pytest.mark.asyncio
    async def test_get_with_structure_executes_query(
        self, mock_session: AsyncMock
    ) -> None:
        """get_with_structure() executes select with options."""
        course = _make_course_mock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = course
        mock_session.execute.return_value = mock_result
        repo = CourseRepository(mock_session, uuid.uuid4())
        result = await repo.get_with_structure(course.id)
        assert result == course
        mock_session.execute.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_get_with_structure_returns_none(
        self, mock_session: AsyncMock
    ) -> None:
        """get_with_structure() returns None for missing course."""
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_session.execute.return_value = mock_result
        repo = CourseRepository(mock_session, uuid.uuid4())
        result = await repo.get_with_structure(uuid.uuid4())
        assert result is None
