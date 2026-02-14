"""Tests for StructureComparator evaluation logic."""

from pathlib import Path

from course_supporter.evals.comparator import (
    EvalReport,
    MetricResult,
    StructureComparator,
)
from course_supporter.models.course import (
    ConceptOutput,
    CourseStructure,
    ExerciseOutput,
    LessonOutput,
    ModuleOutput,
)

FIXTURE_DIR = Path(__file__).resolve().parent.parent.parent / "fixtures" / "eval"


def _load_reference() -> CourseStructure:
    raw = (FIXTURE_DIR / "reference_structure.json").read_text()
    return CourseStructure.model_validate_json(raw)


def _minimal_structure(
    n_modules: int = 0,
    n_lessons: int = 0,
    concept_titles: list[list[list[str]]] | None = None,
    n_exercises: int = 0,
    *,
    fill_fields: bool = True,
) -> CourseStructure:
    """Build a CourseStructure with configurable counts."""
    modules = []
    for mi in range(n_modules):
        lessons = []
        for li in range(n_lessons):
            concepts = []
            if (
                concept_titles
                and mi < len(concept_titles)
                and li < len(concept_titles[mi])
            ):
                for title in concept_titles[mi][li]:
                    defn = "def" if fill_fields else ""
                    concepts.append(ConceptOutput(title=title, definition=defn))
            exercises = [
                ExerciseOutput(description=f"Exercise {ei + 1}")
                for ei in range(n_exercises)
            ]
            lessons.append(
                LessonOutput(
                    title=f"Lesson {mi + 1}.{li + 1}" if fill_fields else "",
                    concepts=concepts,
                    exercises=exercises,
                )
            )
        modules.append(
            ModuleOutput(
                title=f"Module {mi + 1}" if fill_fields else "",
                description=f"Desc {mi + 1}" if fill_fields else "",
                learning_goal=f"Goal {mi + 1}" if fill_fields else "",
                lessons=lessons,
            )
        )
    return CourseStructure(
        title="Test Course" if fill_fields else "",
        description="Test Description" if fill_fields else "",
        learning_goal="Test Goal" if fill_fields else "",
        modules=modules,
    )


class TestIdenticalStructures:
    """Identical structures should score 1.0."""

    def test_identical_structures_score_1_0(self) -> None:
        """Comparing a structure to itself yields overall_score == 1.0."""
        ref = _load_reference()
        comparator = StructureComparator()
        report = comparator.compare(ref, ref)
        assert report.overall_score == 1.0
        for m in report.metrics:
            assert m.score == 1.0


class TestEmptyGenerated:
    """Empty generated structure should score near 0."""

    def test_empty_generated_scores_near_0(self) -> None:
        """An empty structure compared to reference scores close to zero."""
        ref = _load_reference()
        empty = CourseStructure(title="")
        comparator = StructureComparator()
        report = comparator.compare(empty, ref)
        assert report.overall_score < 0.15


class TestModuleCount:
    """Module count metric tests."""

    def test_module_count_exact_match(self) -> None:
        """3 modules vs 3 reference modules → score 1.0."""
        ref = _load_reference()
        gen = _minimal_structure(n_modules=3)
        comparator = StructureComparator()
        report = comparator.compare(gen, ref)
        module_metric = next(m for m in report.metrics if m.name == "module_count")
        assert module_metric.score == 1.0

    def test_module_count_mismatch(self) -> None:
        """2 modules vs 3 reference modules → score < 1.0."""
        ref = _load_reference()
        gen = _minimal_structure(n_modules=2)
        comparator = StructureComparator()
        report = comparator.compare(gen, ref)
        module_metric = next(m for m in report.metrics if m.name == "module_count")
        assert module_metric.score < 1.0
        assert module_metric.score > 0.0


class TestLessonCount:
    """Lesson count metric tests."""

    def test_lesson_count_partial_match(self) -> None:
        """4 lessons vs 6 reference → partial score."""
        ref = _load_reference()
        gen = _minimal_structure(n_modules=2, n_lessons=2)
        comparator = StructureComparator()
        report = comparator.compare(gen, ref)
        lesson_metric = next(m for m in report.metrics if m.name == "lesson_count")
        assert 0.0 < lesson_metric.score < 1.0


