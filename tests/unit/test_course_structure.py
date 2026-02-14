"""Tests for CourseStructure Pydantic output models."""

import pytest
from pydantic import ValidationError

from course_supporter.models.course import (
    ConceptOutput,
    CourseStructure,
    ExerciseOutput,
    LessonOutput,
    ModuleOutput,
    SlideRange,
    WebReference,
)


class TestSlideRange:
    def test_slide_range_basic(self) -> None:
        """SlideRange with start and end."""
        sr = SlideRange(start=1, end=15)
        assert sr.start == 1
        assert sr.end == 15

    def test_slide_range_serialization(self) -> None:
        """SlideRange serializes to dict matching JSONB format."""
        sr = SlideRange(start=5, end=20)
        data = sr.model_dump()
        assert data == {"start": 5, "end": 20}


class TestWebReference:
    def test_web_reference_defaults(self) -> None:
        """WebReference with only url, title and description default to ''."""
        ref = WebReference(url="https://example.com")
        assert ref.url == "https://example.com"
        assert ref.title == ""
        assert ref.description == ""


class TestExerciseOutput:
    def test_exercise_defaults(self) -> None:
        """ExerciseOutput defaults: difficulty=3, optional fields None."""
        ex = ExerciseOutput(description="Write a function")
        assert ex.difficulty_level == 3
        assert ex.reference_solution is None
        assert ex.grading_criteria is None

    def test_exercise_difficulty_validation_min(self) -> None:
        """ExerciseOutput rejects difficulty_level < 1."""
        with pytest.raises(ValidationError):
            ExerciseOutput(description="test", difficulty_level=0)

    def test_exercise_difficulty_validation_max(self) -> None:
        """ExerciseOutput rejects difficulty_level > 5."""
        with pytest.raises(ValidationError):
            ExerciseOutput(description="test", difficulty_level=6)

    def test_exercise_difficulty_valid_range(self) -> None:
        """ExerciseOutput accepts difficulty_level 1-5."""
        for level in (1, 2, 3, 4, 5):
            ex = ExerciseOutput(description="test", difficulty_level=level)
            assert ex.difficulty_level == level


class TestConceptOutput:
    def test_concept_defaults(self) -> None:
        """ConceptOutput defaults: empty lists for optional fields."""
        c = ConceptOutput(title="OOP", definition="Object-oriented programming")
        assert c.examples == []
        assert c.timecodes == []
        assert c.slide_references == []
        assert c.web_references == []


class TestCourseStructure:
    def test_course_structure_minimal(self) -> None:
        """CourseStructure with just title."""
        cs = CourseStructure(title="Python Course")
        assert cs.title == "Python Course"
        assert cs.description == ""
        assert cs.modules == []

    def test_course_structure_full_hierarchy(self) -> None:
        """Full hierarchy: CourseStructure -> Module -> Lesson -> Concept + Exercise."""
        structure = CourseStructure(
            title="Python 101",
            description="Intro to Python",
            modules=[
                ModuleOutput(
                    title="Basics",
                    lessons=[
                        LessonOutput(
                            title="Variables",
                            video_start_timecode="00:00:00",
                            video_end_timecode="00:15:30",
                            slide_range=SlideRange(start=1, end=10),
                            concepts=[
                                ConceptOutput(
                                    title="Variable Assignment",
                                    definition="Binding a name to a value",
                                    examples=["x = 42"],
                                    timecodes=["00:02:15"],
                                    slide_references=[2, 3],
                                    web_references=[
                                        WebReference(
                                            url="https://docs.python.org",
                                            title="Python Docs",
                                        )
                                    ],
                                )
                            ],
                            exercises=[
                                ExerciseOutput(
                                    description="Create variables of different types",
                                    difficulty_level=1,
                                )
                            ],
                        )
                    ],
                )
            ],
        )
        assert len(structure.modules) == 1
        assert len(structure.modules[0].lessons) == 1
        lesson = structure.modules[0].lessons[0]
        assert lesson.slide_range is not None
        assert lesson.slide_range.start == 1
        assert len(lesson.concepts) == 1
        assert len(lesson.exercises) == 1
        assert lesson.concepts[0].web_references[0].title == "Python Docs"

    def test_course_structure_serialization_round_trip(self) -> None:
        """CourseStructure serializes to JSON and deserializes back."""
        original = CourseStructure(
            title="Test",
            modules=[
                ModuleOutput(
                    title="M1",
                    lessons=[
                        LessonOutput(
                            title="L1",
                            concepts=[ConceptOutput(title="C1", definition="Def1")],
                        )
                    ],
                )
            ],
        )
        json_str = original.model_dump_json()
        restored = CourseStructure.model_validate_json(json_str)
        assert restored == original
