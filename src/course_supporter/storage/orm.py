"""SQLAlchemy ORM models for all project entities."""

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any, ClassVar

import uuid_utils as uuid7_lib
from sqlalchemy import (
    CheckConstraint,
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
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from course_supporter.storage.cascade import (
    ScrubCallable,
    scrub_authored_document,
    scrub_course_node,
)


def _uuid7() -> uuid.UUID:
    """Generate a UUIDv7 (time-ordered) for use as default PK value."""
    return uuid.UUID(bytes=uuid7_lib.uuid7().bytes)


class Base(DeclarativeBase):
    """Base class for all ORM models."""


class SoftDeleteMixin:
    """Universal soft-delete pattern (vision §3 KD3, KD12).

    Adds a nullable ``deleted_at`` timestamp to a mapped model. ``NULL``
    means the row is active; a non-null timestamp means soft-deleted.

    Cascade descendants are declared via ``__cascades_soft_delete_to__``
    — a list of model classes that ``CascadeDeleteService`` should also
    soft-delete when this entity is deleted. The skeleton is delivered
    in task 0.1 with empty lists; concrete cascades are wired
    per-entity in later phase tasks (1.x, 3.x, 4.x).

    Author-content scrubbing on delete is the responsibility of each
    entity's own deletion handler — vision KD3 lists per-entity scrub
    rules. This mixin only provides the ``deleted_at`` column and the
    cascade declaration hook.

    The accompanying Alembic migration adds:
      - ``deleted_at TIMESTAMPTZ NULL`` column on every soft-deletable table.
      - ``ix_<table>_active`` partial index on ``deleted_at IS NULL`` for
        cheap "active-only" queries.
      - ``BEFORE UPDATE`` trigger that blocks any UPDATE on already
        soft-deleted rows except ones that change *only* ``deleted_at``
        itself (allowing un-delete and idempotent re-delete via raw
        SQL). Repository-level idempotency is enforced earlier in
        ``SoftDeleteRepository.soft_delete``, so the trigger is a
        safety net for direct DB writes.
    """

    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        default=None,
        comment="Soft-delete timestamp (vision KD3). "
        "NULL = active; NOT NULL = soft-deleted.",
    )

    __cascades_soft_delete_to__: ClassVar[list[type]] = []
    """Descendant model classes that cascade-delete from this entity.

    Populated per-entity in subsequent phase tasks.
    """

    __scrub_callable__: ClassVar[ScrubCallable | None] = None
    """Per-class scrub callable for KD3 content-field clearing on cascade
    soft-delete (models-fix-3 — corrects Choice 1's root-only assertion).

    ``None`` (default) means this class contributes no scrub on cascade
    — appropriate for identification-only models (``APIKey``, ``Job``,
    ``Tenant``) and for entities whose scrub is wired Phase 2.x+
    (``DocumentSummary``, ``DocumentSegment`` — pipeline does not run
    in Phase 1, tables stay empty, cascade traversal through them is
    no-op anyway). Concrete scrub callables for ``CourseNode`` and
    ``AuthoredDocument`` are runtime-assigned at module bottom (same
    pattern as ``__cascades_soft_delete_to__``).

    :class:`CascadeDeleteService` reads this attribute via
    ``getattr(type(victim), "__scrub_callable__", None)`` for every
    victim in the cascade BFS — see :data:`ScrubCallable` for the
    dispatch semantics including the per-call override path.
    """


# ──────────────────────────────────────────────
# Multi-Tenant Auth
# ──────────────────────────────────────────────


class Tenant(SoftDeleteMixin, Base):
    __tablename__ = "tenants"
    __table_args__ = (
        Index(
            "ix_tenants_active",
            "deleted_at",
            postgresql_where=text("deleted_at IS NULL"),
        ),
        {"comment": "Multi-tenant organizations"},
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid7)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    webhook_url: Mapped[str | None] = mapped_column(
        String(2000),
        comment="Default webhook URL for homework submission results",
    )

    # Relationships
    api_keys: Mapped[list["APIKey"]] = relationship(
        # KD-alpha: stripped ``delete-orphan`` so ORM-level ``session.delete``
        # cannot bypass the soft-delete cascade. Soft-deletion flows
        # exclusively through ``CascadeDeleteService`` per vision §3 KD3.
        back_populates="tenant",
        cascade="save-update, merge",
    )


