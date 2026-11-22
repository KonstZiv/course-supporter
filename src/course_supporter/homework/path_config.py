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

    * ``stages`` — named stage definitions, each written once, of two kinds
      (task 10, K5):

      - a MODEL stage (:class:`ModelStage`): everything the router needs to
        execute the stage (``prompt_ref``, ``requires``,
        ``input_budget_ratio``, ``record_output``, a non-empty ``ladder``),
        plus ``deterministic`` and ``ceilings`` (``tool_steps``,
        ``money_usd``, ``output_tokens``);
      - a CODE stage (:class:`CodeStage`): ``code: true`` and
        ``deterministic`` — nothing else. Code alone runs it: no ladder, no
        prompt, no ceilings, and a stage that writes one of them is refused.

      A path lists stage names; a stage that needs different ceilings on
      another path is another name, not an override. The name is the stage's
      own: it may not repeat a stage of ``ladders_*.yaml``, because the call
      register keys a call's cost on it.
    * ``task_types`` — every :class:`~course_supporter.models.source.AssignmentType`
      with ``served_by`` and ``paths``: either all three :class:`SubmissionState`
      values, each mapped to a list of stage names (possibly empty, and always
      empty for ``test`` — a test makes no model call), or ``{}`` for a type no
      path describes yet. ``test_mode: true`` marks a type served by the new
      path WITHOUT charging for it (:func:`in_test_mode`).
      ``replies_enabled: true`` lets the students of a type on the new path
      reply to the remarks of their last review (task 12a, K3;
      :func:`replies_enabled`).

    No field has a default but ``test_mode`` and ``replies_enabled``: a
    description that is missing fails loudly instead of standing in for a
    guessed value. Those two are switches a type opts into beside its
    ``served_by``; without them the type is charged as usual and takes no
    replies.

    :func:`load_path_config` reads the file and checks its shape; every shape
    fault is reported at once. :func:`get_path_config` is the same read, cached
    per process — startup validates what it returns, so the body that later
    walks a path reads the bytes the boot approved.
    :func:`validate_path_config` checks what the shape cannot — every prompt
    file exists and parses, no stage name repeats a ladder stage's, stage names
    resolve, a type has all three states or none, a type switched to the new
    path has paths, a type in test mode is on the new path and is not
    ``test`` (task 10, K6: test mode means not charging, and a test is never
    charged for), a type taking replies is on the new path, is not ``test``
    and has the model stage the reply screen runs (task 12a, K3), no stage
    takes the reply screen's name in the register, a ``test`` path lists no
    stage, pins stay under their stage's output ceiling, rungs are admissible
    against the model registry and carry what the stage requires —
    and reports every such fault at once. Both raise ``ValueError`` and are
    meant for startup: a hole should stop the boot, not a live submission.
    :func:`path_ceiling_estimate` sums a path's money ceilings; a code stage
    adds nothing. :func:`model_stages` — the stages a model runs, by name.
    :func:`in_test_mode` — whether a type is served by the new path without
    charging. :func:`replies_enabled` — whether a type takes students' replies.

    Worked cases, executed: :mod:`tests.unit.test_homework.test_path_config`.

Extending:
    A new stage, or the paths of a declared type, is an edit of the file — and,
    for a model stage, a new prompt file beside it; a code stage needs only its
    executor (``homework/path_stages.py``). A new submission state or a new
    stage field is a code change here and a reader in the code that walks paths
    (task 03).
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from enum import StrEnum
from functools import partial
from pathlib import Path
from typing import Annotated, Any, Final

import yaml
from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
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

REPLY_SCREEN_STAGE: Final = "safety"
"""The model stage whose prompt and ladder the reply screen runs (task 12a, K3).

The screen judges a student's replies with the same safety check the work
gets — one description, read twice — rather than with a copy of it that could
drift (``homework/reply_screen.py``).
"""

REPLY_SAFETY_ACTION: Final = "reply_safety"
"""The name the reply screen's calls are recorded under in the call register.

Its own name, not ``safety``: the register keys a call's cost on it, so the
price of screening replies stays visible apart from the price of screening
the work. No stage may take it (:func:`validate_path_config`).
"""


