"""Request/response schemas for the API layer."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from course_supporter.language import (
    InvalidLanguageError,
    LanguageEntry,
    LanguageNotAllowedError,
    normalize_and_validate,
)
from course_supporter.models.source import AssignmentType, MaterialRole, SourceType
from course_supporter.normalizer import Manifest
from course_supporter.storage.course_node_repository import SummaryStatus

# --- Material Tree Nodes ---


def _validate_language(raw: str) -> str:
    """Pydantic-compatible wrapper around ``normalize_and_validate``.

    Turns the helper's typed exceptions into ``ValueError`` so FastAPI
    renders them as 422 with the helper's message.
    """
    try:
        return normalize_and_validate(raw)
    except (InvalidLanguageError, LanguageNotAllowedError) as exc:
        raise ValueError(str(exc)) from exc


class RootNodeCreateRequest(BaseModel):
    """Request body for creating a root node (= course).

    ``default_language`` is **required** for root nodes (Task 2.4.13:
    course language is the entry-gate for Pass 2a language pinning).
    Accepts any standard language description — 639-1 (``"uk"``), 639-3
    (``"ukr"``), or English name (``"Ukrainian"``) — and stores it as
    canonical 639-3 after validation against ``config/languages.yaml``.

    Example::

        {
            "title": "Python for Beginners",
            "description": "Foundational Python course",
            "default_language": "uk"
        }
    """

    title: str = Field(
        ...,
        min_length=1,
        max_length=500,
        description="Node title displayed in the material tree.",
        examples=["Python for Beginners"],
    )
    description: str | None = Field(
        default=None,
        max_length=5000,
        description="Optional detailed description of the node's purpose.",
        examples=["Foundational Python course."],
    )
    default_language: str = Field(
        ...,
        description=(
            "Required language of instruction. Accepts ISO 639-1 / 639-3 / "
            "English name; stored as canonical ISO 639-3. Must be in the "
            "whitelist served by ``GET /api/v1/config/languages``."
        ),
        examples=["uk", "ukr", "Ukrainian"],
    )

    @field_validator("default_language")
    @classmethod
    def _check_language(cls, raw: str) -> str:
        return _validate_language(raw)


class ChildNodeCreateRequest(BaseModel):
    """Request body for creating a non-root (child) node.

    ``default_language`` is optional on children — language inheritance
    is resolved at ingestion time against the root, and any value set on
    a child is currently unused by the pipeline (KD-2.4-13 §1). Kept
    here for forward-compat in case cascade overrides land later.

    Example::

        {"title": "Module 1: Intro"}
    """

    title: str = Field(
        ...,
        min_length=1,
        max_length=500,
        description="Node title displayed in the material tree.",
        examples=["Module 1: Introduction"],
    )
    description: str | None = Field(
        default=None,
        max_length=5000,
        description="Optional detailed description of the node's purpose.",
        examples=["Overview of the foundational concepts covered in this module."],
    )
    default_language: str | None = Field(
        default=None,
        description=(
            "Optional language override for this subtree. Stored as canonical "
            "ISO 639-3 when set. Pipeline currently reads only the root "
            "language; child overrides are forward-compat metadata."
        ),
        examples=["uk", "ukr"],
    )

    @field_validator("default_language")
    @classmethod
    def _check_language(cls, raw: str | None) -> str | None:
        if raw is None:
            return None
        return _validate_language(raw)


class NodeUpdateRequest(BaseModel):
    """Request body for updating a material tree node.

    All fields are optional — only provided fields are updated.
    To clear the description, send ``"description": null`` explicitly.

    ``default_language`` membership against ``config/languages.yaml`` is
    enforced here when present; the route additionally rejects
    set-to-null on a *root* node (HTTP 422) so the CHECK constraint
    ``course_nodes_root_language_required`` never has to fire as a 500.

    Example::

        {"title": "Updated Title"}
    """

    title: str | None = Field(
        default=None,
        min_length=1,
        max_length=500,
        description="New title for the node. Omit to keep unchanged.",
        examples=["Updated Module Title"],
    )
    description: str | None = Field(
        default=None,
        max_length=5000,
        description=(
            "New description. Send ``null`` to clear, omit to keep unchanged. "
            "Note: distinguishing 'omit' from ``null`` requires checking "
            "``model_fields_set``."
        ),
    )
    default_language: str | None = Field(
        default=None,
        description=(
            "New default language for this subtree (any standard form; stored "
            "as canonical ISO 639-3). Send ``null`` to clear (children only — "
            "root rejects null with 422). Setting it is a pure DB write; does "
            "NOT trigger re-ingestion of existing materials."
        ),
        examples=["uk", "ukr"],
    )

    @field_validator("default_language")
    @classmethod
    def _check_language(cls, raw: str | None) -> str | None:
        if raw is None:
            return None
        return _validate_language(raw)


class AllowedLanguagesResponse(BaseModel):
    """Response shape for ``GET /api/v1/config/languages``.

    Returns the project-wide course-language whitelist (canonical ISO 639-3
    codes enriched with English names; native names when ``iso639`` carries
    them). UI consumes this as the single source of truth for the language
    selector — no hardcoded list on the client.
    """

    items: list[LanguageEntry] = Field(
        description="Allowed languages — canonical 639-3 + display names."
    )
    total: int = Field(
        description="Convenience count of ``items`` (always equals ``len(items)``)."
    )


class NodeMoveRequest(BaseModel):
    """Request body for moving a node within the tree.

    Move a node to a new parent (or to root by setting ``parent_id``
    to ``null``). Cycle detection is enforced server-side.

    Example::

        {"parent_id": "019c707f-73b8-7b53-ba02-0e7be1c89189"}
    """

    parent_id: uuid.UUID | None = Field(
        ...,
        description=(
            "Target parent node ID. Set to ``null`` to move the node to the tree root."
        ),
    )


class NodeReorderRequest(BaseModel):
    """Request body for reordering a node among its siblings.

    Siblings are automatically renumbered (0-based) after the operation.

    Example::

        {"order": 2}
    """

    order: int = Field(
        ...,
        ge=0,
        description=(
            "Desired 0-based position among siblings. "
            "Values exceeding the maximum are clamped automatically."
        ),
        examples=[0, 2],
    )


class NodeResponse(BaseModel):
    """Response schema for a single material tree node.

    Returned by create, update, move, and reorder operations.
    Does not include nested children — use ``NodeTreeResponse``
    for the full tree.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID = Field(description="Unique node identifier (UUIDv7).")
    tenant_id: uuid.UUID = Field(description="Tenant this node belongs to.")
    parent_id: uuid.UUID | None = Field(
        default=None,
        description="Parent node ID, or ``null`` for root nodes.",
        validation_alias="parent_id",
    )
    title: str = Field(description="Node title.")
    description: str | None = Field(description="Optional node description.")
    default_language: str | None = Field(
        default=None,
        description=(
            "Canonical ISO 639-3 language for materials under this subtree. "
            "Root nodes (``parent_id`` is null) are guaranteed non-null by "
            "the ``course_nodes_root_language_required`` CHECK constraint; "
            "child nodes may be null (inheritance is resolved at ingestion "
            "from the root, not stored)."
        ),
    )
    order: int = Field(description="0-based position among siblings.")
    content_hash: str | None = Field(
        description="Merkle hash of this node's content. ``null`` if not computed."
    )
    children_count: int = Field(
        default=0,
        description="Number of direct child nodes.",
    )
    authored_documents_count: int = Field(
        default=0,
        description="Number of authored documents attached to this node.",
    )
    created_at: datetime = Field(description="When this node was created.")
    updated_at: datetime = Field(description="When this node was last modified.")


