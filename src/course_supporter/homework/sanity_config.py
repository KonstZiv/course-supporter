"""Load + validate the sanity gate tuning (``config/sanity.yaml``).

Vision D12 / KD16: the gate's policy is config-driven (deploy-based reload). The
model choice for the ``sanity_check`` stage lives in ``ladders_mentor.yaml``;
this module owns the one behavioural knob — the confidence floor a ``mismatch``
verdict must clear to be terminal (ratified A3-conservative gate).

Shape mirrors ``review_config.py`` (Pydantic model + module-level cache +
path-from-settings) so the config surface stays uniform across the codebase.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field


class SanityConfig(BaseModel):
    """Top-level shape of ``config/sanity.yaml``."""

    model_config = ConfigDict(extra="forbid")

    confidence_threshold: float = Field(
        gt=0.0,
        le=1.0,
        description=(
            "Floor a `mismatch` verdict's confidence must reach to gate the "
            "submission (status `mismatch`, review graph skipped). Conservative "
            "on purpose — gate only clearly-off-task work."
        ),
    )


_config: SanityConfig | None = None


def load_sanity_config(config_path: Path) -> SanityConfig:
    """Load and validate the sanity tuning from YAML.

    Raises:
        FileNotFoundError: when ``config_path`` does not exist.
        ValueError: when YAML parsing or invariant validation fails.
    """
    if not config_path.exists():
        raise FileNotFoundError(f"Sanity config not found: {config_path}")
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValueError(
            f"Failed to parse sanity config '{config_path}': {exc}"
        ) from exc
    return SanityConfig.model_validate(raw)


def _default_config_path() -> Path:
    """Resolve the config path from settings (lazy import, mirrors language.py)."""
    from course_supporter.config import get_settings

    return get_settings().sanity_config_path


def get_sanity_config(config_path: Path | None = None) -> SanityConfig:
    """Return the cached config; load on first call (or when path overridden)."""
    global _config
    if _config is None or config_path is not None:
        path = config_path if config_path is not None else _default_config_path()
        _config = load_sanity_config(path)
    return _config