class SubmissionState(StrEnum):
    """Where a submission stands, as far as choosing its path goes.

    ``REPEAT_WITH_REPLIES`` is reached only by a type that takes replies
    (:func:`replies_enabled`, task 12a, K3): the student's last review has
    replies of theirs that no review has answered yet. A type that takes none
    — every type in production until the replies are switched on together
    with their answers and the portal — never reaches it.
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
    means no pin, so the stage's output ceiling applies — not by the router,
    which only knows the registry's per-model cap, but because
    ``homework/path_stages.py::_execution`` puts ``ceilings.output_tokens`` on
    the rung before the router sees it.
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


class ModelStage(BaseModel):
    """A model stage: everything the router needs, plus the stage's ceilings.

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


def _true_only(value: bool) -> bool:
    """``code`` is written ``true`` or not at all — never ``false``, never ``1``.

    ``Literal[True]`` would take ``1`` and ``1.0`` for ``true``, and pydantic
    cannot make a literal strict; a strict bool that must be true can be.
    """
    if not value:
        msg = "a code stage says `code: true`; a model stage has no `code`"
        raise ValueError(msg)
    return value


class CodeStage(BaseModel):
    """A code stage: code alone runs it, and it calls no model (task 10, K5).

    ``code: true`` says which kind the stage is; ``deterministic`` keeps its
    meaning from a model stage — the same input gives the same output. Nothing
    else is allowed: a ladder, a prompt or a ceiling written on a code stage
    describes a call it never makes, and is refused rather than ignored.
    """

    model_config = _STRICT

    code: Annotated[StrictBool, AfterValidator(_true_only)]
    deterministic: StrictBool


def _stage_of_its_kind(value: Any) -> Any:
    """Validate a raw stage as the kind it declares: with ``code`` — code.

    Each kind is validated on its own, so a fault is reported at the stage's
    own field (``stages.safety.ladder``) and not twice, once per kind.
    """
    if isinstance(value, Mapping):
        if "code" in value:
            return CodeStage.model_validate(value)
        return ModelStage.model_validate(value)
    return value


PathStage = Annotated[ModelStage | CodeStage, BeforeValidator(_stage_of_its_kind)]
"""A stage definition of either kind (see the module)."""


class TaskTypePaths(BaseModel):
    """How one task type is served, and its paths if any are described.

    ``test_mode`` — the type is served by the new path, and its submissions are
    not charged for: nobody reads it yet, the funds port is
    ``AlwaysEnoughFundsPort``; the billing adapter will (:func:`in_test_mode`).
    Refused on ``test`` (:func:`validate_path_config`).

    ``replies_enabled`` — the type's students may reply to the remarks of
    their last review, and the next submission screens the replies and walks
    ``repeat_with_replies`` (task 12a, K3; :func:`replies_enabled`). Off by
    default and everywhere in production until it is switched on together
    with the answers (K4) and the portal (K5). Refused on a type not on the
    new path and on ``test`` (:func:`validate_path_config`).
    """

    model_config = _STRICT

    served_by: ServedBy
    paths: dict[SubmissionState, list[str]]
    test_mode: StrictBool = False
    replies_enabled: StrictBool = False


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
    stage: ModelStage,
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

    * no path stage — of either kind — reuses the name of a stage in the model
      ladders: the register keys a call's cost on the stage name, so two stages
      sharing one would have their costs summed together (task 03 probe,
      question 8);
    * every model stage's prompt file exists and parses (half of
      ``DD-SP-AP``); a code stage has none;
    * every model stage rung is admissible against the model registry
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
    * a type in test mode is served by the new path: test mode is the new path
      without charging, and today's Mentor has no such mode;
    * a type taking replies (task 12a, K3) is served by the new path — the
      reply screen stands in its doors and the replies' state on its paths —
      and is not ``test``, whose review has no remark a reply could name; and
      the file defines :data:`REPLY_SCREEN_STAGE` as a model stage, the check
      the screen runs;
    * no stage is named :data:`REPLY_SAFETY_ACTION`, the reply screen's name
      in the register, for the reason a ladder stage's name is refused;
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
        if stage_name == REPLY_SAFETY_ACTION:
            errors.append(
                f"Stage '{stage_name}' takes the name the reply screen's calls "
                f"are recorded under: the call register keys a call's cost on it"
            )
        if isinstance(stage, CodeStage):
            continue
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

        if declared.test_mode and declared.served_by is not ServedBy.NEW_PATH:
            errors.append(
                f"Task type '{task_type.value}' is in test mode but served by "
                f"'{declared.served_by.value}': test mode is the new path "
                f"without charging"
            )
        # A test makes no model call and is never charged for: test mode on it
        # would read as a switch that does something, and does nothing.
        if declared.test_mode and task_type is AssignmentType.TEST:
            errors.append(
                f"Task type '{task_type.value}' is in test mode: test mode is "
                f"for the types that are charged for, and a test is not"
            )
        if declared.replies_enabled:
            errors.extend(_reply_errors(task_type, declared, config))

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