class NodeTreeResponse(BaseModel):
    """Recursive tree node with nested children.

    Returned by ``GET /nodes/{node_id}/tree``. Each node
    contains its children, forming a full tree structure.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID = Field(description="Unique node identifier (UUIDv7).")
    tenant_id: uuid.UUID = Field(description="Tenant this node belongs to.")
    parent_id: uuid.UUID | None = Field(
        description="Parent node ID, or ``null`` for root nodes."
    )
    title: str = Field(description="Node title.")
    description: str | None = Field(description="Optional node description.")
    order: int = Field(description="0-based position among siblings.")
    content_hash: str | None = Field(
        description="Merkle hash of this node's content. ``null`` if not computed."
    )
    children: list[NodeTreeResponse] = Field(
        default_factory=list,
        description="Child nodes, recursively nested. Empty list for leaf nodes.",
    )
    created_at: datetime = Field(description="When this node was created.")
    updated_at: datetime = Field(description="When this node was last modified.")


class NodeListResponse(BaseModel):
    """Paginated list of root nodes (courses).

    Root nodes (parent_id IS NULL) serve as top-level entities.
    """

    items: list[NodeResponse] = Field(description="Root nodes for the current page.")
    total: int = Field(description="Total number of root nodes (across all pages).")
    limit: int = Field(description="Maximum items per page (as requested).")
    offset: int = Field(description="Number of items skipped (as requested).")


# --- Authored Documents ---


class AuthoredDocumentSummaryResponse(BaseModel):
    """Compact authored document within the tree detail.

    A lighter version of ``AuthoredDocumentResponse`` omitting
    ``job_id`` and ``updated_at`` to keep the tree
    payload concise. Includes the derived ``state`` field.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID = Field(description="Unique entry identifier (UUIDv7).")
    source_type: str = Field(
        description="Material type: ``video``, ``presentation``, ``text``, or ``web``."
    )
    material_role: str = Field(
        description="Role: ``educational`` or ``methodological``."
    )
    task_type: AssignmentType | None = Field(
        default=None,
        description=(
            "Assignment type if this material is a concrete task. "
            "``null`` for regular (non-task) materials."
        ),
    )
    source_url: str = Field(description="URL or S3 path to the raw material.")
    filename: str | None = Field(description="Original filename, if available.")
    order: int = Field(description="0-based position among sibling materials.")
    state: str = Field(
        description=("Derived lifecycle state: ``pending``, ``ready``, or ``error``."),
    )
    processing_phase: str = Field(
        description=(
            "External processing phase (L3, root cause #2): ``queued`` "
            "(standing in the queue, no worker has taken it), ``processing`` "
            "(a worker is processing it), ``awaiting_author`` (№21: prep "
            "produced a file-role proposal the author has not yet confirmed), "
            "``ready``, or ``error``. Splits the in-flight ``pending`` state "
            "into ``queued`` vs ``processing``; the terminal values "
            "(``ready`` / ``error``) mirror ``state`` verbatim. Never null/empty."
        ),
    )
    error_message: str | None = Field(
        description="Error from the last failed processing attempt, if any."
    )
    error_category: str | None = Field(
        default=None,
        description="Structural async-error code (F4), if categorised.",
    )
    created_at: datetime = Field(description="When this entry was created.")


class NodeWithDocumentsResponse(BaseModel):
    """Recursive tree node with attached materials.

    Used in tree detail to provide the full hierarchical view
    including materials at each level.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID = Field(description="Unique node identifier (UUIDv7).")
    title: str = Field(description="Node title.")
    description: str | None = Field(description="Optional node description.")
    default_language: str | None = Field(
        default=None,
        description=(
            "Canonical ISO 639-3 language for materials under this subtree. "
            "Root nodes (``parent_id`` is null) are guaranteed non-null by "
            "the ``course_nodes_root_language_required`` CHECK constraint; "
            "child nodes may be null (inheritance is resolved at ingestion "
            "from the root, not stored)."
        ),
    )
    order: int = Field(description="0-based position among siblings.")
    content_hash: str | None = Field(
        description="Merkle hash of this node's content. ``null`` if not computed."
    )
    summary_status: SummaryStatus = Field(
        default="none",
        description=(
            "Methodist summary badge state (Task 3.2.5b): ``none`` (no "
            "summary generated), ``draft`` (generated, not yet approved), "
            "``approved`` (author-approved). Computed per-node in the "
            "tree-feed — orthogonal to ``materials_changed``."
        ),
    )
    materials_changed: bool = Field(
        default=False,
        description=(
            "Axis-1 staleness ONLY: the node's materials changed after the "
            "summary was generated (``Raw.source_content_hash`` differs from "
            "the live ``CourseNode.content_hash``). NOT the generate-route's "
            'two-axis ``uncovered_stale`` — the label is "materials changed", '
            'not "needs regeneration". Independent of ``summary_status`` (an '
            "approved node can still be ``materials_changed``)."
        ),
    )
    authored_documents: list[AuthoredDocumentSummaryResponse] = Field(
        default_factory=list,
        description="Authored documents attached directly to this node.",
        validation_alias="documents",
    )
    children: list[NodeWithDocumentsResponse] = Field(
        default_factory=list,
        description="Child nodes, recursively nested.",
    )
    created_at: datetime = Field(description="When this node was created.")
    updated_at: datetime = Field(description="When this node was last modified.")


class AuthoredDocumentUpdateRequest(BaseModel):
    """Request body for PATCH /documents/{document_id}.

    All fields are optional — only fields present in the request body
    are updated. Field presence is detected via ``model_fields_set``.
    Passing ``task_type: null`` explicitly clears the task flag.
    """

    material_role: MaterialRole | None = Field(
        default=None,
        description="New role: ``educational`` or ``methodological``.",
    )
    task_type: AssignmentType | None = Field(
        default=None,
        description=(
            "Assignment taxonomy tier. Pass ``null`` to clear. "
            "Omit the field to keep the current value."
        ),
    )


FileRoleToken = Literal["full", "auxiliary", "structure_only"]


class FileRolesDecisionRequest(BaseModel):
    """№21 confirm-screen payload — the author's per-file role decision.

    ``files`` must assign a role to EVERY proposal file and no others (the UI
    expands folder gestures to per-file states; decision 3 / decision 4). Role
    tokens are the three of decision 2 — ``auxiliary`` is legal (author-chosen).
    """

    files: dict[str, FileRoleToken] = Field(
        description=(
            "Per-file role decision. Keys MUST exactly cover the current "
            "``proposal.files`` — a missing or extra path is a 422. Values: "
            "``full`` | ``auxiliary`` | ``structure_only``."
        ),
    )
    tree_digest: str = Field(
        description=(
            "The ``proposal.tree_digest`` being confirmed against. Must equal "
            "the current proposal's digest, else the file tree changed and the "
            "confirmation is stale (409 ``file_roles_stale``)."
        ),
    )


class AuthoredDocumentResponse(BaseModel):
    """Response schema for a single authored document."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID = Field(description="Unique entry identifier (UUIDv7).")
    course_node_id: uuid.UUID = Field(
        description="Parent node this material belongs to."
    )
    course_root_id: uuid.UUID = Field(
        description=(
            "Root CourseNode of the course this document's node belongs to "
            "(KD-delta denormalised root). Projection of the already-stored "
            "``AuthoredDocument.course_root_id`` — a NOT NULL column resolved by "
            "a parent walk at creation, so this is always present (never null); "
            "no migration, no read-time resolution. Equals ``course_node_id`` "
            "when the document sits on a root node."
        ),
    )
    source_type: str = Field(
        description="Material type: ``video``, ``presentation``, ``text``, or ``web``."
    )
    material_role: str = Field(
        description="Role: ``educational`` or ``methodological``."
    )
    task_type: AssignmentType | None = Field(
        default=None,
        description=(
            "Assignment type if this material is a concrete task, else ``null``."
        ),
    )
    source_url: str = Field(description="URL or S3 path to the raw material.")
    filename: str | None = Field(description="Original filename, if available.")
    language: str | None = Field(
        default=None,
        description=(
            "Canonical ISO 639-3 language. ``null`` when unset and not yet "
            "auto-detected."
        ),
    )
    order: int = Field(description="0-based position among sibling materials.")
    state: str = Field(
        description=("Derived lifecycle state: ``pending``, ``ready``, or ``error``."),
    )
    processing_phase: str = Field(
        description=(
            "External processing phase (L3, root cause #2): ``queued`` "
            "(standing in the queue, no worker has taken it), ``processing`` "
            "(a worker is processing it), ``awaiting_author`` (№21: prep "
            "produced a file-role proposal the author has not yet confirmed), "
            "``ready``, or ``error``. Splits the in-flight ``pending`` state "
            "into ``queued`` vs ``processing``; the terminal values "
            "(``ready`` / ``error``) mirror ``state`` verbatim. Never null/empty."
        ),
    )
    error_message: str | None = Field(
        description="Error message from the last failed processing attempt, if any."
    )
    error_category: str | None = Field(
        default=None,
        description="Structural async-error code (F4), if categorised.",
    )
    job_id: uuid.UUID | None = Field(
        description="Job ID currently processing this material, or ``null``."
    )
    file_roles: dict[str, Any] | None = Field(
        default=None,
        description=(
            "№21 author file-role markup — the full ``{proposal, decision}`` "
            "JSONB (proposal written by the DOCUMENT_PREPARATION job; decision "
            "by the confirm endpoint). ``null`` until prep has run. The confirm "
            "screen reads this whole object."
        ),
    )
    created_at: datetime = Field(description="When this entry was created.")
    updated_at: datetime = Field(description="When this entry was last modified.")


