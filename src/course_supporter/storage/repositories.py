"""CRUD repositories for database operations."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlalchemy.orm.attributes import InstrumentedAttribute

from course_supporter.models.course import (
    ConceptOutput,
    CourseStructure,
    ExerciseOutput,
    LessonOutput,
    ModuleOutput,
    SlideVideoMapEntry,
)
from course_supporter.models.reports import CostReport, CostSummary, GroupedCost
from course_supporter.storage.orm import (
    Concept,
    Course,
    Exercise,
    Lesson,
    LLMCall,
    Module,
    SlideVideoMapping,
    SourceMaterial,
)

# Valid status transitions: current_status → set of allowed next statuses
VALID_TRANSITIONS: dict[str, set[str]] = {
    "pending": {"processing"},
    "processing": {"done", "error"},
    "done": set(),  # terminal state
    "error": {"pending"},  # retry workflow
}


class CourseRepository:
    """Tenant-scoped repository for Course CRUD operations.

    All queries are automatically filtered by tenant_id to ensure
    data isolation between tenants.
    """

    def __init__(self, session: AsyncSession, tenant_id: uuid.UUID) -> None:
        self._session = session
        self._tenant_id = tenant_id

    async def create(
        self,
        *,
        title: str,
        description: str | None = None,
    ) -> Course:
        """Create a new course for the current tenant.

        Args:
            title: Course title.
            description: Optional course description.

        Returns:
            The newly created Course ORM instance.
        """
        course = Course(tenant_id=self._tenant_id, title=title, description=description)
        self._session.add(course)
        await self._session.flush()
        return course

    async def get_by_id(self, course_id: uuid.UUID) -> Course | None:
        """Get course by primary key, scoped to current tenant.

        Args:
            course_id: UUID of the course.

        Returns:
            Course if found and belongs to tenant, None otherwise.
        """
        stmt = select(Course).where(
            Course.id == course_id,
            Course.tenant_id == self._tenant_id,
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_with_structure(self, course_id: uuid.UUID) -> Course | None:
        """Get course with all nested structure eagerly loaded.

        Uses selectinload to avoid cartesian product issues
        that joinedload would cause with multiple collections.
        Scoped to current tenant.

        Args:
            course_id: UUID of the course.

        Returns:
            Course with modules, lessons, concepts, exercises,
            and source_materials loaded, or None if not found.
        """
        stmt = (
            select(Course)
            .where(
                Course.id == course_id,
                Course.tenant_id == self._tenant_id,
            )
            .options(
                selectinload(Course.source_materials),
                selectinload(Course.modules)
                .selectinload(Module.lessons)
                .selectinload(Lesson.concepts),
                selectinload(Course.modules)
                .selectinload(Module.lessons)
                .selectinload(Lesson.exercises),
            )
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_all(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Course]:
        """List courses for current tenant, ordered by creation date (newest first).

        Args:
            limit: Maximum number of results.
            offset: Number of results to skip.

        Returns:
            List of Course instances.
        """
        stmt = (
            select(Course)
            .where(Course.tenant_id == self._tenant_id)
            .order_by(Course.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def count(self) -> int:
        """Count courses for current tenant.

        Returns:
            Total number of courses owned by this tenant.
        """
        stmt = (
            select(func.count())
            .select_from(Course)
            .where(Course.tenant_id == self._tenant_id)
        )
        result = await self._session.execute(stmt)
        return result.scalar_one()


class SlideVideoMappingRepository:
    """Repository for slide-video mapping operations."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def batch_create(
        self,
        course_id: uuid.UUID,
        mappings: list[SlideVideoMapEntry],
    ) -> list[SlideVideoMapping]:
        """Create multiple slide-video mappings in a batch.

        Args:
            course_id: FK to the parent course.
            mappings: List of SlideVideoMapEntry Pydantic models.

        Returns:
            List of created SlideVideoMapping ORM instances.
        """
        records = []
        for m in mappings:
            record = SlideVideoMapping(
                course_id=course_id,
                slide_number=m.slide_number,
                video_timecode=m.video_timecode,
            )
            self._session.add(record)
            records.append(record)
        await self._session.flush()
        return records

    async def get_by_course_id(self, course_id: uuid.UUID) -> list[SlideVideoMapping]:
        """Get all slide-video mappings for a course.

        Args:
            course_id: UUID of the parent course.

        Returns:
            List of SlideVideoMapping instances ordered by slide_number.
        """
        stmt = (
            select(SlideVideoMapping)
            .where(SlideVideoMapping.course_id == course_id)
            .order_by(SlideVideoMapping.slide_number)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())


