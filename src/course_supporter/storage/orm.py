"""SQLAlchemy ORM models for all project entities."""

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any, ClassVar

import uuid_utils as uuid7_lib
from sqlalchemy import (
    Boolean,
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
    scrub_node_summary_final,
    scrub_node_summary_final_previous_snapshot,
    scrub_node_summary_raw,
)


def _uuid7() -> uuid.UUID:
    """Generate a UUIDv7 (time-ordered) for use as default PK value."""
    return uuid.UUID(bytes=uuid7_lib.uuid7().bytes)


class Base(DeclarativeBase):
    """Base class for all ORM models."""

    # ``eager_defaults=True`` makes SQLAlchemy populate server-generated
    # column values (``server_default``, ``onupdate``) on the in-memory
    # ORM instance at flush time via ``RETURNING`` — required for
    # async + Pydantic ``from_attributes`` serialization. Without it,
    # post-commit ``model_validate(orm_instance)`` triggers a sync
    # lazy-load on expired server-default columns (e.g. ``updated_at``)
    # outside the greenlet context → ``MissingGreenlet``.
    __mapper_args__ = {"eager_defaults": True}


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
    # Python type is ``str | None`` for compatibility with the
    # ``HashableEntity`` Protocol invariance (the Protocol declares
    # ``content_hash: str | None`` and all sibling hash-bearing models
    # use the same nullable Python type). DB layer is ``NOT NULL +
    # server_default`` so a runtime NULL is impossible — Phase 3.1 Q-G.
    content_hash: Mapped[str | None] = mapped_column(
        String(64),
        nullable=False,
        server_default=text(
            "'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855'"
        ),
        comment="Materialised content_hash (vision §3 KD9, SHA-256 hex). "
        "server_default = EMPTY_NODE_CONTENT_HASH (= compute_content_hash(b'', [])). "
        "Phase 3.1 commit 4 fixed the KD9 NULL-at-INSERT regression: "
        "empty CourseNodes now carry a defined empty-hash, not NULL. "
        "Recomputed at INSERT/UPDATE via ContentHashService.invalidate_up.",
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
            "audio",
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

    # ── Stage 2 safety verdict (KD-2.1-P) ──
    safety_result: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB,
        nullable=True,
        comment="Stage 2 LLM safety verdict persisted regardless of "
        "is_safe outcome. JSON shape per security.schemas.SafetyResult "
        "(source='stage2'; is_safe, violations, confidence, reasoning). "
        "NULL when Stage 2 hasn't run yet (legacy rows pre-Phase-2.1 C6).",
    )

    # ── Derived state ──

    @property
    def state(self) -> MaterialState:
        """Derive current state. Phase 2.x will refine after Pass 2 pipeline.

        Priority: ERROR > PENDING > READY. RAW / INTEGRITY_BROKEN states are
        deferred to Phase 2.x semantics after Pass 2 pipeline introduction.
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


class DocumentSummary(SoftDeleteMixin, Base):
    """1:1 summary of an AuthoredDocument (vision §1.3).

    One ACTIVE row per AuthoredDocument (PARTIAL unique on
    authored_document_id WHERE deleted_at IS NULL, KD-theta.2). Produced by
    Pass 2a of the ingestion pipeline — LLM call for
    video/audio/presentation or deterministic ladder for text/web.
    Regeneration soft-deletes the prior active summary (cascading its
    segments) and inserts a fresh one; soft-deleted history coexists with
    the single active row (Task 3.2.6 Finding 2).
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
        # Active-only 1:1 invariant (Task 3.2.6): at most one summary per
        # AuthoredDocument among non-soft-deleted rows. Replaces the former
        # full column-level unique so reprocess (soft-delete old + insert
        # new) does not collide. Distinct from ix_document_summaries_active
        # (that one is a non-unique deleted_at index).
        Index(
            "uq_document_summaries_authored_document_id_active",
            "authored_document_id",
            unique=True,
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
        index=True,
        comment="FK to parent AuthoredDocument (active-only 1:1 invariant per "
        "KD-theta.2 — enforced by partial unique index "
        "uq_document_summaries_authored_document_id_active)",
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
    title: Mapped[str | None] = mapped_column(
        String(128),
        comment="Optional short heading (Pass 2a LLM emits per segment for "
        "text/web; NULL for audio/video where title is synthesised later). "
        "NOT in content_hash formula -- DD-2.1-W defers to Phase 1.3/1.4.",
    )
    description: Mapped[str | None] = mapped_column(
        String(512),
        comment="1-2 sentence segment description (Pass 2a LLM emits per "
        "segment; populated for text/web at C7, NULL for media pre-Pass-2c). "
        "NOT in content_hash formula -- DD-2.1-W defers to Phase 1.3/1.4.",
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
    visual_content: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'[]'::jsonb"),
        comment="Time-anchored visual descriptions for video segments (task "
        "2.4.6): JSONB array of VisualSceneRef dicts "
        "(position_ms / description / kind / scene_id), kept in temporal "
        "(frame) order. Empty [] for non-video and visual-less segments. "
        "Mirrors the main_concepts JSONB+server_default mechanics; included "
        "in content_hash (KD-2.1-F) only when non-empty (task 2.4.6 D1).",
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
        "Free-form per job_type. "
        "Generic guidance per job_type (legacy taxonomy, retained as "
        "reference per Phase 2.1 D7): "
        "pass_1/pass_2a/pass_2b/pass_2c for document_processing; "
        "bottomup/topdown for node_summary_regeneration; "
        "safety/sanity/review/delivery for homework_processing; "
        "unused for s3_cleanup. "
        "Text/web ingestion taxonomy per KD-2.1-B (Phase 2.1): "
        "'extracting_structure' (Pass 2a premium LLM mapping → "
        "DocumentSummary; ESC.action 'pass_2a_mapping'), "
        "'checking_safety' (Stage 2 LLM safety check; ESC.action "
        "'safety_check'), 'creating_segments' (Pass 2b algorithmic "
        "slice → DocumentSegments; no ESC entry — no LLM call). "
        "Validation lives at the worker level per pipeline.",
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
        "when remaining multi-job orchestration is rewritten to the "
        "single-Job-with-stages pattern in Phase 2.x.",
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
        Uuid,
        index=True,
        comment=(
            "Orphan column post-C9.3 — FK to structure_nodes_editable "
            "dropped together with the table in C9.4. Writer is the "
            "HW-005 task-matching block (deleted in C9.3); reader is "
            "the webhook payload (always None post-C9.3). Column "
            "retained per DD-2.1-AG for Phase 4 reroute on "
            "NodeSummaryFinal; drop_column deferred to natural "
            "cleanup cycle."
        ),
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
    job: Mapped["Job | None"] = relationship()

    def __repr__(self) -> str:
        return (
            f"HomeworkSubmission(id={self.id!r}, status={self.status!r}, "
            f"student_id={self.student_id!r})"
        )


# ──────────────────────────────────────────────
# Methodist layer — NodeSummary models (vision §2.2, KD6, KD9, KD10, KD11, KD12)
# ──────────────────────────────────────────────


class NodeSummaryRaw(SoftDeleteMixin, Base):
    """System-generated methodist summary of a CourseNode (vision §3 KD10).

    1:1 with CourseNode (UNIQUE on course_node_id). Produced by the
    two-pass methodist pipeline: bottom-up generates all canonical
    fields + compressed_summary; top-down generates enclosing_context
    + appends methodist_observations. Per vision rule "є вузол, є Raw"
    (v0.20.x) a Raw row exists for every CourseNode, including empty
    leaves — emptiness is null-valued content fields, NOT row absence.

    Per KD9 the ``content_hash`` covers all content fields EXCEPT
    ``enclosing_context`` (which depends on ancestors, not on own
    content). Per probe-resolved Phase 3 pre-review the second axis
    of memoization for top-down — ``enclosing_context_source_hash``
    (hash of parent enclosing_context) — lives on Raw only; the
    walker service that maintains it lands in Phase 3.2.
    """

    __tablename__ = "node_summaries_raw"
    __table_args__ = (
        Index(
            "ix_node_summaries_raw_active",
            "deleted_at",
            postgresql_where=text("deleted_at IS NULL"),
        ),
        {
            "comment": (
                "System-generated methodist summary (vision §3 KD10) — "
                "1:1 with CourseNode; rule «є вузол, є Raw» (v0.20.x)"
            ),
        },
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid7)
    course_node_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("course_nodes.id", ondelete="CASCADE"),
        unique=True,
        index=True,
        comment="FK to CourseNode (UNIQUE — 1:1 invariant per KD10 «є вузол, є Raw»)",
    )

    # ─── Identity (KD3 scrub targets — str) ──────
    title: Mapped[str | None] = mapped_column(String(500))
    description: Mapped[str | None] = mapped_column(Text)

    # ─── Learning outcomes ───────────────────────
    learning_objectives: Mapped[list[str]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'[]'::jsonb"),
        comment="Bloom-taxonomy outcome statements (3-7; overflow → observations)",
    )
    knowledge: Mapped[list[dict[str, str]]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'[]'::jsonb"),
        comment="LearningOutcomeItem[]: {name, description} — what student WILL KNOW",
    )
    skills: Mapped[list[dict[str, str]]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'[]'::jsonb"),
        comment="LearningOutcomeItem[]: {name, description} — what student WILL DO",
    )

    # ─── Assessment ──────────────────────────────
    success_criteria: Mapped[list[str]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'[]'::jsonb"),
    )
    assessment_approach: Mapped[str | None] = mapped_column(Text)

    # ─── Methodology ─────────────────────────────
    teaching_approach: Mapped[str | None] = mapped_column(Text)
    key_activities: Mapped[list[str]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'[]'::jsonb"),
    )
    common_mistakes: Mapped[list[str]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'[]'::jsonb"),
        comment="EXPLICIT-only (per KD10) — items mentioned in source documents",
    )

    # ─── Concepts (navigation) ───────────────────
    main_concepts: Mapped[list[str]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'[]'::jsonb"),
    )
    secondary_concepts: Mapped[list[str]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'[]'::jsonb"),
    )

    # ─── Cross-level context ─────────────────────
    compressed_summary: Mapped[str | None] = mapped_column(
        Text,
        comment="1-2k tokens, KD10 bottom-up output — passed to parent on next walk",
    )
    enclosing_context: Mapped[str | None] = mapped_column(
        Text,
        comment="1-2k tokens, KD10 top-down output — NOT included in content_hash "
        "(depends on ancestors, not own content); NULL on root (top-down skipped)",
    )

    # ─── Raw-only ────────────────────────────────
    methodist_observations: Mapped[list[str]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'[]'::jsonb"),
        comment="Methodist annotations from both passes; Raw-only (not in Final)",
    )

    # ─── Size metrics ────────────────────────────
    own_documents_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    own_chars_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    cumulative_documents_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    cumulative_chars_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )

    # ─── Hash axes ───────────────────────────────
    # Python type ``str | None`` for HashableEntity Protocol compat;
    # DB-level NOT NULL + server_default makes runtime NULL impossible.
    content_hash: Mapped[str | None] = mapped_column(
        String(64),
        nullable=False,
        server_default=text(
            "'bcaf467defc6a11d9f437368b2388f310b8538fc2fe21621587f69175766cadd'"
        ),
        comment="Materialised content_hash (vision §3 KD9, SHA-256 hex) — "
        "SHA256 over all content fields EXCEPT enclosing_context. "
        "server_default = EMPTY_NODE_SUMMARY_RAW_CONTENT_HASH (rule "
        "«визначений хеш, ніколи NULL» — Phase 3.1 Q-G).",
    )
    enclosing_context_source_hash: Mapped[str | None] = mapped_column(
        String(64),
        comment="Top-down memoization key (KD10) = hash of parent's "
        "enclosing_context. Raw-only — Final does not carry this key. "
        "NULL until first top-down walk populates it (walker — Phase 3.2).",
    )
    source_content_hash: Mapped[str | None] = mapped_column(
        String(64),
        comment="Bottom-up memoization key (KD10 line 1011) = "
        "CourseNode.content_hash at the time Pass 1 last completed. "
        "Distinct from self ``content_hash`` (which hashes own "
        "generated fields per KD9 line 729). NULL until first Pass 1 "
        "lands; written by the Phase 3.2.2 orchestrator after the LLM "
        "hook commits per-node.",
    )

    # ─── Timestamps ──────────────────────────────
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class NodeSummaryFinal(SoftDeleteMixin, Base):
    """Editable canonical methodist summary for downstream agents (vision §3 KD11).

    1:1 with CourseNode (UNIQUE on course_node_id). Created as a
    bitwise copy of NodeSummaryRaw immediately after Raw generation;
    edited by the author thereafter. Three write channels (KD11):
    (1) automatic overwrite on Raw regeneration (resets approved_at),
    (2) author manual edit, (3) top-down direct refresh of
    ``enclosing_context`` + ``enclosing_context_updated_at`` WITHOUT
    resetting approved_at (third channel — KD11 explicit exception).

    Per KD11 ``enclosing_context`` is read-only on Final (not author-
    editable); ``main_concepts`` / ``secondary_concepts`` are read-only
    copies of Raw (concept-based navigation invariant). ``compressed_summary``
    is NOT stored on Final — downstream gets the full fields, so
    duplicating the bottom-up bridge would be redundant.

    Empty-leaf author flow (Phase 3.1 MVP — operator-ratified question 6):
    ``is_manual`` + ``manual_description`` allow the author to
    populate an empty leaf manually as a future generalisation
    point. ``methodist_observations`` is Raw-only and intentionally
    absent here.
    """

    __tablename__ = "node_summaries_final"
    __table_args__ = (
        Index(
            "ix_node_summaries_final_active",
            "deleted_at",
            postgresql_where=text("deleted_at IS NULL"),
        ),
        {
            "comment": (
                "Editable canonical methodist summary (vision §3 KD11) — "
                "1:1 with CourseNode; downstream source of truth"
            ),
        },
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid7)
    course_node_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("course_nodes.id", ondelete="CASCADE"),
        unique=True,
        index=True,
        comment="FK to CourseNode (UNIQUE — 1:1 invariant per KD11)",
    )

    # ─── Editable by author (11 content fields) ──
    title: Mapped[str | None] = mapped_column(String(500))
    description: Mapped[str | None] = mapped_column(Text)
    learning_objectives: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    knowledge: Mapped[list[dict[str, str]]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    skills: Mapped[list[dict[str, str]]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    success_criteria: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    assessment_approach: Mapped[str | None] = mapped_column(Text)
    teaching_approach: Mapped[str | None] = mapped_column(Text)
    key_activities: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    common_mistakes: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )

    # ─── Read-only — copied from Raw (KD11) ──────
    main_concepts: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    secondary_concepts: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    enclosing_context: Mapped[str | None] = mapped_column(
        Text,
        comment="Read-only on Final (KD11). Refreshed via third channel "
        "(top-down direct write) WITHOUT resetting approved_at. "
        "NULL on root (top-down skipped). NOT included in content_hash.",
    )

    # ─── Empty-leaf author flow (probe-resolved question 6, MVP 3.1) ─
    is_manual: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=text("false"),
        comment="True when Final was author-created/edited on an empty "
        "leaf rather than copied from a Raw with content.",
    )
    manual_description: Mapped[str | None] = mapped_column(
        Text,
        comment="Author-provided description for empty-leaf author-flow.",
    )

    # ─── Size metrics ────────────────────────────
    own_documents_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    own_chars_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    cumulative_documents_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    cumulative_chars_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )

    # ─── Hash axis ───────────────────────────────
    # Python type ``str | None`` for HashableEntity Protocol compat;
    # DB-level NOT NULL + server_default makes runtime NULL impossible.
    content_hash: Mapped[str | None] = mapped_column(
        String(64),
        nullable=False,
        server_default=text(
            "'85ef8d5f3881ffae484d1c1b47d18c0984b19c03bc9dd44e6fc1010e9fcdbf03'"
        ),
        comment="Materialised content_hash (vision §3 KD9, SHA-256 hex) — "
        "SHA256 over all content fields EXCEPT enclosing_context. "
        "server_default = EMPTY_NODE_SUMMARY_FINAL_CONTENT_HASH (rule "
        "«визначений хеш, ніколи NULL» — Phase 3.1 Q-G).",
    )

    # ─── Approval pair (read as two dates per KD11) ──────────
    approved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        comment="NULL after automatic Raw-overwrite; set by explicit author "
        "approve. Top-down third channel refresh does NOT reset this.",
    )
    enclosing_context_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        comment="When top-down last refreshed enclosing_context. Paired "
        "with approved_at — content can be approved BEFORE the latest "
        "context refresh. NULL on root (top-down skipped) and until "
        "the first top-down walk lands.",
    )

    # ─── Timestamps ──────────────────────────────
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class NodeSummaryFinalPreviousSnapshot(SoftDeleteMixin, Base):
    """Single-version snapshot of NodeSummaryFinal before last overwrite (KD11).

    1:1 with NodeSummaryFinal (UNIQUE on node_summary_final_id) — at
    most ONE prior version is kept, not a history. Lifecycle (KD11):

    - Created on the FIRST automatic overwrite (Raw regeneration).
    - REPLACED in-place on every subsequent overwrite.
    - HARD-DELETED on explicit author approve (KD12 Path 3 — the
      prior version is irrelevant once the current state is approved).
    - Cascade soft-deleted with scrub on CourseNode soft-delete
      (KD3 + KD12 — operator-ratified Q-B (a) at 3.1 pre-flight:
      both soft-delete-with-scrub and hard-delete-on-approve are
      legitimate, non-conflicting paths).
    """

    __tablename__ = "node_summaries_final_previous_snapshots"
    __table_args__ = (
        Index(
            "ix_node_summaries_final_previous_snapshots_active",
            "deleted_at",
            postgresql_where=text("deleted_at IS NULL"),
        ),
        {
            "comment": (
                "Single prior version of NodeSummaryFinal (vision §3 KD11) — "
                "1:1; replaced on overwrite; hard-deleted on approve"
            ),
        },
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid7)
    node_summary_final_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("node_summaries_final.id", ondelete="CASCADE"),
        unique=True,
        index=True,
        comment="FK to NodeSummaryFinal (UNIQUE — single-version invariant)",
    )
    snapshot: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
        comment="Bitwise copy of prior NodeSummaryFinal content fields.",
    )
    replaced_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        comment="When the prior version was overwritten by automatic Raw regen.",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
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
CourseNode.__cascades_soft_delete_to__ = [
    CourseNode,
    AuthoredDocument,
    NodeSummaryRaw,
    NodeSummaryFinal,
]
AuthoredDocument.__cascades_soft_delete_to__ = [DocumentSummary]
DocumentSummary.__cascades_soft_delete_to__ = [DocumentSegment]
# Phase 3.1 Q-C ratify (Option A — two-level): PreviousSnapshot cascades
# from its parent NodeSummaryFinal, mirroring the AuthoredDocument →
# DocumentSummary → DocumentSegment two-level pattern. This preserves
# the 1:1 Final↔PreviousSnapshot invariant on the cascade path.
NodeSummaryFinal.__cascades_soft_delete_to__ = [NodeSummaryFinalPreviousSnapshot]
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
NodeSummaryRaw.__scrub_callable__ = scrub_node_summary_raw
NodeSummaryFinal.__scrub_callable__ = scrub_node_summary_final
NodeSummaryFinalPreviousSnapshot.__scrub_callable__ = (
    scrub_node_summary_final_previous_snapshot
)
