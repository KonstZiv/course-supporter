"""The arithmetic of a shadow price estimate (BIL-D): steps, peak, units.

Purpose:
    Before an action runs, the platform will show its user an expected price.
    This module prices one step of an action from the catalog
    (``config/external_services.yaml``) and turns the action's expected cost E
    into units. It is pure — no I/O, no clock, no network: the moment of the
    estimate is passed in, so a test pins it.

Interface:
    :func:`window_with_guard` — peak or off-peak for an estimate made at an
    instant: peak inside the peak hours and ``guard_minutes`` before they start.
    :func:`price_token_step` / :func:`price_audio_step` — one step at its
    expected size, on the model of the first rung of its ladder.
    :class:`ActionEstimate` — the result: E, the steps, the catalog version, the
    inputs, the "uncalibrated parameters" flag.
    :func:`price_units`, :func:`reserve_units`, :func:`fact_units` — dollars to
    units, rounded once per action.
    :func:`expected_call_cost` — what one attempt of a step is expected to cost,
    for register rows whose cost is ``unknown`` (the calibration report).

The formulas::

    step  = (tokens_in * p_in + tokens_out * p_out) / 1000 * calls
    audio = ceil(duration / granularity) * granularity * p_per_minute / 60
    E     = sum of the steps (fallback rungs are not in E)
    P_units       = ceil(E * (1 + margin) * K)
    reserve_units = ceil(2 * E * K)
    fact_units    = round_half_up(usd * K), at least 1 when usd > 0

with prices multiplied by the model's peak multiplier when the window is peak.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import ROUND_CEILING, ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal
from enum import StrEnum
from typing import Any, Final

from course_supporter.cost_kinds import TariffWindow
from course_supporter.llm.registry import ModelConfig

MONEY_QUANTUM: Final = Decimal("1e-12")
"""The scale of E, as of every money column of the register."""

_PER_1K: Final = Decimal(1000)
_SECONDS_PER_MINUTE: Final = Decimal(60)


class ActionKind(StrEnum):
    """What kind of action an estimate is for; one action is one key."""

    REVIEW = "review"
    """One submission (``homework_submissions.id``) with all its jobs."""

    PROCESSING = "processing"
    """One processing job of an author's material (``jobs.id``)."""

    TASK_PREPARATION = "task_preparation"
    """One (task version, kind of preparation) pair."""


class EstimateError(ValueError):
    """The estimate cannot be computed: a model, a price or a setting is missing."""


def _money(value: Decimal) -> Decimal:
    return value.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_EVEN)


# ── Peak hours with the guard interval ──


def window_with_guard(
    model: ModelConfig, at: datetime, guard_minutes: int
) -> TariffWindow:
    """The window an estimate made at ``at`` prices ``model`` in.

    Peak when ``at`` is inside the model's peak hours, or a peak starts within
    the next ``guard_minutes`` (the action may run into it). Every minute of
    the guard interval is looked at, so a peak span shorter than the guard is
    not stepped over. Uses the tariff's own window logic
    (:meth:`~course_supporter.llm.registry.TariffConfig.window_at`); it has no
    holiday calendar, and neither has this (open debt of BIL-PRICES).
    """
    tariff = model.tariff
    if tariff is None:
        return TariffWindow.FLAT
    for minute in range(guard_minutes + 1):
        if tariff.window_at(at + timedelta(minutes=minute)) is TariffWindow.PEAK:
            return TariffWindow.PEAK
    return TariffWindow.OFF_PEAK


def _applied(model: ModelConfig, price: Decimal, window: TariffWindow) -> Decimal:
    if model.tariff is None:
        return price
    return price * model.tariff.multiplier(window)


# ── Steps ──


