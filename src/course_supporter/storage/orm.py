"""SQLAlchemy ORM models for all project entities."""

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

import uuid_utils as uuid7_lib
from sqlalchemy import (
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
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
# Material Tree
# ──────────────────────────────────────────────


class MaterialNode(Base):
    """Node in the material tree (recursive adjacency list).

    Root nodes (parent_id IS NULL) serve as "courses" — top-level
    entities owned by a tenant. Child nodes form an arbitrary-depth
    hierarchy for organising materials.
    """

    __tablename__ = "material_nodes"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid7)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("material_nodes.id", ondelete="CASCADE"), index=True
    )
    title: Mapped[str] = mapped_column(String(500))
    description: Mapped[str | None] = mapped_column(Text)
    learning_goal: Mapped[str | None] = mapped_column(Text)
    expected_knowledge: Mapped[list[str] | None] = mapped_column(JSONB)
    expected_skills: Mapped[list[str] | None] = mapped_column(JSONB)
    order: Mapped[int] = mapped_column(Integer, default=0)
    node_fingerprint: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # Relationships
    tenant: Mapped["Tenant"] = relationship()
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
    slide_video_mappings: Mapped[list["SlideVideoMapping"]] = relationship(
        back_populates="node",
        cascade="all, delete-orphan",
    )
    snapshots: Mapped[list["StructureSnapshot"]] = relationship(
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


class MappingValidationState(StrEnum):
    """Validation state for slide-video mappings."""

    VALIDATED = "validated"
    PENDING_VALIDATION = "pending_validation"
    VALIDATION_FAILED = "validation_failed"


class SlideVideoMapping(Base):
    """Maps a specific slide in a presentation to a timecode range in a video.

    Both presentation and video are referenced by FK to MaterialEntry.
    Validation is tracked via ``validation_state`` with optional JSONB
    ``blocking_factors`` (deferred validation) and ``validation_errors``.
    """

    __tablename__ = "slide_video_mappings"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid7)
    node_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("material_nodes.id", ondelete="CASCADE"), index=True
    )
    presentation_entry_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("material_entries.id", ondelete="CASCADE"), index=True
    )
    video_entry_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("material_entries.id", ondelete="CASCADE"), index=True
    )
    slide_number: Mapped[int] = mapped_column(Integer)
    video_timecode_start: Mapped[str] = mapped_column(String(20))
    video_timecode_end: Mapped[str | None] = mapped_column(String(20))
    order: Mapped[int] = mapped_column(Integer, default=0)

    # ── Validation tracking ──
    validation_state: Mapped[str] = mapped_column(
        Enum(
            "validated",
            "pending_validation",
            "validation_failed",
            name="mapping_validation_state_enum",
            create_type=False,
        ),
        default=MappingValidationState.PENDING_VALIDATION,
    )
    blocking_factors: Mapped[list[Any] | None] = mapped_column(JSONB)
    validation_errors: Mapped[list[Any] | None] = mapped_column(JSONB)
    validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # Relationships
    node: Mapped["MaterialNode"] = relationship(back_populates="slide_video_mappings")
    presentation_entry: Mapped["MaterialEntry"] = relationship(
        foreign_keys=[presentation_entry_id]
    )
    video_entry: Mapped["MaterialEntry"] = relationship(foreign_keys=[video_entry_id])


# ──────────────────────────────────────────────
# Structure Snapshots
# ──────────────────────────────────────────────


NIL_UUID = uuid.UUID("00000000-0000-0000-0000-000000000000")
"""Sentinel UUID used in COALESCE expressions for nullable columns."""


class GenerationMode(StrEnum):
    """Mode used for course structure generation."""

    FREE = "free"
    GUIDED = "guided"


class StructureSnapshot(Base):
    """Immutable snapshot of a generated course structure.

    Tied to a specific (node, fingerprint, mode) combination
    for idempotency: re-generating with the same inputs returns the
    existing snapshot instead of calling the LLM again.

    LLM metadata (model_id, tokens, cost) is stored in the linked
    ExternalServiceCall record — no duplication in the snapshot.

    ``node_id`` references the root MaterialNode (= course) for
    course-level snapshots, or a child node for subtree snapshots.
    """

    __tablename__ = "structure_snapshots"
    __table_args__ = (
        Index(
            "uq_snapshots_identity",
            "node_id",
            "node_fingerprint",
            "mode",
            unique=True,
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid7)
    node_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("material_nodes.id", ondelete="CASCADE"), index=True
    )
    externalservicecall_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("external_service_calls.id", ondelete="SET NULL"), index=True
    )
    node_fingerprint: Mapped[str] = mapped_column(String(64))
    mode: Mapped[GenerationMode] = mapped_column(String(20))
    structure: Mapped[dict[str, Any]] = mapped_column(JSONB)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # Relationships
    node: Mapped["MaterialNode"] = relationship(back_populates="snapshots")
    service_call: Mapped["ExternalServiceCall | None"] = relationship()


# ──────────────────────────────────────────────
# Job Tracking
# ──────────────────────────────────────────────


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid7)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("tenants.id", ondelete="SET NULL"), index=True
    )
    node_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("material_nodes.id", ondelete="SET NULL"), index=True
    )
    job_type: Mapped[str] = mapped_column(String(50))
    priority: Mapped[str] = mapped_column(String(20), default="normal")
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    arq_job_id: Mapped[str | None] = mapped_column(String(100))
    input_params: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    depends_on: Mapped[list[str] | None] = mapped_column(JSONB)
    error_message: Mapped[str | None] = mapped_column(Text)
    queued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    estimated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Relationships
    tenant: Mapped["Tenant | None"] = relationship()
    material_entries: Mapped[list["MaterialEntry"]] = relationship(
        back_populates="pending_job"
    )


# ──────────────────────────────────────────────
# Observability
# ──────────────────────────────────────────────


class ExternalServiceCall(Base):
    __tablename__ = "external_service_calls"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid7)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("jobs.id", ondelete="SET NULL"), index=True
    )
    action: Mapped[str] = mapped_column(String(100), default="")
    strategy: Mapped[str] = mapped_column(String(50), default="default")
    provider: Mapped[str] = mapped_column(String(50))
    model_id: Mapped[str] = mapped_column(String(100))
    prompt_ref: Mapped[str | None] = mapped_column(String(50))
    unit_type: Mapped[str | None] = mapped_column(String(20))
    unit_in: Mapped[int | None] = mapped_column(Integer)
    unit_out: Mapped[int | None] = mapped_column(Integer)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    cost_usd: Mapped[float | None] = mapped_column(Float)
    success: Mapped[bool] = mapped_column(default=True)
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # Relationships
    tenant: Mapped["Tenant | None"] = relationship()
    job: Mapped["Job | None"] = relationship()