class SourceMaterialRepository:
    """Repository for SourceMaterial CRUD operations.

    Encapsulates database access for source materials with
    status machine validation for processing lifecycle.

    Status machine::

        pending → processing → done
                             → error

    Invalid transitions raise ValueError.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        course_id: uuid.UUID,
        source_type: str,
        source_url: str,
        filename: str | None = None,
    ) -> SourceMaterial:
        """Create a new source material with status 'pending'.

        Args:
            course_id: FK to the parent course.
            source_type: One of 'video', 'presentation', 'text', 'web'.
            source_url: URL or path to the source file.
            filename: Optional original filename.

        Returns:
            The newly created SourceMaterial ORM instance.
        """
        material = SourceMaterial(
            course_id=course_id,
            source_type=source_type,
            source_url=source_url,
            filename=filename,
            status="pending",
        )
        self._session.add(material)
        await self._session.flush()
        return material

    async def get_by_id(self, material_id: uuid.UUID) -> SourceMaterial | None:
        """Get source material by its primary key.

        Args:
            material_id: UUID of the source material.

        Returns:
            SourceMaterial if found, None otherwise.
        """
        return await self._session.get(SourceMaterial, material_id)

    async def get_by_course_id(self, course_id: uuid.UUID) -> list[SourceMaterial]:
        """Get all source materials for a given course.

        Args:
            course_id: UUID of the parent course.

        Returns:
            List of SourceMaterial instances (may be empty).
        """
        stmt = (
            select(SourceMaterial)
            .where(SourceMaterial.course_id == course_id)
            .order_by(SourceMaterial.created_at)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def update_status(
        self,
        material_id: uuid.UUID,
        status: str,
        *,
        error_message: str | None = None,
        content_snapshot: str | None = None,
    ) -> SourceMaterial:
        """Update processing status with validation and side effects.

        Valid transitions:
            pending → processing
            processing → done (sets processed_at)
            processing → error (sets error_message)

        Args:
            material_id: UUID of the source material.
            status: New status value.
            error_message: Required when transitioning to 'error'.
            content_snapshot: Optional content snapshot to save.

        Returns:
            Updated SourceMaterial instance.

        Raises:
            ValueError: If material not found or transition is invalid.
        """
        material = await self.get_by_id(material_id)
        if material is None:
            raise ValueError(f"SourceMaterial not found: {material_id}")

        current_status = material.status
        allowed = VALID_TRANSITIONS.get(current_status, set())

        if status not in allowed:
            raise ValueError(
                f"Invalid status transition: '{current_status}' → '{status}'. "
                f"Allowed: {allowed or 'none (terminal state)'}"
            )

        material.status = status

        if status == "done":
            material.processed_at = datetime.now(UTC)

        if status == "error":
            if not error_message:
                raise ValueError(
                    "error_message is required when transitioning to 'error'"
                )
            material.error_message = error_message

        if status == "pending":
            material.error_message = None

        if content_snapshot is not None:
            material.content_snapshot = content_snapshot

        await self._session.flush()
        return material

    async def retry(self, material_id: uuid.UUID) -> SourceMaterial:
        """Reset errored material back to pending for re-processing.

        Convenience method for the error -> pending transition.
        Clears error_message.

        Args:
            material_id: UUID of the source material.

        Returns:
            Updated SourceMaterial with status 'pending'.

        Raises:
            ValueError: If material not found or not in 'error' status.
        """
        return await self.update_status(material_id, "pending")

    async def delete(self, material_id: uuid.UUID) -> None:
        """Delete a source material by ID.

        Args:
            material_id: UUID of the source material to delete.

        Raises:
            ValueError: If material not found.
        """
        material = await self.get_by_id(material_id)
        if material is None:
            raise ValueError(f"SourceMaterial not found: {material_id}")
        await self._session.delete(material)
        await self._session.flush()


class CourseStructureRepository:
    """Repository for saving course structure to database.

    Implements replace strategy: existing modules (and their children
    via cascade delete) are removed before creating new ones from
    the Pydantic CourseStructure.

    Uses flush() instead of commit() -- caller controls transaction.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def save(
        self,
        course_id: uuid.UUID,
        structure: CourseStructure,
    ) -> Course:
        """Save course structure, replacing existing modules.

        Args:
            course_id: UUID of the parent course.
            structure: Pydantic CourseStructure from ArchitectAgent.

        Returns:
            Updated Course ORM instance with new modules.

        Raises:
            ValueError: If course not found.
        """
        course = await self._session.get(Course, course_id)
        if course is None:
            raise ValueError(f"Course not found: {course_id}")

        # Update course metadata from structure
        course.title = structure.title
        course.description = structure.description or None
        course.learning_goal = structure.learning_goal or None
        course.expected_knowledge = (
            structure.expected_knowledge if structure.expected_knowledge else None
        )
        course.expected_skills = (
            structure.expected_skills if structure.expected_skills else None
        )

        # Replace strategy: clear existing modules (cascade deletes children)
        course.modules.clear()
        await self._session.flush()

        # Create new modules from Pydantic structure
        for module_idx, module_data in enumerate(structure.modules):
            module = self._create_module(course_id, module_idx, module_data)
            self._session.add(module)

        await self._session.flush()
        return course

    @staticmethod
    def _create_module(
        course_id: uuid.UUID,
        order: int,
        data: ModuleOutput,
    ) -> Module:
        """Create Module ORM from Pydantic ModuleOutput."""
        module = Module(
            course_id=course_id,
            title=data.title,
            description=data.description or None,
            learning_goal=data.learning_goal or None,
            expected_knowledge=(
                data.expected_knowledge if data.expected_knowledge else None
            ),
            expected_skills=(data.expected_skills if data.expected_skills else None),
            difficulty=data.difficulty,
            order=order,
        )
        for lesson_idx, lesson_data in enumerate(data.lessons):
            lesson = CourseStructureRepository._create_lesson(lesson_idx, lesson_data)
            module.lessons.append(lesson)
        return module

    @staticmethod
    def _create_lesson(order: int, data: LessonOutput) -> Lesson:
        """Create Lesson ORM from Pydantic LessonOutput."""
        lesson = Lesson(
            title=data.title,
            order=order,
            video_start_timecode=data.video_start_timecode,
            video_end_timecode=data.video_end_timecode,
            slide_range=(data.slide_range.model_dump() if data.slide_range else None),
        )
        for concept_data in data.concepts:
            concept = CourseStructureRepository._create_concept(concept_data)
            lesson.concepts.append(concept)
        for exercise_data in data.exercises:
            exercise = CourseStructureRepository._create_exercise(exercise_data)
            lesson.exercises.append(exercise)
        return lesson

    @staticmethod
    def _create_concept(data: ConceptOutput) -> Concept:
        """Create Concept ORM from Pydantic ConceptOutput."""
        return Concept(
            title=data.title,
            definition=data.definition,
            examples=data.examples if data.examples else None,
            timecodes=data.timecodes if data.timecodes else None,
            slide_references=(data.slide_references if data.slide_references else None),
            web_references=(
                [ref.model_dump() for ref in data.web_references]
                if data.web_references
                else None
            ),
        )

    @staticmethod
    def _create_exercise(data: ExerciseOutput) -> Exercise:
        """Create Exercise ORM from Pydantic ExerciseOutput."""
        return Exercise(
            description=data.description,
            reference_solution=data.reference_solution,
            grading_criteria=data.grading_criteria,
            difficulty_level=data.difficulty_level,
        )