@dataclass(frozen=True, slots=True, kw_only=True)
class StepEstimate:
    """One step of an action at its expected size.

    ``tokens_in`` / ``tokens_out`` are per call; ``calls`` is the expected
    number of calls (once per chunk, slide ...; a share of segments for a
    conditional step, so not always whole). A speech-to-text step carries
    ``billed_seconds`` and its per-minute price instead of tokens.
    """

    step: str
    provider: str
    model: str
    calls: Decimal
    tokens_in: int | None
    tokens_out: int | None
    billed_seconds: int | None
    price_in_per_1k_usd: Decimal | None
    price_out_per_1k_usd: Decimal | None
    price_per_minute_usd: Decimal | None
    window: TariffWindow
    cost_usd: Decimal

    @property
    def peak(self) -> bool:
        """Whether the step was priced at the peak tariff."""
        return self.window is TariffWindow.PEAK

    def as_json(self) -> dict[str, Any]:
        """The step as the ``breakdown`` column stores it (numbers as strings)."""

        def text(value: Decimal | None) -> str | None:
            return None if value is None else str(value)

        return {
            "step": self.step,
            "provider": self.provider,
            "model": self.model,
            "calls": str(self.calls),
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "billed_seconds": self.billed_seconds,
            "price_in_per_1k_usd": text(self.price_in_per_1k_usd),
            "price_out_per_1k_usd": text(self.price_out_per_1k_usd),
            "price_per_minute_usd": text(self.price_per_minute_usd),
            "window": self.window.value,
            "peak": self.peak,
            "cost_usd": str(self.cost_usd),
        }


def _model(models: dict[str, ModelConfig], step: str, model_id: str) -> ModelConfig:
    model = models.get(model_id)
    if model is None:
        raise EstimateError(f"step {step!r}: model {model_id!r} is not in the catalog")
    return model


def price_token_step(
    models: dict[str, ModelConfig],
    *,
    step: str,
    provider: str,
    model_id: str,
    tokens_in: int,
    tokens_out: int,
    at: datetime,
    guard_minutes: int,
    calls: Decimal | int = 1,
) -> StepEstimate:
    """Price ``calls`` LLM calls of ``tokens_in`` + ``tokens_out`` each.

    Cached input is not assumed: an estimate does not know what the provider
    will have cached, so every input token is priced at the full rate.

    Raises:
        EstimateError: the model is not in the catalog or has no token price.
    """
    model = _model(models, step, model_id)
    p_in, p_out = model.prices.in_per_1k, model.prices.out_per_1k
    if p_in is None or p_out is None:
        raise EstimateError(f"step {step!r}: model {model_id!r} has no token price")
    window = window_with_guard(model, at, guard_minutes)
    p_in, p_out = _applied(model, p_in, window), _applied(model, p_out, window)
    cost = (tokens_in * p_in + tokens_out * p_out) / _PER_1K * calls
    return StepEstimate(
        step=step,
        provider=provider,
        model=model_id,
        calls=Decimal(calls),
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        billed_seconds=None,
        price_in_per_1k_usd=p_in,
        price_out_per_1k_usd=p_out,
        price_per_minute_usd=None,
        window=window,
        cost_usd=_money(cost),
    )


def billed_seconds(model: ModelConfig, duration_sec: float) -> int:
    """The duration rounded up to the model's billing granularity.

    The same rule as the register's (``llm/pricing.py::price_audio_call``):
    without a granularity in the catalog, a whole second.
    """
    step = model.billing_granularity_sec or 1
    return math.ceil(duration_sec / step) * step


def price_audio_step(
    models: dict[str, ModelConfig],
    *,
    step: str,
    provider: str,
    model_id: str,
    duration_sec: float,
    at: datetime,
    guard_minutes: int,
) -> StepEstimate:
    """Price one speech-to-text pass over ``duration_sec`` of audio.

    Raises:
        EstimateError: the model is not in the catalog or has no minute price.
    """
    model = _model(models, step, model_id)
    per_minute = model.prices.per_minute
    if per_minute is None:
        raise EstimateError(f"step {step!r}: model {model_id!r} has no minute price")
    window = window_with_guard(model, at, guard_minutes)
    per_minute = _applied(model, per_minute, window)
    seconds = billed_seconds(model, duration_sec)
    return StepEstimate(
        step=step,
        provider=provider,
        model=model_id,
        calls=Decimal(1),
        tokens_in=None,
        tokens_out=None,
        billed_seconds=seconds,
        price_in_per_1k_usd=None,
        price_out_per_1k_usd=None,
        price_per_minute_usd=per_minute,
        window=window,
        cost_usd=_money(seconds * per_minute / _SECONDS_PER_MINUTE),
    )


