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

**Pass 2a output offset invariants (fixup 2.1.7.1).** Beyond the
type-level checks, the schemas enforce structural invariants that
the Pass 2a prompt (``prompts/pass_2a_mapping/v1.md``) commits to:
``end_pos > start_pos`` per segment; segments order strictly
monotonic (0, 1, 2, ...); adjacency without gaps
(``prev.end_pos == next.start_pos``); full document coverage
(``segments[0].start_pos == 0`` and
``segments[-1].end_pos == content_char_count``). LLM JSON that
violates the invariants raises ``ValidationError`` at parse time;
the upstream ``StageRouter`` ladder treats that as a structural
failure and retries on the next rung. This prevents orphan
``DocumentSummary`` commits when only Pass 2a succeeds but Pass 2b
would reject the offsets (the failure mode that surfaced in C8
smoke A 2026-05-13).
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator, model_validator


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

    @field_validator("start_pos")
    @classmethod
    def _start_pos_non_negative(cls, value: int) -> int:
        if value < 0:
            raise ValueError(
                f"start_pos must be non-negative; got {value} "
                "(prompt rule: 0 <= start_pos < end_pos)"
            )
        return value

    @field_validator("order")
    @classmethod
    def _order_non_negative(cls, value: int) -> int:
        if value < 0:
            raise ValueError(
                f"order must be non-negative; got {value} "
                "(prompt rule: order = 0, 1, 2, ...)"
            )
        return value

    @model_validator(mode="after")
    def _end_pos_strictly_after_start_pos(self) -> DocumentSegmentDraft:
        if self.end_pos <= self.start_pos:
            raise ValueError(
                f"end_pos ({self.end_pos}) must be strictly greater than "
                f"start_pos ({self.start_pos}) for segment order={self.order}; "
                "prompt rule: 'end_pos must be > start_pos'"
            )
        return self


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

    @field_validator("content_char_count")
    @classmethod
    def _content_char_count_non_negative(cls, value: int) -> int:
        if value < 0:
            raise ValueError(f"content_char_count must be non-negative; got {value}")
        return value

    @model_validator(mode="after")
    def _segments_form_full_contiguous_cover(self) -> DocumentSummaryDraft:
        """Strict prompt-literal invariants over the segment sequence.

        Per ``prompts/pass_2a_mapping/v1.md``:

        * ``order`` runs ``0, 1, 2, ...`` without gaps.
        * Segments must not overlap and must not leave gaps
          (``prev.end_pos == next.start_pos``).
        * ``segments[0].start_pos == 0`` and
          ``segments[-1].end_pos == content_char_count`` — the cover
          spans the full document body the LLM analysed.

        Empty ``segments`` is allowed only when the document is
        trivially short (per prompt). In that case the only invariant
        is ``content_char_count >= 0`` enforced field-level above.
        """
        if not self.segments:
            return self

        orders = [seg.order for seg in self.segments]
        expected_orders = list(range(len(self.segments)))
        if orders != expected_orders:
            raise ValueError(
                f"segment order sequence must be strictly monotonic "
                f"0..{len(self.segments) - 1}; got {orders}"
            )

        if self.segments[0].start_pos != 0:
            raise ValueError(
                f"first segment must start at 0; got start_pos="
                f"{self.segments[0].start_pos}"
            )

        for prev, nxt in zip(self.segments, self.segments[1:], strict=False):
            if prev.end_pos != nxt.start_pos:
                raise ValueError(
                    f"segments must be contiguous without gaps or overlap; "
                    f"order={prev.order} end_pos={prev.end_pos} != "
                    f"order={nxt.order} start_pos={nxt.start_pos}"
                )

        last_end = self.segments[-1].end_pos
        if last_end != self.content_char_count:
            raise ValueError(
                f"content_char_count ({self.content_char_count}) must equal "
                f"last segment end_pos ({last_end}); full document coverage "
                "required per prompt"
            )
        return self
