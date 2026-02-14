"""Compare generated CourseStructure against a reference (gold standard).

Pure comparison logic — no I/O, no LLM calls.
Uses difflib.SequenceMatcher for fuzzy title matching.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any, ClassVar

from course_supporter.models.course import CourseStructure


@dataclass
class MetricResult:
    """Single evaluation metric result."""

    name: str
    score: float  # 0.0 - 1.0
    expected: int | str
    actual: int | str
    details: str = ""


@dataclass
class EvalReport:
    """Aggregated evaluation report."""

    metrics: list[MetricResult] = field(default_factory=list)
    overall_score: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        """Serialize report to a JSON-compatible dictionary."""
        return {
            "overall_score": round(self.overall_score, 4),
            "metrics": [
                {
                    "name": m.name,
                    "score": round(m.score, 4),
                    "expected": m.expected,
                    "actual": m.actual,
                    "details": m.details,
                }
                for m in self.metrics
            ],
        }

    def to_table(self) -> str:
        """Render report as an ASCII table."""
        header = f"{'Metric':<25} {'Score':>6} {'Expected':>10} {'Actual':>10}"
        separator = "-" * len(header)
        rows = [header, separator]
        for m in self.metrics:
            rows.append(
                f"{m.name:<25} {m.score:>6.2f} {m.expected!s:>10} {m.actual!s:>10}"
            )
        rows.append(separator)
        rows.append(f"{'OVERALL':<25} {self.overall_score:>6.2f}")
        return "\n".join(rows)


class StructureComparator:
    """Compare generated CourseStructure against a reference."""

    WEIGHTS: ClassVar[dict[str, float]] = {
        "module_count": 0.20,
        "lesson_count": 0.25,
        "concept_coverage": 0.30,
        "exercise_count": 0.15,
        "field_completeness": 0.10,
    }

    def compare(
        self,
        generated: CourseStructure,
        reference: CourseStructure,
    ) -> EvalReport:
        """Run all metrics and return a weighted EvalReport."""
        metrics = [
            self._module_count_score(generated, reference),
            self._lesson_count_score(generated, reference),
            self._concept_coverage_score(generated, reference),
            self._exercise_count_score(generated, reference),
            self._field_completeness_score(generated),
        ]
        overall = sum(self.WEIGHTS[m.name] * m.score for m in metrics)
        return EvalReport(metrics=metrics, overall_score=overall)

    # ------------------------------------------------------------------
    # Individual metric methods
    # ------------------------------------------------------------------

    @staticmethod
    def _module_count_score(
        generated: CourseStructure,
        reference: CourseStructure,
    ) -> MetricResult:
        gen_count = len(generated.modules)
        ref_count = len(reference.modules)
        if ref_count == 0:
            score = 1.0 if gen_count == 0 else 0.0
        else:
            score = 1.0 - abs(gen_count - ref_count) / ref_count
            score = max(score, 0.0)
        return MetricResult(
            name="module_count",
            score=score,
            expected=ref_count,
            actual=gen_count,
        )

    @staticmethod
    def _lesson_count_score(
        generated: CourseStructure,
        reference: CourseStructure,
    ) -> MetricResult:
        gen_count = sum(len(m.lessons) for m in generated.modules)
        ref_count = sum(len(m.lessons) for m in reference.modules)
        if ref_count == 0:
            score = 1.0 if gen_count == 0 else 0.0
        else:
            score = 1.0 - abs(gen_count - ref_count) / ref_count
            score = max(score, 0.0)
        return MetricResult(
            name="lesson_count",
            score=score,
            expected=ref_count,
            actual=gen_count,
        )

    def _concept_coverage_score(
        self,
        generated: CourseStructure,
        reference: CourseStructure,
    ) -> MetricResult:
        gen_titles = [
            c.title for m in generated.modules for ls in m.lessons for c in ls.concepts
        ]
        ref_titles = [
            c.title for m in reference.modules for ls in m.lessons for c in ls.concepts
        ]
        if not ref_titles:
            score = 1.0 if not gen_titles else 0.0
        else:
            score = self._fuzzy_match_titles(gen_titles, ref_titles)
        return MetricResult(
            name="concept_coverage",
            score=score,
            expected=len(ref_titles),
            actual=len(gen_titles),
            details=f"fuzzy matched {len(gen_titles)} vs {len(ref_titles)} titles",
        )

    @staticmethod
    def _exercise_count_score(
        generated: CourseStructure,
        reference: CourseStructure,
    ) -> MetricResult:
        gen_count = sum(
            len(ls.exercises) for m in generated.modules for ls in m.lessons
        )
        ref_count = sum(
            len(ls.exercises) for m in reference.modules for ls in m.lessons
        )
        if ref_count == 0:
            score = 1.0 if gen_count == 0 else 0.0
        else:
            score = 1.0 - abs(gen_count - ref_count) / ref_count
            score = max(score, 0.0)
        return MetricResult(
            name="exercise_count",
            score=score,
            expected=ref_count,
            actual=gen_count,
        )

    @staticmethod
    def _field_completeness_score(
        generated: CourseStructure,
    ) -> MetricResult:
        """Check that key fields are non-empty."""
        checks: list[bool] = []
        checks.append(bool(generated.title))
        checks.append(bool(generated.description))
        checks.append(bool(generated.learning_goal))
        checks.append(len(generated.modules) > 0)
        for module in generated.modules:
            checks.append(bool(module.title))
            checks.append(bool(module.description))
            checks.append(bool(module.learning_goal))
            checks.append(len(module.lessons) > 0)
            for lesson in module.lessons:
                checks.append(bool(lesson.title))
                checks.append(len(lesson.concepts) > 0)
        total = len(checks)
        filled = sum(checks)
        score = filled / total if total > 0 else 0.0
        return MetricResult(
            name="field_completeness",
            score=score,
            expected=total,
            actual=filled,
            details=f"{filled}/{total} fields filled",
        )

    # ------------------------------------------------------------------
    # Fuzzy matching
    # ------------------------------------------------------------------

    @staticmethod
    def _fuzzy_match_titles(
        generated: list[str],
        reference: list[str],
        threshold: float = 0.6,
    ) -> float:
        """Compute fraction of reference titles matched by generated titles.

        Uses difflib.SequenceMatcher for fuzzy comparison.
        Each reference title is matched at most once.
        """
        if not reference:
            return 1.0
        matched = 0
        used: set[int] = set()
        for ref_title in reference:
            best_ratio = 0.0
            best_idx = -1
            for idx, gen_title in enumerate(generated):
                if idx in used:
                    continue
                ratio = SequenceMatcher(
                    None,
                    ref_title.lower(),
                    gen_title.lower(),
                ).ratio()
                if ratio > best_ratio:
                    best_ratio = ratio
                    best_idx = idx
            if best_ratio >= threshold and best_idx >= 0:
                matched += 1
                used.add(best_idx)
        return matched / len(reference)