class APIKey(SoftDeleteMixin, Base):
    __tablename__ = "api_keys"
    __table_args__ = (
        Index(
            "ix_api_keys_active",
            "deleted_at",
            postgresql_where=text("deleted_at IS NULL"),
        ),
        {"comment": "Authentication keys with scope-based access control"},
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid7)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE")
    )
    key_hash: Mapped[str] = mapped_column(
        String(64),
        unique=True,
        index=True,
        comment="SHA-256 hash of the API key. Raw key is never stored",
    )
    key_prefix: Mapped[str] = mapped_column(
        String(16),
        comment="First 8 chars of the key for identification in logs",
    )
    label: Mapped[str] = mapped_column(
        String(100),
        default="default",
        comment="Human-readable label to distinguish keys in UI and logs",
    )
    scopes: Mapped[list[Any]] = mapped_column(
        JSONB,
        default=list,
        comment="JSON array of granted scopes. "
        "Defined in config/auth.yaml, validated by AuthScope enum",
    )
    plan_id: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        server_default="basic",
        comment="Name of plan from config/auth.yaml — "
        "resolves to per-scope rate limits at runtime.",
    )
    is_active: Mapped[bool] = mapped_column(default=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # Relationships
    tenant: Mapped["Tenant"] = relationship(back_populates="api_keys")


# ──────────────────────────────────────────────
# Course Tree (vision §2.2)
# ──────────────────────────────────────────────


class CourseNode(SoftDeleteMixin, Base):
    """Node in the course tree (recursive adjacency list).

    Root nodes (parent_id IS NULL) serve as "courses" — top-level
    entities owned by a tenant. Child nodes form an arbitrary-depth
    hierarchy for organising authored documents.
    """

    __tablename__ = "course_nodes"
    __table_args__ = (
        Index(
            "ix_course_nodes_active",
            "deleted_at",
            postgresql_where=text("deleted_at IS NULL"),
        ),
        {
            "comment": (
                "Hierarchical course tree (vision §2.2). "
                "Root node (parent IS NULL) = course"
            ),
        },
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid7)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("course_nodes.id", ondelete="CASCADE"),
        index=True,
        comment="Self-referential FK. NULL = root node (course level)",
    )
    title: Mapped[str] = mapped_column(String(500))
    description: Mapped[str | None] = mapped_column(Text)
    default_language: Mapped[str | None] = mapped_column(
        String(10),
        comment="Default ISO 639-1 language for materials under this subtree. "
        "Usually set on the root (course) node; inherited by children and "
        "their materials unless overridden on the material itself.",
    )
    order: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="Sibling sort key within parent. NULL = no preferred order, "
        "sorted last by created_at. Sort: ORDER BY order ASC NULLS LAST, "
        "created_at ASC.",
    )
    content_hash: Mapped[str | None] = mapped_column(
        String(64),
        comment="Materialised content_hash (vision §3 KD9, SHA-256 hex). "
        "NULL = never computed / stale. Populated at INSERT/UPDATE; "
        "per-entity formula wired in Phase 2/3.",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # Relationships
    tenant: Mapped["Tenant"] = relationship()
    parent: Mapped["CourseNode | None"] = relationship(
        back_populates="children",
        remote_side="CourseNode.id",
    )
    children: Mapped[list["CourseNode"]] = relationship(
        # KD-alpha: stripped ``delete-orphan`` (self-ref tree). Subtree
        # soft-deletion flows through ``CascadeDeleteService`` per
        # ``__cascades_soft_delete_to__ = [CourseNode, ...]``.
        back_populates="parent",
        cascade="save-update, merge",
    )
    documents: Mapped[list["AuthoredDocument"]] = relationship(
        # KD-alpha: stripped ``delete-orphan``. Document soft-deletion is
        # routed through ``delete_document`` → ``CascadeDeleteService``
        # so ``s3_cleanup`` enqueue and KD-β scrub stay in the loop.
        back_populates="node",
        cascade="save-update, merge",
        foreign_keys="AuthoredDocument.course_node_id",
    )
    snapshots: Mapped[list["StructureSnapshot"]] = relationship(
        # Phase-5-bound legacy entity — ``cascade="all, delete-orphan"``
        # preserved intentionally; deletion semantics revisit when
        # the StructureSnapshot family is removed.
        back_populates="node",
        cascade="all, delete-orphan",
    )
    editables: Mapped[list["StructureNodeEditable"]] = relationship(
        # Phase-5-bound legacy entity (see ``snapshots`` note).
        back_populates="material_node",
        cascade="all, delete-orphan",
        foreign_keys="StructureNodeEditable.materialnode_id",
    )


class MaterialState(StrEnum):
    """Derived state of a AuthoredDocument (not stored in DB)."""

    PENDING = "pending"
    READY = "ready"
    ERROR = "error"


class AuthoredDocument(SoftDeleteMixin, Base):
    """A single material attached to a node in the material tree.

    Separates raw (uploaded) and processed (ingested) layers with a
    pending "receipt" that tracks whether an ingestion job is in flight.
    State is derived via the ``state`` property — see ``MaterialState``.
    """

    __tablename__ = "authored_documents"
    __table_args__ = (
        Index(
            "ix_authored_documents_active",
            "deleted_at",
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index(
            "ix_authored_documents_raw_hash",
            "raw_hash",
            postgresql_where=text("deleted_at IS NULL"),
        ),
        {
            "comment": "Authored documents (vision §1.2): "
            "raw uploads of video / audio / presentation / text / web sources",
        },
    )

    # Cascade engine FK disambiguation. AuthoredDocument carries two
    # FKs to ``course_nodes``: ``course_node_id`` (parent FK — the
    # tree edge) and ``course_root_id`` (KD-delta denormalized root
    # FK). Without this declaration ``cascade._resolve_cascade_columns``
    # cannot pick which to use when CourseNode → AuthoredDocument
    # cascades fire. Parent FK is the cascade-relevant one.
    __cascade_fk_from__: ClassVar[dict[str, str]] = {"CourseNode": "course_node_id"}

    def __repr__(self) -> str:
        return (
            f"<AuthoredDocument(id={self.id}, "
            f"source_type='{self.source_type}', "
            f"course_node_id={self.course_node_id})>"
        )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid7)
    course_node_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("course_nodes.id", ondelete="CASCADE"),
        index=True,
        comment="FK to parent CourseNode in the tree",
    )
    course_root_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("course_nodes.id", ondelete="CASCADE"),
        index=True,
        comment="Denormalised root CourseNode id (KD-delta). Computed via "
        "parent walk to root at INSERT/move; defensive default in "
        "AuthoredDocumentRepository.create() if caller omits.",
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
    material_role: Mapped[str] = mapped_column(
        Enum(
            "educational",
            "methodological",
            name="material_role_enum",
            create_type=False,
        ),
        default="educational",
        server_default="educational",
        comment="Role: educational (content) or methodological (intent)",
    )
    task_type: Mapped[str | None] = mapped_column(
        Enum(
            "test",
            "short_task",
            "task",
            "project",
            name="task_type_enum",
            create_type=False,
        ),
        nullable=True,
        default=None,
        comment="Assignment type when material is a concrete task "
        "(test/short_task/task/project). NULL for regular materials.",
    )
    order: Mapped[int] = mapped_column(Integer, default=0)

    # ── Raw layer ──
    source_url: Mapped[str] = mapped_column(
        String(2000),
        comment="External URL or S3 object path for the raw material",
    )
    filename: Mapped[str | None] = mapped_column(String(500))
    raw_hash: Mapped[str | None] = mapped_column(
        String(64),
        comment="SHA-256 of uploaded raw file for integrity detection",
    )
    raw_size_bytes: Mapped[int | None] = mapped_column(Integer)

    # ── Language ──
    language: Mapped[str | None] = mapped_column(
        String(10),
        comment="ISO 639-1 language of the material. NULL = inherit from course "
        "(root CourseNode.default_language) or auto-detect at STT time. "
        "Auto-detected language is cached back to this column on first success.",
    )

    # ── Processed layer ──
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # ── Content hash (KD9) ──
    content_hash: Mapped[str | None] = mapped_column(
        String(64),
        comment="Materialised content_hash (vision §3 KD9, SHA-256 hex). "
        "NULL = never computed / stale. Populated at INSERT/UPDATE; "
        "per-entity formula wired in Phase 2.",
    )

    # ── Pending "receipt" ──
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("jobs.id", ondelete="SET NULL"),
        index=True,
        comment="FK to in-flight ingestion Job. NULL when idle",
    )
    pending_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # ── Errors ──
    error_message: Mapped[str | None] = mapped_column(Text)

    # ── Derived state ──

    @property
    def state(self) -> MaterialState:
        """Derive current state. Phase 2.x will refine after Pass 2 pipeline.

        Priority: ERROR > PENDING > READY. RAW / INTEGRITY_BROKEN states
        relied on dropped processed_content / processed_hash columns; their
        new semantics land with the Pass 2 pipeline introduction.
        """
        if self.error_message:
            return MaterialState.ERROR
        if self.job_id is not None:
            return MaterialState.PENDING
        return MaterialState.READY

    # ── Timestamps ──
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # Relationships
    node: Mapped["CourseNode"] = relationship(
        back_populates="documents",
        foreign_keys="AuthoredDocument.course_node_id",
    )
    pending_job: Mapped["Job | None"] = relationship(
        back_populates="authored_documents"
    )
    summary: Mapped["DocumentSummary | None"] = relationship(
        # KD-alpha: stripped ``delete-orphan`` from the 1:1 summary (uselist=
        # False since commit (b)). DocumentSummary soft-deletion routes
        # through ``CascadeDeleteService`` per ``AuthoredDocument
        # .__cascades_soft_delete_to__ = [DocumentSummary]``.
        back_populates="authored_document",
        cascade="save-update, merge",
        uselist=False,
    )


