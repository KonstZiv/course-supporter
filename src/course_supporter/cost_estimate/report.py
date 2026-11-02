"""Shadow estimate against fact — the arithmetic of the calibration report (BIL-D).

Pure: the CLI (``scripts/cost_estimate_report.py``) reads the rows, this
module compares them. For one action with a shadow estimate:

* the fact is the sum of its register rows — ``exact`` and ``estimated`` as
  written, ``unknown`` by ``core.expected_call_cost`` (the row's model and
  moment, its own token counts where it has them, the step's expected sizes
  from the estimate where it has not);
* the deviation per step is fact minus estimate for each stage name;
* the share of unknown calls is unknown rows over priced rows.

Which rows belong to which action (``rows_of``):

* review — every row of every job whose subject is the submission, except the
  rows of the preparation stages: those are the author's, whoever's job ran
  them (operator, BIL-D);
* processing — the rows of the job;
* task preparation — the rows of its stage in the job the estimate was made in.

Rows without a provider (funds-port and metrics rows) are not calls.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Final

from course_supporter.cost_estimate.core import ActionKind, expected_call_cost
from course_supporter.llm.registry import ModelConfig

PREPARATION_STAGES: Final = frozenset({"criteria_decomposition", "key_explanation"})
"""Stages whose calls are the author's preparation, never a review's fact."""


@dataclass(frozen=True, slots=True, kw_only=True)
class CallRow:
    """The columns of one register row the report reads."""

    job_id: uuid.UUID
    action: str
    provider: str | None
    model_id: str | None
    cost_usd: Decimal | None
    cost_kind: str | None
    unit_in: int | None
    unit_in_estimate: int | None
    unit_out: int | None
    unit_out_estimate: int | None
    call_started_at: datetime | None


@dataclass(frozen=True, slots=True, kw_only=True)
class EstimateRow:
    """The columns of one ``action_cost_estimates`` row the report reads."""

    action_kind: str
    action_key: str
    job_id: uuid.UUID | None
    e_usd: Decimal
    breakdown: Sequence[Mapping[str, Any]]
    created_at: datetime


@dataclass(frozen=True, slots=True, kw_only=True)
class ReportRow:
    """One action: estimate, fact, and where they part."""

    action_kind: str
    action_key: str
    e_usd: Decimal
    fact_usd: Decimal
    deviation_usd: Decimal
    calls: int
    unknown_calls: int
    unknown_share: Decimal
    step_deviation: dict[str, Decimal]

    def as_csv(self) -> dict[str, str]:
        """The row as the CSV writes it."""
        return {
            "action_kind": self.action_kind,
            "action_key": self.action_key,
            "e_usd": str(self.e_usd),
            "fact_usd": str(self.fact_usd),
            "deviation_usd": str(self.deviation_usd),
            "calls": str(self.calls),
            "unknown_calls": str(self.unknown_calls),
            "unknown_share": str(self.unknown_share),
            "step_deviation": "; ".join(
                f"{k}={v}" for k, v in sorted(self.step_deviation.items())
            ),
        }


def rows_of(
    estimate: EstimateRow,
    *,
    by_job: Mapping[uuid.UUID, Sequence[CallRow]],
    submission_jobs: Mapping[str, Sequence[uuid.UUID]],
) -> list[CallRow]:
    """The register rows of one action (the module's rule)."""
    kind = ActionKind(estimate.action_kind)
    if kind is ActionKind.REVIEW:
        jobs = submission_jobs.get(estimate.action_key, ())
        return [
            r
            for job in jobs
            for r in by_job.get(job, ())
            if r.action not in PREPARATION_STAGES
        ]
    if estimate.job_id is None:
        return []
    rows = list(by_job.get(estimate.job_id, ()))
    if kind is ActionKind.TASK_PREPARATION:
        stage = estimate.action_key.rsplit(":", 1)[-1]
        rows = [r for r in rows if r.action == stage]
    return rows


def _expected(
    row: CallRow,
    step: Mapping[str, Any] | None,
    models: Mapping[str, ModelConfig],
    at: datetime,
) -> Decimal | None:
    model = models.get(row.model_id or "")
    if step is not None and step.get("billed_seconds") is not None:
        return expected_call_cost(
            model,
            at=at,
            guard_minutes=0,
            duration_sec=float(step["billed_seconds"]),
        )
    tokens_in = row.unit_in or row.unit_in_estimate
    tokens_out = row.unit_out or row.unit_out_estimate
    if step is not None:
        tokens_in = tokens_in or step.get("tokens_in")
        tokens_out = tokens_out or step.get("tokens_out")
    return expected_call_cost(
        model,
        at=at,
        guard_minutes=0,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
    )


def compare(
    estimate: EstimateRow,
    rows: Iterable[CallRow],
    *,
    models: Mapping[str, ModelConfig],
) -> ReportRow:
    """Estimate against the fact of one action.

    An unknown row is priced at the moment it was sent, so without the guard
    interval: the window it fell in is known.
    """
    steps: dict[str, Mapping[str, Any]] = {}
    estimated_by_step: dict[str, Decimal] = defaultdict(Decimal)
    for s in estimate.breakdown:
        steps.setdefault(str(s["step"]), s)
        estimated_by_step[str(s["step"])] += Decimal(str(s["cost_usd"]))
    fact_by_step: dict[str, Decimal] = defaultdict(Decimal)
    calls = unknown = 0
    for row in rows:
        if row.provider is None:
            continue
        calls += 1
        if row.cost_kind == "unknown" or row.cost_usd is None:
            unknown += 1
            value = _expected(
                row,
                steps.get(row.action),
                models,
                row.call_started_at or estimate.created_at,
            )
            fact_by_step[row.action] += value or Decimal(0)
        else:
            fact_by_step[row.action] += row.cost_usd
    fact = sum(fact_by_step.values(), Decimal(0))
    names = set(fact_by_step) | set(estimated_by_step)
    return ReportRow(
        action_kind=estimate.action_kind,
        action_key=estimate.action_key,
        e_usd=estimate.e_usd,
        fact_usd=fact,
        deviation_usd=fact - estimate.e_usd,
        calls=calls,
        unknown_calls=unknown,
        unknown_share=(Decimal(unknown) / calls) if calls else Decimal(0),
        step_deviation={
            n: fact_by_step.get(n, Decimal(0)) - estimated_by_step.get(n, Decimal(0))
            for n in names
        },
    )
