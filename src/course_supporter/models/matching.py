"""Pydantic schemas for the task matcher module."""

from __future__ import annotations

import uuid

from pydantic import BaseModel, Field


class TaskMatch(BaseModel):
    """A single matched task from the course structure."""

    node_id: uuid.UUID = Field(
        description="StructureNodeEditable ID of the matched task."
    )
    task_title: str = Field(description="Title of the matched task.")
    task_type: str = Field(
        description="Task type: exercise, short_task, task, or project."
    )
    confidence: float = Field(
        description="Match confidence 0.0-1.0.",
        ge=0.0,
        le=1.0,
    )
    section_hint: str = Field(
        default="",
        description="Which part of the submission relates to this task.",
    )


class MatchResult(BaseModel):
    """Aggregated result of task matching."""

    matches: list[TaskMatch] = Field(
        description="All matched tasks, sorted by confidence descending."
    )
    primary_task_id: uuid.UUID = Field(
        description="ID of the highest-confidence match."
    )
    explanation: str = Field(
        description="Human-readable explanation of the matching logic."
    )


class LLMTaskMatch(BaseModel):
    """Single task match in LLM response (uses index, not UUID)."""

    task_index: int = Field(
        description="0-based index of the task in the provided list."
    )
    confidence: float = Field(
        description="Match confidence 0.0-1.0.",
        ge=0.0,
        le=1.0,
    )
    section_hint: str = Field(
        default="",
        description="Which part of the submission relates to this task.",
    )


class LLMMatchResponse(BaseModel):
    """Schema for LLM structured output during task matching.

    The LLM receives a list of available tasks (with numeric indices)
    and returns matches referencing those indices.
    """

    matches: list[LLMTaskMatch] = Field(
        description="Matched tasks, sorted by confidence descending."
    )
    explanation: str = Field(
        description="Brief explanation of how the submission was matched."
    )