class MacroSectionStatus(StrEnum):
    """Processing status of a DocumentSummary."""

    PENDING = "pending"
    READY = "ready"
    FAILED = "failed"


class DocumentSummary(SoftDeleteMixin, Base):
    """1:1 summary of an AuthoredDocument (vision §1.3).

    One row per AuthoredDocument (UNIQUE on authored_document_id, KD-theta.2).
    Produced by Pass 2a of the ingestion pipeline — LLM call for
    video/audio/presentation or deterministic ladder for text/web.
    Immutable after status=ready; regeneration replaces the row.
    """

    __tablename__ = "document_summaries"
    __table_args__ = (
        Index(
            "ix_document_summaries_unready",
            "status",
            postgresql_where=text("status != 'ready'"),
        ),
        Index(
            "ix_document_summaries_active",
            "deleted_at",
            postgresql_where=text("deleted_at IS NULL"),
        ),
        {
            "comment": (
                "1:1 summary entity per AuthoredDocument "
                "(Pass 2a output of the ingestion pipeline)"
            ),
        },
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid7)
    authored_document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("authored_documents.id", ondelete="CASCADE"),
        unique=True,
        index=True,
        comment="FK to parent AuthoredDocument (UNIQUE — 1:1 invariant per KD-theta.2)",
    )
    course_root_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("course_nodes.id", ondelete="CASCADE"),
        index=True,
        comment="Denormalised root CourseNode id (KD-delta). Inherited from "
        "authored_documents.course_root_id at INSERT.",
    )
    title: Mapped[str] = mapped_column(
        String(128),
        comment="Self-contained summary title (≤128 per vision §2.2)",
    )
    description: Mapped[str | None] = mapped_column(
        String(512),
        comment="Optional brief description (≤512 per vision §2.2)",
    )
    main_concepts: Mapped[list[str]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'[]'::jsonb"),
        comment="List of concept strings taught in depth (KD-gamma jsonb).",
    )
    secondary_concepts: Mapped[list[str]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'[]'::jsonb"),
        comment="List of concept strings mentioned but not taught in depth.",
    )
    content_char_count: Mapped[int | None] = mapped_column(
        Integer,
        comment="Total char count over child DocumentSegment.content "
        "(size-metric for weighting per vision §2.2 line 240).",
    )
    status: Mapped[str] = mapped_column(
        Enum(
            "pending",
            "ready",
            "failed",
            name="material_macro_section_status_enum",
            create_type=False,
        ),
        server_default="pending",
        comment="Lifecycle status: pending → ready | failed. "
        "DB enum type identifier preserved per Phase 1 C2 lock.",
    )
    error_message: Mapped[str | None] = mapped_column(Text)
    llm_call_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("external_service_calls.id", ondelete="SET NULL"),
        comment="FK to ExternalServiceCall that produced this summary. "
        "NULL for deterministic (text/web) summaries",
    )
    content_hash: Mapped[str | None] = mapped_column(
        String(64),
        comment="Materialised content_hash (vision §3 KD9, SHA-256 hex). "
        "NULL = never computed / stale. Populated at INSERT/UPDATE; "
        "Phase 1.3 formula extends over title + description + concepts.",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # Relationships
    authored_document: Mapped["AuthoredDocument"] = relationship(
        back_populates="summary"
    )
    segments: Mapped[list["DocumentSegment"]] = relationship(
        # KD-alpha: stripped ``delete-orphan``. Segment soft-deletion routes
        # through ``CascadeDeleteService`` per ``DocumentSummary
        # .__cascades_soft_delete_to__ = [DocumentSegment]``.
        back_populates="document_summary",
        cascade="save-update, merge",
    )
    llm_call: Mapped["ExternalServiceCall | None"] = relationship()


class DocumentSegment(SoftDeleteMixin, Base):
    """Cleaned/sliced content unit inside a DocumentSummary (vision §1.4).

    Produced by Pass 2b of the ingestion pipeline. For
    video/audio/presentation this is LLM-cleaned narrative; for text/web
    this is a deterministic slice of the upstream document content.

    Concept-search source-of-truth: GIN-indexed jsonb concept columns
    (KD-gamma) — `main_concepts` and `secondary_concepts` are queried via
    JSONB containment (`@>`) for "where in the course is concept X
    explained?" navigation per vision §2.2 lines 273-277.

    Positional invariants (enforced at write time, not by DB):
    - segment.start_pos >= 0 (CHECK)
    - segment.end_pos > segment.start_pos (CHECK)
    """

    __tablename__ = "document_segments"
    __table_args__ = (
        Index(
            "ix_document_segments_summary_order",
            "document_summary_id",
            "order",
        ),
        Index(
            "ix_document_segments_summary_start_pos",
            "document_summary_id",
            "start_pos",
        ),
        Index(
            "ix_document_segments_active",
            "deleted_at",
            postgresql_where=text("deleted_at IS NULL"),
        ),
        CheckConstraint("start_pos >= 0", name="ck_segment_start_pos_nonneg"),
        CheckConstraint("end_pos > start_pos", name="ck_segment_end_pos_gt_start"),
        {
            "comment": (
                "Cleaned / sliced content segments inside a "
                "DocumentSummary (Pass 2b output)"
            ),
        },
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid7)
    document_summary_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("document_summaries.id", ondelete="CASCADE"),
        index=True,
        comment="FK to parent DocumentSummary",
    )
    course_root_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("course_nodes.id", ondelete="CASCADE"),
        index=True,
        comment="Denormalised root CourseNode id (KD-delta). Inherited from "
        "document_summaries.course_root_id at INSERT.",
    )
    order: Mapped[int] = mapped_column(
        Integer,
        comment="0-indexed position within the parent summary",
    )
    start_pos: Mapped[int] = mapped_column(
        Integer,
        comment="Absolute start in the source unit (not relative to the summary)",
    )
    end_pos: Mapped[int] = mapped_column(
        Integer,
        comment="Absolute end in the source unit",
    )
    content: Mapped[str] = mapped_column(
        Text,
        comment="Cleaned (LLM) or sliced (text/web) content of this segment",
    )
    main_concepts: Mapped[list[str]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'[]'::jsonb"),
        comment="List of concept strings taught in this segment (KD-gamma "
        "jsonb; GIN-indexed with jsonb_path_ops opclass for @> containment).",
    )
    secondary_concepts: Mapped[list[str]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'[]'::jsonb"),
        comment="List of concept strings mentioned but not taught (KD-gamma).",
    )
    content_char_count: Mapped[int | None] = mapped_column(
        Integer,
        comment="Char count of content (size-metric for weighting per vision "
        "§2.2 lines 236-238; backfilled from length(content) at INSERT).",
    )
    llm_call_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("external_service_calls.id", ondelete="SET NULL"),
        comment="FK to ExternalServiceCall that produced this segment. "
        "NULL for deterministic (text/web) slices",
    )
    content_hash: Mapped[str | None] = mapped_column(
        String(64),
        comment="Materialised content_hash (vision §3 KD9, SHA-256 hex). "
        "NULL = never computed / stale. Populated at INSERT/UPDATE; "
        "Phase 1.4 formula extends over content + concepts.",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # Relationships
    document_summary: Mapped["DocumentSummary"] = relationship(
        back_populates="segments"
    )
    llm_call: Mapped["ExternalServiceCall | None"] = relationship()


# ──────────────────────────────────────────────
# Structure Snapshots
# ──────────────────────────────────────────────


NIL_UUID = uuid.UUID("00000000-0000-0000-0000-000000000000")
"""Sentinel UUID used in COALESCE expressions for nullable columns."""


class GenerationMode(StrEnum):
    """Mode used for course structure generation."""

    FREE = "free"
    GUIDED = "guided"


class StructureNodeType(StrEnum):
    """Type of a node in the generated course structure."""

    MODULE = "module"
    LESSON = "lesson"
    CONCEPT = "concept"
    EXERCISE = "exercise"


class StructureNode(Base):
    """Recursive node in a generated course structure.

    Each node belongs to a ``StructureSnapshot`` and forms an
    arbitrary-depth tree via ``parent_structurenode_id`` self-reference.
    Root nodes (parent_structurenode_id IS NULL) are top-level modules.

    Fields are organised into six sections corresponding to
    different agent / pipeline stages that populate them.
    """

    __tablename__ = "structure_nodes"
    __table_args__ = {
        "comment": (
            "Recursive tree of generated course structure elements. "
            "Editable by course author for corrections and additions"
        ),
    }

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid7)
    structuresnapshot_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("structure_snapshots.id", ondelete="CASCADE"), index=True
    )
    parent_structurenode_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("structure_nodes.id", ondelete="CASCADE"),
        index=True,
        comment="Self-referential FK. NULL = top-level module",
    )
    node_type: Mapped[str] = mapped_column(
        String(30),
        index=True,
        comment="Enum: module, lesson, concept, exercise",
    )
    order: Mapped[int] = mapped_column(Integer, default=0)

    # ── Section 1: Formal & organisational (Methodologist agent) ──
    title: Mapped[str] = mapped_column(String(500))
    description: Mapped[str | None] = mapped_column(Text)
    learning_goal: Mapped[str | None] = mapped_column(Text)
    expected_knowledge: Mapped[list[dict[str, str]] | None] = mapped_column(JSONB)
    expected_skills: Mapped[list[dict[str, str]] | None] = mapped_column(JSONB)
    prerequisites: Mapped[list[str] | None] = mapped_column(JSONB)
    difficulty: Mapped[str | None] = mapped_column(String(20))
    estimated_duration: Mapped[int | None] = mapped_column(Integer)

    # ── Section 2: Results & assessment ──
    success_criteria: Mapped[str | None] = mapped_column(Text)
    assessment_method: Mapped[str | None] = mapped_column(String(50))
    competencies: Mapped[list[str] | None] = mapped_column(JSONB)

    # ── Section 3: Methodological accents ──
    key_concepts: Mapped[list[dict[str, str]] | None] = mapped_column(
        JSONB, comment="JSONB array of concept strings"
    )
    common_mistakes: Mapped[list[str] | None] = mapped_column(JSONB)
    teaching_strategy: Mapped[str | None] = mapped_column(String(50))
    activities: Mapped[list[str] | None] = mapped_column(JSONB)

    # ── Section 4: Context & adaptivity ──
    teaching_style: Mapped[str | None] = mapped_column(String(50))
    deep_dive_references: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB)
    content_version: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # ── Section 5: Material references (Indexer agent) ──
    timecodes: Mapped[list[dict[str, Any]] | None] = mapped_column(
        JSONB, comment="JSONB of video timecode references"
    )
    slide_references: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB)
    web_references: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB)

    # ── Section 6: Semantic search (Embedding pipeline) ──
    # embedding: Mapped[...] — deferred until pgvector integration

    # ── Timestamps ──
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # Relationships
    snapshot: Mapped["StructureSnapshot"] = relationship(
        back_populates="structure_nodes"
    )
    parent: Mapped["StructureNode | None"] = relationship(
        back_populates="children",
        remote_side="StructureNode.id",
    )
    children: Mapped[list["StructureNode"]] = relationship(
        back_populates="parent",
        cascade="all, delete-orphan",
    )