class LessonRepository:
    """Repository for Lesson read operations."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id_for_course(
        self,
        lesson_id: uuid.UUID,
        course_id: uuid.UUID,
    ) -> Lesson | None:
        """Get lesson by ID, ensuring it belongs to the given course.

        Joins Module to verify course ownership and eagerly loads
        concepts and exercises.

        Args:
            lesson_id: UUID of the lesson.
            course_id: UUID of the parent course.

        Returns:
            Lesson with concepts and exercises loaded,
            or None if not found or wrong course.
        """
        stmt = (
            select(Lesson)
            .join(Module, Lesson.module_id == Module.id)
            .where(Lesson.id == lesson_id, Module.course_id == course_id)
            .options(
                selectinload(Lesson.concepts),
                selectinload(Lesson.exercises),
            )
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()


class LLMCallRepository:
    """Repository for LLM call analytics and cost reporting.

    Optionally scoped by tenant_id. When tenant_id is provided,
    all queries filter by it. When None, returns all records.
    """

    def __init__(
        self, session: AsyncSession, tenant_id: uuid.UUID | None = None
    ) -> None:
        self._session = session
        self._tenant_id = tenant_id

    async def get_summary(self) -> CostSummary:
        """Get aggregate summary of LLM calls."""
        stmt = select(
            func.count().label("total_calls"),
            func.count().filter(LLMCall.success.is_(True)).label("successful_calls"),
            func.count().filter(LLMCall.success.is_(False)).label("failed_calls"),
            func.coalesce(func.sum(LLMCall.cost_usd), 0.0).label("total_cost_usd"),
            func.coalesce(func.sum(LLMCall.tokens_in), 0).label("total_tokens_in"),
            func.coalesce(func.sum(LLMCall.tokens_out), 0).label("total_tokens_out"),
            func.coalesce(func.avg(LLMCall.latency_ms), 0.0).label("avg_latency_ms"),
        ).select_from(LLMCall)
        if self._tenant_id is not None:
            stmt = stmt.where(LLMCall.tenant_id == self._tenant_id)
        result = await self._session.execute(stmt)
        row = result.one()
        return CostSummary(
            total_calls=row.total_calls,
            successful_calls=row.successful_calls,
            failed_calls=row.failed_calls,
            total_cost_usd=float(row.total_cost_usd),
            total_tokens_in=int(row.total_tokens_in),
            total_tokens_out=int(row.total_tokens_out),
            avg_latency_ms=float(row.avg_latency_ms),
        )

    async def get_full_report(self) -> CostReport:
        """Get complete cost report with summary and all breakdowns."""
        return CostReport(
            summary=await self.get_summary(),
            by_action=await self.get_by_action(),
            by_provider=await self.get_by_provider(),
            by_model=await self.get_by_model(),
        )

    async def get_by_action(self) -> list[GroupedCost]:
        """Get cost breakdown grouped by action."""
        return await self._grouped_query(LLMCall.action)

    async def get_by_provider(self) -> list[GroupedCost]:
        """Get cost breakdown grouped by provider."""
        return await self._grouped_query(LLMCall.provider)

    async def get_by_model(self) -> list[GroupedCost]:
        """Get cost breakdown grouped by model_id."""
        return await self._grouped_query(LLMCall.model_id)

    async def _grouped_query(
        self,
        group_column: InstrumentedAttribute[str],
    ) -> list[GroupedCost]:
        """Run a GROUP BY query on the given column."""
        stmt = (
            select(
                group_column.label("group"),
                func.count().label("calls"),
                func.coalesce(func.sum(LLMCall.cost_usd), 0.0).label("cost_usd"),
                func.coalesce(func.sum(LLMCall.tokens_in), 0).label("tokens_in"),
                func.coalesce(func.sum(LLMCall.tokens_out), 0).label("tokens_out"),
                func.coalesce(func.avg(LLMCall.latency_ms), 0.0).label(
                    "avg_latency_ms"
                ),
            )
            .select_from(LLMCall)
            .group_by(group_column)
            .order_by(func.count().desc())
        )
        if self._tenant_id is not None:
            stmt = stmt.where(LLMCall.tenant_id == self._tenant_id)
        result = await self._session.execute(stmt)
        return [
            GroupedCost(
                group=row.group,
                calls=row.calls,
                cost_usd=float(row.cost_usd),
                tokens_in=int(row.tokens_in),
                tokens_out=int(row.tokens_out),
                avg_latency_ms=float(row.avg_latency_ms),
            )
            for row in result.all()
        ]
