"""SQLAlchemy ORM models for all project entities."""

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

import uuid_utils as uuid7_lib
from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    CheckConstraint,
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
# Multi-Tenant Auth
# ──────────────────────────────────────────────


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid7)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # Relationships
    api_keys: Mapped[list["APIKey"]] = relationship(
        back_populates="tenant", cascade="all, delete-orphan"
    )


class APIKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid7)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE")
    )
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    key_prefix: Mapped[str] = mapped_column(String(16))
    label: Mapped[str] = mapped_column(String(100), default="default")
    scopes: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    rate_limit_prep: Mapped[int] = mapped_column(Integer, default=60)
    rate_limit_check: Mapped[int] = mapped_column(Integer, default=300)
    is_active: Mapped[bool] = mapped_column(default=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # Relationships
    tenant: Mapped["Tenant"] = relationship(back_populates="api_keys")


# ──────────────────────────────────────────────
# Course & Source Materials
# ──────────────────────────────────────────────


class Course(Base):
    __tablename__ = "courses"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid7)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    title: Mapped[str] = mapped_column(String(500))
    description: Mapped[str | None] = mapped_column(Text)
    learning_goal: Mapped[str | None] = mapped_column(Text)
    expected_knowledge: Mapped[list[Any] | None] = mapped_column(JSONB)
    expected_skills: Mapped[list[Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # Relationships
    tenant: Mapped["Tenant"] = relationship()
    source_materials: Mapped[list["SourceMaterial"]] = relationship(
        back_populates="course", cascade="all, delete-orphan"
    )
    slide_video_mappings: Mapped[list["SlideVideoMapping"]] = relationship(
        back_populates="course", cascade="all, delete-orphan"
    )
    modules: Mapped[list["Module"]] = relationship(
        back_populates="course", cascade="all, delete-orphan"
    )
    material_nodes: Mapped[list["MaterialNode"]] = relationship(
        back_populates="course", cascade="all, delete-orphan"
    )


class MaterialNode(Base):
    """Node in the material tree (recursive adjacency list).

    Represents a folder-like grouping within a course. Materials
    (MaterialEntry, added in S2-014) attach to nodes at any depth.
    """

    __tablename__ = "material_nodes"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid7)
    course_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("courses.id", ondelete="CASCADE"), index=True
    )
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("material_nodes.id", ondelete="CASCADE"), index=True
    )
    title: Mapped[str] = mapped_column(String(500))
    description: Mapped[str | None] = mapped_column(Text)
    order: Mapped[int] = mapped_column(Integer, default=0)
    node_fingerprint: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # Relationships
    course: Mapped["Course"] = relationship(back_populates="material_nodes")
    parent: Mapped["MaterialNode | None"] = relationship(
        back_populates="children",
        remote_side="MaterialNode.id",
    )
    children: Mapped[list["MaterialNode"]] = relationship(
        back_populates="parent",
        cascade="all, delete-orphan",
    )
    materials: Mapped[list["MaterialEntry"]] = relationship(
        back_populates="node",
        cascade="all, delete-orphan",
    )


class MaterialState(StrEnum):
    """Derived state of a MaterialEntry (not stored in DB)."""

    RAW = "raw"
    PENDING = "pending"
    READY = "ready"
    INTEGRITY_BROKEN = "integrity_broken"
    ERROR = "error"


class MaterialEntry(Base):
    """A single material attached to a node in the material tree.

    Separates raw (uploaded) and processed (ingested) layers with a
    pending "receipt" that tracks whether an ingestion job is in flight.
    State is derived via the ``state`` property — see ``MaterialState``.
    """

    __tablename__ = "material_entries"

    def __repr__(self) -> str:
        return (
            f"<MaterialEntry(id={self.id}, "
            f"source_type='{self.source_type}', "
            f"node_id={self.node_id})>"
        )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid7)
    node_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("material_nodes.id", ondelete="CASCADE"), index=True
    )
    source_type: Mapped[str] = mapped_column(
        Enum(
            "video",
            "presentation",
            "text",
            "web",
            name="source_type_enum",
            create_type=False,
        )
    )
    order: Mapped[int] = mapped_column(Integer, default=0)

    # ── Raw layer ──
    source_url: Mapped[str] = mapped_column(String(2000))
    filename: Mapped[str | None] = mapped_column(String(500))
    raw_hash: Mapped[str | None] = mapped_column(String(64))
    raw_size_bytes: Mapped[int | None] = mapped_column(Integer)

    # ── Processed layer ──
    processed_hash: Mapped[str | None] = mapped_column(String(64))
    processed_content: Mapped[str | None] = mapped_column(Text)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # ── Pending "receipt" ──
    pending_job_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("jobs.id", ondelete="SET NULL"), index=True
    )
    pending_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # ── Fingerprint ──
    content_fingerprint: Mapped[str | None] = mapped_column(String(64))

    # ── Errors ──
    error_message: Mapped[str | None] = mapped_column(Text)

    # ── Derived state ──

    @property
    def state(self) -> MaterialState:
        """Derive current state from entry fields.

        Priority: ERROR > PENDING > RAW > INTEGRITY_BROKEN > READY.
        """
        if self.error_message:
            return MaterialState.ERROR
        if self.pending_job_id is not None:
            return MaterialState.PENDING
        if self.processed_content is None:
            return MaterialState.RAW
        if self.raw_hash and self.processed_hash != self.raw_hash:
            return MaterialState.INTEGRITY_BROKEN
        return MaterialState.READY

    # ── Timestamps ──
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # Relationships
    node: Mapped["MaterialNode"] = relationship(back_populates="materials")
    pending_job: Mapped["Job | None"] = relationship(back_populates="material_entries")


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
    description: Mapped[str | None] = mapped_column(Text)
    learning_goal: Mapped[str | None] = mapped_column(Text)
    expected_knowledge: Mapped[list[Any] | None] = mapped_column(JSONB)
    expected_skills: Mapped[list[Any] | None] = mapped_column(JSONB)
    difficulty: Mapped[str | None] = mapped_column(String(20))
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
# Job Tracking
# ──────────────────────────────────────────────


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        CheckConstraint(
            "NOT (result_material_id IS NOT NULL AND result_snapshot_id IS NOT NULL)",
            name="chk_job_result_exclusive",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid7)
    course_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("courses.id", ondelete="SET NULL"), index=True
    )
    node_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, index=True)
    job_type: Mapped[str] = mapped_column(String(50))
    priority: Mapped[str] = mapped_column(String(20), default="normal")
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    arq_job_id: Mapped[str | None] = mapped_column(String(100))
    input_params: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    result_material_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    result_snapshot_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    depends_on: Mapped[list[str] | None] = mapped_column(JSONB)
    error_message: Mapped[str | None] = mapped_column(Text)
    queued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    estimated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Relationships
    course: Mapped["Course | None"] = relationship()
    material_entries: Mapped[list["MaterialEntry"]] = relationship(
        back_populates="pending_job"
    )


# ──────────────────────────────────────────────
# Observability
# ──────────────────────────────────────────────


class LLMCall(Base):
    __tablename__ = "llm_calls"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid7)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
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

    # Relationships
    tenant: Mapped["Tenant | None"] = relationship()
