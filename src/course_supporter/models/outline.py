"""Material outline schemas for pre-agent lossless restructuring."""

from __future__ import annotations

from pydantic import BaseModel, Field, model_validator


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
    code: str = Field(description="Code verbatim — whitespace, comments preserved.")
    context: str = Field(description="What this snippet demonstrates (one sentence).")


class OutlineSection(BaseModel):
    """One thematic block of 2-5 minutes with timestamps.

    Content layer: lossless restructuring only.
    Every thesis, example, and code snippet must be preserved.
    """

    start_sec: float = Field(description="Block start in the source material.")
    end_sec: float = Field(description="Block end in the source material.")
    title: str = Field(description="Thematic title (one line).")
    narration: str = Field(
        description=(
            "What the presenter says — cleaned of mechanical artifacts "
            "(stutters, filler words) but every thesis/example preserved."
        ),
    )
    screen_content: str = Field(
        description=(
            "What is on screen — each meaningful change described, "
            "without repeating unchanged boilerplate."
        ),
    )
    code_snippets: list[CodeSnippet] = Field(
        default_factory=list,
        description="Code fragments shown on screen, exactly as displayed.",
    )
    key_concepts: list[str] = Field(
        default_factory=list,
        description="Concepts introduced or explained in this block.",
    )

    @model_validator(mode="after")
    def _check_time_range(self) -> OutlineSection:
        if self.end_sec <= self.start_sec:
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
    - **Content layer** (sections): ``narration``, ``screen_content``,
      ``code_snippets``, ``key_concepts`` — lossless restructuring,
      every thesis and example preserved verbatim.
    """

    # -- Macro information --
    title: str = Field(description="Material title/topic.")
    duration_sec: float = Field(description="Total duration in seconds.")
    language: str = Field(description="Presentation language (uk, en, de, …).")
    source_type: str = Field(
        description="Material type: video / lecture / screencast / presentation.",
    )

    # -- Presenter (navigation layer — summarized) --
    presenter: PresenterInfo

    # -- Overview (navigation layer — summarized) --
    summary: str = Field(
        description="2-3 sentences for quick orientation (navigation aid).",
    )
    topics: list[str] = Field(description="Key topics covered.")
    tools: list[str] = Field(
        description="IDEs, languages, libraries, frameworks mentioned.",
    )
    target_audience: str = Field(description="Intended audience level/role.")
    prerequisites: list[str] = Field(
        default_factory=list,
        description="Knowledge assumed by the presenter.",
    )

    # -- Thematic blocks (content layer — lossless) --
    sections: list[OutlineSection] = Field(
        min_length=1,
        description="Chronological thematic blocks (2-5 min granularity).",
    )

    @model_validator(mode="after")
    def _check_sections_ordered(self) -> MaterialOutline:
        for i in range(1, len(self.sections)):
            if self.sections[i].start_sec < self.sections[i - 1].start_sec:
                msg = (
                    f"Sections must be ordered by start_sec: "
                    f"section[{i}].start_sec ({self.sections[i].start_sec}) < "
                    f"section[{i - 1}].start_sec ({self.sections[i - 1].start_sec})"
                )
                raise ValueError(msg)
        return self
