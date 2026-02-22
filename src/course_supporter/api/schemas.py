"""Request/response schemas for the API layer."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from course_supporter.models.course import SlideVideoMapEntry
from course_supporter.models.source import SourceType

# --- Course ---


class CourseCreateRequest(BaseModel):
    """Request body for POST /courses."""

    title: str = Field(..., min_length=1, max_length=500)
    description: str | None = None


class CourseResponse(BaseModel):
    """Response for course creation and listing."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    description: str | None
    created_at: datetime
    updated_at: datetime


class CourseListResponse(BaseModel):
    """Paginated response for ``GET /courses``.

    Contains a list of courses and pagination metadata.
    Use ``limit`` and ``offset`` query parameters to page
    through results.

    Example::

        {
            "items": [{"id": "...", "title": "Python 101", ...}],
            "total": 42,
            "limit": 20,
            "offset": 0
        }
    """

    items: list[CourseResponse] = Field(
        description="List of courses for the current page."
    )
    total: int = Field(
        description="Total number of courses matching the query (across all pages)."
    )
    limit: int = Field(description="Maximum items per page (as requested).")
    offset: int = Field(description="Number of items skipped (as requested).")


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
    """Request body for POST /courses/{id}/nodes/{node_id}/slide-mapping."""

    mappings: list[SlideVideoMapEntry] = Field(..., min_length=1)


class SlideVideoMapItemResponse(BaseModel):
    """Single slide-video mapping.

    Links a presentation slide to a video timecode range.
    Each mapping identifies a specific slide by number within a presentation
    and the corresponding time range in a video where that slide is discussed.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID = Field(description="Unique mapping identifier (UUIDv7).")
    node_id: uuid.UUID = Field(
        description="Material tree node this mapping belongs to."
    )
    presentation_entry_id: uuid.UUID = Field(
        description="MaterialEntry ID of the presentation (PDF/PPTX)."
    )
    video_entry_id: uuid.UUID = Field(
        description="MaterialEntry ID of the video recording."
    )
    slide_number: int = Field(
        description=(
            "1-based slide number within the presentation. "
            "Valid range depends on the actual number of slides in the file."
        ),
    )
    video_timecode_start: str = Field(
        description=(
            "Start timecode in the video (format ``HH:MM:SS`` or ``MM:SS``). "
            "Indicates when discussion of this slide begins."
        ),
    )
    video_timecode_end: str | None = Field(
        description=(
            "End timecode in the video (format ``HH:MM:SS`` or ``MM:SS``). "
            "``null`` if the slide extends to the next mapping or end of video."
        ),
    )
    order: int = Field(description="0-based position within this node's mapping list.")
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
    """List of slide-video mappings for a material tree node.

    Each mapping links a specific slide (by number) in a presentation
    to a timecode range in a video. Returned sorted by ``order``
    (ascending, 0-based). An empty ``items`` list (with ``total: 0``)
    is returned when the node has no mappings — this is not an error.
    """

    items: list[SlideVideoMapItemResponse] = Field(
        description="Mappings ordered by ``order`` (0-based)."
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


# --- Course Detail (nested) ---


class ExerciseResponse(BaseModel):
    """Exercise within a lesson."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    description: str
    reference_solution: str | None
    grading_criteria: str | None
    difficulty_level: int | None


class ConceptResponse(BaseModel):
    """Concept card within a lesson."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    definition: str
    examples: list[str] | None
    timecodes: list[str] | None
    slide_references: list[int] | None
    web_references: list[dict[str, str]] | None


class LessonResponse(BaseModel):
    """Lesson within a module."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    order: int
    video_start_timecode: str | None
    video_end_timecode: str | None
    slide_range: dict[str, int] | None
    concepts: list[ConceptResponse]
    exercises: list[ExerciseResponse]


class ModuleResponse(BaseModel):
    """Module within a course."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    description: str | None
    learning_goal: str | None
    difficulty: str | None
    order: int
    lessons: list[LessonResponse]


class SourceMaterialResponse(BaseModel):
    """Source material attached to a course."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    source_type: str
    source_url: str
    filename: str | None
    status: str
    created_at: datetime


class MaterialEntrySummaryResponse(BaseModel):
    """Compact material entry within the course detail tree.

    A lighter version of ``MaterialEntryResponse`` omitting
    ``pending_job_id`` and ``updated_at`` to keep the tree
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
    content_fingerprint: str | None = Field(
        description="SHA-256 of processed content. ``null`` if not computed."
    )
    created_at: datetime = Field(description="When this entry was created.")


class NodeWithMaterialsResponse(BaseModel):
    """Recursive tree node with attached materials.

    Used in ``CourseDetailResponse.material_tree`` to provide
    the full hierarchical view including materials at each level.

    Example (abbreviated)::

        {
            "title": "Module 1",
            "materials": [{"source_type": "video", "state": "ready", ...}],
            "children": [
                {"title": "Lesson 1.1", "materials": [...], "children": []}
            ]
        }
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID = Field(description="Unique node identifier (UUIDv7).")
    title: str = Field(description="Node title.")
    description: str | None = Field(description="Optional node description.")
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


