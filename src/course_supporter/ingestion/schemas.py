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
(``prev.end_pos == next.start_pos``); ``segments[0].start_pos == 0``.
LLM JSON that violates the invariants raises ``ValidationError`` at
parse time; this prevents orphan ``DocumentSummary`` commits when
only Pass 2a succeeds but Pass 2b would reject the offsets.

**``content_char_count`` server-side derivation (fixup 2.1.7.2).** The
LLM no longer emits ``content_char_count``; the field was a vector
for anchor-bias drift on the calibration examples (3 datapoints
2026-05-13: DeepSeek V4 Flash echoed Example 3's 6000-char anchor
on a 10017-char Ukrainian document). The total-cover invariant
``segments[-1].end_pos == reference_text_length`` is now asserted
inside ``DocumentSummaryDraft._coverage_matches_reference_length``,
which reads the deterministic server-derived length from Pydantic
``ValidationInfo.context`` (key ``reference_text_length``).
``api/tasks.py`` passes ``len(doc.assemble_text())`` into the
context via the StageRouter's ``response_validator`` closure; a
:class:`pydantic.ValidationError` raised inside that closure is
translated to :class:`StructuralRetryError` so the StageRouter
ladder retry mechanism (KD16) can attempt instructor-style retry
and fall through to alternative providers before declaring final
failure. Architectural principle: never ask the LLM for quantities
we can compute deterministically.

The coverage check is gated on context presence -- callers that
construct drafts without the context (existing unit-test fixtures,
ad-hoc tooling) bypass the cover assertion. The structural offset
invariants (order monotonic, contiguous, first.start_pos == 0)
still run unconditionally.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, ValidationInfo, field_validator, model_validator


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
    """In-flight summary draft (Pass 2a output, pre-ORM).

    Per fixup 2.1.7.2 (ratified 2026-05-13), ``content_char_count`` is
    NOT a field of this Pydantic carrier: it is derived server-side
    from ``len(reference_text)`` in ``api/tasks.py`` and forwarded to
    the repository at INSERT time. The LLM is never asked for the
    document length (anchor-bias mitigation per Etap 0 forensic).
    """

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
    segments: list[DocumentSegmentDraft] = Field(
        default_factory=list,
        description=(
            "Child segment drafts. Pass 2a may emit empty list; "
            "Pass 2b appends drafts before ORM materialisation."
        ),
    )

    @model_validator(mode="after")
    def _segments_form_contiguous_cover(self) -> DocumentSummaryDraft:
        """Strict prompt-literal invariants over the segment sequence.

        Per ``prompts/pass_2a_mapping/v1.md``:

        * ``order`` runs ``0, 1, 2, ...`` without gaps.
        * Segments must not overlap and must not leave gaps
          (``prev.end_pos == next.start_pos``).
        * ``segments[0].start_pos == 0``.

        Full-cover invariant (``segments[-1].end_pos ==
        reference_text_length``) is enforced separately by
        :meth:`_coverage_matches_reference_length`, which reads the
        deterministic server-derived length from Pydantic
        ``ValidationInfo.context``. Empty ``segments`` is allowed
        when the document is trivially short.
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

        return self

    @model_validator(mode="after")
    def _coverage_matches_reference_length(
        self, info: ValidationInfo
    ) -> DocumentSummaryDraft:
        """Full-cover invariant gated on Pydantic context.

        When parsed via
        ``DocumentSummaryDraft.model_validate_json(content,
        context={"reference_text_length": N})``, asserts that
        ``segments[-1].end_pos == N``. The caller (process_macro
        closure passed as StageRouter ``response_validator``)
        translates the resulting ``ValidationError`` into
        :class:`StructuralRetryError`, so coverage mismatch drives
        the existing instructor-style retry + ladder fallback.

        Skipped silently when context is ``None`` or
        ``reference_text_length`` is absent (unit-test fixtures,
        ad-hoc tooling). Empty ``segments`` also skips the check --
        the prompt allows a trivially short document to produce no
        segments, and the upstream Pass 2a output is then expected
        to surface zero-segment summaries through the parent
        invariants in :meth:`_segments_form_contiguous_cover`.
        """
        if not self.segments:
            return self
        context = info.context
        if context is None:
            return self
        expected_length = context.get("reference_text_length")
        if expected_length is None:
            return self
        actual_last_end_pos = self.segments[-1].end_pos
        if actual_last_end_pos != expected_length:
            raise ValueError(
                f"segments do not cover the reference text exactly: "
                f"last segment end_pos={actual_last_end_pos} != "
                f"reference_text_length={expected_length} "
                f"(segment_count={len(self.segments)}); "
                "prompt rule: 'last segment.end_pos must equal "
                "the total document character count'"
            )
        return self
