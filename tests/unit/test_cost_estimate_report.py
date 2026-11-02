"""The calibration report's arithmetic (BIL-D): which rows, which fact."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from course_supporter.cost_estimate.report import CallRow, EstimateRow, compare, rows_of
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
