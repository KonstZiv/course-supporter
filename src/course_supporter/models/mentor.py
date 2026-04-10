"""Pydantic schemas for the Mentor review pipeline.

Step 1 (analysis): MentorAnalysis — structured evaluation by budget LLM.
Step 2 (humanize): review_text — natural-language review by premium LLM.
Combined into MentorReview for storage and webhook delivery.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class ReviewIssue(BaseModel):
    """Single issue found during code/submission review."""

    severity: Literal["critical", "moderate", "minor"] = Field(
        description="Impact level: critical (blocks acceptance), "
        "moderate (should fix), minor (suggestion).",
    )
    description: str = Field(
        description="What the issue is.",
    )
    suggestion: str | None = Field(
        default=None,
        description="How to fix or improve.",
    )
    code_fragment: str | None = Field(
        default=None,
        description="Relevant code snippet (if applicable).",
    )


class NotableSolution(BaseModel):
    """Noteworthy good decision in the submission worth remembering."""

    description: str = Field(
        description="What was done well and why it matters.",
    )
    concept: str | None = Field(
        default=None,
        description="Concept or technique demonstrated (e.g. 'lazy evaluation').",
    )
    code_fragment: str | None = Field(
        default=None,
        description="Relevant code snippet (if applicable).",
    )


class MentorAnalysis(BaseModel):
    """Structured output from step 1 (analysis by budget LLM).

    Each field is populated only when relevant to the submission type
    and available context. Empty lists mean 'nothing to report', not
    'not evaluated'.
    """

    passed: bool = Field(
        description="Whether the submission meets acceptance criteria.",
    )
    score: int = Field(
        description="Overall score 0-100.",
        ge=0,
        le=100,
    )
    task_understanding: str = Field(
        description="How the reviewer interprets the task. "
        "Especially important when task description is vague.",
    )
    correctness: Literal["correct", "partially_correct", "incorrect"] = Field(
        description="Whether the solution achieves the goal.",
    )
    issues: list[ReviewIssue] = Field(
        default_factory=list,
        description="Problems found, sorted by severity.",
    )
    notable_solutions: list[NotableSolution] = Field(
        default_factory=list,
        description="Good decisions worth remembering for student history.",
    )
    strengths: list[str] = Field(
        default_factory=list,
        description="General positive aspects of the submission.",
    )
    recommendations: list[str] = Field(
        default_factory=list,
        description="Actionable next steps for the student.",
    )
    concepts_demonstrated: list[str] = Field(
        default_factory=list,
        description="Concepts the student showed understanding of.",
    )
    recurring_mistakes: list[str] = Field(
        default_factory=list,
        description="Mistakes repeated from previous submissions.",
    )


class MentorReview(BaseModel):
    """Combined Mentor output: structured analysis + human-readable text.

    Stored as JSONB in HomeworkSubmission.review_result.
    review_text goes to HomeworkSubmission.review_markdown.
    """

    analysis: MentorAnalysis = Field(
        description="Structured evaluation from analysis step.",
    )
    review_text: str = Field(
        description="Human-readable review in mentor style.",
    )
    response_language: str = Field(
        description="ISO 639-1 code of the language used for review_text.",
    )
    reviewed_at: datetime = Field(
        description="UTC timestamp of review completion.",
    )
