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

    * ``stages`` — named stage definitions, each written once: everything the
      router needs to execute the stage (``prompt_ref``, ``requires``,
      ``input_budget_ratio``, ``record_output``, a non-empty ``ladder``), plus
      ``deterministic`` and ``ceilings`` (``tool_steps``, ``money_usd``,
      ``output_tokens``). A path lists stage names; a stage that needs different
      ceilings on another path is another name, not an override. The name is the
      stage's own: it may not repeat a stage of ``ladders_*.yaml``, because the
      call register keys a call's cost on it.
    * ``task_types`` — every :class:`~course_supporter.models.source.AssignmentType`
      with ``served_by`` and ``paths``: either all three :class:`SubmissionState`
      values, each mapped to a list of stage names (possibly empty, and always
      empty for ``test`` — a test makes no model call), or ``{}`` for a type no
      path describes yet.

    No field has a default: a description that is missing fails loudly instead
    of standing in for a guessed value.

    :func:`load_path_config` reads the file and checks its shape; every shape
    fault is reported at once. :func:`get_path_config` is the same read, cached
    per process — startup validates what it returns, so the body that later
    walks a path reads the bytes the boot approved.
    :func:`validate_path_config` checks what the shape cannot — every prompt
    file exists and parses, no stage name repeats a ladder stage's, stage names
    resolve, a type has all three states or none, a type switched to the new
    path has paths, a ``test`` path lists no stage, pins stay under their
    stage's output ceiling, rungs are admissible against the model registry and
    carry what the stage requires — and reports every such fault at once. Both
    raise ``ValueError`` and are meant for startup: a hole should stop the boot,
    not a live submission.
    :func:`path_ceiling_estimate` sums a path's money ceilings.

    Worked cases, executed: :mod:`tests.unit.test_homework.test_path_config`.

Extending:
    A new stage, or the paths of a declared type, is an edit of the file — and,
    for a stage, a new prompt file beside it. A new submission state or a new
    stage field is a code change here and a reader in the code that walks paths
    (task 03).
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from enum import StrEnum
from functools import partial
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

from course_supporter.llm.error_categories import InvalidPromptError
from course_supporter.llm.prompt_loader_md import load_prompt
from course_supporter.llm.registry import (
    Capability,
    ModelConfig,
    ModelRegistryConfig,
)
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
    """A stage definition: everything the router needs, plus the stage's ceilings.

    The router-facing fields carry the same names and meanings as on a
    ``ladders_*.yaml`` stage (:class:`~course_supporter.llm.ladder_config.StageConfig`),
    so a reader who knows one knows the other, and none of them has a default:
    a path stage that forgets one stops the boot instead of quietly borrowing a
    value nobody chose. ``input_budget_ratio: null`` is such a choice — it means
    "estimate nothing, skip no rung" — and has to be written.
    """

    model_config = _STRICT

    deterministic: StrictBool
    ceilings: StageCeilings
    prompt_ref: str
    requires: list[Capability]
    input_budget_ratio: float | None = Field(gt=0.0, le=1.0)
    record_output: StrictBool
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


def _stage_model_errors(
    stage_name: str,
    index: int,
    rung: PathRung,
    stage: PathStage,
    model: ModelConfig,
) -> list[str]:
    """The stage's own checks against one rung's resolved model.

    The same two the ladder stages apply
    (:func:`~course_supporter.llm.ladder_config._stage_model_errors`), phrased
    the same way: they depend on the stage's description rather than on the
    registry alone, which is what ``model_checks`` is for.
    """
    errors: list[str] = []

    missing = set(stage.requires) - set(model.capabilities)
    if missing:
        errors.append(
            f"Stage '{stage_name}' rung {index} model '{rung.model}' "
            f"lacks required capabilities: {sorted(missing)}"
        )

    if stage.input_budget_ratio is not None and model.max_context is None:
        errors.append(
            f"Stage '{stage_name}' rung {index} model '{rung.model}' "
            f"has no max_context in the registry "
            f"(required by input_budget_ratio={stage.input_budget_ratio})"
        )

    return errors


def validate_path_config(
    config: PathConfig,
    registry: ModelRegistryConfig,
    *,
    ladder_stage_names: Collection[str] = (),
    prompt_base_path: Path | None = None,
) -> None:
    """Check what the file's shape cannot, reporting every fault at once.

    ``ladder_stage_names`` are the stage names of ``ladders_*.yaml``; a path
    stage may not reuse one. ``prompt_base_path`` is the resolution base for
    ``prompt_ref``, as on
    :func:`~course_supporter.llm.prompt_loader_md.load_prompt` — ``None`` in
    production, a temporary directory in tests.

    * every stage's prompt file exists and parses (half of ``DD-SP-AP``);
    * no path stage reuses the name of a stage in the model ladders: the
      register keys a call's cost on the stage name, so two stages sharing one
      would have their costs summed together (task 03 probe, question 8);
    * every stage rung is admissible against the model registry
      (:func:`~course_supporter.llm.rung_registry_check.rung_registry_errors`),
      carries the capabilities the stage requires, and — when the stage declares
      an input-budget ratio — has a context window in the registry;
    * a pinned rung stays at or under its stage's output-token ceiling — the
      money ceiling is never compared with a pin (different units; whether a
      money ceiling can pay for an attempt is not checked at startup, П10);
    * every task type in the code is declared;
    * a declared type describes all three submission states or none — a
      partial set would hide behind the switch until the day it is flipped;
    * a type switched to the new path describes its paths;
    * a ``test`` path lists no stage — a test makes no model call;
    * every stage name in a path is defined, and appears once in that path.

    Raises:
        ValueError: listing every fault found.
    """
    errors: list[str] = []

    for stage_name, stage in config.stages.items():
        if stage_name in ladder_stage_names:
            errors.append(
                f"Stage '{stage_name}' reuses the name of a stage in the model "
                f"ladders: a path stage needs its own name, because the call "
                f"register keys a call's cost on it"
            )
        try:
            load_prompt(stage.prompt_ref, base_path=prompt_base_path)
        except (FileNotFoundError, InvalidPromptError) as exc:
            errors.append(f"Stage '{stage_name}' prompt cannot be read: {exc}")
        for i, rung in enumerate(stage.ladder):
            errors.extend(
                rung_registry_errors(
                    stage_name,
                    i,
                    rung,
                    registry,
                    model_checks=partial(
                        _stage_model_errors, stage_name, i, rung, stage
                    ),
                )
            )
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
            # A test is checked by code against its answer key: a stage written
            # here would make every test submission pay, and would still boot.
            if task_type is AssignmentType.TEST and stage_names:
                errors.append(
                    f"Path '{key}' lists stages {stage_names}, but a test path "
                    f"makes no model call"
                )
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


_config: PathConfig | None = None


def _default_config_path() -> Path:
    """Resolve the config path from settings (lazy import, mirrors sanity_config)."""
    from course_supporter.config import get_settings

    return get_settings().submission_paths_config_path


def get_path_config(config_path: Path | None = None) -> PathConfig:
    """Return the cached configuration; load on first call (or on an override).

    The file is read once per process, like the sanity tuning beside it: the
    reload policy is deploy-based (KD16), so an edit takes effect on the next
    start of the app and the worker (``DD-2.1-AA``). Startup validates what is
    cached here, so the body that later walks a path reads the very bytes the
    boot approved rather than the file as it stands at that moment.
    """
    global _config
    if _config is None or config_path is not None:
        path = config_path if config_path is not None else _default_config_path()
        _config = load_path_config(path)
    return _config


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
