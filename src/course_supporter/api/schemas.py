"""Request/response schemas for the API layer."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from course_supporter.models.course import TIMECODE_RE, SlideVideoMapEntry
from course_supporter.models.source import SourceType
from course_supporter.storage.orm import GenerationMode, MappingValidationState

# --- Slide-Video Mapping ---


class ValidationState(StrEnum):
    """Validation state for slide-video mappings (API-layer enum)."""

    VALIDATED = "validated"
    PENDING_VALIDATION = "pending_validation"
    VALIDATION_FAILED = "validation_failed"


class BlockingFactorResponse(BaseModel):
    """Reason why validation was deferred for a material entry."""

    type: str = Field(description="Blocking reason type, e.g. ``material_not_ready``.")
    material_entry_id: str = Field(
        description="UUID of the material entry that is not yet ready."
    )
    filename: str | None = Field(
        description="Original filename of the blocking material, if available."
    )
    material_state: str = Field(
        description="Current state of the blocking material entry."
    )
    message: str = Field(description="Human-readable explanation.")
    blocked_checks: list[str] = Field(
        description="List of validation checks that were skipped."
    )


class ValidationErrorResponse(BaseModel):
    """Single validation error with optional hint for fixing it."""

    field: str = Field(description="Name of the field that failed validation.")
    message: str = Field(description="What went wrong.")
    hint: str | None = Field(
        default=None, description="Suggested action to fix the error."
    )


class SlideVideoMapRequest(BaseModel):
    """Request body for POST /nodes/{node_id}/slide-mapping."""

    mappings: list[SlideVideoMapEntry] = Field(..., min_length=1)


class SlideVideoMapItemResponse(BaseModel):
    """Single slide-video mapping.

    Links a presentation slide to a video timecode range.
    Each mapping identifies a specific slide by number within a presentation
    and the corresponding time range in a video where that slide is discussed.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID = Field(description="Unique mapping identifier (UUIDv7).")
    materialnode_id: uuid.UUID = Field(
        description="Material tree node this mapping belongs to."
    )
    presentation_materialentry_id: uuid.UUID = Field(
        description="MaterialEntry ID of the presentation (PDF/PPTX)."
    )
    video_materialentry_id: uuid.UUID = Field(
        description="MaterialEntry ID of the video recording."
    )
    slide_number: int = Field(
        ge=1,
        description=(
            "1-based slide number within the presentation. "
            "Valid range depends on the actual number of slides in the file."
        ),
    )
    video_timecode_start: str = Field(
        pattern=TIMECODE_RE,
        description=(
            "Start timecode in the video (format ``HH:MM:SS`` or ``MM:SS``). "
            "Indicates when discussion of this slide begins."
        ),
    )
    video_timecode_end: str | None = Field(
        default=None,
        pattern=TIMECODE_RE,
        description=(
            "End timecode in the video (format ``HH:MM:SS`` or ``MM:SS``). "
            "``null`` if the slide extends to the next mapping or end of video."
        ),
    )
    validation_state: ValidationState = Field(
        description="Current validation status of this mapping.",
    )
    blocking_factors: list[BlockingFactorResponse] | None = Field(
        default=None,
        description=(
            "Present only when ``validation_state`` is ``pending_validation``."
        ),
    )
    validation_errors: list[ValidationErrorResponse] | None = Field(
        default=None,
        description=(
            "Present only when ``validation_state`` is ``validation_failed``."
        ),
    )
    validated_at: datetime | None = Field(
        description=(
            "Timestamp of successful validation. "
            "``null`` if not yet validated or validation failed."
        ),
    )
    created_at: datetime = Field(description="When this mapping was created.")


class SlideVideoMapListResponse(BaseModel):
    """List of slide-video mappings for a material tree node."""

    items: list[SlideVideoMapItemResponse] = Field(
        description="Mappings ordered by slide number."
    )
    total: int = Field(description="Total number of mappings for this node.")


class RejectedMappingResponse(BaseModel):
    """Single rejected mapping with errors and hints."""

    index: int
    errors: list[dict[str, str | None]]


class SkippedMappingResponse(BaseModel):
    """Duplicate mapping that was skipped."""

    index: int
    hint: str