class TestConceptCoverage:
    """Concept coverage metric tests with fuzzy matching."""

    def test_concept_coverage_fuzzy_match(self) -> None:
        """Similar concept titles should be matched via fuzzy matching."""
        ref = _minimal_structure(
            n_modules=1,
            n_lessons=1,
            concept_titles=[[["Variable Assignment", "Dynamic Typing"]]],
        )
        gen = _minimal_structure(
            n_modules=1,
            n_lessons=1,
            concept_titles=[[["Assignment of Variables", "Dynamic Type System"]]],
        )
        comparator = StructureComparator()
        report = comparator.compare(gen, ref)
        concept_metric = next(m for m in report.metrics if m.name == "concept_coverage")
        assert concept_metric.score > 0.0

    def test_concept_coverage_no_match(self) -> None:
        """Completely unrelated concept titles yield 0 coverage."""
        ref = _minimal_structure(
            n_modules=1,
            n_lessons=1,
            concept_titles=[[["Variable Assignment"]]],
        )
        gen = _minimal_structure(
            n_modules=1,
            n_lessons=1,
            concept_titles=[[["Quantum Entanglement"]]],
        )
        comparator = StructureComparator()
        report = comparator.compare(gen, ref)
        concept_metric = next(m for m in report.metrics if m.name == "concept_coverage")
        assert concept_metric.score == 0.0


class TestExerciseCount:
    """Exercise count metric tests."""

    def test_exercise_count_metric(self) -> None:
        """Exercise count metric reflects actual vs expected."""
        ref = _load_reference()
        gen = _minimal_structure(n_modules=3, n_lessons=2, n_exercises=1)
        comparator = StructureComparator()
        report = comparator.compare(gen, ref)
        ex_metric = next(m for m in report.metrics if m.name == "exercise_count")
        assert ex_metric.score == 1.0
        assert ex_metric.expected == 6
        assert ex_metric.actual == 6


class TestFieldCompleteness:
    """Field completeness metric tests."""

    def test_field_completeness_all_filled(self) -> None:
        """All required fields filled → score 1.0."""
        gen = _minimal_structure(
            n_modules=2,
            n_lessons=1,
            concept_titles=[[["C1"]], [["C2"]]],
            fill_fields=True,
        )
        comparator = StructureComparator()
        report = comparator.compare(gen, gen)
        fc_metric = next(m for m in report.metrics if m.name == "field_completeness")
        assert fc_metric.score == 1.0

    def test_field_completeness_missing_fields(self) -> None:
        """Missing fields → score < 1.0."""
        gen = _minimal_structure(
            n_modules=1,
            n_lessons=1,
            concept_titles=[[["C1"]]],
            fill_fields=False,
        )
        comparator = StructureComparator()
        report = comparator.compare(gen, gen)
        fc_metric = next(m for m in report.metrics if m.name == "field_completeness")
        assert fc_metric.score < 1.0


class TestFuzzyMatch:
    """Tests for _fuzzy_match_titles static method."""

    def test_fuzzy_match_threshold(self) -> None:
        """Titles below threshold are not matched."""
        score = StructureComparator._fuzzy_match_titles(
            generated=["xyz abc"],
            reference=["Variable Assignment"],
            threshold=0.6,
        )
        assert score == 0.0

        score_match = StructureComparator._fuzzy_match_titles(
            generated=["Variable Assignment"],
            reference=["Variable Assignment"],
            threshold=0.6,
        )
        assert score_match == 1.0


class TestReportFormats:
    """Tests for EvalReport serialization."""

    def test_report_to_dict_format(self) -> None:
        """to_dict returns correct JSON-compatible structure."""
        report = EvalReport(
            metrics=[
                MetricResult(name="test_metric", score=0.85, expected=10, actual=8),
            ],
            overall_score=0.85,
        )
        d = report.to_dict()
        assert d["overall_score"] == 0.85
        assert len(d["metrics"]) == 1
        assert d["metrics"][0]["name"] == "test_metric"
        assert d["metrics"][0]["score"] == 0.85
        assert "expected" in d["metrics"][0]
        assert "actual" in d["metrics"][0]

    def test_report_to_table_includes_scores(self) -> None:
        """to_table output contains metric names and OVERALL label."""
        report = EvalReport(
            metrics=[
                MetricResult(name="module_count", score=1.0, expected=3, actual=3),
                MetricResult(name="lesson_count", score=0.67, expected=6, actual=4),
            ],
            overall_score=0.82,
        )
        table = report.to_table()
        assert "module_count" in table
        assert "lesson_count" in table
        assert "OVERALL" in table
        assert "0.82" in table