class CourseDetailResponse(BaseModel):
    """Full course detail with nested structure.

    Includes the legacy ``source_materials`` flat list (for backward
    compatibility) and the new ``material_tree`` with recursive nodes,
    attached materials, and derived states.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    description: str | None
    learning_goal: str | None
    created_at: datetime
    updated_at: datetime
    modules: list[ModuleResponse]
    source_materials: list[SourceMaterialResponse]
    course_fingerprint: str | None = Field(
        default=None,
        description=(
            "Merkle hash of the entire material tree. "
            "``null`` if any node fingerprint is missing."
        ),
    )
    material_tree: list[NodeWithMaterialsResponse] = Field(
        default_factory=list,
        description=(
            "Hierarchical material tree with nested children and "
            "attached materials. Empty list if no tree has been built."
        ),
    )


class LessonDetailResponse(BaseModel):
    """Lesson detail with concepts and exercises."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    order: int
    video_start_timecode: str | None
    video_end_timecode: str | None
    slide_range: dict[str, int] | None
    concepts: list[ConceptResponse]
    exercises: list[ExerciseResponse]


# --- Materials ---


class MaterialCreateResponse(BaseModel):
    """Response for material creation."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    source_type: str
    source_url: str
    filename: str | None
    status: str
    created_at: datetime
    job_id: uuid.UUID | None = None


# --- Jobs ---


class JobResponse(BaseModel):
    """Response for GET /jobs/{job_id}."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    job_type: str
    priority: str
    status: str
    course_id: uuid.UUID | None
    node_id: uuid.UUID | None
    arq_job_id: str | None
    error_message: str | None
    queued_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    estimated_at: datetime | None


# --- Material Tree Nodes ---


class NodeCreateRequest(BaseModel):
    """Request body for creating a material tree node.

    Used by both root node creation (``POST /courses/{id}/nodes``)
    and child node creation (``POST /courses/{id}/nodes/{node_id}/children``).

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
    course_id: uuid.UUID = Field(description="Course this node belongs to.")
    parent_id: uuid.UUID | None = Field(
        description="Parent node ID, or ``null`` for root nodes."
    )
    title: str = Field(description="Node title.")
    description: str | None = Field(description="Optional node description.")
    order: int = Field(description="0-based position among siblings.")
    node_fingerprint: str | None = Field(
        description="Merkle hash of this node's content. ``null`` if not computed."
    )
    created_at: datetime = Field(description="When this node was created.")
    updated_at: datetime = Field(description="When this node was last modified.")


class NodeTreeResponse(BaseModel):
    """Recursive tree node with nested children.

    Returned by ``GET /courses/{id}/nodes/tree``. Each node
    contains its children, forming a full tree structure.

    Example response::

        [
            {
                "id": "...",
                "title": "Module 1",
                "children": [
                    {
                        "id": "...",
                        "title": "Lesson 1.1",
                        "children": []
                    }
                ]
            }
        ]
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID = Field(description="Unique node identifier (UUIDv7).")
    course_id: uuid.UUID = Field(description="Course this node belongs to.")
    parent_id: uuid.UUID | None = Field(
        description="Parent node ID, or ``null`` for root nodes."
    )
    title: str = Field(description="Node title.")
    description: str | None = Field(description="Optional node description.")
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


# --- Material Entries ---


class MaterialEntryCreateRequest(BaseModel):
    """Request body for adding a material to a tree node.

    Creates a ``MaterialEntry`` under the specified node and
    auto-enqueues ingestion. The response includes the ``job_id``
    for tracking progress via ``GET /jobs/{job_id}``.

    Example::

        {
            "source_type": "presentation",
            "source_url": "https://example.com/slides.pdf",
            "filename": "slides.pdf"
        }
    """

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
        description=(
            "URL or S3 path to the raw material. "
            "For file uploads, this is populated by the upload endpoint."
        ),
        examples=["https://example.com/slides.pdf", "s3://bucket/key"],
    )
    filename: str | None = Field(
        default=None,
        max_length=500,
        description=(
            "Original filename for display purposes. Optional for URL-based materials."
        ),
        examples=["slides.pdf", "lecture-01.mp4"],
    )


class MaterialEntryResponse(BaseModel):
    """Response schema for a single material entry.

    The ``state`` field is a derived property computed from the entry's
    internal fields (see ``MaterialState`` enum for possible values):

    - ``raw`` — uploaded but not yet processed
    - ``pending`` — ingestion job is in progress
    - ``ready`` — processed successfully, hashes match
    - ``integrity_broken`` — raw source changed after processing
    - ``error`` — last processing attempt failed

    Returned by list and detail operations.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID = Field(description="Unique entry identifier (UUIDv7).")
    node_id: uuid.UUID = Field(description="Parent node this material belongs to.")
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
    content_fingerprint: str | None = Field(
        description="SHA-256 of processed content. ``null`` if not computed."
    )
    pending_job_id: uuid.UUID | None = Field(
        description="Job ID currently processing this material, or ``null``."
    )
    created_at: datetime = Field(description="When this entry was created.")
    updated_at: datetime = Field(description="When this entry was last modified.")


class MaterialEntryCreateResponse(BaseModel):
    """Response for material entry creation.

    Extends the base response with ``job_id`` — the ID of the
    ingestion job that was auto-enqueued. Use
    ``GET /api/v1/jobs/{job_id}`` to track processing status.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID = Field(description="Unique entry identifier (UUIDv7).")
    node_id: uuid.UUID = Field(description="Parent node this material belongs to.")
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
    created_at: datetime = Field(description="When this entry was created.")
