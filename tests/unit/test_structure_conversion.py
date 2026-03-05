"""Tests for StructureNode conversion from CourseStructure."""

from __future__ import annotations

import uuid

import pytest

from course_supporter.models.course import (
    ConceptOutput,
    CourseStructure,
    ExerciseOutput,
    LessonOutput,
    ModuleOutput,
    SlideRange,
    WebReference,
)
from course_supporter.structure_conversion import convert_to_structure_nodes


@pytest.fixture()
def snapshot_id() -> uuid.UUID:
    return uuid.uuid4()


def _minimal_structure() -> CourseStructure:
    return CourseStructure(
        title="Test Course",
        modules=[
            ModuleOutput(
                title="Module 1",
                description="Intro module",
                learning_goal="Learn basics",
                expected_knowledge=["Python"],
                expected_skills=["Coding"],
                difficulty="easy",
                lessons=[
                    LessonOutput(
                        title="Lesson 1",
                        video_start_timecode="00:01:00",
                        video_end_timecode="00:10:00",
                        slide_range=SlideRange(start=1, end=5),
                        concepts=[
                            ConceptOutput(
                                title="Concept 1",
                                definition="A basic concept",
                                timecodes=["00:02:00"],
                                slide_references=[2],
                                web_references=[
                                    WebReference(
                                        url="https://example.com",
                                        title="Example",
                                        description="Ref",
                                    )
                                ],
                            )
                        ],
                        exercises=[
                            ExerciseOutput(
                                description="Do something",
                                grading_criteria="Must work",
                                difficulty_level=2,
                            )
                        ],
                    ),
                ],
            ),
        ],
    )