def _reply_errors(
    task_type: AssignmentType, declared: TaskTypePaths, config: PathConfig
) -> list[str]:
    """What stops a type from taking replies (task 12a, K3), for the boot.

    The screen of the replies stands in the new path's doors and the state
    they make is a key of its paths, so a type on today's Mentor would take
    replies that nothing ever screens or answers. A test's review has no
    remark with an id a reply could name. And the screen runs
    :data:`REPLY_SCREEN_STAGE`'s prompt and ladder: without that model stage
    the first reply would stop a submission instead of the boot.
    """
    errors: list[str] = []
    if declared.served_by is not ServedBy.NEW_PATH:
        errors.append(
            f"Task type '{task_type.value}' takes replies but is served by "
            f"'{declared.served_by.value}': the replies are screened and "
            f"answered on the new path"
        )
    if task_type is AssignmentType.TEST:
        errors.append(
            f"Task type '{task_type.value}' takes replies: a test's review has "
            f"no remark a reply could name"
        )
    if not isinstance(config.stages.get(REPLY_SCREEN_STAGE), ModelStage):
        errors.append(
            f"Task type '{task_type.value}' takes replies, but no model stage "
            f"'{REPLY_SCREEN_STAGE}' is defined: the reply screen runs its "
            f"prompt and ladder"
        )
    return errors


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
    no model layer can take yet (П8), so neither enters the sum. A code stage
    pays for nothing and adds ``0.0``; a path with no model stage estimates
    ``0.0``. Expects a config that passed :func:`validate_path_config`.

    Raises:
        KeyError: when no path is described for ``key``.
    """
    paths = config.task_types[key.task_type].paths
    if key.state not in paths:
        raise KeyError(f"No path is described for '{key}'")
    return sum((_money_ceiling(config.stages[name]) for name in paths[key.state]), 0.0)


def _money_ceiling(stage: ModelStage | CodeStage) -> float:
    return stage.ceilings.money_usd if isinstance(stage, ModelStage) else 0.0


def model_stages(config: PathConfig) -> dict[str, ModelStage]:
    """The stages a model runs, by name — what has a ladder to run and price.

    A code stage is left out: it has no ladder, and the shadow price estimate
    of a review (``cost_estimate.actions.estimate_review``) prices the model
    stages of a path alone.
    """
    return {
        name: stage
        for name, stage in config.stages.items()
        if isinstance(stage, ModelStage)
    }


def in_test_mode(task_type: AssignmentType, config: PathConfig | None = None) -> bool:
    """Is ``task_type`` served by the new path without charging?

    True only for a type on the new path that says ``test_mode: true``; a
    type the file does not declare is not. The billing adapter of the funds
    port is the reader this is for: today the port is
    ``AlwaysEnoughFundsPort`` and nobody asks. ``config`` defaults to the
    cached file (:func:`get_path_config`); tests pass their own.
    """
    cfg = config if config is not None else get_path_config()
    declared = cfg.task_types.get(task_type)
    return (
        declared is not None
        and declared.served_by is ServedBy.NEW_PATH
        and declared.test_mode
    )


def replies_enabled(
    task_type: AssignmentType, config: PathConfig | None = None
) -> bool:
    """Does ``task_type`` take students' replies to its reviews? (task 12a, K3)

    True only for a type on the new path that says ``replies_enabled: true``;
    a type the file does not declare does not. Every reader of the replies
    asks this first — the two routes, the reply screen, the submission's
    state, the rule of the same work — so one switch turns all of them on or
    off together. ``config`` defaults to the cached file
    (:func:`get_path_config`); tests pass their own.
    """
    cfg = config if config is not None else get_path_config()
    declared = cfg.task_types.get(task_type)
    return (
        declared is not None
        and declared.served_by is ServedBy.NEW_PATH
        and declared.replies_enabled
    )
