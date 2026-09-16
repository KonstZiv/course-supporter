"""The submission doors — what can be read at all, before any model is called.

Purpose:
    Every submission passes free, synchronous checks before a single paid call
    is made: what the file is, whether it can be read, and whether what was
    read fits the reading models. These helpers were private to the ARQ task
    module and therefore reachable only from today's Mentor body; the new path
    walks through the same doors (mentor-rebuild task 03), so they live where
    both bodies can reach them, unchanged.

Interface:
    * :func:`extract_document_text` — Stage 1's document seam: the bytes of one
      document in, its text out, ``None`` when the extractor declines.
    * :func:`assemble_submission_text` — a finished Stage 1 result in, the text
      the Mentor reads plus the entries it will not see out. Raises
      ``SecurityRejectedError`` when nothing survives the reading budget.

    Neither takes a session, touches the network, or knows which body called
    it. :func:`extract_document_text` is pure; :func:`assemble_submission_text`
    is pure apart from reading the ladder configuration and the model registry
    for the text budget (``submission_text_budget_chars``, cached per process).

Extending:
    A new shape coming out of Stage 1 is a branch in
    :func:`assemble_submission_text`; a new reason a file went unread is a
    member of the Stage 1 vocabulary and needs nothing here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

    from course_supporter.security.schemas import NotOpenedEntry
    from course_supporter.security.stage1 import Stage1Result


def extract_document_text(raw: bytes) -> str | None:
    """Stage 1's document seam, backed by the normalizer's extractor.

    Supplied from here rather than imported inside ``security`` so the two
    packages keep their one-way dependency: the normalizer already reads the
    security policies, and importing it back would tie them together. The same
    extractor serves the project-submission path (``homework/
    project_submission.py``), so a docx cannot mean one thing there and
    another here.
    """
    from course_supporter.normalizer.extract import DefaultTextExtractor
    from course_supporter.normalizer.models import EntryClass

    return DefaultTextExtractor().extract(EntryClass.DOCUMENT, raw)


def assemble_submission_text(
    result: Stage1Result, *, file_bytes: bytes, filename: str
) -> tuple[str, tuple[NotOpenedEntry, ...]]:
    """Build the text the Mentor reads, and the list of what it will not see.

    Three shapes come out of Stage 1: an archive yields the members that were
    read, a text or document input yields screened text, and anything else
    falls back to a best-effort decode. All three are then bounded by what the
    reading models can hold -- an archive by dropping the files that do not
    fit, a single input by being refused outright, never by truncation. A
    review of half a solution, presented as a review of the solution, is worse
    than no review.

    The block naming everything left out is appended last and covers both
    reasons a file went unread: the checker could not open it, or it did not
    fit.
    """
    from course_supporter.homework.text_budget import (
        ensure_single_file_fits,
        fit_archive_entries,
        submission_text_budget_chars,
    )
    from course_supporter.security.exceptions import (
        ErrorCategory,
        SecurityRejectedError,
    )

    budget = submission_text_budget_chars()

    if result.archive_entries is not None:
        # ``archive_entries`` carries only what was read, so nothing set aside
        # is decoded into the prompt here.
        fitted = fit_archive_entries(result.archive_entries, budget_chars=budget)
        body = fitted.text
        not_opened = result.not_opened + fitted.over_budget
        if not body:
            # Everything the checker could read was then too large to read.
            raise SecurityRejectedError(
                ErrorCategory.OVER_BUDGET,
                f"no file in {filename!r} fits the {budget}-character review",
            )
    else:
        body = (
            result.nfc_text
            if result.nfc_text is not None
            else file_bytes.decode("utf-8", errors="replace")
        )
        ensure_single_file_fits(body, filename=filename, budget_chars=budget)
        not_opened = result.not_opened

    return body + _not_opened_block(not_opened), not_opened


def _not_opened_block(entries: Sequence[NotOpenedEntry]) -> str:
    """Render the tail block naming the archive members that were not read.

    Appended to ``submission_text`` so the Mentor cannot rest a review on a
    partial reading without knowing it did. Deliberately shaped UNLIKE the
    ``--- name ---`` file separator: a different frame, one block, and no body
    after each name, so the model cannot mistake the list for more work to
    grade. Reasons stay service keys — the Mentor is a black box here and its
    prompts are not touched; the human wording lives on the portal.
    """
    if not entries:
        return ""
    lines = "\n".join(f"{e.arcname} · {e.reason.value} · {e.size}" for e in entries)
    return f"\n\n=== NOT OPENED ({len(entries)}) ===\n{lines}\n"
