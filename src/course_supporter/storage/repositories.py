"""CRUD repositories for database operations."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.models.course import (
    ConceptOutput,
    CourseStructure,
    ExerciseOutput,
    LessonOutput,
    ModuleOutput,
)
from course_supporter.storage.orm import (
    Concept,
    Course,
    Exercise,
    Lesson,
    Module,
    SourceMaterial,
)

# Valid status transitions: current_status → set of allowed next statuses
VALID_TRANSITIONS: dict[str, set[str]] = {
    "pending": {"processing"},
    "processing": {"done", "error"},
    "done": set(),  # terminal state
    # TODO: consider error → pending for retry workflow
    "error": set(),  # terminal state
}


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

        if content_snapshot is not None:
            material.content_snapshot = content_snapshot

        await self._session.flush()
        return material

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
        if structure.description:
            course.description = structure.description
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
