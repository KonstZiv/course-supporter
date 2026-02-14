"""Tests for eval reference structure fixture."""

from pathlib import Path

from course_supporter.models.course import CourseStructure

FIXTURE_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "eval"


class TestReferenceStructure:
    """Validate the gold-standard reference structure fixture."""

    def test_reference_loads_as_course_structure(self) -> None:
        """JSON fixture deserializes into a valid CourseStructure."""
        raw = (FIXTURE_DIR / "reference_structure.json").read_text()
        structure = CourseStructure.model_validate_json(raw)
        assert structure.title
        assert structure.description
        assert structure.learning_goal

    def test_reference_has_expected_modules(self) -> None:
        """Reference contains 3 modules with expected titles."""
        raw = (FIXTURE_DIR / "reference_structure.json").read_text()
        structure = CourseStructure.model_validate_json(raw)
        assert len(structure.modules) == 3
        titles = [m.title for m in structure.modules]
        assert "Variables and Data Types" in titles
        assert "Functions" in titles
        assert "Loops and Iteration" in titles

    def test_reference_has_concepts_and_exercises(self) -> None:
        """Every lesson has at least one concept and one exercise."""
        raw = (FIXTURE_DIR / "reference_structure.json").read_text()
        structure = CourseStructure.model_validate_json(raw)
        for module in structure.modules:
            for lesson in module.lessons:
                assert len(lesson.concepts) >= 1, (
                    f"Lesson '{lesson.title}' has no concepts"
                )
                assert len(lesson.exercises) >= 1, (
                    f"Lesson '{lesson.title}' has no exercises"
                )
