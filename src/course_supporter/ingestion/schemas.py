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

from typing import Any

from pydantic import BaseModel, Field, ValidationInfo, field_validator, model_validator

from course_supporter.concept_dedup import dedupe_concepts


class VisualSceneRef(BaseModel):
    """One time-anchored visual description attached to a video segment.

    Minimal draft carrier for the visual stream of a video segment
    (task 2.4.6): each ref is a Pass 1 frame description (the ``text``
    of a ``VISUAL_SCENE`` chunk) plus the temporal/scene metadata that
    chunk carried (B', task 2.4.5). It is intentionally *not* a
    ``ContentChunk`` — that is the heavyweight assembled-document type;
    this is a per-segment draft field materialised into the
    ``DocumentSegment.visual_content`` JSONB column at Pass 2b.

    Refs are kept in temporal (frame) order within a segment — that
    order is meaningful and is preserved through the ORM and the
    ``content_hash`` formula (it is NOT sorted, unlike concept sets).
    """

    position_ms: int = Field(
        description="Frame timestamp in ms from the start of the video "
        "(``VISUAL_SCENE`` chunk ``metadata.frame_position_ms``).",
    )
    description: str = Field(
        description="Pass 1 visual description of the frame (the chunk text).",
    )
    kind: str = Field(
        default="",
        description="Frame role (e.g. ``anchor`` / ``diff``) from the chunk "
        "``metadata.kind``; empty string when unknown.",
    )
    scene_id: int = Field(
        default=0,
        description="Scene grouping id (``VISUAL_SCENE`` chunk "
        "``metadata.scene_id``) so consecutive frames of one scene can be "
        "grouped downstream.",
    )


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
    is_auxiliary: bool = Field(
        default=False,
        description=(
            "№21 (decision 5): True when the author marked this file auxiliary "
            "(its concepts merge into the material's secondary only); False for "
            "the central/main role. Written from the same file-role decision as "
            "the structure-tree role."
        ),
    )
    content: str | None = Field(
        default=None,
        description=(
            "``None`` for text/web (KD-2.1-O): Pass 2b fills it from "
            "``doc.text[start_pos:end_pos]``. For audio/video, may be "
            "populated by the LLM in Pass 2a."
        ),
    )
    start_time_sec: float | None = Field(
        default=None,
        description=(
            "Optional segment start time in seconds (KD-2.2-F). "
            "Populated for audio/video source types via the Pass 2b "
            "char-offset bridge; ``None`` for text/web."
        ),
    )
    end_time_sec: float | None = Field(
        default=None,
        description=(
            "Optional segment end time in seconds (KD-2.2-F). "
            "Populated for audio/video source types via the Pass 2b "
            "char-offset bridge; ``None`` for text/web."
        ),
    )
    start_slide: int | None = Field(
        default=None,
        description=(
            "Optional 1-indexed inclusive slide number where the "
            "segment begins (KD-2.3-Q). Populated for the presentation "
            "source type via the Pass 2b slide-boundary bridge; "
            "``None`` for audio/text/web/video. Preserved even when "
            "the slide's chunk was filtered out by the empty-text "
            "guard (KD-2.3-O / v0.3 N3), so downstream UI can still "
            "address the original slide number."
        ),
    )
    end_slide: int | None = Field(
        default=None,
        description=(
            "Optional 1-indexed inclusive slide number where the "
            "segment ends (KD-2.3-Q). Populated for the presentation "
            "source type via the Pass 2b slide-boundary bridge; "
            "``None`` for audio/text/web/video."
        ),
    )
    start_paragraph: int | None = Field(
        default=None,
        description=(
            "Optional 0-indexed inclusive paragraph ordinal where the "
            "segment begins (Phase 3.3a). Populated for text/web via the "
            "Pass 2b paragraph-boundary bridge; counts only "
            "PARAGRAPH (text) / WEB_CONTENT (web) chunks — headings are "
            "skipped. ``None`` for audio/video/presentation."
        ),
    )
    end_paragraph: int | None = Field(
        default=None,
        description=(
            "Optional 0-indexed inclusive paragraph ordinal where the "
            "segment ends (Phase 3.3a). Populated for text/web via the "
            "Pass 2b paragraph-boundary bridge; ``None`` for "
            "audio/video/presentation."
        ),
    )
    file_path: str | None = Field(
        default=None,
        description=(
            "Canonical POSIX-relative source-file path — the fourth "
            "positional-anchor kind (task-code-materials): one included "
            "file = one segment, so a single column, not a start/end "
            "pair. Populated for the code source type in Pass 2b; "
            "``None`` for text/web/audio/video/presentation."
        ),
    )
    noisy: bool = Field(
        default=False,
        description=(
            "True if Pass 2a flagged the segment as containing speech "
            "disfluencies (audio source_type, KD-2.2-F v0.4); routes "
            "Pass 2c selective denoise in "
            "``AudioProcessor.process_detail`` and drives the UI "
            "noisy-badge per KD-2.2-J item 5. Default ``False`` for "
            "text / web / video — non-audio source_types preserve "
            "backward-compat without caller changes."
        ),
    )
    visual_content: list[VisualSceneRef] | None = Field(
        default=None,
        description=(
            "Time-anchored visual descriptions for this segment (task "
            "2.4.6, video source_type only). Populated at Pass 2b by the "
            "start-time partition of ``VISUAL_SCENE`` chunks over the "
            "segment timeline; ``None`` for audio / text / web / "
            "presentation (the second authored stream exists only for "
            "video). Refs are kept in temporal order; materialised into "
            "the ``DocumentSegment.visual_content`` JSONB column."
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

    @model_validator(mode="after")
    def _dedupe_concept_spellings(self) -> DocumentSegmentDraft:
        """Collapse spelling variants within each concept list (PHASE-1 lever 1).

        This runs at segment-draft build time — the single point that covers
        all six pipelines: direct construction (code / presentation / audio /
        video) and nested validation via ``model_validate_json`` of the
        document draft (text / web). ``main_concepts`` and
        ``secondary_concepts`` are consolidated independently, because a
        concept may be main in one segment and secondary in another. Verbatim
        winners in first-seen order — no sorting here; the segment write path
        stays deliberately literal (PHASE-1 §3.2). Assigning inside an
        ``after`` validator is part of construction and does not require
        ``validate_assignment``.
        """
        self.main_concepts = dedupe_concepts(self.main_concepts)
        self.secondary_concepts = dedupe_concepts(self.secondary_concepts)
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
    structure: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Code-material project tree (task-code-materials): entries "
            "with path / cls / size + exclusion reason. SERVER-SIDE only "
            "— never LLM-emitted; CodeProcessor sets it from the "
            "extraction manifest after parsing, and ``api/tasks.py`` "
            "threads it into ``DocumentSummaryRepository.create``. "
            "``None`` for every non-code source type."
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


# Audio source-type schemas (Phase 2.2, KD-2.2-H + KD-2.2-I).
#
# These models live alongside the text/web ``DocumentSegmentDraft`` +
# ``DocumentSummaryDraft`` above. The audio pipeline emits Pass 2a
# segment metadata via :class:`AudioPass2aResult`; Pass 2b
# algorithmically materialises char offsets via the cumulative-word
# bridge (KD-2.2-F, helper lives in ``ingestion/audio.py``); Pass 2c
# selectively denoises noisy segments via the single-string output
# pattern of :class:`AudioPass2cResult` (KD-2.2-I).
#
# Depth is enforced through the Python type system (KD-2.2-H):
# :class:`AudioSubsegmentDraft` is a leaf class without a
# ``subsegments`` field, so the LLM cannot emit a third-level
# nesting via JSON. ``order`` fields are deliberately absent on
# audio classes — position is implicit via ``enumerate(...)`` and
# the LLM hallucination surface is reduced.
#
# STTWord (the STT-domain primitive consumed by Pass 2a via the
# ``word_idx`` bridge of KD-2.2-F) lives canonically in
# :mod:`course_supporter.stt.schemas` per KD-2.2-C.


class AudioSubsegmentDraft(BaseModel):
    """Leaf audio segment (Pass 2a output, KD-2.2-H).

    No ``subsegments`` field — the Python type system enforces the
    2-level maximum depth (KD-2.2-H: third-level nesting is
    rejected at parse time, not at validator time). No ``order``
    field — position is implicit via
    ``enumerate(parent.subsegments)``.
    """

    start_word_idx: int = Field(
        description=("Inclusive start word index into the ``STTResult.words`` stream."),
    )
    end_word_idx: int = Field(
        description=(
            "Exclusive end word index into the ``STTResult.words`` "
            "stream (must be > ``start_word_idx``)."
        ),
    )
    title: str | None = Field(
        default=None,
        description="Optional short heading for this subsegment.",
    )
    description: str = Field(
        description=("1-2 sentence description of what this subsegment covers."),
    )
    main_concepts: list[str] = Field(
        default_factory=list,
        description="Concept strings taught in this subsegment.",
    )
    secondary_concepts: list[str] = Field(
        default_factory=list,
        description="Concept strings mentioned but not taught.",
    )
    noisy: bool = Field(
        description=(
            "True if speech disfluencies warrant a Pass 2c denoise "
            "call for this subsegment."
        ),
    )


class AudioSegmentDraft(BaseModel):
    """Top-level audio segment (Pass 2a output, KD-2.2-H).

    Carries optional ``subsegments`` (2-level hierarchy capped at 5
    entries per KD-2.2-H prompt rule). Together with the parent
    ``AudioPass2aResult.segments`` cap of 10, the LLM hierarchy is
    bounded to ``10 * 5 = 50`` nodes.

    No ``order`` field — position is implicit via
    ``enumerate(AudioPass2aResult.segments)``.
    """

    start_word_idx: int = Field(
        description=("Inclusive start word index into the ``STTResult.words`` stream."),
    )
    end_word_idx: int = Field(
        description=(
            "Exclusive end word index into the ``STTResult.words`` "
            "stream (must be > ``start_word_idx``)."
        ),
    )
    title: str | None = Field(
        default=None,
        description="Optional short heading for this segment.",
    )
    description: str = Field(
        description=("1-2 sentence description of what this segment covers."),
    )
    main_concepts: list[str] = Field(
        default_factory=list,
        description="Concept strings taught in this segment.",
    )
    secondary_concepts: list[str] = Field(
        default_factory=list,
        description="Concept strings mentioned but not taught.",
    )
    noisy: bool = Field(
        description=(
            "True if speech disfluencies warrant a Pass 2c denoise "
            "call for this segment (raw slice) or for each of its "
            "subsegments individually."
        ),
    )
    subsegments: list[AudioSubsegmentDraft] = Field(
        default_factory=list,
        max_length=5,
        description=(
            "Optional sub-level segments. Empty list means the "
            "segment is flat; non-empty triggers the "
            "``_subsegments_cover_parent`` validator (KD-2.2-H)."
        ),
    )


class AudioPass2aResult(BaseModel):
    """Pass 2a LLM output for the audio source type (KD-2.2-H).

    Top-level container for the Pass 2a metadata-mapping call.
    Carries doc-level synthesis (``title``, ``description``) plus
    up to 10 segments (KD-2.2-H prompt cap). Three
    ``model_validator(mode="after")`` gates enforce the structural
    invariants over the segment sequence:

    * :meth:`_segments_contiguous` — segments are adjacent on
      ``word_idx`` (``prev.end_word_idx == next.start_word_idx``),
      first segment starts at 0, and each segment has
      ``end_word_idx > start_word_idx``.
    * :meth:`_full_cover` — ``segments[-1].end_word_idx`` equals
      ``total_word_count`` read from
      ``ValidationInfo.context["total_word_count"]``. Skipped when
      context is absent (unit-test fixtures, ad-hoc tooling).
    * :meth:`_subsegments_cover_parent` — within each segment that
      has subsegments, the subsegments cover the parent
      ``word_idx`` range exactly. Closes the mechanical failure
      mode observed in the DeepSeek narrow probe (DD-2.2-X,
      2026-05-15).

    Doc-level fields (PHASE.md v0.3 correction 2026-05-15):
    ``title`` + ``description`` symmetric до text/web
    :class:`DocumentSummaryDraft` contract. :class:`AudioProcessor`
    ``process_macro`` робить pass-through conversion of these
    fields into the returned :class:`DocumentSummaryDraft` without
    algorithmic derivation; preserves LLM judgement over
    client-side filename-based defaults (dev-time orientation:
    quality > simplicity).
    """

    title: str | None = Field(
        default=None,
        max_length=128,
        description=(
            "Doc-level title summarising the opening theme of the "
            "audio (target ≤80 chars per prompt directive; schema "
            "cap 128). ``None`` tolerates the «talk without explicit "
            "theme» edge case, parallel до text/web "
            "DocumentSummaryDraft contract."
        ),
    )
    description: str = Field(
        max_length=512,
        description=(
            "Doc-level description (2-3 sentences) covering the "
            "doc subject and main-concepts coverage. Required; "
            "schema cap 512 chars. Pass 2a LLM emits this field "
            "alongside ``segments``; AudioProcessor.process_macro "
            "passes it through to DocumentSummaryDraft.description."
        ),
    )
    segments: list[AudioSegmentDraft] = Field(
        default_factory=list,
        max_length=10,
        description=("Top-level audio segments (KD-2.2-H cap of 10)."),
    )

    @model_validator(mode="after")
    def _segments_contiguous(self) -> AudioPass2aResult:
        """Strict adjacency over ``word_idx`` (KD-2.2-H).

        * ``segments[0].start_word_idx == 0``.
        * For every adjacent pair,
          ``prev.end_word_idx == next.start_word_idx``.
        * Each segment has ``end_word_idx > start_word_idx``.

        Empty ``segments`` is allowed (trivially short audio
        document with no surfaced structure).
        """
        if not self.segments:
            return self

        first = self.segments[0]
        if first.start_word_idx != 0:
            raise ValueError(
                f"first audio segment must start at word_idx 0; got "
                f"start_word_idx={first.start_word_idx}"
            )

        for prev, nxt in zip(self.segments, self.segments[1:], strict=False):
            if prev.end_word_idx != nxt.start_word_idx:
                raise ValueError(
                    f"audio segments must be contiguous on word_idx; "
                    f"prev.end_word_idx={prev.end_word_idx} != "
                    f"nxt.start_word_idx={nxt.start_word_idx}"
                )

        for seg in self.segments:
            if seg.end_word_idx <= seg.start_word_idx:
                raise ValueError(
                    f"audio segment must have end_word_idx > "
                    f"start_word_idx; got start={seg.start_word_idx}, "
                    f"end={seg.end_word_idx}"
                )

        return self

    @model_validator(mode="after")
    def _full_cover(self, info: ValidationInfo) -> AudioPass2aResult:
        """Trailing-words coverage gated on Pydantic context.

        When parsed via
        ``AudioPass2aResult.model_validate_json(content,
        context={"total_word_count": N})``, asserts that
        ``segments[-1].end_word_idx == N``. The caller (Pass 2a
        ``response_validator`` closure in ``ingestion/audio.py``)
        translates the resulting :class:`pydantic.ValidationError`
        into :class:`~course_supporter.llm.errors.StructuralRetryError`,
        so trailing undercoverage drives the existing
        instructor-style retry + ladder fallback chain (KD7 /
        KD16).

        Skipped silently when context is ``None`` or
        ``total_word_count`` is absent. Empty ``segments`` also
        skips the check — :meth:`_segments_contiguous` is the
        sole structural gate in that case.
        """
        if not self.segments:
            return self
        context = info.context
        if context is None:
            return self
        expected_total = context.get("total_word_count")
        if expected_total is None:
            return self
        actual_last = self.segments[-1].end_word_idx
        if actual_last != expected_total:
            raise ValueError(
                f"audio segments do not cover the word stream "
                f"exactly: last.end_word_idx={actual_last} != "
                f"total_word_count={expected_total} "
                f"(segment_count={len(self.segments)}); "
                "prompt rule: 'last segment must cover trailing words'"
            )
        return self

    @model_validator(mode="after")
    def _subsegments_cover_parent(self) -> AudioPass2aResult:
        """Within each segment, subsegments cover the parent range.

        Closes the mechanical failure mode observed in the DeepSeek
        narrow probe (DD-2.2-X, 2026-05-15): parent's
        ``end_word_idx`` was inherited from the last subsegment,
        leaving trailing words uncovered whenever the subsegments
        did not extend to the parent's actual end.

        For every segment with a non-empty ``subsegments`` list:

        * ``subsegments[0].start_word_idx == segment.start_word_idx``.
        * ``subsegments[-1].end_word_idx == segment.end_word_idx``.
        * Subsegments are contiguous on ``word_idx`` within the
          parent (``prev.end_word_idx == next.start_word_idx``).
        * Each subsegment has
          ``end_word_idx > start_word_idx``.

        Empty ``subsegments`` is allowed — flat segments without
        2-level hierarchy bypass this gate (parent's invariants
        are enforced by :meth:`_segments_contiguous`).
        """
        for seg in self.segments:
            if not seg.subsegments:
                continue

            first_sub = seg.subsegments[0]
            if first_sub.start_word_idx != seg.start_word_idx:
                raise ValueError(
                    f"first subsegment must start at "
                    f"parent.start_word_idx; parent.start="
                    f"{seg.start_word_idx} != first_sub.start="
                    f"{first_sub.start_word_idx}"
                )

            last_sub = seg.subsegments[-1]
            if last_sub.end_word_idx != seg.end_word_idx:
                raise ValueError(
                    f"last subsegment must end at "
                    f"parent.end_word_idx; parent.end="
                    f"{seg.end_word_idx} != last_sub.end="
                    f"{last_sub.end_word_idx} "
                    f"(prompt rule: subsegments cover parent's "
                    f"full range)"
                )

            for prev, nxt in zip(seg.subsegments, seg.subsegments[1:], strict=False):
                if prev.end_word_idx != nxt.start_word_idx:
                    raise ValueError(
                        f"subsegments within a segment must be "
                        f"contiguous on word_idx; "
                        f"prev.end_word_idx={prev.end_word_idx} != "
                        f"nxt.start_word_idx={nxt.start_word_idx}"
                    )

            for sub in seg.subsegments:
                if sub.end_word_idx <= sub.start_word_idx:
                    raise ValueError(
                        f"audio subsegment must have end_word_idx "
                        f"> start_word_idx; got "
                        f"start={sub.start_word_idx}, "
                        f"end={sub.end_word_idx}"
                    )

        return self


class AudioPass2cInput(BaseModel):
    """Pass 2c selective-denoise prompt input (KD-2.2-I).

    Carries a single noisy segment's raw text plus the Pass 2a
    metadata that contextualises the denoise prompt. Routed
    selectively: only segments (or subsegments) with
    ``noisy=True`` flow through Pass 2c. Rationale (KD-2.2-I):
    denoise spends model budget per noisy segment rather than on
    the entire document.
    """

    content: str = Field(
        description=(
            "Raw text of the noisy segment (space-joined "
            "``STTResult.words`` slice for the segment range)."
        ),
    )
    title: str | None = Field(
        default=None,
        description="Parent segment title (Pass 2a output).",
    )
    description: str = Field(
        description="Parent segment description (Pass 2a output).",
    )
    main_concepts: list[str] = Field(
        default_factory=list,
        description="Main concepts from the parent segment.",
    )
    secondary_concepts: list[str] = Field(
        default_factory=list,
        description="Secondary concepts from the parent segment.",
    )


class AudioPass2cResult(BaseModel):
    """Pass 2c selective-denoise LLM output (KD-2.2-I).

    Single-string content output — distinct from
    :class:`AudioPass2aResult`'s JSON-object shape. The
    ``StageRouter.response_validator`` closure (Phase 2.1 fixup
    2.1.7.2) wraps the raw string LLM response into this model;
    :class:`pydantic.ValidationError` raised here is translated to
    :class:`~course_supporter.llm.errors.StructuralRetryError`, so
    the ladder retry + fallback chain (KD7 / KD16) handles
    Pass 2c structural failures symmetrically to Pass 2a.
    """

    content: str = Field(
        description=(
            "Denoised version of the segment text — disfluencies "
            "removed, semantic content and word order preserved."
        ),
    )


# Presentation source-type schemas (Phase 2.3 + task 2.4.15).
#
# Pass 2a maps a slide-list into contiguous segments and — per task
# 2.4.15 — also emits per-segment concepts (``main_concepts`` /
# ``secondary_concepts``) symmetric with the text/web/audio/video
# Pass 2a contract. Doc-level ``main_concepts`` / ``secondary_concepts``
# are NOT emitted by the LLM: ``PresentationProcessor.process_macro``
# computes them algorithmically as ``sorted(set-union)`` over segments
# (KD-2.1-O), mirroring the text/audio aggregation pattern.
#
# KD2d split (task 2.4.15): (a) "presentation Pass 2a carries no
# concepts" — LIFTED; Pass 2a now emits concepts per segment. (b)
# "presentation has no Pass 2c selective denoise" — STILL HOLDS;
# per-slide VD output is clean by construction, so no denoise step.


class PresentationSegment(BaseModel):
    """One Pass 2a segment for the presentation source type (KD-2.3-H).

    Slide boundaries are 1-indexed inclusive on both ends:
    ``start_slide=3, end_slide=5`` covers slides 3, 4, and 5. The
    char-offset bridge (``PresentationProcessor.process_macro``) maps
    these slide numbers onto ``DocumentSegmentDraft.start_pos`` /
    ``end_pos`` via the ``chars_per_slide_cumsum`` helper.

    No ``order`` field — position is implicit through array order
    (parallel to the audio segment classes). Concept fields landed in
    task 2.4.15 (KD2d (a) lifted); aggregated to doc-level via the
    union+dedup pattern shared with text/audio (KD-2.1-O).
    """

    start_slide: int = Field(
        description="Inclusive 1-indexed slide number where the segment begins.",
    )
    end_slide: int = Field(
        description=(
            "Inclusive 1-indexed slide number where the segment ends "
            "(must be >= start_slide)."
        ),
    )
    title: str | None = Field(
        default=None,
        description="Optional short heading for this segment.",
    )
    description: str = Field(
        description="One-sentence description of what this segment covers.",
    )
    main_concepts: list[str] = Field(
        default_factory=list,
        description="Concept strings taught in this segment (task 2.4.15).",
    )
    secondary_concepts: list[str] = Field(
        default_factory=list,
        description="Concept strings mentioned but not taught in this segment.",
    )


class PresentationPass2aResult(BaseModel):
    """Pass 2a LLM output for the presentation source type (KD-2.3-H).

    Top-level container for the slide-mapping call. Carries doc-level
    synthesis (``title`` / ``description``) plus a contiguous segment
    sequence. Two ``model_validator(mode="after")`` gates enforce the
    structural invariants the spike v2 calibrated against:

    * :meth:`_segments_contiguous` — segments tile the slide range
      without gaps or overlap (``segments[i+1].start_slide ==
      segments[i].end_slide + 1``), the first segment starts at slide
      1, each segment has ``end_slide >= start_slide``, and the count
      satisfies Miller's rule (``2 <= count <= 7``).
    * :meth:`_last_segment_covers_n_slides` — ``segments[-1].end_slide``
      equals ``n_slides`` read from
      ``ValidationInfo.context["n_slides"]``. Skipped when context is
      absent (unit-test fixtures, ad-hoc tooling) so the structural
      gate above remains the sole check in that case.

    A Pydantic ``ValidationError`` raised here is translated to
    :class:`StructuralRetryError` by the ``process_macro``
    response_validator closure, so structural failures drive the
    existing instructor-style retry + ladder fallback chain (KD7).
    """

    title: str | None = Field(
        default=None,
        max_length=128,
        description=(
            "Doc-level title summarising the presentation theme "
            "(schema cap 128). ``None`` tolerates a deck without an "
            "explicit theme, parallel to the audio "
            "``AudioPass2aResult`` contract."
        ),
    )
    description: str = Field(
        max_length=512,
        description=(
            "Doc-level description (1-3 sentences) covering the "
            "presentation subject. Required; schema cap 512 chars."
        ),
    )
    segments: list[PresentationSegment] = Field(
        default_factory=list,
        description="Contiguous slide-range segments (Miller's rule 2-7).",
    )

    @model_validator(mode="after")
    def _segments_contiguous(self) -> PresentationPass2aResult:
        """Strict adjacency over slide numbers + Miller's rule.

        * ``2 <= len(segments) <= 7`` (cognitive-chunking cap).
        * ``segments[0].start_slide == 1``.
        * each segment has ``end_slide >= start_slide``.
        * ``segments[i+1].start_slide == segments[i].end_slide + 1``.

        Unlike the audio result, an empty ``segments`` list is NOT
        allowed — the presentation prompt commits to at least 2
        segments, so an empty or single-segment payload is a
        structural failure that should drive a retry.
        """
        count = len(self.segments)
        if not (2 <= count <= 7):
            raise ValueError(
                f"presentation segments must number between 2 and 7 "
                f"(Miller's rule); got {count}"
            )

        first = self.segments[0]
        if first.start_slide != 1:
            raise ValueError(
                f"first presentation segment must start at slide 1; got "
                f"start_slide={first.start_slide}"
            )

        for seg in self.segments:
            if seg.end_slide < seg.start_slide:
                raise ValueError(
                    f"presentation segment must have end_slide >= "
                    f"start_slide; got start={seg.start_slide}, "
                    f"end={seg.end_slide}"
                )

        for prev, nxt in zip(self.segments, self.segments[1:], strict=False):
            if nxt.start_slide != prev.end_slide + 1:
                raise ValueError(
                    f"presentation segments must be contiguous; "
                    f"prev.end_slide={prev.end_slide} + 1 != "
                    f"nxt.start_slide={nxt.start_slide}"
                )

        return self

    @model_validator(mode="after")
    def _last_segment_covers_n_slides(
        self, info: ValidationInfo
    ) -> PresentationPass2aResult:
        """Trailing-slide coverage gated on Pydantic context.

        When parsed via
        ``PresentationPass2aResult.model_validate_json(content,
        context={"n_slides": N})``, asserts ``segments[-1].end_slide
        == N``. Skipped silently when context is ``None`` or
        ``n_slides`` is absent.
        """
        if not self.segments:
            return self
        context = info.context
        if context is None:
            return self
        expected = context.get("n_slides")
        if expected is None:
            return self
        actual_last = self.segments[-1].end_slide
        if actual_last != expected:
            raise ValueError(
                f"presentation segments do not cover all slides: "
                f"last.end_slide={actual_last} != n_slides={expected}"
            )
        return self


# Code source-type schemas (task-code-materials).
#
# The code pipeline diverges from every other source type: segmentation
# is DETERMINISTIC (one included file = one segment; offsets computed in
# code, never LLM-emitted — ratified F1), so there is no Pass 2a mapping
# schema. The LLM stages are:
#
# * ``code_skeleton_extraction`` — rung 3 of the per-file extraction
#   ladder (AST → regex → LLM → deterministic minimum, R4); output is
#   :class:`CodeSkeletonResult`.
# * ``code_segment_description`` — one cheap call per included file
#   (input = the file's skeleton); output is
#   :class:`CodeSegmentDescription`. ``title`` is NOT asked — it is the
#   deterministic file name (ratified segment model).
# * ``code_summary`` — ONE large-window call per document (input =
#   project structure + the ready per-file descriptions, NOT skeletons);
#   output is :class:`CodeSummaryResult`. Doc-level concepts are NOT
#   asked — aggregated via the KD-2.1-O union+dedup pattern.


class CodeSkeletonDeclaration(BaseModel):
    """One declaration in an LLM-extracted code skeleton."""

    kind: str = Field(
        description=(
            "Declaration kind: module/class/widget/function/method/"
            "constant/table/index/property."
        ),
    )
    name: str = Field(description="Declaration identifier, verbatim.")
    parent: str | None = Field(
        default=None,
        description="Enclosing declaration name, or null for top-level.",
    )
    signature: str = Field(
        description="Header line(s) verbatim, collapsed to one line.",
    )
    docstring: str | None = Field(
        default=None,
        description="Verbatim docstring/doc-comment for THIS declaration.",
    )


class CodeSkeletonResult(BaseModel):
    """LLM output of the ``code_skeleton_extraction`` stage.

    Calibrated by the task spike (SPIKE-code-materials-results.md):
    gemini-3.1-flash-lite primary → deepseek-v4-flash fallback, both
    100% JSON-valid on the fixture set. The 8192-byte skeleton cap is
    enforced CODE-SIDE after rendering (ratified: the model's own cap
    compliance is not trusted), so no size validator lives here.
    """

    language: str = Field(description="Lowercase language name.")
    module_namespace: list[str] = Field(
        default_factory=list,
        description="Top-level namespace/module names, outermost first.",
    )
    imports: list[str] = Field(
        default_factory=list,
        description="Imported module/package identifiers, verbatim.",
    )
    declarations: list[CodeSkeletonDeclaration] = Field(
        default_factory=list,
        description="Declarations in source order.",
    )
    leading_comments: str | None = Field(
        default=None,
        description="Verbatim file-leading comment block, or null.",
    )
    truncated: bool = Field(
        default=False,
        description="True when the model stopped at a declaration boundary.",
    )


class CodeSegmentDescription(BaseModel):
    """LLM output of the per-file ``code_segment_description`` stage.

    ``title`` is deliberately absent — the segment title is the
    deterministic file name (ratified segment model; truncate to the
    ORM ``String(128)`` at the bridge). Concepts: the deterministic
    namespace core is rendered into the prompt and the LLM verifies /
    supplements — never invents (prompt hard rule).
    """

    description: str = Field(
        max_length=512,
        description=(
            "1-2 sentence description of what this source file "
            "contains / teaches (not a paraphrase of the code)."
        ),
    )
    main_concepts: list[str] = Field(
        default_factory=list,
        description="Concept strings taught/central in this file.",
    )
    secondary_concepts: list[str] = Field(
        default_factory=list,
        description="Concept strings mentioned but not central.",
    )


class CodeSummaryResult(BaseModel):
    """LLM output of the ONE per-document ``code_summary`` stage.

    Mirrors the audio/presentation doc-level contract: title nullable
    (≤128), description required (≤512). Segments/concepts are NOT
    emitted here — segmentation is deterministic and doc-level concepts
    aggregate from per-file descriptions (KD-2.1-O).
    """

    title: str | None = Field(
        default=None,
        max_length=128,
        description="Doc-level title for the code material (≤128 chars).",
    )
    description: str = Field(
        max_length=512,
        description=(
            "Doc-level description (2-3 sentences): what the project/"
            "file is, its stack, and what it demonstrates."
        ),
    )
