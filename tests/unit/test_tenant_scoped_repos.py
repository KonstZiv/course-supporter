"""Tests for tenant-scoped repositories (PD-006)."""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

from course_supporter.storage.orm import Course
from course_supporter.storage.repositories import CourseRepository, LLMCallRepository


def _make_course(
    tenant_id: uuid.UUID,
    title: str = "Python 101",
) -> MagicMock:
    """Create a mock Course with given tenant_id."""
    course = MagicMock(spec=Course)
    course.id = uuid.uuid4()
    course.tenant_id = tenant_id
    course.title = title
    course.description = None
    course.created_at = datetime.now(UTC)
    course.updated_at = datetime.now(UTC)
    return course


def _mock_session() -> AsyncMock:
    session = AsyncMock()
    session.add = MagicMock()
    return session


class TestCourseRepositoryTenantScoping:
    """Tests for tenant-scoped CourseRepository."""

    async def test_create_course_sets_tenant_id(self) -> None:
        """create() auto-sets tenant_id from constructor."""
        session = _mock_session()
        tenant_id = uuid.uuid4()
        repo = CourseRepository(session, tenant_id)

        await repo.create(title="Test Course")

        session.add.assert_called_once()
        course: Course = session.add.call_args[0][0]
        assert isinstance(course, Course)
        assert course.tenant_id == tenant_id

    async def test_get_by_id_wrong_tenant(self) -> None:
        """get_by_id() returns None when course belongs to different tenant."""
        session = _mock_session()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        session.execute.return_value = mock_result

        tenant_a = uuid.uuid4()
        repo = CourseRepository(session, tenant_a)
        result = await repo.get_by_id(uuid.uuid4())

        assert result is None
        session.execute.assert_awaited_once()

    async def test_list_courses_scoped(self) -> None:
        """list_all() returns only courses for the given tenant."""
        session = _mock_session()
        tenant_id = uuid.uuid4()
        courses = [_make_course(tenant_id), _make_course(tenant_id, "ML 201")]

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = courses
        session.execute.return_value = mock_result

        repo = CourseRepository(session, tenant_id)
        result = await repo.list_all()

        assert len(result) == 2
        session.execute.assert_awaited_once()

    async def test_get_with_structure_scoped(self) -> None:
        """get_with_structure() scoped to tenant."""
        session = _mock_session()
        tenant_id = uuid.uuid4()
        course = _make_course(tenant_id)

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = course
        session.execute.return_value = mock_result

        repo = CourseRepository(session, tenant_id)
        result = await repo.get_with_structure(course.id)

        assert result == course
        session.execute.assert_awaited_once()


class TestLLMCallRepositoryTenantScoping:
    """Tests for tenant-scoped LLMCallRepository."""

    async def test_llm_call_repo_scoped(self) -> None:
        """get_summary() filters by tenant_id when provided."""
        session = _mock_session()
        tenant_id = uuid.uuid4()

        mock_result = MagicMock()
        mock_result.one.return_value = MagicMock(
            total_calls=3,
            successful_calls=3,
            failed_calls=0,
            total_cost_usd=0.01,
            total_tokens_in=3000,
            total_tokens_out=1500,
            avg_latency_ms=300.0,
        )
        session.execute.return_value = mock_result

        repo = LLMCallRepository(session, tenant_id)
        summary = await repo.get_summary()

        assert summary.total_calls == 3
        session.execute.assert_awaited_once()

    async def test_llm_call_repo_no_tenant(self) -> None:
        """get_summary() returns all records when tenant_id is None."""
        session = _mock_session()

        mock_result = MagicMock()
        mock_result.one.return_value = MagicMock(
            total_calls=10,
            successful_calls=9,
            failed_calls=1,
            total_cost_usd=0.05,
            total_tokens_in=10000,
            total_tokens_out=5000,
            avg_latency_ms=400.0,
        )
        session.execute.return_value = mock_result

        repo = LLMCallRepository(session)
        summary = await repo.get_summary()

        assert summary.total_calls == 10
        session.execute.assert_awaited_once()
