"""The version of the evaluator that writes a review (task 13).

Purpose:
    The dedup (:mod:`~course_supporter.homework.attempt_dedup`) gives the same
    work its earlier review without a model call. That review is only as right
    as the evaluator that wrote it, and the evaluator changes: a prompt is
    edited, a stage gets another first model, the rules by which the code sets
    a verdict are fixed — the fix of the quote check changed the right answer
    for the very same work. This module names an evaluator with one short
    string; a review is given again only while the evaluator that wrote it is
    still in force.

What enters a version:
    The path of ``(task_type, state)`` in the submission-path file: every MODEL
    stage on it, in the path's order, each with

    * the SHA-256 of its prompt, read by the loader the router reads it with
      (:func:`~course_supporter.llm.prompt_loader_md.load_prompt`) — the
      template's ``StagePrompt.content_hash()``, the hash the call register
      stores beside ``prompt_ref``. It covers the
      sections the model is sent; an editorial section the loader drops never
      reaches the model and changes nothing;
    * its first rung: provider and model — the rung that answers unless it
      fails. The later rungs are not in it;

    and :data:`~course_supporter.homework.criteria_verdicts.VERDICT_RULES_VERSION`.
    A code stage is not in it. The value is ``"ev1:"`` and the first 16 hex
    characters of the SHA-256 of the canonical JSON of these (keys sorted, no
    spaces). ``ev1`` names this recipe: another recipe takes another prefix,
    so no value of the old one ever equals a value of the new.

    ``None`` — the type is not on the new path, the state has no path, or the
    path has no model stage (a test, scored by code alone).

Which evaluator a copy may come from:
    The student's first attempt walks the ``first`` path, which also asks the
    attempt classifier; every later attempt walks a ``repeat_*`` path, which
    does not. The two paths are two versions even when nothing has changed, so
    a review is not compared with the version of the path the NEW submission
    walks, but with the versions of every path of its type as the file stands
    now: :func:`evaluator_versions_in_force`. A review whose version is one of
    them was written by an evaluator still in force. None of them — no copy.

Interface:
    :func:`evaluator_version` — the version of one path.
    :func:`evaluator_versions_in_force` — the versions a source of a copy may
    carry.
    :data:`EVALUATOR_VERSION_RECEIPT_KEY` — its key in the attempt receipt.

    Pure functions over the configuration and the prompt files: no session, no
    clock, no network. ``prompt_base_path`` resolves ``prompt_ref`` as on
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
from course_supporter.homework.path_config import (
    ModelStage,
    PathConfig,
    ServedBy,
    SubmissionState,
)
from course_supporter.llm.prompt_loader_md import load_prompt
from course_supporter.models.source import AssignmentType

__all__ = [
    "EVALUATOR_VERSION_RECEIPT_KEY",
    "evaluator_version",
    "evaluator_versions_in_force",
]

EVALUATOR_VERSION_RECEIPT_KEY: Final = "evaluator_version"
"""The attempt receipt's key for the version of the evaluator of its review."""

_PREFIX: Final = "ev1:"
_HEX_CHARS: Final = 16


def evaluator_version(
    task_type: AssignmentType,
    state: SubmissionState,
    config: PathConfig,
    *,
    prompt_base_path: Path | None = None,
) -> str | None:
    """The version of the evaluator on the path of ``(task_type, state)``.

    See the module for what enters it; ``None`` when the type is not on the
    new path, the state has no path, or the path has no model stage.

    Raises:
        FileNotFoundError, InvalidPromptError: a stage's prompt cannot be
            read — what the boot's check of the file refuses first.
    """
    declared = config.task_types.get(task_type)
    if (
        declared is None
        or declared.served_by is not ServedBy.NEW_PATH
        or state not in declared.paths
    ):
        return None
    stages = [
        {
            "prompt_sha256": load_prompt(
                stage.prompt_ref, base_path=prompt_base_path
            ).content_hash(),
            "provider": stage.ladder[0].provider,
            "model": stage.ladder[0].model,
        }
        for name in declared.paths[state]
        if isinstance(stage := config.stages[name], ModelStage)
    ]
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


def evaluator_versions_in_force(
    task_type: AssignmentType,
    state: SubmissionState,
    config: PathConfig,
    *,
    prompt_base_path: Path | None = None,
) -> frozenset[str]:
    """The versions a review given again to a submission on this path may carry.

    Empty when the submission's own path has no version: no copy then. Else
    the version of every path of the type, as the file stands now (the
    module's "Which evaluator a copy may come from").
    """
    if (
        evaluator_version(task_type, state, config, prompt_base_path=prompt_base_path)
        is None
    ):
        return frozenset()
    versions = (
        evaluator_version(task_type, other, config, prompt_base_path=prompt_base_path)
        for other in config.task_types[task_type].paths
    )
    return frozenset(version for version in versions if version is not None)
