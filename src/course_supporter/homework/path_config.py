"""Submission paths of the rebuilt Mentor, as configuration (mentor-rebuild task 02).

Purpose:
    Which stages a submission goes through, and within what limits, is a
    setting rather than a property of the code. A *path* is the stage list for
    one :class:`PathKey` — an assignment type paired with a submission state —
    and every stage carries its model ladder, three ceilings and the
    deterministic-settings flag. The same file is the per-type switch between
    today's Mentor and the new path (sprint rule 5). Nothing here picks a path
    or runs a stage — that is task 03; this module describes, validates and
    estimates.

Interface:
    The file (``Settings.submission_paths_config_path``) has two sections:

    * ``stages`` — named stage definitions, each written once: ``deterministic``,
      ``ceilings`` (``tool_steps``, ``money_usd``, ``output_tokens``) and a
      non-empty ``ladder``. A path lists stage names; a stage that needs
      different ceilings on another path is another name, not an override.
    * ``task_types`` — every :class:`~course_supporter.models.source.AssignmentType`
      with ``served_by`` and ``paths``: either all three :class:`SubmissionState`
      values, each mapped to a list of stage names (possibly empty — a ``test``
      path makes no model call), or ``{}`` for a type no path describes yet.

    No field has a default: a description that is missing fails loudly instead
    of standing in for a guessed value.

    :func:`load_path_config` reads the file and checks its shape; every shape
    fault is reported at once. :func:`validate_path_config` checks what the
    shape cannot — stage names resolve, a type has all three states or none, a
    type switched to the new path has paths, pins stay under their stage's
    output ceiling, rungs are admissible against the model registry — and
    reports every such fault at once. Both raise ``ValueError`` and are meant
    for startup: a hole should stop the boot, not a live submission.
    :func:`path_ceiling_estimate` sums a path's money ceilings.

    Worked cases, executed: :mod:`tests.unit.test_homework.test_path_config`.

Extending:
    A new stage, or the paths of a declared type, is an edit of the file. A new
    submission state or a new stage field is a code change here and a reader in
    the code that walks paths (task 03).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    NonNegativeInt,
    PositiveFloat,
    PositiveInt,
    StrictBool,
    ValidationError,
)

from course_supporter.llm.registry import ModelRegistryConfig
from course_supporter.llm.rung_registry_check import rung_registry_errors
from course_supporter.models.source import AssignmentType

# Strict, immutable input models: a typo in a key fails validation instead of
# silently shadowing a real field, and a loaded config cannot be edited in place.
_STRICT = ConfigDict(extra="forbid", frozen=True)


class SubmissionState(StrEnum):
    """Where a submission stands, as far as choosing its path goes.

    ``REPEAT_WITH_REPLIES`` is described but unreachable until replies exist as
    records (task 12): nothing in today's data says a submission carries them,
    and a guessed signal would be worse than a state nobody reaches.
    """

    FIRST = "first"
    REPEAT_WITHOUT_REPLIES = "repeat_without_replies"
    REPEAT_WITH_REPLIES = "repeat_with_replies"


class ServedBy(StrEnum):
    """Which Mentor serves a task type (sprint rule 5)."""

    TODAYS_MENTOR = "todays_mentor"
    NEW_PATH = "new_path"


@dataclass(frozen=True, slots=True)
class PathKey:
    """What selects a path: the assignment type and the submission state."""

    task_type: AssignmentType
    state: SubmissionState

    def __str__(self) -> str:
        return f"{self.task_type.value}/{self.state.value}"


class PathRung(BaseModel):
    """One rung of a stage ladder; every key is required and ``null`` is a value.

    ``reasoning: null`` means no reasoning form; ``max_output_tokens: null``
    means no pin, so the stage's output ceiling applies.
    """

    model_config = _STRICT

    provider: str
    model: str
    reasoning: dict[str, Any] | None
    max_output_tokens: PositiveInt | None


class StageCeilings(BaseModel):
    """The three ceilings of a stage."""

    model_config = _STRICT

    tool_steps: NonNegativeInt
    # Decimal dollars — the same type as the registry's ``cost_per_1k`` prices,
    # so the estimate and the actual cost of a call add up without conversion.
    money_usd: PositiveFloat
    output_tokens: PositiveInt


class PathStage(BaseModel):
    """A stage definition: ladder, ceilings, deterministic-settings flag."""

    model_config = _STRICT

    deterministic: StrictBool
    ceilings: StageCeilings
    ladder: list[PathRung] = Field(min_length=1)


class TaskTypePaths(BaseModel):
    """How one task type is served, and its paths if any are described."""

    model_config = _STRICT

    served_by: ServedBy
    paths: dict[SubmissionState, list[str]]


class PathConfig(BaseModel):
    """The whole submission-path file."""

    model_config = _STRICT

    stages: dict[str, PathStage]
    task_types: dict[AssignmentType, TaskTypePaths]


def load_path_config(config_path: Path) -> PathConfig:
    """Read the submission-path file and check its shape.

    Raises:
        FileNotFoundError: when ``config_path`` does not exist.
        ValueError: when the YAML does not parse, or any field is missing,
            unknown or out of range (every such fault in one message).
    """
    if not config_path.exists():
        raise FileNotFoundError(f"Submission path config not found: {config_path}")
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValueError(
            f"Failed to parse submission path config '{config_path}': {exc}"
        ) from exc
    try:
        return PathConfig.model_validate(raw)
    except ValidationError as exc:
        raise ValueError(
            f"Invalid submission path config '{config_path}': {exc}"
        ) from exc


def validate_path_config(config: PathConfig, registry: ModelRegistryConfig) -> None:
    """Check what the file's shape cannot, reporting every fault at once.

    * every stage rung is admissible against the model registry
      (:func:`~course_supporter.llm.rung_registry_check.rung_registry_errors`);
    * a pinned rung stays at or under its stage's output-token ceiling — the
      money ceiling is never compared with a pin (different units; whether a
      money ceiling can pay for an attempt is not checked at startup, П10);
    * every task type in the code is declared;
    * a declared type describes all three submission states or none — a
      partial set would hide behind the switch until the day it is flipped;
    * a type switched to the new path describes its paths;
    * every stage name in a path is defined, and appears once in that path.

    Raises:
        ValueError: listing every fault found.
    """
    errors: list[str] = []

    for stage_name, stage in config.stages.items():
        for i, rung in enumerate(stage.ladder):
            errors.extend(rung_registry_errors(stage_name, i, rung, registry))
            if (
                rung.max_output_tokens is not None
                and rung.max_output_tokens > stage.ceilings.output_tokens
            ):
                errors.append(
                    f"Stage '{stage_name}' rung {i} pins "
                    f"max_output_tokens={rung.max_output_tokens} above the stage "
                    f"output ceiling {stage.ceilings.output_tokens}"
                )

    all_states = set(SubmissionState)
    for task_type in AssignmentType:
        declared = config.task_types.get(task_type)
        if declared is None:
            errors.append(
                f"Task type '{task_type.value}' is not declared: every type "
                f"needs served_by and paths"
            )
            continue

        described = set(declared.paths)
        if not described:
            if declared.served_by is ServedBy.NEW_PATH:
                errors.append(
                    f"Task type '{task_type.value}' is switched to the new path "
                    f"but describes no paths"
                )
        elif described != all_states:
            missing = sorted(state.value for state in all_states - described)
            errors.append(
                f"Task type '{task_type.value}' describes some submission states "
                f"but not {missing}: a type describes all three or none"
            )

        for state, stage_names in declared.paths.items():
            key = PathKey(task_type, state)
            seen: set[str] = set()
            for stage_name in stage_names:
                if stage_name not in config.stages:
                    errors.append(f"Path '{key}' lists undefined stage '{stage_name}'")
                if stage_name in seen:
                    errors.append(
                        f"Path '{key}' lists stage '{stage_name}' more than once"
                    )
                seen.add(stage_name)

    if errors:
        raise ValueError(
            "Submission path validation failed:\n"
            + "\n".join(f"  - {e}" for e in errors)
        )


def path_ceiling_estimate(config: PathConfig, key: PathKey) -> float:
    """Sum of the money ceilings of the stages on ``key``'s path, in dollars.

    Output-token ceilings are not money, and the tool-step ceiling counts steps
    no model layer can take yet (П8), so neither enters the sum. A path with no
    model stage estimates ``0.0``. Expects a config that passed
    :func:`validate_path_config`.

    Raises:
        KeyError: when no path is described for ``key``.
    """
    paths = config.task_types[key.task_type].paths
    if key.state not in paths:
        raise KeyError(f"No path is described for '{key}'")
    return sum(
        (config.stages[name].ceilings.money_usd for name in paths[key.state]), 0.0
    )