class StructureNodeEditable(Base):
    """Mutable working copy of a course structure node.

    Linked to a ``CourseNode`` (not a snapshot) so it survives
    re-generation.  Auto-created from the latest ``StructureSnapshot``
    after each generation run.  Users edit fields here; the
    ``edited_fields`` JSONB list tracks which fields were changed
    manually vs. copied from the LLM baseline.
    """

    __tablename__ = "structure_nodes_editable"
    __table_args__ = {
        "comment": (
            "Mutable overlay for course structure nodes. "
            "Working copy that survives re-generation"
        ),
    }

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid7)
    materialnode_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("course_nodes.id", ondelete="CASCADE"),
        index=True,
        comment="FK to owning CourseNode (survives re-generation)",
    )
    source_snapshot_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("structure_snapshots.id", ondelete="SET NULL"),
        index=True,
        comment="Which snapshot this editable was initialised from",
    )
    source_structurenode_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("structure_nodes.id", ondelete="SET NULL"),
        index=True,
        comment="Original StructureNode this was copied from (for diff tracking)",
    )
    parent_editable_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("structure_nodes_editable.id", ondelete="CASCADE"),
        index=True,
        comment="Self-referential FK. NULL = top-level module",
    )
    node_type: Mapped[str] = mapped_column(
        String(30),
        index=True,
        comment="Enum: module, lesson, concept, exercise",
    )
    order: Mapped[int] = mapped_column(Integer, default=0)

    # ── Section 1: Formal & organisational ──
    title: Mapped[str] = mapped_column(String(500))
    description: Mapped[str | None] = mapped_column(Text)
    learning_goal: Mapped[str | None] = mapped_column(Text)
    expected_knowledge: Mapped[list[dict[str, str]] | None] = mapped_column(JSONB)
    expected_skills: Mapped[list[dict[str, str]] | None] = mapped_column(JSONB)
    prerequisites: Mapped[list[str] | None] = mapped_column(JSONB)
    difficulty: Mapped[str | None] = mapped_column(String(20))
    estimated_duration: Mapped[int | None] = mapped_column(Integer)

    # ── Section 2: Results & assessment ──
    success_criteria: Mapped[str | None] = mapped_column(Text)
    assessment_method: Mapped[str | None] = mapped_column(String(50))
    competencies: Mapped[list[str] | None] = mapped_column(JSONB)

    # ── Section 3: Methodological accents ──
    key_concepts: Mapped[list[dict[str, str]] | None] = mapped_column(JSONB)
    common_mistakes: Mapped[list[str] | None] = mapped_column(JSONB)
    teaching_strategy: Mapped[str | None] = mapped_column(String(50))
    activities: Mapped[list[str] | None] = mapped_column(JSONB)

    # ── Section 4: Context & adaptivity ──
    teaching_style: Mapped[str | None] = mapped_column(String(50))
    deep_dive_references: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB)
    content_version: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # ── Section 5: Material references ──
    timecodes: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB)
    slide_references: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB)
    web_references: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB)

    # ── Section 6: Methodist output (Layer 3) ──
    methodological_content: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB,
        comment="MethodistNodeOutput JSON (structured Layer 3)",
    )
    methodological_markdown: Mapped[str | None] = mapped_column(
        Text,
        comment="Rendered Markdown for author review",
    )
    methodist_call_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("external_service_calls.id", ondelete="SET NULL"),
        comment="FK to ExternalServiceCall that produced Methodist output",
    )

    # ── Edit tracking ──
    edited_fields: Mapped[list[str]] = mapped_column(
        JSONB,
        default=list,
        server_default="[]",
        comment="Field names manually edited by the user",
    )

    # ── Timestamps ──
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # Relationships
    material_node: Mapped["CourseNode"] = relationship(
        back_populates="editables",
        foreign_keys=[materialnode_id],
    )
    source_snapshot: Mapped["StructureSnapshot | None"] = relationship(
        foreign_keys=[source_snapshot_id],
    )
    methodist_call: Mapped["ExternalServiceCall | None"] = relationship(
        foreign_keys=[methodist_call_id],
    )
    parent: Mapped["StructureNodeEditable | None"] = relationship(
        back_populates="children",
        remote_side="StructureNodeEditable.id",
    )
    children: Mapped[list["StructureNodeEditable"]] = relationship(
        back_populates="parent",
        cascade="all, delete-orphan",
    )


