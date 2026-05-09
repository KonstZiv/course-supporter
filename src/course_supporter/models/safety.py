"""Pydantic schemas retained for the post-Stage-1 homework path.

Phase 1.2 C4 (KD-1.2-K, second amendment 2026-05-08) removed the
3 orphan classes (``SafetyResult``, ``SafetyVerdict``,
``PatternMatch``) that formed the legacy Stage 1+2 verdict
composition cluster. Their sole consumers (``safety/checker.py`` +
``safety/patterns.py``) were deleted alongside in C4. The canonical
Stage 2 verdict shape now lives at
:class:`course_supporter.security.schemas.SafetyResult`.

3 classes retained as transitional residue (Phase 2.1 migration
territory per KD-1.2-J / KD-1.2-K):

* :class:`FileContent` -- per-file payload row inside an extracted
  submission. Consumed by ``safety/archive.py`` (retained per
  KD-1.2-J) and homework-side test fixtures.
* :class:`SubmissionContent` -- aggregated extraction result
  consumed by ``safety/archive.extract_submission_content`` and
  the post-Stage-1 homework path (``homework/matcher.py``,
  ``homework/mentor_context.py``, ``homework/language.py``).
* :class:`CourseContext` -- canonical Stage 2 input (course-aware
  off-topic judgement). Consumed by
  ``security.stage2.run_stage2_safety_check`` and
  ``api/tasks.py:arq_process_homework`` (KD-1.2-I).

Phase 2.1 absorbs these alongside ``safety/archive.py`` +
``safety/exceptions.py`` migration into canonical homes.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class FileContent(BaseModel):
    """Content of a single file extracted from a submission."""

    filename: str = Field(description="Filename or path within archive.")
    content: str = Field(description="Text content of the file.")
    size: int = Field(description="Size in bytes of the raw content.")


class SubmissionContent(BaseModel):
    """Aggregated content extracted from a homework submission."""

    model_config = {"arbitrary_types_allowed": True}

    files: list[FileContent] = Field(description="Individual file contents.")
    total_size: int = Field(description="Total size in bytes across all files.")
    security_warnings: list[Any] = Field(
        default_factory=list,
        description="Non-fatal security observations found during extraction.",
        exclude=True,
    )

    @property
    def full_text(self) -> str:
        """Concatenate all file contents for analysis."""
        parts: list[str] = []
        for f in self.files:
            parts.append(f"--- {f.filename} ---")
            parts.append(f.content)
        return "\n".join(parts)


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
        description="Outline summary from AuthoredDocument, if available.",
    )
