"""The version of the evaluator that writes a type's reviews (task 13).

Purpose:
    The dedup (:mod:`~course_supporter.homework.attempt_dedup`) gives the same
    work its earlier review without a model call. That review is only as right
    as the evaluator that wrote it, and the evaluator changes: a prompt is
    edited, a stage gets another first model, the code that shapes a review is
    fixed — the fix of the quote check changed the right answer for the very
    same work. This module names the evaluator of a task type with one short
    string; a review is given again only while that string is the type's
    version today.

What enters a version:
    ONE version per task type, not per path. Every MODEL stage on any path of
    the type in the submission-path file — the union of its paths, each stage
    once, in the order of the stage's name — each with

    * the SHA-256 of its prompt, read by the loader the router reads it with
      (:func:`~course_supporter.llm.prompt_loader_md.load_prompt`) — the
      template's ``StagePrompt.content_hash()``, the hash the call register
      stores beside ``prompt_ref``. It covers the sections the model is sent;
      an editorial section the loader drops never reaches the model and
      changes nothing;
    * its first rung: provider and model — the rung that answers unless it
      fails. The later rungs are not in it;

    and :data:`~course_supporter.homework.criteria_verdicts.VERDICT_RULES_VERSION`
    — the code's share. A code stage is not in it, nor the state a stage is
    listed under: the student's first attempt walks ``first`` (which also asks
    the attempt classifier) and every later one a ``repeat_*`` path, and both
    are reviewed by the evaluator of their type, one version. A change of any
    model stage of any path of the type is a new version for all of them.

    The value is ``"ev1:"`` and the first 16 hex characters of the SHA-256 of
    the canonical JSON of these (keys sorted, no spaces). ``ev1`` names this
    recipe: another recipe takes another prefix, so no value of the old one
    ever equals a value of the new.

    ``None`` — the type is not on the new path, or no path of it has a model
    stage (a test, scored by code alone): such a type is not deduplicated.

Interface:
    :func:`evaluator_version` — the version of a type's evaluator.
    :data:`EVALUATOR_VERSION_RECEIPT_KEY` — its key in the attempt receipt.

    A pure function over the configuration and the prompt files: no session,
    no clock, no network. ``prompt_base_path`` resolves ``prompt_ref`` as on
    :func:`~course_supporter.llm.prompt_loader_md.load_prompt` — ``None`` in
    production (the working directory, as the router), a temporary directory
    in tests.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Final

from course_supporter.homework import criteria_verdicts
from course_supporter.homework.path_config import ModelStage, PathConfig, ServedBy
from course_supporter.llm.prompt_loader_md import load_prompt
from course_supporter.models.source import AssignmentType

__all__ = ["EVALUATOR_VERSION_RECEIPT_KEY", "evaluator_version"]

EVALUATOR_VERSION_RECEIPT_KEY: Final = "evaluator_version"
"""The attempt receipt's key for the version of the evaluator of its review."""

_PREFIX: Final = "ev1:"
_HEX_CHARS: Final = 16


def evaluator_version(
    task_type: AssignmentType,
    config: PathConfig,
    *,
    prompt_base_path: Path | None = None,
) -> str | None:
    """The version of the evaluator of ``task_type``'s reviews.

    See the module for what enters it; ``None`` when the type is not on the
    new path or no path of it has a model stage.

    Raises:
        FileNotFoundError, InvalidPromptError: a stage's prompt cannot be
            read — what the boot's check of the file refuses first.
    """
    declared = config.task_types.get(task_type)
    if declared is None or declared.served_by is not ServedBy.NEW_PATH:
        return None
    on_its_paths = {name for stages in declared.paths.values() for name in stages}
    stages = {
        name: {
            "prompt_sha256": load_prompt(
                stage.prompt_ref, base_path=prompt_base_path
            ).content_hash(),
            "provider": stage.ladder[0].provider,
            "model": stage.ladder[0].model,
        }
        for name in sorted(on_its_paths)
        if isinstance(stage := config.stages[name], ModelStage)
    }
    if not stages:
        return None
    canonical = json.dumps(
        {
            "stages": stages,
            "verdict_rules": criteria_verdicts.VERDICT_RULES_VERSION,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return _PREFIX + digest[:_HEX_CHARS]