class StructureSnapshot(Base):
    """Immutable snapshot of a generated course structure.

    Tied to a specific (node, fingerprint, mode) combination
    for idempotency: re-generating with the same inputs returns the
    existing snapshot instead of calling the LLM again.

    LLM metadata (model_id, tokens, cost) is stored in the linked
    ExternalServiceCall record — no duplication in the snapshot.

    ``materialnode_id`` references the root CourseNode (= course) for
    course-level snapshots, or a child node for subtree snapshots.
    """

    __tablename__ = "structure_snapshots"
    __table_args__ = (
        Index(
            "uq_snapshots_identity",
            "materialnode_id",
            "node_fingerprint",
            "mode",
            unique=True,
        ),
        {
            "comment": (
                "Immutable LLM response built from MaterialEntries.processed_content "
                "for a node or subtree. Used to populate StructureNodes which the "
                "course author can then edit"
            ),
        },
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid7)
    materialnode_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("course_nodes.id", ondelete="CASCADE"),
        index=True,
        comment="FK to target CourseNode (root = whole course)",
    )
    externalservicecall_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("external_service_calls.id", ondelete="SET NULL"), index=True
    )
    node_fingerprint: Mapped[str] = mapped_column(
        String(64),
        comment="Merkle hash at generation time for idempotency",
    )
    mode: Mapped[GenerationMode] = mapped_column(String(20))
    structure: Mapped[dict[str, Any]] = mapped_column(
        JSONB, comment="JSONB of generated course structure"
    )
    step_type: Mapped[str | None] = mapped_column(
        String(20),
        nullable=True,
        comment="Step type: generate, reconcile, refine",
    )
    summary: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="LLM-generated summary for cross-node context",
    )
    core_concepts: Mapped[list[str] | None] = mapped_column(
        JSONB,
        nullable=True,
        comment="Concepts covered in depth at this node",
    )
    mentioned_concepts: Mapped[list[str] | None] = mapped_column(
        JSONB,
        nullable=True,
        comment="Concepts mentioned but not covered in depth",
    )
    corrections: Mapped[list[dict[str, Any]] | None] = mapped_column(
        JSONB,
        nullable=True,
        comment="Reconciliation corrections audit trail",
    )
    summary_nested_nodes: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="LLM-generated compressed summary of all nested node snapshots",
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # Relationships
    node: Mapped["CourseNode"] = relationship(back_populates="snapshots")
    service_call: Mapped["ExternalServiceCall | None"] = relationship()
    structure_nodes: Mapped[list["StructureNode"]] = relationship(
        back_populates="snapshot", cascade="all, delete-orphan"
    )


