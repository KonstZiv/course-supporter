"""Request/response schemas for the API layer."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from course_supporter.models.methodist import AssignmentType
from course_supporter.models.source import MaterialRole, SourceType
from course_supporter.storage.orm import GenerationMode

# --- Material Tree Nodes ---


class NodeCreateRequest(BaseModel):
    """Request body for creating a material tree node.

    Used by both root node creation (``POST /nodes``)
    and child node creation (``POST /nodes/{node_id}/children``).

    Example::

        {
            "title": "Module 1: Introduction",
            "description": "Overview of core concepts"
        }
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
        pattern=r"^[a-z]{2}$",
        description=(
            "Optional ISO 639-1 language for materials under this node. "
            "Usually set on the root/course node; inherited by materials "
            "unless overridden. Leave empty to rely on STT auto-detection."
        ),
        examples=["uk", "en"],
    )


class NodeUpdateRequest(BaseModel):
    """Request body for updating a material tree node.

    All fields are optional — only provided fields are updated.
    To clear the description, send ``"description": null`` explicitly.

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
        pattern=r"^[a-z]{2}$",
        description=(
            "New default ISO 639-1 language for this subtree. "
            "Send ``null`` to clear, omit to keep unchanged. "
            "Setting it is a pure DB write; does NOT trigger re-ingestion of "
            "existing materials. New uploads and explicit retries will pick it up."
        ),
        examples=["uk", "en"],
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
        description=("Default ISO 639-1 language for materials under this subtree."),
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
    error_message: str | None = Field(
        description="Error from the last failed processing attempt, if any."
    )
    created_at: datetime = Field(description="When this entry was created.")


class NodeWithMaterialsResponse(BaseModel):
    """Recursive tree node with attached materials.

    Used in tree detail to provide the full hierarchical view
    including materials at each level.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID = Field(description="Unique node identifier (UUIDv7).")
    title: str = Field(description="Node title.")
    description: str | None = Field(description="Optional node description.")
    order: int = Field(description="0-based position among siblings.")
    content_hash: str | None = Field(
        description="Merkle hash of this node's content. ``null`` if not computed."
    )
    authored_documents: list[AuthoredDocumentSummaryResponse] = Field(
        default_factory=list,
        description="Authored documents attached directly to this node.",
        validation_alias="documents",
    )
    children: list[NodeWithMaterialsResponse] = Field(
        default_factory=list,
        description="Child nodes, recursively nested.",
    )
    created_at: datetime = Field(description="When this node was created.")
    updated_at: datetime = Field(description="When this node was last modified.")


class AuthoredDocumentCreateRequest(BaseModel):
    """Request body for adding a material to a tree node."""

    source_type: SourceType = Field(
        ...,
        description=(
            "Type of the source material. "
            "Must be one of: ``video``, ``presentation``, ``text``, ``web``."
        ),
        examples=["video", "presentation", "text", "web"],
    )
    material_role: MaterialRole = Field(
        default=MaterialRole.EDUCATIONAL,
        description=(
            "Role of the material: ``educational`` (delivers content to students) "
            "or ``methodological`` (declares course intent/goals)."
        ),
        examples=["educational", "methodological"],
    )
    task_type: AssignmentType | None = Field(
        default=None,
        description=(
            "Mark the material as a concrete assignment of the given taxonomy "
            "tier (``test``, ``short_task``, ``task``, ``project``). When set, "
            "the Methodist agent preserves this material as a "
            "recommended_assignment verbatim. ``null`` for regular materials."
        ),
        examples=[None, "test", "task"],
    )
    source_url: str = Field(
        ...,
        max_length=2000,
        description="URL or S3 path to the raw material.",
        examples=["https://example.com/slides.pdf", "s3://bucket/key"],
    )
    filename: str | None = Field(
        default=None,
        max_length=500,
        description="Original filename for display purposes.",
        examples=["slides.pdf", "lecture-01.mp4"],
    )
    language: str | None = Field(
        default=None,
        pattern=r"^[a-z]{2}$",
        description=(
            "Optional ISO 639-1 language override. When absent, the course "
            "default is used, and STT auto-detection is the final fallback."
        ),
        examples=["uk", "en"],
    )


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


