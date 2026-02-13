"""SQLAlchemy ORM models for all Sprint 1 entities."""

import uuid
from datetime import datetime
from typing import Any

import uuid_utils as uuid7_lib
from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _uuid7() -> uuid.UUID:
    """Generate a UUIDv7 (time-ordered) for use as default PK value."""
    return uuid.UUID(bytes=uuid7_lib.uuid7().bytes)


class Base(DeclarativeBase):
    """Base class for all ORM models."""


# ──────────────────────────────────────────────
# Course & Source Materials
# ──────────────────────────────────────────────


class Course(Base):
    __tablename__ = "courses"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid7)
    title: Mapped[str] = mapped_column(String(500))
    description: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # Relationships
    source_materials: Mapped[list["SourceMaterial"]] = relationship(
        back_populates="course", cascade="all, delete-orphan"
    )
    slide_video_mappings: Mapped[list["SlideVideoMapping"]] = relationship(
        back_populates="course", cascade="all, delete-orphan"
    )
    modules: Mapped[list["Module"]] = relationship(
        back_populates="course", cascade="all, delete-orphan"
    )


class SourceMaterial(Base):
    __tablename__ = "source_materials"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid7)
    course_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("courses.id", ondelete="CASCADE")
    )
    source_type: Mapped[str] = mapped_column(
        Enum(
            "video",
            "presentation",
            "text",
            "web",
            name="source_type_enum",
        )
    )
    source_url: Mapped[str] = mapped_column(String(2000))
    filename: Mapped[str | None] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(
        Enum(
            "pending",
            "processing",
            "done",
            "error",
            name="processing_status_enum",
        ),
        default="pending",
    )
    content_snapshot: Mapped[str | None] = mapped_column(Text)
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # Relationships
    course: Mapped["Course"] = relationship(back_populates="source_materials")


class SlideVideoMapping(Base):
    __tablename__ = "slide_video_mappings"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid7)
    course_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("courses.id", ondelete="CASCADE")
    )
    slide_number: Mapped[int] = mapped_column(Integer)
    video_timecode: Mapped[str] = mapped_column(String(20))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # Relationships
    course: Mapped["Course"] = relationship(back_populates="slide_video_mappings")


# ──────────────────────────────────────────────
# Course Structure
# ──────────────────────────────────────────────


class Module(Base):
    __tablename__ = "modules"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid7)
    course_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("courses.id", ondelete="CASCADE")
    )
    title: Mapped[str] = mapped_column(String(500))
    order: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # Relationships
    course: Mapped["Course"] = relationship(back_populates="modules")
    lessons: Mapped[list["Lesson"]] = relationship(
        back_populates="module", cascade="all, delete-orphan"
    )


class Lesson(Base):
    __tablename__ = "lessons"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid7)
    module_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("modules.id", ondelete="CASCADE")
    )
    title: Mapped[str] = mapped_column(String(500))
    order: Mapped[int] = mapped_column(Integer)
    video_start_timecode: Mapped[str | None] = mapped_column(String(20))
    video_end_timecode: Mapped[str | None] = mapped_column(String(20))
    slide_range: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # Relationships
    module: Mapped["Module"] = relationship(back_populates="lessons")
    concepts: Mapped[list["Concept"]] = relationship(
        back_populates="lesson", cascade="all, delete-orphan"
    )
    exercises: Mapped[list["Exercise"]] = relationship(
        back_populates="lesson", cascade="all, delete-orphan"
    )


class Concept(Base):
    __tablename__ = "concepts"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid7)
    lesson_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("lessons.id", ondelete="CASCADE")
    )
    title: Mapped[str] = mapped_column(String(500))
    definition: Mapped[str] = mapped_column(Text)
    examples: Mapped[list[Any] | None] = mapped_column(JSONB)
    timecodes: Mapped[list[Any] | None] = mapped_column(JSONB)
    slide_references: Mapped[list[Any] | None] = mapped_column(JSONB)
    web_references: Mapped[list[Any] | None] = mapped_column(JSONB)
    # WARNING: Vector dimension is tied to a specific embedding model.
    # 1536 = OpenAI text-embedding-3-small. Changing the model later
    # requires an ALTER COLUMN migration. Consider making this configurable
    # or choosing a model before committing to a dimension.
    embedding: Mapped[list[float] | None] = mapped_column(Vector(1536))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # Relationships
    lesson: Mapped["Lesson"] = relationship(back_populates="concepts")


class Exercise(Base):
    __tablename__ = "exercises"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid7)
    lesson_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("lessons.id", ondelete="CASCADE")
    )
    description: Mapped[str] = mapped_column(Text)
    reference_solution: Mapped[str | None] = mapped_column(Text)
    grading_criteria: Mapped[str | None] = mapped_column(Text)
    difficulty_level: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # Relationships
    lesson: Mapped["Lesson"] = relationship(back_populates="exercises")


# ──────────────────────────────────────────────
# Observability
# ──────────────────────────────────────────────


class LLMCall(Base):
    __tablename__ = "llm_calls"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid7)
    action: Mapped[str] = mapped_column(String(100), default="")
    strategy: Mapped[str] = mapped_column(String(50), default="default")
    provider: Mapped[str] = mapped_column(String(50))
    model_id: Mapped[str] = mapped_column(String(100))
    prompt_version: Mapped[str | None] = mapped_column(String(50))
    tokens_in: Mapped[int | None] = mapped_column(Integer)
    tokens_out: Mapped[int | None] = mapped_column(Integer)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    cost_usd: Mapped[float | None] = mapped_column(Float)
    success: Mapped[bool] = mapped_column(default=True)
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