# ──────────────────────────────────────────────
# Reconciliation Preview Cache
# ──────────────────────────────────────────────


class ReconciliationPreview(Base):
    """Cached reconciliation preview result with fingerprint-based idempotency.

    Stores LLM-generated issues for a given (materialnode_id,
    combined_fingerprint) pair. The combined fingerprint is derived
    from the Merkle node fingerprint and the editable tree hash,
    so re-running preview with identical inputs returns the cached
    result without an LLM call.
    """

    __tablename__ = "reconciliation_previews"
    __table_args__ = (
        Index(
            "uq_recon_preview_identity",
            "materialnode_id",
            "combined_fingerprint",
            unique=True,
        ),
        {
            "comment": (
                "Cached reconciliation preview results keyed by "
                "combined fingerprint (node materials + editable tree)"
            ),
        },
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid7)
    materialnode_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("course_nodes.id", ondelete="CASCADE"),
        index=True,
        comment="FK to CourseNode being reconciled",
    )
    combined_fingerprint: Mapped[str] = mapped_column(
        String(64),
        comment="SHA-256(node_fp + ':' + editable_hash) for idempotency",
    )
    node_fingerprint: Mapped[str] = mapped_column(
        String(64),
        comment="Merkle hash of materials at preview creation time",
    )
    editable_tree_hash: Mapped[str] = mapped_column(
        String(64),
        comment="SHA-256 of editable tree content at preview creation time",
    )
    issues: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB,
        comment="List of reconciliation issues from LLM",
    )
    context_summary: Mapped[str | None] = mapped_column(Text)
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("jobs.id", ondelete="SET NULL"),
        index=True,
        comment="FK to the Job that produced this preview",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # Relationships
    material_node: Mapped["CourseNode"] = relationship()
    job: Mapped["Job | None"] = relationship()