# ── The action ──


@dataclass(frozen=True, slots=True, kw_only=True)
class ActionEstimate:
    """The expected cost E of one action, and what it was computed from.

    ``uncalibrated`` is True when any parameter the estimate used is still
    marked "не калібровано" in the settings, or when no units-per-dollar rate
    is set — the number is then a starting point, not a measurement.
    """

    kind: ActionKind
    e_usd: Decimal
    steps: tuple[StepEstimate, ...]
    catalog_version: str | None
    inputs: dict[str, Any] = field(default_factory=dict)
    uncalibrated: bool = True

    @staticmethod
    def of(
        kind: ActionKind,
        steps: list[StepEstimate],
        *,
        catalog_version: str | None,
        inputs: dict[str, Any],
        uncalibrated: bool,
    ) -> ActionEstimate:
        """Sum the steps into E."""
        total = sum((s.cost_usd for s in steps), Decimal(0))
        return ActionEstimate(
            kind=kind,
            e_usd=_money(total),
            steps=tuple(steps),
            catalog_version=catalog_version,
            inputs=inputs,
            uncalibrated=uncalibrated,
        )

    def breakdown(self) -> list[dict[str, Any]]:
        """The steps as the ``breakdown`` column stores them."""
        return [s.as_json() for s in self.steps]


# ── Units: one rounding per action ──


def _ceil(value: Decimal) -> int:
    return int(value.to_integral_value(rounding=ROUND_CEILING))


def price_units(
    e_usd: Decimal, *, margin: Decimal, units_per_usd: Decimal | None
) -> int | None:
    """P in units: ``ceil(E * (1 + margin) * K)``; ``None`` while K is not set."""
    if units_per_usd is None:
        return None
    return _ceil(e_usd * (1 + margin) * units_per_usd)


def reserve_units(e_usd: Decimal, *, units_per_usd: Decimal | None) -> int | None:
    """The reserve in units: ``ceil(2 * E * K)``; ``None`` while K is not set.

    The amount actually held at the start is the smaller of this and the
    available units — decided by whoever holds them, not here.
    """
    if units_per_usd is None:
        return None
    return _ceil(2 * e_usd * units_per_usd)


def fact_units(usd: Decimal, *, units_per_usd: Decimal | None) -> int | None:
    """The actual cost in units: half up to a whole unit, at least 1 if anything.

    0 when the action cost nothing; ``None`` while K is not set.
    """
    if units_per_usd is None:
        return None
    if usd <= 0:
        return 0
    units = int((usd * units_per_usd).to_integral_value(rounding=ROUND_HALF_UP))
    return max(units, 1)


# ── One attempt, for register rows of unknown cost ──


def expected_call_cost(
    model: ModelConfig | None,
    *,
    at: datetime,
    guard_minutes: int,
    tokens_in: int | None = None,
    tokens_out: int | None = None,
    duration_sec: float | None = None,
) -> Decimal | None:
    """What one attempt is expected to cost, by the formula of the estimate.

    For a register row whose cost is ``unknown``: the model the row names, the
    instant it was sent, and the step's expected sizes (tokens, or the audio
    duration). ``None`` when the model or the sizes give no number.
    """
    if model is None:
        return None
    try:
        if duration_sec is not None:
            return price_audio_step(
                {"m": model},
                step="call",
                provider=model.provider,
                model_id="m",
                duration_sec=duration_sec,
                at=at,
                guard_minutes=guard_minutes,
            ).cost_usd
        if tokens_in is None or tokens_out is None:
            return None
        return price_token_step(
            {"m": model},
            step="call",
            provider=model.provider,
            model_id="m",
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            at=at,
            guard_minutes=guard_minutes,
        ).cost_usd
    except EstimateError:
        return None