class ProcessingEstimate(BaseModel):
    """Informational ingestion queue-wait hint (DD-3.3c-I-B).

    Returned on successful intake so the client can set expectations: the
    single serial worker drains the queue slowly on modest hardware. Both
    values are operator-tuned settings (``intake_hint_*``). The UI localizes
    the surrounding copy; this carries only the numbers.
    """

    max_hours: float = Field(
        description="Upper-bound processing time before the document is ready, hours."
    )
    check_after_hours: float = Field(
        description="Suggested delay before re-checking job status, hours."
    )


class AuthoredDocumentCreateResponse(BaseModel):
    """Response for authored document creation.

    Extends the base response with ``job_id`` — the ID of the
    ingestion job that was auto-enqueued.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID = Field(description="Unique entry identifier (UUIDv7).")
    course_node_id: uuid.UUID = Field(
        description="Parent node this material belongs to."
    )
    source_type: str = Field(
        description="Material type: ``video``, ``presentation``, ``text``, or ``web``."
    )
    material_role: str = Field(
        description="Role: ``educational`` or ``methodological``."
    )
    task_type: AssignmentType | None = Field(
        default=None,
        description=(
            "Assignment type if this material is a concrete task, else ``null``."
        ),
    )
    source_url: str = Field(description="URL or S3 path to the raw material.")
    filename: str | None = Field(description="Original filename, if available.")
    language: str | None = Field(
        default=None,
        description=(
            "Canonical ISO 639-3 language. ``null`` when unset and not yet "
            "auto-detected."
        ),
    )
    order: int = Field(description="0-based position among sibling materials.")
    state: str = Field(description="Derived lifecycle state (will be ``pending``).")
    processing_phase: str = Field(
        default="queued",
        description=(
            "External processing phase (L3, root cause #2): on create/confirm/"
            "retry this is ``queued`` (the ingestion job was just enqueued and "
            "no worker has taken it). Full vocabulary elsewhere: ``queued`` | "
            "``processing`` | ``awaiting_author`` | ``ready`` | ``error``. "
            "Never null/empty."
        ),
    )
    job_id: uuid.UUID | None = Field(
        default=None,
        description="ID of the auto-enqueued ingestion job for progress tracking.",
    )
    warnings: list[str] = Field(
        default_factory=list,
        description="Non-fatal warnings (e.g. unverified platform).",
    )
    processing_estimate: ProcessingEstimate | None = Field(
        default=None,
        description=(
            "Informational queue-wait hint (DD-3.3c-I-B); ``null`` until the "
            "route attaches it on successful intake."
        ),
    )
    created_at: datetime = Field(description="When this entry was created.")


# --- Homework ---


class HomeworkSubmitResponse(BaseModel):
    """Response for POST /homework/submit (202 Accepted)."""

    submission_id: uuid.UUID = Field(
        description="Unique ID of the created homework submission."
    )
    student_id: uuid.UUID = Field(
        description="Student record ID (created or existing)."
    )
    status: str = Field(description="Initial status: ``received``.")
    job_id: uuid.UUID | None = Field(
        default=None,
        description="Background job ID for tracking processing progress. "
        "None when returning a cached duplicate result.",
    )
    duplicate: bool = Field(
        default=False,
        description="True if this is a cached result from a "
        "prior identical submission.",
    )


class TenantWebhookResponse(BaseModel):
    """Response for PATCH /tenant/webhook."""

    webhook_url: str | None = Field(
        description="Current default webhook URL for the tenant. None if cleared.",
    )


# --- Jobs ---


class JobResponse(BaseModel):
    """Response for GET /jobs/{job_id}."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    job_type: str
    priority: str
    status: str
    tenant_id: uuid.UUID | None
    course_node_id: uuid.UUID | None
    arq_job_id: str | None
    current_stage: str | None = None
    stage_progress: dict[str, Any] | None = None
    result_data: dict[str, Any] | None = None
    error_message: str | None
    error_category: str | None = None
    queued_at: datetime
    started_at: datetime | None
    completed_at: datetime | None


# --- Author work-list (step A) ---


