"""LLM ladder configuration loader (KD16).

Loads ``ladders_*.yaml`` files from a directory at startup,
validates each against the Pydantic schema, and merges them into a
single mapping keyed by stage name. Cross-file stage-name
uniqueness is enforced.

Sister module to :mod:`course_supporter.llm.registry`; this is the
KD16 replacement for the legacy ``actions`` / ``strategies`` model.
The legacy module remains untouched until Phase 5 cleanup.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

# Strict input-config models: typo in a YAML key fails validation
# instead of silently shadowing a real field.
_FORBID = ConfigDict(extra="forbid")


class LadderEntry(BaseModel):
    """One step in a stage's fallback ladder.

    The router walks ladder entries in order; ``provider`` must match
    a registered provider name from
    :mod:`course_supporter.llm.providers`.
    """

    model_config = _FORBID

    provider: str
    model: str


class StageConfig(BaseModel):
    """Per-stage routing configuration.

    Attributes:
        prompt_ref: Markdown prompt path resolved relative to the
            backend repo CWD (e.g. ``"prompts/example/v1.md"``). The
            actual file read happens in
            :mod:`course_supporter.llm.prompt_loader_md`.
        ladder: Ordered fallback ladder; at least one entry.
    """

    model_config = _FORBID

    prompt_ref: str
    ladder: list[LadderEntry] = Field(min_length=1)


class LadderFile(BaseModel):
    """Per-file YAML container.

    Each ``ladders_*.yaml`` deserialises into a ``LadderFile``;
    multiple files are merged into a single :class:`LadderConfig`
    by :func:`load_ladder_config`.
    """

    model_config = _FORBID

    stages: dict[str, StageConfig] = Field(default_factory=dict)


class LadderConfig(BaseModel):
    """Merged view of all loaded ``ladders_*.yaml`` files.

    Stage names are globally unique across files; the loader raises
    ``ValueError`` if two files declare the same stage name.
    """

    stages: dict[str, StageConfig] = Field(default_factory=dict)

    def get_stage(self, name: str) -> StageConfig:
        """Look up a stage by name.

        Raises:
            KeyError: if the stage is not registered.
        """
        if name not in self.stages:
            raise KeyError(f"Unknown stage: '{name}'")
        return self.stages[name]


def load_ladder_config(directory: Path) -> LadderConfig:
    """Load and merge ``ladders_*.yaml`` files from ``directory``.

    Args:
        directory: Directory containing ``ladders_*.yaml`` files.
            Files are discovered via glob and processed in sorted
            order for deterministic merge.

    Returns:
        Merged :class:`LadderConfig`. If no ``ladders_*.yaml`` files
        match the glob, the returned config has an empty ``stages``
        mapping (any ``get_stage`` call raises ``KeyError``).

    Raises:
        FileNotFoundError: if ``directory`` does not exist.
        ValueError: if YAML parsing fails, schema validation fails,
            or two files declare the same stage name.
    """
    if not directory.exists():
        raise FileNotFoundError(f"Ladder config directory not found: {directory}")

    merged: dict[str, StageConfig] = {}
    origins: dict[str, Path] = {}

    for path in sorted(directory.glob("ladders_*.yaml")):
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise ValueError(f"Failed to parse ladder config '{path}': {exc}") from exc

        try:
            ladder_file = LadderFile.model_validate(raw)
        except ValidationError as exc:
            raise ValueError(f"Invalid ladder config '{path}': {exc}") from exc

        for stage_name, stage_cfg in ladder_file.stages.items():
            if stage_name in origins:
                raise ValueError(
                    f"Duplicate stage name '{stage_name}' "
                    f"declared in '{origins[stage_name]}' and '{path}'"
                )
            merged[stage_name] = stage_cfg
            origins[stage_name] = path

    return LadderConfig(stages=merged)
