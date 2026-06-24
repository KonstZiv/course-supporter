"""Load + validate the Mentor review graph tuning (``config/mentor_review.yaml``).

Vision D12: the graph's behaviour is config-driven (deploy-based reload, KD16).
The model choice per phase lives in ``ladders_mentor.yaml``; this module owns
the deterministic-scoring knobs — base layer weights, the denoise cap, and the
verdict bands — and pins their invariants at load time:

* each layer weight strictly in ``(0, 1)`` and the three summing to ``1.0``
  (the D7 x D8 invariant, enforced before any review runs);
* the verdict bands ordered (``partially_correct_min <= correct_min``).

Shape mirrors ``language.py`` (Pydantic model + module-level cache +
path-from-settings) so the config surface stays uniform across the codebase.
"""

from __future__ import annotations

import math
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

_WEIGHT_SUM_TOLERANCE = 1e-6


class LayerWeights(BaseModel):
    """Base aggregation weights for the three D8 layers (strictly in (0, 1))."""

    model_config = ConfigDict(extra="forbid")

    node: float = Field(gt=0.0, lt=1.0)
    course: float = Field(gt=0.0, lt=1.0)
    industry: float = Field(gt=0.0, lt=1.0)

    @model_validator(mode="after")
    def _weights_sum_to_one(self) -> LayerWeights:
        total = self.node + self.course + self.industry
        if not math.isclose(total, 1.0, abs_tol=_WEIGHT_SUM_TOLERANCE):
            msg = (
                f"layer_weights must sum to 1.0 (got {total}); "
                "they form a weighted average over the three D8 layers."
            )
            raise ValueError(msg)
        return self


class DenoiseConfig(BaseModel):
    """History-denoise bound (D10): |delta| ceiling so history never dominates."""

    model_config = ConfigDict(extra="forbid")

    cap: int = Field(ge=0, description="Ceiling on |denoise_delta| in score points.")


class VerdictThresholds(BaseModel):
    """Score bands the verdict is code-derived from (no LLM verdict emission)."""

    model_config = ConfigDict(extra="forbid")

    pass_score: int = Field(ge=0, le=100)
    correct_min: int = Field(ge=0, le=100)
    partially_correct_min: int = Field(ge=0, le=100)

    @model_validator(mode="after")
    def _bands_ordered(self) -> VerdictThresholds:
        if self.partially_correct_min > self.correct_min:
            msg = (
                "verdict_thresholds must satisfy "
                "partially_correct_min <= correct_min "
                f"(got {self.partially_correct_min} > {self.correct_min})."
            )
            raise ValueError(msg)
        return self


class MentorReviewConfig(BaseModel):
    """Top-level shape of ``config/mentor_review.yaml``."""

    model_config = ConfigDict(extra="forbid")

    layer_weights: LayerWeights
    denoise: DenoiseConfig
    verdict_thresholds: VerdictThresholds


_config: MentorReviewConfig | None = None


def load_mentor_review_config(config_path: Path) -> MentorReviewConfig:
    """Load and validate the Mentor review tuning from YAML.

    Raises:
        FileNotFoundError: when ``config_path`` does not exist.
        ValueError: when YAML parsing or invariant validation fails.
    """
    if not config_path.exists():
        raise FileNotFoundError(f"Mentor review config not found: {config_path}")
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValueError(
            f"Failed to parse Mentor review config '{config_path}': {exc}"
        ) from exc
    return MentorReviewConfig.model_validate(raw)


def _default_config_path() -> Path:
    """Resolve the config path from settings (lazy import, mirrors language.py)."""
    from course_supporter.config import get_settings

    return get_settings().mentor_review_config_path


def get_mentor_review_config(
    config_path: Path | None = None,
) -> MentorReviewConfig:
    """Return the cached config; load on first call (or when path overridden)."""
    global _config
    if _config is None or config_path is not None:
        path = config_path if config_path is not None else _default_config_path()
        _config = load_mentor_review_config(path)
    return _config