class JobListItemResponse(BaseModel):
    """One row of the author work-list (door 1); also the ``last_job`` of a
    history row (door 2). Fields resolve server-side over one query (step A §4)."""

    id: uuid.UUID
    job_type: str
    job_state: str = Field(
        description=(
            "Work-state token (step A §3): ``queued`` | ``processing`` | "
            "``ready`` | ``error`` | ``cancelled`` | ``obsolete``. Derived purely "
            "from Job.status — NOT the material phase (a job row never carries "
            "``awaiting_author``)."
        )
    )
    queued_at: datetime
    started_at: datetime | None = Field(
        description=(
            "Worker-pickup time; NULL for work terminated straight from the "
            "queue (never ran)."
        )
    )
    completed_at: datetime | None
    subject_type: str | None
    subject_id: uuid.UUID | None
    material_id: uuid.UUID | None = Field(
        description="Resolved material anchor; NULL for node-summary work."
    )
    display_name: str | None = Field(
        description=(
            "Material label as stored — a scrubbed marker for a deleted source; "
            "the server neither invents nor hides it."
        )
    )
    display_deleted: bool = Field(
        description="Whether the label's source material is soft-deleted."
    )
    display_deleted_at: datetime | None = Field(
        description=(
            "Machine deletion moment from the source's ``deleted_at``; NULL when live."
        )
    )
    material_source_type: str | None = Field(
        description="Source type (document work only), for the deleted marker."
    )
    base_version: int | None = Field(
        description="Base-archive version (base-normalize work only)."
    )
    current_stage: str | None = None
    stage_progress: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Per-job-type progress checkpoint as stored; shape not normalised "
            "here (a later step consumes it)."
        ),
    )


class JobListResponse(BaseModel):
    """Door 1 — a flat page of author work."""

    items: list[JobListItemResponse]
    total: int
    limit: int
    offset: int


class MaterialHistoryItemResponse(BaseModel):
    """Door 2 — one material with its work history (grouped server-side)."""

    material_id: uuid.UUID
    display_name: str | None
    material_deleted: bool
    material_deleted_at: datetime | None
    material_source_type: str | None
    processing_phase: str | None = Field(
        description=(
            "The material's real processing phase; NULL when the material is "
            "deleted (a deleted material is a marker, not a phase)."
        )
    )
    last_job: JobListItemResponse
    jobs_count: int = Field(
        description=(
            "Count of work over this material (from the same list — no separate "
            "computed number)."
        )
    )


class MaterialHistoryResponse(BaseModel):
    """Door 2 — a page of materials with their history."""

    items: list[MaterialHistoryItemResponse]
    total: int
    limit: int
    offset: int


# --- Presigned Upload ---


class PresignedUrlRequest(BaseModel):
    """Request body for POST /nodes/{node_id}/documents/upload-url."""

    filename: str = Field(
        min_length=1,
        max_length=500,
        description="Original filename (used for S3 key and extension validation).",
    )
    content_type: str = Field(
        min_length=1,
        max_length=200,
        description="MIME type of the file to upload.",
    )
    source_type: SourceType = Field(
        description="Material type (video, presentation, text, web, audio, code).",
    )
    size_bytes: int | None = Field(
        default=None,
        ge=1,
        description="File size in bytes (optional, for pre-validation).",
    )


class PresignedUrlResponse(BaseModel):
    """Response for POST /nodes/{node_id}/documents/upload-url."""

    upload_url: str = Field(description="Presigned PUT URL for direct S3 upload.")
    key: str = Field(description="S3 object key to use in confirm-upload.")
    expires_in: int = Field(description="URL validity in seconds.")


class ProjectBaseAttachResponse(BaseModel):
    """Response for POST /documents/{document_id}/base (KD18 P2).

    Returned with 202 — the base archive is stored and the deterministic
    normalization is enqueued; the version starts ``pending`` and flips to
    ``ready`` | ``failed`` in the background.
    """

    model_config = ConfigDict(from_attributes=True)

    base_version_id: uuid.UUID = Field(
        description="The created ProjectBase version id."
    )
    version: int = Field(description="1-based version number for this document.")
    state: str = Field(description="Normalization state (``pending`` on attach).")


class ProjectBaseDescriptorResponse(BaseModel):
    """Response for GET /homework/tasks/{id}/base (KD18 P2, mode-1 API-key).

    The active base — the latest ``ready`` version — or, if none is ready yet,
    the latest version with its ``state`` (``snapshot_hash`` is ``null`` until
    ready). ``snapshot_hash`` is exposed by design: the P3 submit path echoes
    it as ``base_snapshot_hash``. ``original_url`` is a presigned GET of the
    author's original archive.
    """

    model_config = ConfigDict(from_attributes=True)

    base_version_id: uuid.UUID = Field(description="The ProjectBase version id.")
    version: int = Field(description="1-based version number.")
    snapshot_hash: str | None = Field(
        default=None,
        description="Aggregate snapshot hash (echo-match key); null until ready.",
    )
    state: str = Field(description="Normalization state: pending | ready | failed.")
    original_url: str = Field(
        description="Presigned GET URL of the original base archive."
    )


class ProjectBaseStateResponse(BaseModel):
    """Response for GET /documents/{document_id}/base (KD18 P6, author).

    The author-monitoring read-surface: the LATEST base version REGARDLESS of
    state (``get_latest``), deliberately NOT the mode-1 student descriptor's
    latest-ready-else-latest. After a re-upload the author must see the fresh
    version's ``pending`` / ``failed`` progress, not an older READY version
    masking it — the opposite goal of ``GET /homework/tasks/{id}/base``, which
    needs a *usable* READY base for the student. ``failure_reason`` is non-null
    only for ``failed``; ``snapshot_hash`` is null until ``ready``.
    """

    model_config = ConfigDict(from_attributes=True)

    version: int = Field(description="1-based version number for this document.")
    state: str = Field(description="Normalization state: pending | ready | failed.")
    failure_reason: str | None = Field(
        default=None,
        description="Human-readable reason; non-null only when state='failed'.",
    )
    snapshot_hash: str | None = Field(
        default=None,
        description="Aggregate snapshot hash; null until state='ready'.",
    )


# KD18 P6 (DD-6-V): the base-manifest response IS the P1 ``Manifest`` dataclass —
# a single source of truth, not a hand-maintained Pydantic duplicate (Rule 1).
# FastAPI serializes the frozen dataclass byte-identically to the stored JSONB
# (its StrEnum members render as their string value); the route reconstructs the
# dataclass from JSONB via the P1 reviver so the return is a typed ``Manifest``,
# not an opaque ``dict[str, Any]``.
type ProjectBaseManifestResponse = Manifest


class ConfirmUploadRequest(BaseModel):
    """Request body for POST /nodes/{node_id}/documents/confirm-upload."""

    key: str = Field(
        min_length=1,
        max_length=2000,
        description="S3 object key returned by upload-url endpoint.",
    )
    source_type: SourceType = Field(
        description="Material type (video, presentation, text, web, audio, code).",
    )
    material_role: MaterialRole = Field(
        default=MaterialRole.EDUCATIONAL,
        description=(
            "Role: ``educational`` (delivers content) "
            "or ``methodological`` (declares course intent)."
        ),
    )
    task_type: AssignmentType | None = Field(
        default=None,
        description=(
            "Mark the material as a concrete task of the given taxonomy tier. "
            "``null`` for regular materials."
        ),
    )
    filename: str | None = Field(
        default=None,
        max_length=500,
        description="Override filename (defaults to key basename).",
    )
    language: str | None = Field(
        default=None,
        description=(
            "Optional language override. Accepts ISO 639-1 / 639-3 / English "
            "name; stored as canonical ISO 639-3. When absent, the course "
            "default is used, and STT auto-detection is the final fallback."
        ),
        examples=["uk", "ukr"],
    )

    @field_validator("language")
    @classmethod
    def _check_language(cls, raw: str | None) -> str | None:
        if raw is None:
            return None
        return _validate_language(raw)