class AuthoredDocumentResponse(BaseModel):
    """Response schema for a single authored document."""

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
            "ISO 639-1 language. ``null`` when unset and not yet auto-detected."
        ),
    )
    order: int = Field(description="0-based position among sibling materials.")
    state: str = Field(
        description=("Derived lifecycle state: ``pending``, ``ready``, or ``error``."),
    )
    error_message: str | None = Field(
        description="Error message from the last failed processing attempt, if any."
    )
    job_id: uuid.UUID | None = Field(
        description="Job ID currently processing this material, or ``null``."
    )
    created_at: datetime = Field(description="When this entry was created.")
    updated_at: datetime = Field(description="When this entry was last modified.")


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
            "ISO 639-1 language. ``null`` when unset and not yet auto-detected."
        ),
    )
    order: int = Field(description="0-based position among sibling materials.")
    state: str = Field(description="Derived lifecycle state (will be ``pending``).")
    job_id: uuid.UUID | None = Field(
        default=None,
        description="ID of the auto-enqueued ingestion job for progress tracking.",
    )
    warnings: list[str] = Field(
        default_factory=list,
        description="Non-fatal warnings (e.g. unverified platform).",
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
    queued_at: datetime
    started_at: datetime | None
    completed_at: datetime | None


# --- Structure Generation ---


class GenerateRequest(BaseModel):
    """Request body for POST /nodes/{node_id}/generate.

    The target node is specified in the URL path.
    """

    mode: GenerationMode = Field(
        default=GenerationMode.FREE,
        description=(
            "Generation mode. ``free`` generates from scratch; "
            "``guided`` preserves existing tree structure."
        ),
    )


class GenerationPlanResponse(BaseModel):
    """Response for POST /nodes/{node_id}/generate."""

    generation_jobs: list[JobResponse] = Field(
        default_factory=list,
        description="Per-node generation jobs in bottom-up DAG order.",
    )
    reconciliation_jobs: list[JobResponse] = Field(
        default_factory=list,
        description="Per-node reconciliation jobs in top-down order.",
    )
    ingestion_jobs: list[JobResponse] = Field(
        default_factory=list,
        description="Ingestion jobs enqueued for stale materials before generation.",
    )
    estimated_llm_calls: int = Field(
        default=0,
        description="Total LLM calls expected for this plan.",
    )


class MethodistPlanResponse(BaseModel):
    """Response for POST /nodes/{node_id}/methodist."""

    bottom_up_jobs: list[JobResponse] = Field(
        default_factory=list,
        description="Per-node Methodist jobs in bottom-up order.",
    )
    top_down_jobs: list[JobResponse] = Field(
        default_factory=list,
        description="Per-node consistency jobs in top-down order.",
    )
    estimated_llm_calls: int = Field(
        default=0,
        description="Total LLM calls expected.",
    )


class MethodistNodeResponse(BaseModel):
    """Methodist output for a single editable node."""

    editable_id: uuid.UUID = Field(
        description="StructureNodeEditable UUID.",
    )
    node_type: str = Field(description="Node type.")
    title: str = Field(description="Node title.")
    methodological_content: dict[str, Any] | None = Field(
        default=None,
        description="Structured MethodistNodeOutput JSON.",
    )
    methodological_markdown: str | None = Field(
        default=None,
        description="Rendered Markdown for author.",
    )
    has_methodist_output: bool = Field(
        description="Whether Methodist has been run for this node.",
    )


class ServiceCallSummary(BaseModel):
    """LLM metadata from the linked ExternalServiceCall."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID = Field(description="ExternalServiceCall UUID.")
    provider: str = Field(description="LLM provider name.")
    model_id: str = Field(description="LLM model identifier used.")
    prompt_ref: str | None = Field(description="Prompt template reference.")
    unit_in: int | None = Field(description="Input units (tokens) consumed.")
    unit_out: int | None = Field(description="Output units (tokens) generated.")
    cost_usd: float | None = Field(description="Estimated cost in USD.")


class SnapshotSummaryResponse(BaseModel):
    """Snapshot metadata without the full structure payload."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID = Field(description="Unique snapshot identifier (UUIDv7).")
    course_node_id: uuid.UUID = Field(
        description="Target node for this snapshot.",
        validation_alias="materialnode_id",
    )
    mode: GenerationMode = Field(description="Generation mode: ``free`` or ``guided``.")
    content_hash: str = Field(
        description="Merkle fingerprint of the target subtree at generation time.",
        validation_alias="node_fingerprint",
    )
    externalservicecall_id: uuid.UUID | None = Field(
        description="Linked ExternalServiceCall UUID."
    )
    service_call: ServiceCallSummary | None = Field(
        default=None,
        description="LLM call metadata (joined from ExternalServiceCall).",
    )
    created_at: datetime = Field(description="When this snapshot was created.")


class StructureNodeResponse(BaseModel):
    """Recursive node in a generated course structure tree."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    node_type: str
    order: int
    title: str
    description: str | None = None
    learning_goal: str | None = None
    expected_knowledge: list[dict[str, str]] | None = None
    expected_skills: list[dict[str, str]] | None = None
    prerequisites: list[str] | None = None
    difficulty: str | None = None
    estimated_duration: int | None = None
    success_criteria: str | None = None
    assessment_method: str | None = None
    competencies: list[str] | None = None
    key_concepts: list[dict[str, str]] | None = None
    common_mistakes: list[str] | None = None
    teaching_strategy: str | None = None
    activities: list[str] | None = None
    teaching_style: str | None = None
    deep_dive_references: list[dict[str, Any]] | None = None
    timecodes: list[dict[str, Any]] | None = None
    slide_references: list[dict[str, Any]] | None = None
    web_references: list[dict[str, Any]] | None = None
    children: list[StructureNodeResponse] = Field(default_factory=list)


class SnapshotDetailResponse(SnapshotSummaryResponse):
    """Full snapshot including the generated structure."""

    structure: dict[str, Any] = Field(description="Raw CourseStructure JSON from LLM.")
    structure_tree: list[StructureNodeResponse] = Field(
        default_factory=list,
        description="Parsed structure as a recursive node tree.",
    )


class SnapshotListResponse(BaseModel):
    """Paginated list of structure snapshots (metadata only)."""

    items: list[SnapshotSummaryResponse] = Field(
        description="Snapshot summaries for the current page."
    )
    total: int = Field(description="Total number of snapshots for this node.")
    limit: int = Field(description="Maximum items per page (as requested).")
    offset: int = Field(description="Number of items skipped (as requested).")


# --- Editable Structure Nodes ---


class EditableNodeResponse(BaseModel):
    """Single editable structure node with edit-tracking metadata."""

    # populate_by_name=True required because the handler
    # _orm_to_response (editable.py:60-66) constructs a dict with
    # field-name keys and calls model_validate(dict). Dict input
    # doesn't trigger from_attributes alias resolution; both alias
    # and field-name must be accepted by Pydantic.
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    id: uuid.UUID
    course_node_id: uuid.UUID = Field(validation_alias="materialnode_id")
    source_snapshot_id: uuid.UUID | None = None
    source_structurenode_id: uuid.UUID | None = None
    node_type: str
    order: int
    title: str
    description: str | None = None
    learning_goal: str | None = None
    expected_knowledge: list[dict[str, str]] | None = None
    expected_skills: list[dict[str, str]] | None = None
    prerequisites: list[str] | None = None
    difficulty: str | None = None
    estimated_duration: int | None = None
    success_criteria: str | None = None
    assessment_method: str | None = None
    competencies: list[str] | None = None
    key_concepts: list[dict[str, str]] | None = None
    common_mistakes: list[str] | None = None
    teaching_strategy: str | None = None
    activities: list[str] | None = None
    teaching_style: str | None = None
    deep_dive_references: list[dict[str, Any]] | None = None
    timecodes: list[dict[str, Any]] | None = None
    slide_references: list[dict[str, Any]] | None = None
    web_references: list[dict[str, Any]] | None = None
    edited_fields: list[str] = Field(default_factory=list)
    children: list[EditableNodeResponse] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class EditableTreeResponse(BaseModel):
    """Full editable tree for a CourseNode."""

    course_node_id: uuid.UUID
    source_snapshot_id: uuid.UUID | None = None
    nodes: list[EditableNodeResponse]


class EditableNodeUpdateRequest(BaseModel):
    """Partial update for an editable structure node.

    Only provided fields are updated. Field names are
    auto-tracked in ``edited_fields``.
    """

    title: str | None = None
    description: str | None = None
    learning_goal: str | None = None
    expected_knowledge: list[dict[str, str]] | None = None
    expected_skills: list[dict[str, str]] | None = None
    prerequisites: list[str] | None = None
    difficulty: str | None = None
    estimated_duration: int | None = None
    success_criteria: str | None = None
    assessment_method: str | None = None
    competencies: list[str] | None = None
    key_concepts: list[dict[str, str]] | None = None
    common_mistakes: list[str] | None = None
    teaching_strategy: str | None = None
    activities: list[str] | None = None
    teaching_style: str | None = None
    deep_dive_references: list[dict[str, Any]] | None = None
    timecodes: list[dict[str, Any]] | None = None
    slide_references: list[dict[str, Any]] | None = None
    web_references: list[dict[str, Any]] | None = None


class EditableInitRequest(BaseModel):
    """Request to (re-)initialise editable tree from a snapshot."""

    snapshot_id: uuid.UUID | None = Field(
        default=None,
        description="Specific snapshot to init from. ``null`` = latest.",
    )
    preserve_edited: bool = Field(
        default=True,
        description="Carry over manually-edited field values.",
    )


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
        description="Material type (video, presentation, text).",
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


class ConfirmUploadRequest(BaseModel):
    """Request body for POST /nodes/{node_id}/documents/confirm-upload."""

    key: str = Field(
        min_length=1,
        max_length=2000,
        description="S3 object key returned by upload-url endpoint.",
    )
    source_type: SourceType = Field(
        description="Material type (video, presentation, text).",
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
        pattern=r"^[a-z]{2}$",
        description=(
            "Optional ISO 639-1 language override. When absent, the course "
            "default is used, and STT auto-detection is the final fallback."
        ),
        examples=["uk", "en"],
    )


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


# --- Reconciliation ---


class ReconciliationIssueResponse(BaseModel):
    """Single field-level issue detected during reconciliation."""

    id: uuid.UUID
    editable_node_id: uuid.UUID
    node_title: str
    field: str
    issue_type: str
    description: str
    current_value: Any = None
    suggested_value: Any = None
    reasoning: str


class ReconciliationPreviewResponse(BaseModel):
    """Full reconciliation preview result."""

    issues: list[ReconciliationIssueResponse]
    context_summary: str


class ReconciliationStatusResponse(BaseModel):
    """Reconciliation status for a node including freshness info."""

    has_preview: bool
    preview: ReconciliationPreviewResponse | None = None
    freshness: str = Field(
        description=("One of: fresh, stale_materials, stale_edited, stale_both, none"),
    )
    job_id: str | None = None
    job_status: str | None = None


class ReconcileApplyRequest(BaseModel):
    """Request to apply selected reconciliation issues."""

    accepted_issue_ids: list[uuid.UUID] = Field(
        description="IDs of issues the user accepted.",
    )
    issues: list[ReconciliationIssueResponse] = Field(
        description="Full issue objects (from preview response).",
    )
