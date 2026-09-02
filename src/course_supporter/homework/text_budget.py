"""How much of a submission can actually be read, derived from what reads it.

The ceiling on submission text is not a product preference and not a price
list — it is the smallest context window among the models that will be handed
that text. Four calls read ``submission_text`` in full: the Stage 2 safety
check, the sanity gate, and the two layered evaluations (node+course, and
industry). Whichever rung of those four ladders has the tightest window is the
real limit; sending more buys a provider-side truncation, which is the one
outcome worse than a refusal, because nobody is told.

The number is therefore READ, never written. ``config/ladders_mentor.yaml``
says which models those stages may use and ``config/external_services.yaml``
says how large each one's window is; changing a rung moves the budget on its
own. A constant here would be a fourth place to forget.

## Why the money is not the input

An earlier draft derived this from a per-submission cost ceiling. There is no
such ceiling in the code — the $0.10 of the Г1 run was a stop condition the
implementer held in their head — and the context window binds first anyway:
the top rung of safety and sanity carries 131 072 tokens, an order of
magnitude under the million-token rungs elsewhere in the ladder. Cost follows
from the budget rather than setting it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache

import structlog

from course_supporter.config import get_settings
from course_supporter.llm.ladder_config import load_ladder_config
from course_supporter.llm.registry import load_registry
from course_supporter.security.archive import ClassifiedEntry, ExtractedFile
from course_supporter.security.exceptions import ErrorCategory, SecurityRejectedError
from course_supporter.security.schemas import NotOpenedEntry

logger = structlog.get_logger()

# The four calls that receive ``submission_text`` in full. Verified against the
# code, not assumed: safety (security/stage2.py), sanity (homework/
# sanity_gate.py) and the two evaluations dispatched together by
# homework/review_graph.py. ``criteria_decomposition`` is absent on purpose --
# it reads the TASK, which is why its result caches across submissions.
STAGES_READING_SUBMISSION: tuple[str, ...] = (
    "safety_check",
    "sanity_check",
    "mentor_layered_evaluation_node_course",
    "mentor_layered_evaluation_industry",
)

# Share of the window left for everything that is not the student's text: the
# rendered prompt, the task statement, the criteria, the node and course
# summaries, the author's notes, the attempt history, and the model's own
# answer. Half is the ratio this project already uses for the same purpose --
# ``input_budget_ratio: 0.5`` in the ladder files, KD10 «Token budget policy».
# Reused rather than re-derived so the two cannot drift into different ideas of
# what a prompt costs.
_PROMPT_RESERVE_RATIO = 0.5

# Tokens are budgeted; text is measured in characters. The project's existing
# estimator (llm/token_budget.py) divides by 3.5, which DD-3.2.3-pre-B records
# as an UNDER-estimate on Ukrainian content -- Cyrillic tokenizes closer to two
# characters per token. Under-estimating tokens is safe where that estimator is
# used (it over-states the input and skips a rung early); here the conversion
# runs the other way, so the same 3.5 would over-state how much text fits. Two
# is the conservative direction for this use.
_CHARS_PER_TOKEN = 2.0

# Rendered frame around one archive member inside ``submission_text``. Counted
# against the budget because the model pays for it like any other character.
_ENTRY_FRAME = "--- {name} ---\n"


@dataclass(frozen=True, slots=True)
class FittedBody:
    """The body that fits, and what had to be left out to make it fit."""

    text: str
    over_budget: tuple[NotOpenedEntry, ...]


@lru_cache(maxsize=1)
def submission_text_budget_chars() -> int:
    """Characters of submission text the tightest reader can still accept.

    Cached: the inputs are configuration files read once at startup, and the
    answer cannot change while the process lives.

    Raises:
        RuntimeError: when no rung of the reading stages carries a declared
            context window. Guessing a budget would mean guessing what the
            model can hold, and a wrong guess is a silent truncation.
    """
    settings = get_settings()
    ladders = load_ladder_config(settings.ladders_dir)
    registry = load_registry(settings.external_services_path)

    windows: list[int] = []
    for stage_name in STAGES_READING_SUBMISSION:
        stage = ladders.stages.get(stage_name)
        if stage is None:
            continue
        for rung in stage.ladder:
            model = registry.models.get(rung.model)
            if model is not None and model.max_context is not None:
                windows.append(model.max_context)

    if not windows:
        raise RuntimeError(
            "no rung of "
            f"{STAGES_READING_SUBMISSION} declares max_context; the submission "
            "budget cannot be derived and must not be guessed"
        )

    tightest = min(windows)
    budget = int(tightest * (1.0 - _PROMPT_RESERVE_RATIO) * _CHARS_PER_TOKEN)
    logger.info(
        "submission_text_budget_derived",
        tightest_context_tokens=tightest,
        budget_chars=budget,
    )
    return budget


def ensure_single_file_fits(text: str, *, filename: str, budget_chars: int) -> None:
    """Refuse a single submission whose text cannot be read whole.

    Truncating is not on offer: a review of the first half of a solution,
    presented as a review of the solution, is worse than no review. The student
    is told instead, and can decide what to leave out.
    """
    if len(text) > budget_chars:
        raise SecurityRejectedError(
            ErrorCategory.OVER_BUDGET,
            (
                f"{filename!r} carries {len(text)} characters of text; the "
                f"review reads at most {budget_chars}"
            ),
        )


def fit_archive_entries(
    entries: Sequence[ExtractedFile | ClassifiedEntry], *, budget_chars: int
) -> FittedBody:
    """Render the members that fit; name the ones that do not.

    Smallest first. Fitting the large files first would spend the whole budget
    on one generated artefact and drop every hand-written file behind it; going
    up from the smallest keeps the most files, which is the same reason a
    reviewer opens the short ones first.

    Selection is by size, but the rendered body keeps the archive's own order:
    the Mentor should see the work laid out as the student packed it, not
    sorted by length.
    """
    rendered = {
        entry.arcname: _ENTRY_FRAME.format(name=entry.arcname)
        + entry.content.decode("utf-8", errors="replace")
        for entry in entries
    }

    kept: set[str] = set()
    over: list[NotOpenedEntry] = []
    used = 0
    for entry in sorted(entries, key=lambda e: len(rendered[e.arcname])):
        block = rendered[entry.arcname]
        # ``+ 1`` for the newline this block will be joined with.
        cost = len(block) + (1 if kept else 0)
        if used + cost <= budget_chars:
            kept.add(entry.arcname)
            used += cost
        else:
            over.append(
                NotOpenedEntry(
                    arcname=entry.arcname,
                    reason=ErrorCategory.OVER_BUDGET,
                    size=len(entry.content),
                )
            )

    body = "\n".join(rendered[e.arcname] for e in entries if e.arcname in kept)
    return FittedBody(text=body, over_budget=tuple(over))
