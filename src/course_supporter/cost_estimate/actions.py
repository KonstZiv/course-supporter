"""The expected cost E of each kind of action (BIL-D). Pure: no I/O, no clock.

Purpose:
    One function per kind of action turns what is known about it before its
    first paid step — the path and the submission's size, the task document's
    size, the material's cost drivers — into an
    :class:`~course_supporter.cost_estimate.core.ActionEstimate`.

The rule for every LLM step::

    tokens_in  = prompt_tokens (settings) + the material the step reads
    rung       = the first rung of the stage's ladder the input fits by the
                 router's input_budget_ratio rule (llm/stage_router.py)
    tokens_out = min(output_tokens (settings), the rung's output cap)
    cost       = (tokens_in * p_in + tokens_out * p_out) / 1000 * calls

Speech-to-text is priced by the duration, on the first model of the
``transcribe`` chain, rounded up to its billing granularity. Fallback rungs and
fallback STT models are not in E.

Interface:
    :func:`estimate_review` — one submission on the new path.
    :func:`estimate_task_preparation` — criteria decomposition or key explanation.
    :func:`estimate_processing` — one processing job of a material.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_CEILING, Decimal
from typing import Any, Protocol

from course_supporter.cost_estimate.core import (
    ActionEstimate,
    ActionKind,
    EstimateError,
    StepEstimate,
    price_audio_step,
    price_token_step,
)
from course_supporter.cost_estimate.settings import EstimateSettings, StepSettings
from course_supporter.llm.registry import ModelConfig
from course_supporter.llm.token_budget import tokens_for_chars

STT_ACTION = "transcribe"
"""The registry action whose chain the speech-to-text router walks."""


class Rung(Protocol):
    """What an estimate reads of a rung: a ladder rung or a path rung."""

    @property
    def provider(self) -> str: ...

    @property
    def model(self) -> str: ...

    @property
    def max_output_tokens(self) -> int | None: ...


class LadderStage(Protocol):
    """What an estimate reads of a stage: a ladder stage or a path stage."""

    @property
    def ladder(self) -> Sequence[Rung]: ...

    @property
    def input_budget_ratio(self) -> float | None: ...


@dataclass(frozen=True, slots=True, kw_only=True)
class Catalog:
    """The catalog an estimate prices from: models, the STT chain, the version."""

    models: Mapping[str, ModelConfig]
    stt_chain: Sequence[ModelConfig]
    version: str | None


# ── One LLM step ──


def first_fitting_rung(
    stage_name: str,
    stage: LadderStage,
    models: Mapping[str, ModelConfig],
    tokens_in: int,
) -> Rung:
    """The first rung the router would not skip for the size of the input.

    The router skips a rung when the stage declares ``input_budget_ratio`` and
    the input exceeds ``int(ratio * max_context)`` of the rung's model; the
    estimate prices the rung the router would call, not one it would skip.

    Raises:
        EstimateError: no rung fits — the router would exhaust the ladder.
    """
    ratio = stage.input_budget_ratio
    for entry in stage.ladder:
        if ratio is None:
            return entry
        model = models.get(entry.model)
        if model is None or model.max_context is None:
            continue
        if tokens_in <= int(ratio * model.max_context):
            return entry
    raise EstimateError(
        f"step {stage_name!r}: no rung fits an input of ~{tokens_in} tokens"
    )


def _output_cap(stage: LadderStage, entry: Rung, model: ModelConfig) -> int:
    """What the rung may answer: its pin, else the stage ceiling, else the model's.

    A path stage pins its ``ceilings.output_tokens`` on an unpinned rung
    (``homework/path_stages.py::_execution``); a ladder stage sends the
    model's own cap (``stage_router.py::_build_request``).
    """
    if entry.max_output_tokens is not None:
        return entry.max_output_tokens
    ceilings = getattr(stage, "ceilings", None)
    if ceilings is not None:
        return int(ceilings.output_tokens)
    if model.max_output_tokens is None:
        raise EstimateError(f"model {entry.model!r} has no output cap")
    return model.max_output_tokens


def llm_step(
    stage_name: str,
    stage: LadderStage,
    step: StepSettings,
    *,
    catalog: Catalog,
    material_tokens: int,
    at: datetime,
    guard_minutes: int,
    calls: Decimal | None = None,
) -> StepEstimate:
    """Price one LLM step at its expected size (the module's rule)."""
    tokens_in = step.prompt_tokens + material_tokens
    entry = first_fitting_rung(stage_name, stage, catalog.models, tokens_in)
    model = catalog.models.get(entry.model)
    if model is None:
        raise EstimateError(f"step {stage_name!r}: model {entry.model!r} unknown")
    tokens_out = min(step.output_tokens, _output_cap(stage, entry, model))
    return price_token_step(
        dict(catalog.models),
        step=stage_name,
        provider=entry.provider,
        model_id=entry.model,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        at=at,
        guard_minutes=guard_minutes,
        calls=step.calls if calls is None else calls,
    )


def _setting(table: Mapping[str, StepSettings], name: str) -> StepSettings:
    try:
        return table[name]
    except KeyError:
        raise EstimateError(f"no settings for step {name!r}") from None


def _stage(stages: Mapping[str, LadderStage], name: str) -> LadderStage:
    try:
        return stages[name]
    except KeyError:
        raise EstimateError(f"no ladder for step {name!r}") from None


def _uncalibrated(settings: EstimateSettings, *flags: bool) -> bool:
    return settings.units_per_usd is None or not all(flags)


# ── Review ──


def estimate_review(
    *,
    stage_names: Sequence[str],
    path_stages: Mapping[str, LadderStage],
    settings: EstimateSettings,
    catalog: Catalog,
    submission_chars: int,
    at: datetime,
    inputs: Mapping[str, Any] | None = None,
) -> ActionEstimate:
    """E of one submission on the new path: every stage of its path once.

    Each stage reads the whole submission text (its doors already held it to
    the text budget) beside its prompt. A path without stages — a test — costs
    nothing: E = 0.
    """
    material = tokens_for_chars(submission_chars)
    steps: list[StepEstimate] = []
    flags: list[bool] = []
    for name in stage_names:
        step = _setting(settings.review, name)
        flags.append(step.calibrated)
        steps.append(
            llm_step(
                name,
                _stage(path_stages, name),
                step,
                catalog=catalog,
                material_tokens=material,
                at=at,
                guard_minutes=settings.guard_minutes,
            )
        )
    return ActionEstimate.of(
        ActionKind.REVIEW,
        steps,
        catalog_version=catalog.version,
        inputs={
            **(inputs or {}),
            "stages": list(stage_names),
            "submission_chars": submission_chars,
            "at": at.isoformat(),
        },
        uncalibrated=_uncalibrated(settings, *flags),
    )


# ── Task preparation ──


def estimate_task_preparation(
    *,
    kind: str,
    ladders: Mapping[str, LadderStage],
    settings: EstimateSettings,
    catalog: Catalog,
    task_chars: int,
    at: datetime,
    inputs: Mapping[str, Any] | None = None,
) -> ActionEstimate:
    """E of one preparation of a task version: one call of its stage.

    ``kind`` is the stage name, ``criteria_decomposition`` or
    ``key_explanation``; ``task_chars`` is the size of what the stage is shown
    of the task (its text, title, description, answers).
    """
    step = _setting(settings.task_preparation, kind)
    estimate = llm_step(
        kind,
        _stage(ladders, kind),
        step,
        catalog=catalog,
        material_tokens=tokens_for_chars(task_chars),
        at=at,
        guard_minutes=settings.guard_minutes,
    )
    return ActionEstimate.of(
        ActionKind.TASK_PREPARATION,
        [estimate],
        catalog_version=catalog.version,
        inputs={
            **(inputs or {}),
            "kind": kind,
            "task_chars": task_chars,
            "at": at.isoformat(),
        },
        uncalibrated=_uncalibrated(settings, step.calibrated),
    )


# ── Processing ──


@dataclass(frozen=True, slots=True, kw_only=True)
class MaterialFactors:
    """What drives the cost of processing one material, as far as it is known.

    ``duration_sec`` for audio and video; ``slides`` / ``image_only_slides``
    for a presentation (a PDF is one); ``files`` for code; ``text_chars`` (the
    assembled text) and ``safety_chars`` (what the safety check reads) for the
    kinds whose text exists before the first paid step.
    """

    duration_sec: float | None = None
    slides: int | None = None
    image_only_slides: int | None = None
    files: int | None = None
    text_chars: int | None = None
    safety_chars: int | None = None

    def as_json(self) -> dict[str, Any]:
        """The known factors, for the ``inputs`` column."""
        return {
            k: v
            for k, v in {
                "duration_sec": self.duration_sec,
                "slides": self.slides,
                "image_only_slides": self.image_only_slides,
                "files": self.files,
                "text_chars": self.text_chars,
                "safety_chars": self.safety_chars,
            }.items()
            if v is not None
        }


def _need(value: Any, name: str, kind: str) -> Any:
    if value is None:
        raise EstimateError(f"{kind}: factor {name!r} is unknown")
    return value


def _ceil(value: Decimal) -> int:
    return int(value.to_integral_value(rounding=ROUND_CEILING))


class _Steps:
    """Collects the steps of one processing estimate and their calibration."""

    def __init__(
        self,
        settings: EstimateSettings,
        catalog: Catalog,
        ladders: Mapping[str, LadderStage],
        at: datetime,
    ) -> None:
        self.settings, self.catalog, self.ladders, self.at = (
            settings,
            catalog,
            ladders,
            at,
        )
        self.steps: list[StepEstimate] = []
        self.flags: list[bool] = []

    def llm(
        self, name: str, material_tokens: int, calls: Decimal | None = None
    ) -> StepEstimate:
        step = _setting(self.settings.processing.steps, name)
        self.flags.append(step.calibrated)
        estimate = llm_step(
            name,
            _stage(self.ladders, name),
            step,
            catalog=self.catalog,
            material_tokens=material_tokens,
            at=self.at,
            guard_minutes=self.settings.guard_minutes,
            calls=calls,
        )
        self.steps.append(estimate)
        return estimate

    def stt(self, duration_sec: float) -> None:
        if not self.catalog.stt_chain:
            raise EstimateError("the transcribe chain is empty")
        first = self.catalog.stt_chain[0]
        self.steps.append(
            price_audio_step(
                {first.model_id: first},
                step=STT_ACTION,
                provider=first.provider,
                model_id=first.model_id,
                duration_sec=duration_sec,
                at=self.at,
                guard_minutes=self.settings.guard_minutes,
            )
        )


def _speech(s: _Steps, kind: str, duration_sec: float) -> None:
    """STT, safety, Pass 2a and Pass 2c of a recording (audio or video)."""
    p = s.settings.processing
    minutes = Decimal(repr(duration_sec)) / 60
    words = minutes * p.speech.words_per_minute
    speech_tokens = tokens_for_chars(int(words * p.speech.chars_per_word))
    per_word = (
        p.speech.audio_2a_chars_per_word
        if kind == "audio"
        else p.speech.video_2a_chars_per_word
    )
    words_json_tokens = tokens_for_chars(int(words * per_word))
    segments = max(_ceil(minutes / p.denoise.minutes_per_segment), 1)
    denoise_calls = segments * p.denoise.noisy_share
    s.flags += [p.speech.calibrated, p.denoise.calibrated]

    s.llm("safety_check_authored", speech_tokens)
    if kind == "audio":
        s.llm("audio_pass_2a_mapping", words_json_tokens)
        s.llm("audio_pass_2c_denoise", speech_tokens // segments, denoise_calls)
        return
    # Video: Pass 2a reads the words and the frame descriptions of Pass 1.
    frames = minutes * p.video.frames_per_minute
    vision_calls = Decimal(max(_ceil(frames / p.video.frames_per_vision_call), 1))
    s.flags.append(p.video.calibrated)
    vision = s.llm("video_pass_1_vision", 0, vision_calls)
    vision_out = int((vision.tokens_out or 0) * vision_calls)
    s.llm("video_pass_2a_mapping", words_json_tokens + vision_out)
    s.llm("video_pass_2c_denoise", speech_tokens // segments, denoise_calls)


def estimate_processing(
    *,
    kind: str,
    factors: MaterialFactors,
    ladders: Mapping[str, LadderStage],
    settings: EstimateSettings,
    catalog: Catalog,
    at: datetime,
    inputs: Mapping[str, Any] | None = None,
) -> ActionEstimate:
    """E of one processing job of a material of ``kind`` (``SourceType`` value).

    The steps per kind, as the pipelines call them:

    * audio — STT; safety; Pass 2a; Pass 2c on the noisy share of segments;
    * video — STT; Pass 1 vision per chunk of frames; safety; Pass 2a over the
      words and the frame descriptions; Pass 2c as for audio;
    * presentation — safety over the slide text; Pass 1 vision per slide; a
      second safety over the descriptions of image-only slides, when there
      are any (decided by the slides, known at the estimate's point); Pass 2a;
    * text, web — safety; Pass 2a;
    * code — safety; per file: the skeleton on its share of files and the
      description; one summary.
    """
    s = _Steps(settings, catalog, ladders, at)
    if kind in ("audio", "video"):
        duration = _need(factors.duration_sec, "duration_sec", kind)
        s.stt(duration)
        _speech(s, kind, duration)
    elif kind == "presentation":
        slides = _need(factors.slides, "slides", kind)
        image_only = factors.image_only_slides or 0
        text = tokens_for_chars(_need(factors.text_chars, "text_chars", kind))
        safety = tokens_for_chars(factors.safety_chars or 0)
        s.llm("safety_check_authored", safety)
        vision = s.llm("presentation_pass_1_vision", 0, Decimal(slides))
        described = (vision.tokens_out or 0) * slides
        if image_only:
            s.llm("safety_check_authored", (vision.tokens_out or 0) * image_only)
        s.llm("presentation_pass_2a_mapping", text + described)
    elif kind in ("text", "web"):
        text = tokens_for_chars(_need(factors.text_chars, "text_chars", kind))
        s.llm("safety_check_authored", tokens_for_chars(factors.safety_chars or 0))
        s.llm("pass_2a_mapping", text)
    elif kind == "code":
        files = _need(factors.files, "files", kind)
        text = tokens_for_chars(_need(factors.text_chars, "text_chars", kind))
        per_file = text // files if files else 0
        share = settings.processing.code.llm_skeleton_share
        s.flags.append(settings.processing.code.calibrated)
        s.llm("safety_check_authored", tokens_for_chars(factors.safety_chars or 0))
        if files:
            s.llm("code_skeleton_extraction", per_file, files * share)
            description = s.llm("code_segment_description", per_file, Decimal(files))
            s.llm("code_summary", (description.tokens_out or 0) * files)
    else:
        raise EstimateError(f"no processing estimate for kind {kind!r}")
    return ActionEstimate.of(
        ActionKind.PROCESSING,
        s.steps,
        catalog_version=catalog.version,
        inputs={
            **(inputs or {}),
            "kind": kind,
            "factors": factors.as_json(),
            "at": at.isoformat(),
        },
        uncalibrated=_uncalibrated(settings, *s.flags),
    )