# ──────────────────────────────────────────────
# Job Tracking
# ──────────────────────────────────────────────


class Job(SoftDeleteMixin, Base):
    __tablename__ = "jobs"
    __table_args__ = (
        Index(
            "ix_jobs_active",
            "deleted_at",
            postgresql_where=text("deleted_at IS NULL"),
        ),
        {"comment": "Background task queue entries (ingestion, generation)"},
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid7)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("tenants.id", ondelete="SET NULL"), index=True
    )
    course_node_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("course_nodes.id", ondelete="SET NULL"),
        index=True,
        comment="FK to target CourseNode (legacy table name course_nodes "
        "until Phase 1.1 rename). NULL for orphaned jobs.",
    )
    job_type: Mapped[str] = mapped_column(String(50))
    priority: Mapped[str] = mapped_column(String(20), default="normal")
    status: Mapped[str] = mapped_column(
        String(20),
        default="queued",
        index=True,
        comment="Lifecycle: queued → active → complete/failed; "
        "also queued → cancelled, failed → queued (retry)",
    )
    arq_job_id: Mapped[str | None] = mapped_column(String(100))
    input_params: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, comment="JSONB of task-specific parameters"
    )
    current_stage: Mapped[str | None] = mapped_column(
        String(50),
        comment="Internal pipeline stage marker per vision §3 KD13. "
        "Free-form per job_type (e.g. pass_1/pass_2a/pass_2b/pass_2c "
        "for document_processing; bottomup/topdown for "
        "node_summary_regeneration; safety/sanity/review/delivery for "
        "homework_processing; unused for s3_cleanup). Validation lives "
        "at the worker level per pipeline.",
    )
    stage_progress: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB,
        comment="Per-job-type checkpoint state for resume after retry "
        "(KD4a in-flight resume + KD13 reactivate). Schema is "
        "per-pipeline and intentionally not enforced at the DB level.",
    )
    depends_on: Mapped[list[str] | None] = mapped_column(
        JSONB,
        comment="JSONB array of jobs.id UUIDs (as strings) "
        "that must complete before this job runs. "
        "DEPRECATED in v0.20 vision (KD13 — one Job = one logical "
        "action, internal stages via current_stage). To be dropped "
        "when methodist_orchestrator and generation_orchestrator are "
        "rewritten to single-Job-with-stages pattern in Phase 2.x.",
    )
    result_data: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, comment="JSONB result payload (e.g. reconciliation preview issues)"
    )
    error_message: Mapped[str | None] = mapped_column(Text)
    queued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Relationships
    tenant: Mapped["Tenant | None"] = relationship()
    authored_documents: Mapped[list["AuthoredDocument"]] = relationship(
        back_populates="pending_job"
    )


# ──────────────────────────────────────────────
# Observability
# ──────────────────────────────────────────────


class ExternalServiceCall(Base):
    __tablename__ = "external_service_calls"
    __table_args__ = {
        "comment": ("Audit log of all external API calls (LLM, transcription, etc.)"),
    }

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid7)
    job_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("jobs.id", ondelete="NO ACTION"),
        index=True,
        comment="KD5 — only mandatory FK. Tenant + course_node + any other "
        "context resolves through Job and its input_params. NO ACTION "
        "(default) protects against accidental hard-delete of Jobs that "
        "have attached ESCs; soft-delete preserves the FK.",
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
    job: Mapped["Job"] = relationship()


# ──────────────────────────────────────────────
# Homework Submissions
# ──────────────────────────────────────────────


class HomeworkStatus(StrEnum):
    """Homework submission lifecycle states."""

    RECEIVED = "received"
    SAFETY_CHECK = "safety_check"
    MATCHING = "matching"
    MATCHED = "matched"
    REVIEWING = "reviewing"
    COMPLETED = "completed"
    DELIVERED = "delivered"
    REJECTED = "rejected"
    FAILED = "failed"


