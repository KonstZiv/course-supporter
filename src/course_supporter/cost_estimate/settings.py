"""Load and check ``config/cost_estimate.yaml`` — the shadow estimate's settings.

The file holds the operator's parameters (margin, units per dollar, the peak
guard) and the typical sizes the estimate assumes for every step it prices.
Every size carries ``calibrated: false`` until a calibration report has
measured it; an estimate that used one says so (``uncalibrated``).

The shape is checked when the file is read, as for the other config files. A
setting that is missing for a step the estimate meets fails the ESTIMATE, not
the boot: the estimate never stops an action (BIL-D), and the boot does not
read this file.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class StepSettings(_Frozen):
    """The typical size of one call of a step, and how many calls it makes.

    ``prompt_tokens`` — the input beside the material the step reads (the
    prompt template, the task, the criteria ...); ``output_tokens`` — the
    typical answer; ``calls`` — expected calls per action where the code does
    not decide it from the material (a repeat that happens on some answers).
    """

    prompt_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    calls: Decimal = Field(default=Decimal(1), ge=0)
    calibrated: bool = False


class SpeechSettings(_Frozen):
    """How much text a minute of speech becomes."""

    words_per_minute: Decimal = Field(gt=0)
    chars_per_word: Decimal = Field(gt=0)
    audio_2a_chars_per_word: Decimal = Field(gt=0)
    video_2a_chars_per_word: Decimal = Field(gt=0)
    calibrated: bool = False


class VideoSettings(_Frozen):
    """How many frames a minute of video gives the vision pass, and per call."""

    frames_per_minute: Decimal = Field(ge=0)
    frames_per_vision_call: int = Field(gt=0)
    calibrated: bool = False


class DenoiseSettings(_Frozen):
    """How many Pass 2c calls a minute of speech is expected to make."""

    minutes_per_segment: Decimal = Field(gt=0)
    noisy_share: Decimal = Field(ge=0, le=1)
    calibrated: bool = False


class CodeSettings(_Frozen):
    """The share of included files whose skeleton needs the model."""

    llm_skeleton_share: Decimal = Field(ge=0, le=1)
    calibrated: bool = False


class ProcessingSettings(_Frozen):
    """The cost drivers of processing an author's material, and its steps."""

    speech: SpeechSettings
    video: VideoSettings
    denoise: DenoiseSettings
    code: CodeSettings
    steps: dict[str, StepSettings]


class EstimateSettings(_Frozen):
    """Top-level shape of ``config/cost_estimate.yaml``."""

    margin: Decimal = Field(ge=0)
    units_per_usd: Decimal | None = Field(default=None, gt=0)
    guard_minutes: int = Field(ge=0, le=24 * 60)
    review: dict[str, StepSettings]
    task_preparation: dict[str, StepSettings]
    processing: ProcessingSettings


def load_estimate_settings(config_path: Path) -> EstimateSettings:
    """Read and check the settings.

    Raises:
        FileNotFoundError: the file does not exist.
        ValueError: the YAML does not parse or does not have the shape.
    """
    if not config_path.exists():
        raise FileNotFoundError(f"Cost estimate config not found: {config_path}")
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValueError(f"Failed to parse '{config_path}': {exc}") from exc
    return EstimateSettings.model_validate(raw)


_settings: EstimateSettings | None = None


def get_estimate_settings(config_path: Path | None = None) -> EstimateSettings:
    """The cached settings; read on first use (or on an override).

    Read once per process, like the other config files (deploy-based reload).
    """
    global _settings
    if _settings is None or config_path is not None:
        if config_path is None:
            from course_supporter.config import get_settings

            config_path = get_settings().cost_estimate_config_path
        _settings = load_estimate_settings(config_path)
    return _settings