class TestConvertToStructureNodes:
    def test_returns_flat_list(self, snapshot_id: uuid.UUID) -> None:
        structure = _minimal_structure()
        nodes = convert_to_structure_nodes(structure, snapshot_id)
        assert isinstance(nodes, list)
        # 1 module + 1 lesson + 1 concept + 1 exercise = 4
        assert len(nodes) == 4

    def test_all_nodes_have_snapshot_id(self, snapshot_id: uuid.UUID) -> None:
        nodes = convert_to_structure_nodes(_minimal_structure(), snapshot_id)
        for node in nodes:
            assert node.structuresnapshot_id == snapshot_id

    def test_module_fields(self, snapshot_id: uuid.UUID) -> None:
        nodes = convert_to_structure_nodes(_minimal_structure(), snapshot_id)
        module = nodes[0]
        assert module.node_type == "module"
        assert module.title == "Module 1"
        assert module.description == "Intro module"
        assert module.learning_goal == "Learn basics"
        assert module.difficulty == "easy"
        assert module.order == 0
        assert module.parent_structurenode_id is None

    def test_module_expected_knowledge_format(self, snapshot_id: uuid.UUID) -> None:
        nodes = convert_to_structure_nodes(_minimal_structure(), snapshot_id)
        module = nodes[0]
        assert module.expected_knowledge == [{"summary": "Python", "details": ""}]
        assert module.expected_skills == [{"summary": "Coding", "details": ""}]

    def test_lesson_parent_is_module(self, snapshot_id: uuid.UUID) -> None:
        nodes = convert_to_structure_nodes(_minimal_structure(), snapshot_id)
        module = nodes[0]
        lesson = nodes[1]
        assert lesson.node_type == "lesson"
        assert lesson.parent_structurenode_id == module.id
        assert lesson.title == "Lesson 1"

    def test_lesson_timecodes(self, snapshot_id: uuid.UUID) -> None:
        nodes = convert_to_structure_nodes(_minimal_structure(), snapshot_id)
        lesson = nodes[1]
        assert lesson.timecodes == [{"start": "00:01:00", "end": "00:10:00"}]

    def test_lesson_slide_references(self, snapshot_id: uuid.UUID) -> None:
        nodes = convert_to_structure_nodes(_minimal_structure(), snapshot_id)
        lesson = nodes[1]
        assert lesson.slide_references == [{"start": 1, "end": 5}]

    def test_concept_parent_is_lesson(self, snapshot_id: uuid.UUID) -> None:
        nodes = convert_to_structure_nodes(_minimal_structure(), snapshot_id)
        lesson = nodes[1]
        concept = nodes[2]
        assert concept.node_type == "concept"
        assert concept.parent_structurenode_id == lesson.id
        assert concept.title == "Concept 1"
        assert concept.description == "A basic concept"

    def test_concept_key_concepts(self, snapshot_id: uuid.UUID) -> None:
        nodes = convert_to_structure_nodes(_minimal_structure(), snapshot_id)
        concept = nodes[2]
        assert concept.key_concepts == [
            {"summary": "Concept 1", "details": "A basic concept"}
        ]

    def test_concept_timecodes(self, snapshot_id: uuid.UUID) -> None:
        nodes = convert_to_structure_nodes(_minimal_structure(), snapshot_id)
        concept = nodes[2]
        assert concept.timecodes == [{"timecode": "00:02:00"}]

    def test_concept_slide_references(self, snapshot_id: uuid.UUID) -> None:
        nodes = convert_to_structure_nodes(_minimal_structure(), snapshot_id)
        concept = nodes[2]
        assert concept.slide_references == [{"slide": 2}]

    def test_concept_web_references(self, snapshot_id: uuid.UUID) -> None:
        nodes = convert_to_structure_nodes(_minimal_structure(), snapshot_id)
        concept = nodes[2]
        assert concept.web_references == [
            {"url": "https://example.com", "title": "Example", "description": "Ref"}
        ]

    def test_exercise_parent_is_lesson(self, snapshot_id: uuid.UUID) -> None:
        nodes = convert_to_structure_nodes(_minimal_structure(), snapshot_id)
        lesson = nodes[1]
        exercise = nodes[3]
        assert exercise.node_type == "exercise"
        assert exercise.parent_structurenode_id == lesson.id
        assert exercise.title == "Exercise 1"
        assert exercise.description == "Do something"
        assert exercise.success_criteria == "Must work"

    @pytest.mark.parametrize(
        ("level", "expected"),
        [
            (1, "easy"),
            (2, "easy"),
            (3, "medium"),
            (4, "hard"),
            (5, "hard"),
        ],
    )
    def test_exercise_difficulty_mapping(
        self, snapshot_id: uuid.UUID, level: int, expected: str
    ) -> None:
        structure = CourseStructure(
            title="C",
            modules=[
                ModuleOutput(
                    title="M",
                    lessons=[
                        LessonOutput(
                            title="L",
                            exercises=[
                                ExerciseOutput(description="E", difficulty_level=level),
                            ],
                        )
                    ],
                )
            ],
        )
        nodes = convert_to_structure_nodes(structure, snapshot_id)
        exercise = next(n for n in nodes if n.node_type == "exercise")
        assert exercise.difficulty == expected

    def test_multiple_modules_ordering(self, snapshot_id: uuid.UUID) -> None:
        structure = CourseStructure(
            title="Multi",
            modules=[
                ModuleOutput(title="M1"),
                ModuleOutput(title="M2"),
                ModuleOutput(title="M3"),
            ],
        )
        nodes = convert_to_structure_nodes(structure, snapshot_id)
        assert len(nodes) == 3
        assert [n.order for n in nodes] == [0, 1, 2]
        assert [n.title for n in nodes] == ["M1", "M2", "M3"]

    def test_empty_structure(self, snapshot_id: uuid.UUID) -> None:
        structure = CourseStructure(title="Empty")
        nodes = convert_to_structure_nodes(structure, snapshot_id)
        assert nodes == []

    def test_lesson_without_timecodes_or_slides(self, snapshot_id: uuid.UUID) -> None:
        structure = CourseStructure(
            title="C",
            modules=[ModuleOutput(title="M", lessons=[LessonOutput(title="L")])],
        )
        nodes = convert_to_structure_nodes(structure, snapshot_id)
        lesson = next(n for n in nodes if n.node_type == "lesson")
        assert lesson.timecodes is None
        assert lesson.slide_references is None

    def test_concept_without_optional_fields(self, snapshot_id: uuid.UUID) -> None:
        structure = CourseStructure(
            title="C",
            modules=[
                ModuleOutput(
                    title="M",
                    lessons=[
                        LessonOutput(
                            title="L",
                            concepts=[ConceptOutput(title="C", definition="D")],
                        )
                    ],
                )
            ],
        )
        nodes = convert_to_structure_nodes(structure, snapshot_id)
        concept = next(n for n in nodes if n.node_type == "concept")
        assert concept.timecodes is None
        assert concept.slide_references is None
        assert concept.web_references is None

    def test_unique_ids(self, snapshot_id: uuid.UUID) -> None:
        nodes = convert_to_structure_nodes(_minimal_structure(), snapshot_id)
        ids = [n.id for n in nodes]
        assert len(set(ids)) == len(ids)

    def test_module_empty_knowledge_skills_are_none(
        self, snapshot_id: uuid.UUID
    ) -> None:
        """Module with empty expected_knowledge/skills produces None."""
        structure = CourseStructure(
            title="C",
            modules=[ModuleOutput(title="M")],
        )
        nodes = convert_to_structure_nodes(structure, snapshot_id)
        assert nodes[0].expected_knowledge is None
        assert nodes[0].expected_skills is None