class Student(SoftDeleteMixin, Base):
    __tablename__ = "students"
    __table_args__ = (
        Index("uq_student_tenant_external", "tenant_id", "external_id", unique=True),
        Index(
            "ix_students_active",
            "deleted_at",
            postgresql_where=text("deleted_at IS NULL"),
        ),
        {"comment": "External students submitting homework"},
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid7)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    external_id: Mapped[str] = mapped_column(
        String(200),
        comment="Student identifier from the caller's system",
    )
    metadata_: Mapped[dict[str, Any] | None] = mapped_column(
        "metadata",
        JSONB,
        comment="Arbitrary caller-provided metadata about the student",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    preferred_language: Mapped[str | None] = mapped_column(
        String(10),
        comment="ISO 639-1 language code from last review (e.g. uk, en)",
    )

    # Relationships
    tenant: Mapped["Tenant"] = relationship()
    submissions: Mapped[list["HomeworkSubmission"]] = relationship(
        # KD-alpha: stripped ``delete-orphan``. HomeworkSubmission soft-
        # deletion routes through ``CascadeDeleteService`` per
        # ``Student.__cascades_soft_delete_to__ = [HomeworkSubmission]``.
        back_populates="student",
        cascade="save-update, merge",
    )

    def __repr__(self) -> str:
        return (
            f"Student(id={self.id!r}, tenant_id={self.tenant_id!r}, "
            f"external_id={self.external_id!r})"
        )


class HomeworkSubmission(SoftDeleteMixin, Base):
    __tablename__ = "homework_submissions"
    __table_args__ = (
        Index(
            "ix_homework_submissions_active",
            "deleted_at",
            postgresql_where=text("deleted_at IS NULL"),
        ),
        {"comment": "Homework submissions from external systems for Mentor review"},
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid7)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    student_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("students.id", ondelete="CASCADE"), index=True
    )
    course_node_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("course_nodes.id", ondelete="CASCADE"),
        index=True,
        comment="Root CourseNode representing the course",
    )
    node_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("course_nodes.id", ondelete="CASCADE"),
        index=True,
        comment="Specific course node the submission targets",
    )
    task_hint_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        comment="Optional task ID hint from the caller (not FK, validated in app)",
    )
    matched_task_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("structure_nodes_editable.id", ondelete="SET NULL"),
        index=True,
        comment="Matched editable node after task identification",
    )

    # File
    file_url: Mapped[str] = mapped_column(
        String(2000), comment="S3/B2 path to the uploaded file"
    )
    file_type: Mapped[str] = mapped_column(
        String(50), comment="MIME type of the uploaded file"
    )
    original_filename: Mapped[str | None] = mapped_column(String(500))
    file_hash: Mapped[str | None] = mapped_column(
        String(64),
        index=True,
        comment="SHA-256 hex digest for deduplication",
    )

    # Status
    status: Mapped[str] = mapped_column(
        String(30),
        default="received",
        index=True,
        comment="Lifecycle: received → safety_check → matching → matched "
        "→ reviewing → completed → delivered | rejected | failed",
    )

    # Results (JSONB)
    safety_result: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, comment="Safety check result (safe, reason, flags)"
    )
    match_result: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB,
        comment="Task matching result (matched_node_id, task_type, confidence)",
    )
    review_result: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB,
        comment="Mentor review result (score, passed, feedback_sections)",
    )
    review_markdown: Mapped[str | None] = mapped_column(
        Text, comment="Rendered Markdown of the Mentor review"
    )

    # Webhook
    webhook_url: Mapped[str | None] = mapped_column(
        String(2000),
        comment="Per-submission webhook URL override (falls back to tenant default)",
    )
    webhook_delivered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )

    # Language
    response_language: Mapped[str | None] = mapped_column(
        String(10),
        comment="ISO 639-1 language used for the review response",
    )

    # Error & tracking
    error_message: Mapped[str | None] = mapped_column(Text)
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("jobs.id", ondelete="SET NULL"), index=True
    )

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # Relationships
    tenant: Mapped["Tenant"] = relationship()
    student: Mapped["Student"] = relationship(back_populates="submissions")
    course_node: Mapped["CourseNode"] = relationship(foreign_keys=[course_node_id])
    node: Mapped["CourseNode"] = relationship(foreign_keys=[node_id])
    matched_task: Mapped["StructureNodeEditable | None"] = relationship()
    job: Mapped["Job | None"] = relationship()

    def __repr__(self) -> str:
        return (
            f"HomeworkSubmission(id={self.id!r}, status={self.status!r}, "
            f"student_id={self.student_id!r})"
        )


# ──────────────────────────────────────────────
# Cascade soft-delete topology (vision §3 KD3 + KD12)
# ──────────────────────────────────────────────
# Each list enumerates direct cascade descendants whose ``deleted_at``
# must be set when the parent is soft-deleted. ``build_cascade_map``
# (storage/cascade.py) walks transitive closure from these declarations.
# Wiring per Phase 1 PHASE.md §1.2 cascade declaration audit. Self-
# reference on CourseNode supports recursive course-tree traversal.
Tenant.__cascades_soft_delete_to__ = [APIKey, CourseNode, Student, HomeworkSubmission]
CourseNode.__cascades_soft_delete_to__ = [CourseNode, AuthoredDocument]
AuthoredDocument.__cascades_soft_delete_to__ = [DocumentSummary]
DocumentSummary.__cascades_soft_delete_to__ = [DocumentSegment]
Student.__cascades_soft_delete_to__ = [HomeworkSubmission]


# ──────────────────────────────────────────────
# Per-class scrub callables (vision §3 KD3 — models-fix-3)
# ──────────────────────────────────────────────
# CascadeDeleteService dispatches these per-victim during BFS so
# every entity in the cascade clears its KD3 content fields before
# the ``deleted_at`` write — see :data:`ScrubCallable` for the
# dispatch semantics. Tenant deliberately has no class-level
# declaration: KD-β ``webhook_url`` scrub is route-specific (per-
# call ``scrub_callable=`` on the Tenant-rooted cascade) rather than
# a class invariant. DocumentSummary + DocumentSegment scrub
# callables deferred to Phase 2.x first pipeline writes — Phase 1
# tables stay empty, cascade traversal through them is no-op.
CourseNode.__scrub_callable__ = scrub_course_node
AuthoredDocument.__scrub_callable__ = scrub_authored_document
