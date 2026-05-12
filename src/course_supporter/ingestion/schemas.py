"""Draft Pydantic models for the ingestion pipeline (KD-2.1-M).

In-flight data carriers between processor stages and repository
``.create()`` calls. Distinct from ORM models in
:mod:`course_supporter.storage.orm`: drafts are short-lived
Pydantic validation containers; ORM models are session-bound
persistence entities with content_hash, lifecycle status, and
soft-delete state.

Phase 2.1 commit 4.5 introduces these drafts as the typed return
shape for :meth:`MaterialProcessor.process_macro` (Pass 2a) and
:meth:`MaterialProcessor.process_detail` (Pass 2b). Concrete
processor overrides (TextProcessor / WebProcessor) arrive in
commits 5 and 7; ORM materialisation through
``DocumentSummaryRepository.create`` arrives in commit 5.

All non-list fields are required (no Optional). LLM callers must
emit complete payloads; partial output fails fast through Pydantic
validation rather than silently propagating None into ORM rows.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class DocumentSegmentDraft(BaseModel):
    """In-flight segment draft (Pass 2a metadata + Pass 2b content, pre-ORM).

    Per KD-2.1-O (ratified 2026-05-12), for text/web source types Pass 2a
    emits only structural metadata (offsets, title, description, concepts);
    ``content`` is left ``None`` and populated algorithmically in Pass 2b
    (C7) as ``doc.text[start_pos:end_pos]``. For audio/video, ``content``
    may be filled by the LLM in Pass 2a directly (final decision deferred
    to Phase 2.3 / 2.5).

    The ORM ``DocumentSegment`` row requires ``content`` non-null at
    INSERT time — that invariant is preserved by Pass 2b before
    ``DocumentSegmentRepository.create``.
    """

    order: int = Field(description="0-indexed position within parent summary.")
    start_pos: int = Field(
        description="Absolute start char offset in the source unit.",
    )
    end_pos: int = Field(
        description="Absolute end char offset (must be > start_pos).",
    )
    title: str | None = Field(
        default=None,
        description="Optional short heading for this segment.",
    )
    description: str = Field(
        description=(
            "1-2 sentence description of what this segment covers "
            "(not a paraphrase of its content)."
        ),
    )
    main_concepts: list[str] = Field(
        default_factory=list,
        description="Concept strings taught in this segment (KD-gamma).",
    )
    secondary_concepts: list[str] = Field(
        default_factory=list,
        description="Concept strings mentioned but not taught in this segment.",
    )
    content: str | None = Field(
        default=None,
        description=(
            "``None`` for text/web (KD-2.1-O): Pass 2b fills it from "
            "``doc.text[start_pos:end_pos]``. For audio/video, may be "
            "populated by the LLM in Pass 2a."
        ),
    )


class DocumentSummaryDraft(BaseModel):
    """In-flight summary draft (Pass 2a output, pre-ORM)."""

    title: str = Field(description="Self-contained summary title (<=128 chars).")
    description: str = Field(description="Brief description (<=512 chars).")
    main_concepts: list[str] = Field(
        default_factory=list,
        description="Concept strings taught in depth across the document (KD-gamma).",
    )
    secondary_concepts: list[str] = Field(
        default_factory=list,
        description="Concept strings mentioned but not taught in depth.",
    )
    content_char_count: int = Field(
        description=(
            "Total char count over child DocumentSegment.content "
            "(size-metric for weighting per vision section 2.2)."
        ),
    )
    segments: list[DocumentSegmentDraft] = Field(
        default_factory=list,
        description=(
            "Child segment drafts. Pass 2a may emit empty list; "
            "Pass 2b appends drafts before ORM materialisation."
        ),
    )