class SlideVideoMapResponse(BaseModel):
    """Response for slide-video mapping batch creation."""

    created: int
    skipped: int
    failed: int
    mappings: list[SlideVideoMapItemResponse]
    skipped_items: list[SkippedMappingResponse]
    rejected: list[RejectedMappingResponse]
    hints: dict[str, str]


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


class NodeMoveRequest(BaseModel):
    """Request body for moving a node within the tree.

    Move a node to a new parent (or to root by setting ``parent_materialnode_id``
    to ``null``). Cycle detection is enforced server-side.

    Example::

        {"parent_materialnode_id": "019c707f-73b8-7b53-ba02-0e7be1c89189"}
    """

    parent_materialnode_id: uuid.UUID | None = Field(
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
    parent_materialnode_id: uuid.UUID | None = Field(
        description="Parent node ID, or ``null`` for root nodes."
    )
    title: str = Field(description="Node title.")
    description: str | None = Field(description="Optional node description.")
    learning_goal: str | None = Field(
        default=None, description="Learning goal for this node."
    )
    expected_knowledge: list[str] | None = Field(
        default=None, description="Expected knowledge items."
    )
    expected_skills: list[str] | None = Field(
        default=None, description="Expected skills items."
    )
    order: int = Field(description="0-based position among siblings.")
    node_fingerprint: str | None = Field(
        description="Merkle hash of this node's content. ``null`` if not computed."
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
    parent_materialnode_id: uuid.UUID | None = Field(
        description="Parent node ID, or ``null`` for root nodes."
    )
    title: str = Field(description="Node title.")
    description: str | None = Field(description="Optional node description.")
    learning_goal: str | None = Field(
        default=None, description="Learning goal for this node."
    )
    expected_knowledge: list[str] | None = Field(
        default=None, description="Expected knowledge items."
    )
    expected_skills: list[str] | None = Field(
        default=None, description="Expected skills items."
    )
    order: int = Field(description="0-based position among siblings.")
    node_fingerprint: str | None = Field(
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

    Root nodes (parent_materialnode_id IS NULL) serve as top-level entities.
    """

    items: list[NodeResponse] = Field(description="Root nodes for the current page.")
    total: int = Field(description="Total number of root nodes (across all pages).")
    limit: int = Field(description="Maximum items per page (as requested).")
    offset: int = Field(description="Number of items skipped (as requested).")


# --- Material Entries ---


class MaterialEntrySummaryResponse(BaseModel):
    """Compact material entry within the tree detail.

    A lighter version of ``MaterialEntryResponse`` omitting
    ``job_id`` and ``updated_at`` to keep the tree
    payload concise. Includes the derived ``state`` field.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID = Field(description="Unique entry identifier (UUIDv7).")
    source_type: str = Field(
        description="Material type: ``video``, ``presentation``, ``text``, or ``web``."
    )
    source_url: str = Field(description="URL or S3 path to the raw material.")
    filename: str | None = Field(description="Original filename, if available.")
    order: int = Field(description="0-based position among sibling materials.")
    state: str = Field(
        description=(
            "Derived lifecycle state: "
            "``raw``, ``pending``, ``ready``, ``integrity_broken``, or ``error``."
        ),
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
    learning_goal: str | None = Field(
        default=None, description="Learning goal for this node."
    )
    expected_knowledge: list[str] | None = Field(
        default=None, description="Expected knowledge items."
    )
    expected_skills: list[str] | None = Field(
        default=None, description="Expected skills items."
    )
    order: int = Field(description="0-based position among siblings.")
    node_fingerprint: str | None = Field(
        description="Merkle hash of this node's content. ``null`` if not computed."
    )
    materials: list[MaterialEntrySummaryResponse] = Field(
        default_factory=list,
        description="Materials attached directly to this node.",
    )
    children: list[NodeWithMaterialsResponse] = Field(
        default_factory=list,
        description="Child nodes, recursively nested.",
    )
    created_at: datetime = Field(description="When this node was created.")
    updated_at: datetime = Field(description="When this node was last modified.")


class MaterialEntryCreateRequest(BaseModel):
    """Request body for adding a material to a tree node."""

    source_type: SourceType = Field(
        ...,
        description=(
            "Type of the source material. "
            "Must be one of: ``video``, ``presentation``, ``text``, ``web``."
        ),
        examples=["video", "presentation", "text", "web"],
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


class MaterialEntryResponse(BaseModel):
    """Response schema for a single material entry."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID = Field(description="Unique entry identifier (UUIDv7).")
    materialnode_id: uuid.UUID = Field(
        description="Parent node this material belongs to."
    )
    source_type: str = Field(
        description="Material type: ``video``, ``presentation``, ``text``, or ``web``."
    )
    source_url: str = Field(description="URL or S3 path to the raw material.")
    filename: str | None = Field(description="Original filename, if available.")
    order: int = Field(description="0-based position among sibling materials.")
    state: str = Field(
        description=(
            "Derived lifecycle state: "
            "``raw``, ``pending``, ``ready``, ``integrity_broken``, or ``error``."
        ),
    )
    error_message: str | None = Field(
        description="Error message from the last failed processing attempt, if any."
    )
    job_id: uuid.UUID | None = Field(
        description="Job ID currently processing this material, or ``null``."
    )
    created_at: datetime = Field(description="When this entry was created.")
    updated_at: datetime = Field(description="When this entry was last modified.")


class MaterialEntryCreateResponse(BaseModel):
    """Response for material entry creation.

    Extends the base response with ``job_id`` — the ID of the
    ingestion job that was auto-enqueued.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID = Field(description="Unique entry identifier (UUIDv7).")
    materialnode_id: uuid.UUID = Field(
        description="Parent node this material belongs to."
    )
    source_type: str = Field(
        description="Material type: ``video``, ``presentation``, ``text``, or ``web``."
    )
    source_url: str = Field(description="URL or S3 path to the raw material.")
    filename: str | None = Field(description="Original filename, if available.")
    order: int = Field(description="0-based position among sibling materials.")
    state: str = Field(
        description="Derived lifecycle state (will be ``raw`` or ``pending``)."
    )
    job_id: uuid.UUID | None = Field(
        default=None,
        description="ID of the auto-enqueued ingestion job for progress tracking.",
    )
    warnings: list[str] = Field(
        default_factory=list,
        description="Non-fatal warnings (e.g. unverified platform).",
    )
    created_at: datetime = Field(description="When this entry was created.")


# --- Jobs ---


class JobResponse(BaseModel):
    """Response for GET /jobs/{job_id}."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    job_type: str
    priority: str
    status: str
    tenant_id: uuid.UUID | None
    materialnode_id: uuid.UUID | None
    arq_job_id: str | None
    error_message: str | None
    queued_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    estimated_at: datetime | None


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


class MappingWarningResponse(BaseModel):
    """Warning about a slide-video mapping with problematic validation state."""

    model_config = ConfigDict(from_attributes=True)

    mapping_id: uuid.UUID = Field(description="SlideVideoMapping UUID.")
    materialnode_id: uuid.UUID = Field(description="Parent MaterialNode UUID.")
    slide_number: int = Field(description="Slide number in the presentation.")
    validation_state: MappingValidationState = Field(
        description="Validation state of the mapping.",
    )


class GenerationPlanResponse(BaseModel):
    """Response for POST /nodes/{node_id}/generate."""

    generation_jobs: list[JobResponse] = Field(
        default_factory=list,
        description="Per-node generation jobs in bottom-up DAG order.",
    )
    ingestion_jobs: list[JobResponse] = Field(
        default_factory=list,
        description="Ingestion jobs enqueued for stale materials before generation.",
    )
    estimated_llm_calls: int = Field(
        default=0,
        description="Total LLM calls expected for this plan.",
    )
    mapping_warnings: list[MappingWarningResponse] = Field(
        default_factory=list,
        description=(
            "Slide-video mappings with problematic "
            "validation states in the target subtree."
        ),
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
    materialnode_id: uuid.UUID = Field(description="Target node for this snapshot.")
    mode: GenerationMode = Field(description="Generation mode: ``free`` or ``guided``.")
    node_fingerprint: str = Field(
        description="Merkle fingerprint of the target subtree at generation time."
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
