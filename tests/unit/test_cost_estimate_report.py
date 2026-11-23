"""The calibration report's arithmetic (BIL-D): which rows, which fact."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from course_supporter.cost_estimate.report import (
    CallRow,
    EstimateRow,
    compare,
    rows_of,
    select_reviews,
)
from course_supporter.llm.registry import load_registry

AT = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)


def _call(job: uuid.UUID, action: str, **kw: Any) -> CallRow:
    base: dict[str, Any] = {
        "job_id": job,
        "action": action,
        "provider": "deepseek_thinking",
        "model_id": "deepseek-v4-pro",
        "cost_usd": None,
        "cost_kind": "exact",
        "unit_in": None,
        "unit_in_estimate": None,
        "unit_out": None,
        "unit_out_estimate": None,
        "call_started_at": AT,
    }
    return CallRow(**(base | kw))


class TestReport:
    def test_review_fact_leaves_preparation_to_the_author(self) -> None:
        submission, job_a, job_b = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        estimate = EstimateRow(
            action_kind="review",
            action_key=str(submission),
            job_id=job_a,
            e_usd=Decimal("0.10"),
            breakdown=[{"step": "safety", "cost_usd": "0.10"}],
            created_at=AT,
        )
        by_job = {
            job_a: [
                _call(job_a, "safety", cost_usd=Decimal("0.04")),
                _call(job_a, "criteria_decomposition", cost_usd=Decimal("0.50")),
            ],
            job_b: [_call(job_b, "safety", cost_usd=Decimal("0.03"))],
        }
        rows = rows_of(
            estimate,
            by_job=by_job,
            submission_jobs={str(submission): [job_a, job_b]},
        )
        report = compare(estimate, rows, models={})
        assert report.fact_usd == Decimal("0.07")
        assert report.deviation_usd == Decimal("-0.03")
        assert report.step_deviation == {"safety": Decimal("-0.03")}

    def test_preparation_takes_its_stage_from_the_job(self) -> None:
        job = uuid.uuid4()
        estimate = EstimateRow(
            action_kind="task_preparation",
            action_key=f"{uuid.uuid4()}:hash:criteria_decomposition",
            job_id=job,
            e_usd=Decimal("0.02"),
            breakdown=[{"step": "criteria_decomposition", "cost_usd": "0.02"}],
            created_at=AT,
        )
        by_job = {
            job: [
                _call(job, "safety", cost_usd=Decimal("0.04")),
                _call(job, "criteria_decomposition", cost_usd=Decimal("0.025")),
            ]
        }
        rows = rows_of(estimate, by_job=by_job, submission_jobs={})
        assert compare(estimate, rows, models={}).fact_usd == Decimal("0.025")

    def test_unknown_rows_are_priced_by_the_estimate_formula(self) -> None:
        registry = load_registry(Path("config/external_services.yaml"))
        job = uuid.uuid4()
        estimate = EstimateRow(
            action_kind="processing",
            action_key=str(job),
            job_id=job,
            e_usd=Decimal("0.01"),
            breakdown=[
                {
                    "step": "video_pass_2a_mapping",
                    "cost_usd": "0.01",
                    "tokens_in": 1000,
                    "tokens_out": 1000,
                }
            ],
            created_at=AT,
        )
        rows = [
            # Its own input count, the step's expected output: 1000 + 1000.
            _call(
                job,
                "video_pass_2a_mapping",
                cost_kind="unknown",
                unit_in_estimate=1000,
            ),
            _call(job, "video_pass_2a_mapping", cost_usd=Decimal("0.001")),
            # A funds-port row is not a call.
            _call(job, "funds_port", provider=None, model_id=None),
        ]
        report = compare(estimate, rows, models=registry.models)
        assert report.calls == 2
        assert report.unknown_calls == 1
        assert report.unknown_share == Decimal("0.5")
        assert report.fact_usd == Decimal("0.00264") + Decimal("0.001")

    def test_csv_row(self) -> None:
        job = uuid.uuid4()
        estimate = EstimateRow(
            action_kind="processing",
            action_key=str(job),
            job_id=job,
            e_usd=Decimal("0.01"),
            breakdown=[],
            created_at=AT,
        )
        row = compare(estimate, [], models={}).as_csv()
        assert row["fact_usd"] == "0"
        assert row["unknown_share"] == "0"


class TestTestStudents:
    """BIL-5: the reviews of test students are left out unless asked."""

    @staticmethod
    def _estimate(kind: str, key: str) -> EstimateRow:
        return EstimateRow(
            action_kind=kind,
            action_key=key,
            job_id=uuid.uuid4(),
            e_usd=Decimal("0.01"),
            breakdown=[],
            created_at=AT,
        )

    def _sample(self) -> tuple[list[EstimateRow], dict[str, tuple[uuid.UUID, bool]]]:
        tester, student = uuid.uuid4(), uuid.uuid4()
        test_sub, sub_a, sub_b = (str(uuid.uuid4()) for _ in range(3))
        estimates = [
            self._estimate("review", test_sub),
            self._estimate("review", sub_a),
            self._estimate("review", sub_b),  # the same student, a second work
            self._estimate("processing", str(uuid.uuid4())),
        ]
        students = {
            test_sub: (tester, True),
            sub_a: (student, False),
            sub_b: (student, False),
        }
        return estimates, students

    def test_by_default_a_test_students_review_is_left_out(self) -> None:
        estimates, students = self._sample()
        kept, sample = select_reviews(estimates, students, include_test_students=False)
        assert kept == estimates[1:]  # the processing estimate stays
        assert (sample.test_reviews, sample.non_test_students) == (1, 1)
        assert sample.line() == (
            "review: 1 estimate(s) of test students excluded; "
            "1 distinct non-test student(s) in the sample"
        )

    def test_include_test_students_keeps_every_estimate(self) -> None:
        estimates, students = self._sample()
        kept, sample = select_reviews(estimates, students, include_test_students=True)
        assert kept == estimates
        assert (sample.test_reviews, sample.non_test_students) == (1, 1)
        assert "included (--include-test-students)" in sample.line()

    def test_other_actions_are_never_filtered(self) -> None:
        processing = self._estimate("processing", "k")
        preparation = self._estimate("task_preparation", "k")
        # Even a key that happens to name a test student's submission.
        students = {"k": (uuid.uuid4(), True)}
        kept, sample = select_reviews(
            [processing, preparation], students, include_test_students=False
        )
        assert kept == [processing, preparation]
        assert (sample.test_reviews, sample.non_test_students) == (0, 0)

    def test_a_review_of_an_unknown_submission_is_kept_and_names_no_student(
        self,
    ) -> None:
        estimate = self._estimate("review", str(uuid.uuid4()))
        kept, sample = select_reviews([estimate], {}, include_test_students=False)
        assert kept == [estimate]
        assert (sample.test_reviews, sample.non_test_students) == (0, 0)
