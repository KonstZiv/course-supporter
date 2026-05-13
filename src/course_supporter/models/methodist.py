"""Methodist agent output schemas and task taxonomy.

Defines the structured output produced by MethodistAgent for each
course structure node. Includes assignment recommendations with
a four-tier taxonomy (test, short_task, task, project).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from course_supporter.models.source import AssignmentType


class AssignmentRecommendation(BaseModel):
    """Recommended assignment for a course structure node.

    Part of the Methodist output — suggests what assessments
    should be added to verify student learning.
    """

    assignment_type: AssignmentType = Field(
        description=(
            "Type: test (5-10 min quiz), short_task (20-60 min), "
            "task (multi-step, each 20-60 min), "
            "project (1-2 weeks)."
        ),
    )
    title: str = Field(description="Assignment title.")
    description: str = Field(
        description="What the student should do.",
    )
    estimated_duration_minutes: int = Field(
        description="Expected time to complete in minutes.",
    )
    learning_objectives: list[str] = Field(
        default_factory=list,
        description="What this assignment verifies.",
    )
    grading_criteria: str | None = Field(
        default=None,
        description="How to evaluate the submission.",
    )
    sample_questions: list[str] = Field(
        default_factory=list,
        description="Example questions (for test type).",
    )
    steps: list[str] = Field(
        default_factory=list,
        description="Step breakdown (for task/project types).",
    )


# -- Methodist Output --


class ConceptDetail(BaseModel):
    """Detailed concept description with relationships."""

    name: str = Field(description="Concept name.")
    definition: str = Field(
        description="Clear definition in the course context.",
    )
    relationships: list[str] = Field(
        default_factory=list,
        description="Related concepts in this course.",
    )
    importance: str = Field(
        description="Why this concept matters at this point.",
    )


class Gap(BaseModel):
    """Identified gap in course materials or structure."""

    description: str = Field(
        description="What is missing or insufficient.",
    )
    severity: Literal["critical", "moderate", "minor"] = Field(
        description="Impact on student learning.",
    )
    recommendation: str = Field(
        description="How to address this gap.",
    )


class Contradiction(BaseModel):
    """Contradiction detected between materials or nodes."""

    description: str = Field(
        description="What contradicts what.",
    )
    source_a: str = Field(
        description="First source (node title or material ref).",
    )
    source_b: str = Field(
        description="Second source (node title or material ref).",
    )
    recommendation: str = Field(
        description="How to resolve the contradiction.",
    )


class MethodistNodeOutput(BaseModel):
    """Per-node methodological document produced by MethodistAgent.

    Contains detailed methodological materials for a single node in
    the course structure tree. Stored as JSONB on StructureNodeEditable.
    The rendered_markdown field provides an author-friendly view.
    """

    # -- Detailed methodological content --
    learning_objectives: list[str] = Field(
        description=(
            "Measurable learning outcomes for this node. "
            "Each should be verifiable via assessment."
        ),
    )
    key_concepts_detailed: list[ConceptDetail] = Field(
        default_factory=list,
        description="Concepts with definitions and relationships.",
    )
    common_misconceptions: list[str] = Field(
        default_factory=list,
        description="Common student mistakes and misconceptions.",
    )
    teaching_recommendations: str = Field(
        description="Pedagogy recommendations for this topic.",
    )
    prerequisites_verified: list[str] = Field(
        default_factory=list,
        description=(
            "Prerequisites verified against the course structure. "
            "Flags if a prerequisite is not covered earlier."
        ),
    )

    # -- Assessment recommendations --
    recommended_assignments: list[AssignmentRecommendation] = Field(
        default_factory=list,
        description="Suggested assessments for this node.",
    )

    # -- Quality analysis --
    gaps: list[Gap] = Field(
        default_factory=list,
        description="Identified gaps in materials or coverage.",
    )
    contradictions: list[Contradiction] = Field(
        default_factory=list,
        description="Contradictions between materials or nodes.",
    )
    improvement_suggestions: list[str] = Field(
        default_factory=list,
        description="General improvement recommendations.",
    )

    # -- Rendered for author --
    rendered_markdown: str = Field(
        description=(
            "Complete methodological document as Markdown. "
            "Includes all sections above in readable form."
        ),
    )