# --- Storage Management ---


class StorageFileResponse(BaseModel):
    """Single file in tenant's S3 storage."""

    key: str = Field(description="Full S3 object key.")
    size_bytes: int = Field(description="File size in bytes.")
    last_modified: datetime = Field(description="Last modification timestamp.")


class StorageUsageResponse(BaseModel):
    """Tenant storage usage summary."""

    total_bytes: int = Field(description="Total storage used in bytes.")
    file_count: int = Field(description="Number of files in storage.")


# --- NodeSummaryFinal — methodist layer HTTP surface (Phase 3.2.4 c2) ---


class NodeSummaryGenerateRequest(BaseModel):
    """Request body for ``POST /api/v1/nodes/{node_id}/summary/generate``.

    Triggers the two-pass methodist run via ARQ (orchestrator runs in
    the worker; the route returns immediately with the queued Job).

    ``force`` widens scope only for the API's 422 decision on
    ``uncovered_stale_node_ids`` — memo-skip on both axes is
    unconditional inside the orchestrator (K1 ratify, Phase 3.2.2).
    """

    force: bool = Field(
        default=False,
        description=(
            "When true, the run proceeds even if scope-validation flags "
            "ancestors as uncovered_stale. Memo-skip on both hash axes "
            "is unconditional; force only widens what the API accepts."
        ),
    )


