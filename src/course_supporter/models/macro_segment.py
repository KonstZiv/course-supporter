"""Pydantic schemas for the macro-section / segment pipeline.

Two layers:

- **LLM output** (``MacroSectionsLLMOutput`` + ``MacroSectionCandidate``) —
  raw result of the Stage-5 LLM call. Provides titles and start-snippets
  that the orchestrator resolves into exact character offsets.
- **Drafts** (``MacroSectionDraft``, ``SegmentDraft``) — resolved,
  position-anchored in-memory records ready to be persisted as
  ``MaterialMacroSection`` / ``MaterialSegment`` ORM rows.

These are intentionally kept separate from the ORM models so the
pipeline can be unit-tested without touching the database.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class MacroSectionCandidate(BaseModel):
    """Raw section descriptor coming from the Stage-5 LLM call.

    The LLM returns ``start_snippet`` (a verbatim fragment from the
    start of the section) rather than character offsets directly,
    because LLMs are unreliable at counting characters. The orchestrator
    resolves snippets to exact offsets via :func:`str.find`.
    """

    title: str = Field(
        min_length=1,
        description="Self-contained, human-readable section title.",
    )
    start_snippet: str = Field(
        min_length=30,
        max_length=120,
        description=(
            "Verbatim copy of the first 50-80 characters of the section "
            "as it appears in the source. Used to locate the section's "
            "start position via substring search."
        ),
    )


class MacroSectionsLLMOutput(BaseModel):
    """Top-level LLM output for Stage-5 macro generation."""

    sections: list[MacroSectionCandidate] = Field(
        min_length=1,
        description="Ordered list of thematic sections in document order.",
    )


class MacroSectionDraft(BaseModel):
    """Resolved in-memory macro section ready for persistence.

    ``start_pos`` / ``end_pos`` are absolute character offsets in the
    source ``processed_content``: half-open ``[start, end)`` semantics
    for text and web.
    """

    order: int = Field(ge=0)
    title: str
    start_pos: int = Field(ge=0)
    end_pos: int = Field(gt=0)


class SegmentDraft(BaseModel):
    """Resolved in-memory segment inside a macro section.

    Positions are absolute in ``processed_content`` (not relative to
    the macro's start).
    """

    order: int = Field(ge=0)
    start_pos: int = Field(ge=0)
    end_pos: int = Field(gt=0)
    content: str = Field(min_length=1)
