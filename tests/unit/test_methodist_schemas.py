"""Tests for Methodist output schemas and task taxonomy."""

from __future__ import annotations

import pytest

from course_supporter.models.methodist import (
    AssignmentRecommendation,
    AssignmentType,
    ConceptDetail,
    Contradiction,
    Gap,
    MethodistNodeOutput,
)


class TestAssignmentType:
    def test_all_types_exist(self) -> None:
        assert AssignmentType.TEST == "test"
        assert AssignmentType.SHORT_TASK == "short_task"
        assert AssignmentType.TASK == "task"
        assert AssignmentType.PROJECT == "project"


class TestAssignmentRecommendation:
    def test_minimal(self) -> None:
        rec = AssignmentRecommendation(
            assignment_type=AssignmentType.TEST,
            title="ORM Basics Quiz",
            description="Verify understanding of model definition.",
            estimated_duration_minutes=10,
        )
        assert rec.assignment_type == "test"
        assert rec.estimated_duration_minutes == 10
        assert rec.sample_questions == []
        assert rec.steps == []

    def test_full(self) -> None:
        rec = AssignmentRecommendation(
            assignment_type=AssignmentType.PROJECT,
            title="Library System",
            description="Build a multi-model Django app.",
            estimated_duration_minutes=1200,
            learning_objectives=["Design models", "Write queries"],
            grading_criteria="Working CRUD + tests",
            steps=["Design models", "Implement views", "Write tests"],
        )
        assert len(rec.steps) == 3
        assert rec.grading_criteria is not None


class TestMethodistNodeOutput:
    def test_minimal_valid(self) -> None:
        output = MethodistNodeOutput(
            learning_objectives=["Understand ORM basics"],
            teaching_recommendations="Start with schema diagrams.",
            rendered_markdown="# ORM Basics\n\nContent here.",
        )
        assert len(output.learning_objectives) == 1
        assert output.key_concepts_detailed == []
        assert output.gaps == []
        assert output.recommended_assignments == []

    def test_full_output(self) -> None:
        output = MethodistNodeOutput(
            learning_objectives=[
                "Define Django models",
                "Run migrations",
            ],
            key_concepts_detailed=[
                ConceptDetail(
                    name="Model",
                    definition="Python class mapping to a DB table.",
                    relationships=["Field", "Migration"],
                    importance="Foundation of Django data layer.",
                ),
            ],
            common_misconceptions=["Models auto-create tables without migrate"],
            teaching_recommendations="Use live coding demo.",
            prerequisites_verified=["Python classes", "SQL basics"],
            recommended_assignments=[
                AssignmentRecommendation(
                    assignment_type=AssignmentType.TEST,
                    title="Model Quiz",
                    description="Multiple choice on field types.",
                    estimated_duration_minutes=10,
                    sample_questions=["Which field type for email?"],
                ),
                AssignmentRecommendation(
                    assignment_type=AssignmentType.SHORT_TASK,
                    title="Blog Model",
                    description="Create a blog post model.",
                    estimated_duration_minutes=30,
                    learning_objectives=["Define model fields"],
                ),
            ],
            gaps=[
                Gap(
                    description="No coverage of ForeignKey.",
                    severity="moderate",
                    recommendation="Add a section on relationships.",
                ),
            ],
            contradictions=[
                Contradiction(
                    description="Lesson 2.1 says no SQL needed.",
                    source_a="Lesson 2.1",
                    source_b="Lesson 2.3",
                    recommendation="Clarify SQL prerequisites.",
                ),
            ],
            improvement_suggestions=["Add a migration troubleshooting guide"],
            rendered_markdown="# Django Models\n\n## Objectives\n...",
        )
        assert len(output.recommended_assignments) == 2
        assert len(output.gaps) == 1
        assert len(output.contradictions) == 1
        assert output.key_concepts_detailed[0].name == "Model"

    def test_json_roundtrip(self) -> None:
        """MethodistNodeOutput survives JSON serialization."""
        output = MethodistNodeOutput(
            learning_objectives=["Objective 1"],
            teaching_recommendations="Recommendation.",
            rendered_markdown="# Title",
        )
        json_str = output.model_dump_json()
        restored = MethodistNodeOutput.model_validate_json(json_str)
        assert restored.learning_objectives == output.learning_objectives

    def test_missing_required_fields_raises(self) -> None:
        with pytest.raises(Exception):  # noqa: B017
            MethodistNodeOutput()  # type: ignore[call-arg]
