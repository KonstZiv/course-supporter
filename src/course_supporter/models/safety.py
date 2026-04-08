"""Pydantic schemas for the safety checker module."""

from __future__ import annotations

from pydantic import BaseModel, Field


class FileContent(BaseModel):
    """Content of a single file extracted from a submission."""

    filename: str = Field(description="Filename or path within archive.")
    content: str = Field(description="Text content of the file.")
    size: int = Field(description="Size in bytes of the raw content.")


class SubmissionContent(BaseModel):
    """Aggregated content extracted from a homework submission."""

    files: list[FileContent] = Field(description="Individual file contents.")
    total_size: int = Field(description="Total size in bytes across all files.")

    @property
    def full_text(self) -> str:
        """Concatenate all file contents for analysis."""
        parts: list[str] = []
        for f in self.files:
            parts.append(f"--- {f.filename} ---")
            parts.append(f.content)
        return "\n".join(parts)


class PatternMatch(BaseModel):
    """A regex pattern match found during injection scanning."""

    pattern_name: str = Field(description="Name of the matched pattern.")
    matched_text: str = Field(description="Actual text that matched.")
    line_number: int = Field(description="1-based line number of the match.")


class SafetyVerdict(BaseModel):
    """LLM-generated safety classification result."""

    safe: bool = Field(description="Whether the content is safe to process.")
    confidence: float = Field(
        description="Confidence score 0.0-1.0.",
        ge=0.0,
        le=1.0,
    )
    reason: str = Field(description="Explanation of the verdict.")
    flags: list[str] = Field(
        default_factory=list,
        description="Detected issue flags (e.g. 'prompt_injection', 'off_topic').",
    )


class CourseContext(BaseModel):
    """Minimal course context for relevance checking."""

    course_title: str = Field(description="Title of the course (root node).")
    course_description: str = Field(
        default="", description="Description of the course."
    )
    node_title: str = Field(description="Title of the target node.")
    node_description: str = Field(
        default="", description="Description of the target node."
    )
    outline_summary: str = Field(
        default="",
        description="Outline summary from MaterialEntry, if available.",
    )


class SafetyResult(BaseModel):
    """Combined result of the full safety check pipeline."""

    safe: bool = Field(description="Final safety verdict.")
    reason: str = Field(description="Human-readable reason for the verdict.")
    flags: list[str] = Field(
        default_factory=list,
        description="All detected flags from regex + LLM.",
    )
    verdict: SafetyVerdict | None = Field(
        default=None,
        description="LLM verdict (None if rejected by regex before LLM call).",
    )
    pattern_matches: list[PatternMatch] = Field(
        default_factory=list,
        description="Regex pattern matches found in content.",
    )