class NodeSummaryFinalResponse(BaseModel):
    """Response shape for ``GET /api/v1/nodes/{node_id}/summary`` (P6, 200 branch).

    Full ``NodeSummaryFinal`` contract per KD11 §1043-1071. Read-only
    on enclosing_context (refreshed by the third channel — top-down,
    NOT author-editable). The approval pair (``approved_at``,
    ``enclosing_context_updated_at``) is two timestamps, not one
    derived field — downstream MUST NOT assume the whole Final was
    approved in a single moment (KD11 line 1069).
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID = Field(description="Unique NodeSummaryFinal identifier (UUIDv7).")
    course_node_id: uuid.UUID = Field(
        description="The CourseNode this Final summarises (1:1, UNIQUE)."
    )

    # Editable family (KD11 §1043-1054).
    title: str | None = Field(description="Methodist-canonical title.")
    description: str | None = Field(description="Methodist-canonical description.")
    learning_objectives: list[str] = Field(
        default_factory=list,
        description="Bloom-taxonomy outcome statements (3-7 typical).",
    )
    knowledge: list[dict[str, str]] = Field(
        default_factory=list,
        description=(
            "LearningOutcomeItem[]: {name, description} — what student will know."
        ),
    )
    skills: list[dict[str, str]] = Field(
        default_factory=list,
        description=(
            "LearningOutcomeItem[]: {name, description} — what student will do."
        ),
    )
    success_criteria: list[str] = Field(default_factory=list)
    assessment_approach: str | None = Field(default=None)
    teaching_approach: str | None = Field(default=None)
    key_activities: list[str] = Field(default_factory=list)
    common_mistakes: list[str] = Field(default_factory=list)

    # Read-only — copied from Raw (KD11).
    main_concepts: list[str] = Field(default_factory=list)
    secondary_concepts: list[str] = Field(default_factory=list)
    enclosing_context: str | None = Field(
        default=None,
        description=(
            "Read-only on Final. Top-down refresh updates it WITHOUT "
            "resetting approved_at (KD11 §1066 third channel)."
        ),
    )

    # Empty-leaf author flow placeholder (Phase 3.1 MVP).
    is_manual: bool = Field(
        description=(
            "Forward-compat marker for the empty-leaf author-create flow. "
            "In Phase 3.2.4 no HTTP API sets this — always ``false`` for "
            "Finals created by the channel-1 automatic overwrite."
        ),
    )
    manual_description: str | None = Field(default=None)

    # Size metrics.
    own_documents_count: int = Field(default=0)
    own_chars_count: int = Field(default=0)
    cumulative_documents_count: int = Field(default=0)
    cumulative_chars_count: int = Field(default=0)

    # Hash + approval pair (KD11).
    content_hash: str | None = Field(
        description=(
            "Materialised SHA-256 hex over content fields EXCEPT "
            "``enclosing_context`` (KD9 line 729 invariant)."
        ),
    )
    approved_at: datetime | None = Field(
        default=None,
        description=(
            "NULL after every automatic Raw-overwrite; set by explicit "
            "approve. The third channel (top-down) does NOT reset this."
        ),
    )
    enclosing_context_updated_at: datetime | None = Field(
        default=None,
        description=(
            "When top-down last refreshed ``enclosing_context``. NULL on "
            "root (top-down skipped) and until the first top-down lands."
        ),
    )

    created_at: datetime
    updated_at: datetime


class NodeSummaryFinalUpdateRequest(BaseModel):
    """Request body for ``PATCH /api/v1/node-summaries/{node_id}/final``.

    Editable family per KD11 §1043-1054 (Identity + Learning outcomes
    + Assessment + Methodology). Pydantic rejects unknown keys via
    ``extra='forbid'`` — concepts / ``enclosing_context`` / hash /
    timestamps / ``is_manual`` / ``manual_description`` all 422 at the
    request-parsing boundary. The repository's deep ``ValueError`` gate
    is the defence-in-depth fallback.

    All fields are optional — only present keys land on the row.
    ``approved_at`` is NOT touched by PATCH (KD11: author edit does
    not auto-invalidate a prior approval; explicit approve is a
    separate action).
    """

    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=500)
    description: str | None = Field(default=None, max_length=10_000)
    learning_objectives: list[str] | None = Field(default=None)
    knowledge: list[dict[str, str]] | None = Field(default=None)
    skills: list[dict[str, str]] | None = Field(default=None)
    success_criteria: list[str] | None = Field(default=None)
    assessment_approach: str | None = Field(default=None, max_length=10_000)
    teaching_approach: str | None = Field(default=None, max_length=10_000)
    key_activities: list[str] | None = Field(default=None)
    common_mistakes: list[str] | None = Field(default=None)


class NodeSummaryEditViewResponse(BaseModel):
    """Response shape for ``GET /api/v1/node-summaries/{node_id}/edit-view``.

    Combined view per KD11 §1086-1101: editable Final + Raw's
    ``methodist_observations`` (renamed to ``raw_observations`` at the
    API boundary per operator-ratified Phase 3.2.4 pre-flight question
    (в) — ORM column name unchanged) + the optional single-version
    snapshot.
    """

    model_config = ConfigDict(from_attributes=True)

    final: NodeSummaryFinalResponse
    raw_observations: list[str] = Field(
        default_factory=list,
        description=(
            "Methodist observations from both passes (API-side alias "
            "for ``NodeSummaryRaw.methodist_observations``). Shown to "
            "the author before approve."
        ),
    )
    previous_snapshot: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Snapshot of the prior Final state captured at the last "
            "automatic overwrite. ``null`` when no overwrite has "
            "occurred yet (the very first run produced the current "
            "Final via INSERT)."
        ),
    )


# --- Student portal (Phase 6 T1, KD17) ---


class PortalLoginRequest(BaseModel):
    """Request body for POST /portal/login.

    The portal is tenant-scoped and the login lookup happens before the
    student is known, so the tenant is resolved explicitly from ``tenant_id``
    (KD17 ratify Q2) rather than guessed.
    """

    tenant_id: uuid.UUID = Field(description="Tenant the student belongs to.")
    login: str = Field(min_length=1, max_length=200, description="Student login.")
    password: str = Field(min_length=1, description="Student password (plaintext).")


class PortalLoginResponse(BaseModel):
    """Response for a successful POST /portal/login."""

    access_token: str = Field(description="HS256 bearer session token.")
    token_type: str = Field(default="bearer", description="Always ``bearer``.")
    student_id: uuid.UUID = Field(description="Authenticated student ID.")
    display_name: str | None = Field(
        default=None, description="Student's portal display name, if set."
    )


class PortalMeResponse(BaseModel):
    """Response for GET /portal/me (the authenticated student's identity).

    The recovery-email status is folded in (Phase 6 R2) rather than exposed on
    a separate GET: ``recovery_email`` is the current address (or None), and
    ``recovery_email_confirmed`` reflects whether it has been verified. A
    forgot-password link is only ever sent to a confirmed address.
    """

    student_id: uuid.UUID
    tenant_id: uuid.UUID
    login: str
    display_name: str | None = None
    recovery_email: str | None = None
    recovery_email_confirmed: bool = False


class RecoveryEmailRequest(BaseModel):
    """Request body for POST /portal/recovery-email (set/change; protected).

    The email is validated only minimally — a single ``@`` with a non-empty
    local part and domain (RP6): deliverability is proven by the confirm flow
    (the mail must arrive and its token be redeemed), not a strict RFC
    validator, so no ``email-validator`` dependency is pulled in. ``max_length``
    mirrors the ``String(320)`` column so an overflow is a 422, not a DB error.
    """

    email: str = Field(
        min_length=3,
        max_length=320,
        description="Student-owned recovery email to set or change.",
    )

    @field_validator("email")
    @classmethod
    def _validate_email_shape(cls, v: str) -> str:
        local, sep, domain = v.strip().partition("@")
        if not sep or not local or not domain:
            msg = "email must contain a local part and a domain (e.g. name@example.com)"
            raise ValueError(msg)
        return v.strip()


class RecoveryEmailResponse(BaseModel):
    """Response for POST /portal/recovery-email (the new pending state)."""

    recovery_email: str = Field(description="The address now on file.")
    recovery_email_confirmed: bool = Field(
        description="Always False right after set/change — a confirm mail was sent."
    )


class ConfirmRecoveryEmailRequest(BaseModel):
    """Request body for POST /portal/recovery-email/confirm (public)."""

    token: str = Field(min_length=1, description="The email-confirm token.")


class ForgotPasswordRequest(BaseModel):
    """Request body for POST /portal/password/forgot (public).

    Identifies the credential by ``(tenant_id, login)`` — the stable portal
    identifier — not by email (recovery emails are optional and not unique).
    The route always returns a generic 202 regardless of existence
    (anti-enumeration); a reset link is only sent to a confirmed recovery email.
    """

    tenant_id: uuid.UUID = Field(description="Tenant the student belongs to.")
    login: str = Field(min_length=1, max_length=200, description="Student login.")


class ResetPasswordRequest(BaseModel):
    """Request body for POST /portal/password/reset (public).

    ``password`` carries the schema floor only (``min_length=1``); the real
    minimum-length policy is enforced at hash time (argon2id), mapping a weak
    password to 422 — mirroring provisioning.
    """

    token: str = Field(min_length=1, description="The password-reset token.")
    password: str = Field(min_length=1, description="New password (plaintext).")


class ProvisionStudentRequest(BaseModel):
    """Request body for POST /students (tenant-admin provisioning, scope PREP).

    ``external_id`` has three modes (KD17 ratify Q2 refinement B):

    * ``generate`` — the system mints a fresh ``external_id`` (omit the field);
    * ``manual``   — caller supplies ``external_id`` (must be new in the tenant);
    * ``existing`` — caller supplies ``student_id`` of an existing student; the
      credential is attached to it (the integration-mode student gains a portal
      login and its prior history). No new student row is created.
    """

    mode: Literal["generate", "manual", "existing"] = Field(
        description="external_id resolution mode."
    )
    external_id: str | None = Field(
        default=None,
        max_length=200,
        description="Required for mode='manual'; must be omitted for "
        "mode='generate'; ignored for mode='existing'.",
    )
    student_id: uuid.UUID | None = Field(
        default=None,
        description="Required for mode='existing' — the existing student to "
        "attach the credential to.",
    )
    login: str = Field(min_length=1, max_length=200, description="Portal login.")
    password: str = Field(
        min_length=1,
        description="Portal password (plaintext; only the argon2id hash is "
        "stored). Must meet the minimum-length policy.",
    )
    display_name: str | None = Field(
        default=None, max_length=200, description="Portal display name."
    )

    @model_validator(mode="after")
    def _check_mode_fields(self) -> Self:
        if self.mode == "manual" and not self.external_id:
            raise ValueError("external_id is required when mode='manual'")
        if self.mode == "existing" and self.student_id is None:
            raise ValueError("student_id is required when mode='existing'")
        if self.mode == "generate" and self.external_id is not None:
            raise ValueError("external_id must be omitted when mode='generate'")
        return self


class ProvisionStudentResponse(BaseModel):
    """Response for a successful POST /students (201)."""

    student_id: uuid.UUID
    external_id: str
    login: str


class SetStudentPasswordRequest(BaseModel):
    """Request body for POST /students/{id}/password (DD-6-J, scope PREP).

    Author-side password reset — the fallback path when a student has no
    confirmed recovery email. Schema floor only (``min_length=1``); the real
    minimum-length policy is enforced at hash time (argon2id, mapping a weak
    password to 422), exactly as provisioning does.
    """

    password: str = Field(min_length=1, description="New portal password (plaintext).")


class EnrollmentRequest(BaseModel):
    """Request body for POST /students/enrollments (bind student↔course)."""

    student_id: uuid.UUID = Field(description="Student to enroll.")
    course_node_id: uuid.UUID = Field(
        description="Root CourseNode (the course) to grant access to."
    )


class StudentRosterItem(BaseModel):
    """One row of the tenant-admin student roster (GET /students, T5).

    Projects ``Student`` LEFT JOIN ``StudentCredential``: ``login`` and
    ``is_active`` are null for integration-mode students that never got a
    portal credential — they are still listed so the admin can attach one
    via the ``existing`` provisioning mode. ``is_active`` mirrors the
    revoke state (deferred hard-delete, DD-6-A): null = no credential,
    true = active, false = access revoked.
    """

    student_id: uuid.UUID = Field(description="Student primary key.")
    external_id: str = Field(description="Caller-system student identifier.")
    login: str | None = Field(
        default=None,
        description="Portal login, or null if no credential provisioned.",
    )
    display_name: str | None = Field(
        default=None, description="Portal display name (nullable)."
    )
    is_active: bool | None = Field(
        default=None,
        description="Credential state: null = no credential, true = active, "
        "false = access revoked. Mirrors the revoke/restore toggle.",
    )
    enrollment_count: int = Field(
        description="Number of course enrollments (correlated count)."
    )


class StudentRosterResponse(BaseModel):
    """Paginated tenant-admin student roster (GET /students, T5)."""

    tenant_id: uuid.UUID = Field(
        description="The tenant these students belong to (DD-6-M). Lets the "
        "author UI build the shared portal link ${PORTAL_ORIGIN}/${tenant_id}/"
        "login without exposing the UUID elsewhere; present even at 0 courses."
    )
    items: list[StudentRosterItem] = Field(
        description="Students for the current page (newest-first)."
    )
    total: int = Field(description="Total non-deleted students for the tenant.")
    limit: int = Field(description="Maximum items per page (as requested).")
    offset: int = Field(description="Number of items skipped (as requested).")


class StudentEnrollmentItem(BaseModel):
    """One enrollment of a student (GET /students/{id}/enrollments, T5).

    Only ``course_node_id`` + ``enrolled_at`` — the title is resolved
    client-side from the roots the admin UI already holds. Enrollments to
    a since-soft-deleted course are preserved here (unlike the portal
    listing) so the admin can see and unbind them.
    """

    course_node_id: uuid.UUID = Field(description="Enrolled root CourseNode id.")
    enrolled_at: datetime = Field(description="When the grant was created.")


class StudentEnrollmentsResponse(BaseModel):
    """A student's enrollments (GET /students/{id}/enrollments, T5)."""

    student_id: uuid.UUID = Field(description="The student these grants belong to.")
    items: list[StudentEnrollmentItem] = Field(
        description="Enrollments, oldest-first (mirrors bind order)."
    )


class PortalSubmitResponse(BaseModel):
    """Response for a portal session submission (Phase 6 T2, mode-2 in_app).

    The student tracks progress + reads the review via the read-path (status on
    the same endpoint), so the response is minimal: no caller-facing job_id, no
    student_id (it is the session's own student).
    """

    submission_id: uuid.UUID = Field(
        description="The created (or duplicate) submission."
    )
    status: str = Field(
        description="Submission status ('received' for a fresh "
        "submission; the existing status when a duplicate was returned)."
    )
    duplicate: bool = Field(
        default=False,
        description="True when an identical file for this task was already "
        "submitted — the existing submission is returned, no new review runs.",
    )


class PortalVerdict(BaseModel):
    """Caller-facing verdict shown to the student (Phase 6 T2 read-path).

    Derived from ``review_result['verdict']`` (the only review_result key the
    student ever sees); None until a review has written it. The full layered
    ``review_result`` JSONB is NEVER exposed.
    """

    passed: bool = Field(description="Whether the submission meets the bar.")
    correctness: str = Field(
        description="Coarse correctness (correct / partially_correct / incorrect)."
    )


class PortalNotOpened(BaseModel):
    """One file from the submission the checker did not read, and why.

    Shown to the student on both a passing and a refused attempt: a review
    that quietly rested on part of the work is the thing this exists to
    prevent. ``reason`` is the same code vocabulary as
    :class:`PortalRejection`, so the interface keeps one dictionary.
    """

    path: str = Field(description="Name of the file inside the archive.")
    reason: str = Field(description="Why it was not read (ErrorCategory value).")
    size: int = Field(description="Size in bytes, as declared by the archive.")


class PortalRejection(BaseModel):
    """Why an attempt was refused, in terms the interface can phrase.

    Carries the CODE, never the internal message. ``DD-6-D`` forbade exposing
    the trace and that stands: ``error_message`` is a developer string with
    library vocabulary in it, and it is not serialized here or anywhere on the
    portal. The review of that ban (ratified 2026-09-02) was narrow — the
    prohibition is on technical detail, not on telling the student what
    happened, and a stable code is what lets the interface say it in their
    language.

    ``details`` is only the short curated fact the phrase cannot know by
    itself, such as which file it was.
    """

    code: str = Field(description="Reason code (ErrorCategory value or status).")
    details: str | None = Field(
        default=None, description="Curated short specifics — never the trace."
    )


class PortalSubmissionListItem(BaseModel):
    """One attempt in the read-path list (Phase 6 T2). NO review_markdown.

    The list is intentionally light: the heavy ``review_markdown`` is fetched
    only in the detail view. The internal trace
    (``review_result``/``safety_result``/``sanity_result``) is NEVER included.
    """

    id: uuid.UUID
    status: str = Field(description="Lifecycle milestone (also the progress signal).")
    score: int | None = Field(default=None, description="Final 0-100 grade, if any.")
    verdict: PortalVerdict | None = Field(default=None)
    created_at: datetime
    original_filename: str | None = None
    rejection: PortalRejection | None = Field(
        default=None, description="Why the attempt was refused; null if it was not."
    )
    not_opened: list[PortalNotOpened] = Field(
        default_factory=list,
        description="Files skipped during checking — also on a passing attempt.",
    )


class PortalDeltaReceipt(BaseModel):
    """I2 delta counters + staleness for a project submission (KD18 P5).

    Derived on read (KD-P3-B) from the persisted base + submission manifests —
    the DB stores manifests, not counts, and ``compute_delta`` is BE-only. The
    student sees how their submission diverged from the base and whether the
    base has since moved on. Null on the parent detail for a non-project
    submission (no delta concept) — a DISTINCT state from an all-zero delta.

    The hygiene level (normalizer-excluded new files) is deliberately NOT
    surfaced to the student.
    """

    changed: int = Field(
        description="Files present in both base and submission with different content."
    )
    new: int = Field(description="Files added relative to the base.")
    deleted: int = Field(description="Base files absent from the submission.")
    base_version: int | None = Field(
        default=None,
        description="Version of the base this submission was built on; null when "
        "no base was attached.",
    )
    latest_version: int | None = Field(
        default=None,
        description="Latest READY base version now; null when no base was attached.",
    )
    is_stale: bool = Field(
        description="True when a newer READY base exists than the one built on "
        "(a signal, not a blocker)."
    )


class PortalSubmissionDetail(BaseModel):
    """One attempt — the full curated slice (Phase 6 T2 read-path detail).

    The student sees ``status`` / ``score`` / ``verdict`` / ``review_markdown``
    only. The internal trace (``review_result`` / ``safety_result`` /
    ``sanity_result``) is NEVER serialized here. A project submission also
    carries the ``delta`` receipt (KD18 P5); it is null for a non-project one.
    """

    id: uuid.UUID
    status: str = Field(description="Lifecycle milestone (also the progress signal).")
    score: int | None = Field(default=None, description="Final 0-100 grade, if any.")
    verdict: PortalVerdict | None = Field(default=None)
    review_markdown: str | None = Field(
        default=None, description="Rendered Markdown review (None until reviewed)."
    )
    created_at: datetime
    original_filename: str | None = None
    delta: PortalDeltaReceipt | None = Field(
        default=None,
        description="I2 delta receipt (KD18 P5) for a project submission — "
        "counters + staleness; null for a non-project submission.",
    )
    rejection: PortalRejection | None = Field(
        default=None, description="Why the attempt was refused; null if it was not."
    )
    not_opened: list[PortalNotOpened] = Field(
        default_factory=list,
        description="Files skipped during checking — also on a passing attempt.",
    )


class PortalBaseDownload(BaseModel):
    """Presigned download of a project task's active base ORIGINAL (KD18 P5).

    The student downloads the AUTHOR's original base archive (not the normalized
    snapshot, KD17) to build their submission. Served on demand by the portal
    base route so the tree descriptor stays cheap — no presigned URL is baked
    into every task node. The active base is the latest READY version.
    """

    original_url: str = Field(
        description="Fresh presigned GET of the active (latest READY) base "
        "original archive."
    )


class PortalMediaResponse(BaseModel):
    """How to render a material to the student, originals-only (Phase 6 T3).

    A single descriptor the portal SPA branches on (KD17). ``kind``:

    - ``external`` — YouTube/web: ``url`` is the original external link
      (never proxied through S3).
    - ``file`` — uploaded video/audio: ``url`` is a fresh presigned-GET on the
      object (flat 300-min TTL, KD17).
    - ``slides`` — presentation: ``slide_urls`` is the ordered list of fresh
      presigned-GET URLs for the persisted WebP renders (empty for a
      presentation ingested before T3 — never an error).

    Deliberately minimal and forward-extensible (T4 informs the final shape).
    """

    kind: Literal["external", "file", "slides"] = Field(
        description="Render mode the portal branches on."
    )
    url: str | None = Field(
        default=None,
        description="External link (kind=external) or presigned-GET URL "
        "(kind=file). None for kind=slides.",
    )
    slide_urls: list[str] | None = Field(
        default=None,
        description="Ordered presigned-GET URLs for the slide WebPs "
        "(kind=slides only; empty list for a pre-T3 presentation). None "
        "otherwise.",
    )


# --- Portal materials-listing (Phase 6 T4a, KD17) ---


class PortalCourseListItem(BaseModel):
    """One enrolled course on the portal landing list (Phase 6 T4a c1).

    Deliberately bare — id + title only. No counts, no peeking into the tree:
    the first screen is cheap, and the material tree is a separate per-course
    call. Enrollment IS the visibility gate (publish-gate A, KD17): the list is
    exactly the student's active enrollments.
    """

    id: uuid.UUID
    title: str = Field(description="Course (root CourseNode) title.")


class PortalAttemptResult(BaseModel):
    """A single attempt's curated outcome (Phase 6 T4a c2 overlay).

    Carried by ``last`` and ``best`` in the task overlay. Only the score and
    the curated verdict — the internal trace (``review_result`` beyond
    ``verdict`` / ``safety_result`` / ``sanity_result``) is never here.
    """

    score: int | None = Field(
        default=None, description="Final 0-100 grade, if reviewed."
    )
    verdict: PortalVerdict | None = Field(default=None)


class PortalSubmissionOverlay(BaseModel):
    """Per-task submission overlay for the materials tree (Phase 6 T4a c2).

    Computed from the student's OWN active (non-deleted) submissions on the
    task — one query per course, grouped in Python (no N+1).
    """

    submission_status: Literal["none", "pending", "reviewed", "error"] = Field(
        description="By the LATEST attempt: ``none`` (no attempts) / ``pending`` "
        "(in flight: received / safety_ok / sanity_ok / reviewing) / ``reviewed`` "
        "(a review is written: completed / delivered) / ``error`` (a terminal "
        "error: rejected / mismatch / failed). The tree carries only this coarse "
        "``error`` bucket — the precise terminal is the read-path detail's "
        "concern.",
    )
    last: PortalAttemptResult | None = Field(
        default=None,
        description="The latest attempt's curated result (present iff ≥1 attempt).",
    )
    best: PortalAttemptResult | None = Field(
        default=None,
        description="The highest-scored REVIEWED attempt with a non-null score "
        "(pending / null-score attempts never compete). Absent when no attempt "
        "has a usable score yet; equals ``last`` when there is exactly one.",
    )


class PortalTaskBase(BaseModel):
    """Active base-archive descriptor for a project task (KD18 P5).

    Attached to a project ``PortalMaterialItem`` when a base version exists.
    ``state == 'ready'`` describes the active (latest READY) version: the
    student may submit and ``snapshot_hash`` is the auto-echo source the portal
    sends back on submit. ``pending`` / ``failed`` describes the latest version
    when none is READY yet — submit is blocked and ``snapshot_hash`` is null.

    A project task with NO base attached carries ``base = null`` on the item —
    a DISTINCT state (submit allowed, everything is new), never collapsed into
    a non-ready ``state``.
    """

    version: int = Field(description="1-based version of the described base.")
    snapshot_hash: str | None = Field(
        default=None,
        description="Echo-match hash of the active READY base (the portal sends "
        "it back automatically on submit); null unless ``state`` is ``ready``.",
    )
    state: Literal["pending", "ready", "failed"] = Field(
        description="Lifecycle of the described base version."
    )


class PortalMaterialItem(BaseModel):
    """One document in the course tree — a reading material or a task.

    ``kind`` discriminates (mirroring the T3 descriptor so the FE consumes one
    flat contract): ``material`` (``task_type`` IS NULL) carries no overlay;
    ``task`` (``task_type`` IS NOT NULL) carries the submission overlay.
    ``task_type`` and ``base`` (KD18 P5) let the portal render the project
    affordance (base download + echo) without a second call.
    """

    id: uuid.UUID
    kind: Literal["material", "task"] = Field(
        description="``task`` if the document is a submittable assignment "
        "(task_type IS NOT NULL), else ``material``.",
    )
    label: str = Field(
        description="Display label: the filename, else ``{source_type} #{order}``."
    )
    source_type: str = Field(description="video / presentation / text / web / audio.")
    order: int = Field(description="0-based position among sibling documents.")
    task_type: str | None = Field(
        default=None,
        description="Assignment type when ``kind=task`` (e.g. ``project``); null "
        "for a material.",
    )
    base: PortalTaskBase | None = Field(
        default=None,
        description="Active base descriptor (KD18 P5) — present only for a "
        "project task WITH a base; null for a base-less project task, a "
        "non-project task, or a material.",
    )
    overlay: PortalSubmissionOverlay | None = Field(
        default=None,
        description="Submission overlay — present iff ``kind=task``, else null.",
    )


class PortalMaterialTreeNode(BaseModel):
    """Recursive course-tree node, curated for the portal (Phase 6 T4a c2).

    Narrower than the author-facing ``NodeWithDocumentsResponse``: only
    id / title / order + children + documents (no content_hash, summary_status,
    language, timestamps). Soft-deleted nodes / documents and non-READY
    documents are filtered out before projection (publish-gate A + READY-only).
    """

    id: uuid.UUID
    title: str = Field(description="Node title.")
    order: int = Field(description="0-based position among siblings.")
    documents: list[PortalMaterialItem] = Field(
        default_factory=list,
        description="READY documents directly on this node (materials + tasks).",
    )
    children: list[PortalMaterialTreeNode] = Field(
        default_factory=list,
        description="Child nodes, recursively nested.",
    )
