"""Material outline schemas for pre-agent lossless restructuring.

Supports all source types (video, presentation, text, web).
Schema uses generic field names with backward compatibility for
video-specific field names via Pydantic validation aliases.
"""

from __future__ import annotations

from pydantic import AliasChoices, BaseModel, Field, model_validator


class PresenterInfo(BaseModel):
    """Presenter/author metadata inferred from the source material."""

    description: str = Field(
        description="Who the presenter is (role, background if mentioned).",
    )
    style: str = Field(
        description="Delivery style: conversational / academic / practical.",
    )
    delivery_notes: str = Field(
        description="Pace, digressions, humor, notable mannerisms.",
    )


class CodeSnippet(BaseModel):
    """A code fragment exactly as shown on screen."""

    language: str = Field(description="Programming language.")
    code: str = Field(
        description="Code verbatim — whitespace, comments preserved.",
    )
    context: str = Field(
        description="What this snippet demonstrates (one sentence).",
    )


class OutlineSection(BaseModel):
    """One thematic block of content from the source material.

    Generic across material types. Position markers and content
    fields are filled according to the source type:

    - **Video**: start_sec/end_sec for time range, content = narration,
      visual_content = screen description.
    - **Presentation**: position_index = slide number, content = slide text,
      visual_content = slide visual description.
    - **Text/Web**: position_index = section/paragraph index,
      content = section body, visual_content = diagrams/figures.
    """

    title: str = Field(description="Thematic title (one line).")
    content: str = Field(
        validation_alias=AliasChoices("content", "narration"),
        description=(
            "Primary textual content of this section. "
            "For video: cleaned narration. "
            "For text/web: section body. "
            "For presentation: slide text."
        ),
    )
    visual_content: str = Field(
        default="",
        validation_alias=AliasChoices(
            "visual_content",
            "screen_content",
        ),
        description=(
            "Visual/supplementary content. "
            "For video: screen content description. "
            "For presentation: slide visual description. "
            "For text/web: diagrams or figures description."
        ),
    )
    code_snippets: list[CodeSnippet] = Field(
        default_factory=list,
        description="Code fragments shown or included, exactly as displayed.",
    )
    key_concepts: list[str] = Field(
        default_factory=list,
        description="Concepts introduced or explained in this block.",
    )

    # Position markers — at least one set should be filled
    start_sec: float | None = Field(
        default=None,
        description="Block start in seconds (video/audio materials).",
    )
    end_sec: float | None = Field(
        default=None,
        description="Block end in seconds (video/audio materials).",
    )
    position_index: int | None = Field(
        default=None,
        description=(
            "Position marker for non-video materials: "
            "slide number (presentation), section index (text/web)."
        ),
    )

    @model_validator(mode="after")
    def _check_time_range(self) -> OutlineSection:
        """Validate time range when present (video materials)."""
        if (
            self.start_sec is not None
            and self.end_sec is not None
            and self.end_sec <= self.start_sec
        ):
            msg = (
                f"end_sec ({self.end_sec}) must be greater "
                f"than start_sec ({self.start_sec})"
            )
            raise ValueError(msg)
        return self


class MaterialOutline(BaseModel):
    """Structured lossless outline of a single source material.

    Two layers with different processing rules:

    - **Navigation layer** (macro fields): ``summary``, ``topics``,
      ``tools``, ``target_audience``, ``prerequisites``, ``presenter``
      — inferred/summarized from the material.
    - **Content layer** (sections): ``content``, ``visual_content``,
      ``code_snippets``, ``key_concepts`` — lossless restructuring,
      every thesis and example preserved verbatim.

    Supports all source types. Video-specific fields (duration_sec,
    presenter) are optional for non-video materials.
    """

    # -- Universal macro information --
    title: str = Field(description="Material title/topic.")
    language: str = Field(
        description="Presentation language (uk, en, de, ...).",
    )
    source_type: str = Field(
        description=(
            "Material type: video / lecture / screencast / presentation / text / web."
        ),
    )

    # -- Optional depending on source type --
    duration_sec: float | None = Field(
        default=None,
        description="Total duration in seconds (video/audio only).",
    )
    presenter: PresenterInfo | None = Field(
        default=None,
        description=(
            "Presenter/author metadata (video/presentation). "
            "None for text/web without clear authorship."
        ),
    )

    # -- Overview (navigation layer — summarized) --
    summary: str = Field(
        description="2-3 sentences for quick orientation (navigation aid).",
    )
    topics: list[str] = Field(description="Key topics covered.")
    tools: list[str] = Field(
        description="IDEs, languages, libraries, frameworks mentioned.",
    )
    target_audience: str = Field(
        description="Intended audience level/role.",
    )
    prerequisites: list[str] = Field(
        default_factory=list,
        description="Knowledge assumed by the author/presenter.",
    )

    # -- Thematic blocks (content layer — lossless) --
    sections: list[OutlineSection] = Field(
        min_length=1,
        description="Chronological thematic blocks.",
    )

    @model_validator(mode="after")
    def _check_sections_ordered(self) -> MaterialOutline:
        """Validate section ordering for time-based materials."""
        for i in range(1, len(self.sections)):
            prev_start = self.sections[i - 1].start_sec
            curr_start = self.sections[i].start_sec
            if (
                prev_start is not None
                and curr_start is not None
                and curr_start < prev_start
            ):
                msg = (
                    f"Sections must be ordered by start_sec: "
                    f"section[{i}].start_sec ({curr_start}) < "
                    f"section[{i - 1}].start_sec ({prev_start})"
                )
                raise ValueError(msg)
        return self
