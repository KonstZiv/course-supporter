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
      :func:`assemble_submission_work` is the same for the new path, with the
      text file by file beside it (task 09b); today's Mentor keeps calling the
      first, unchanged.
    * :class:`DoorReading` — what the doors read, as both bodies carry it on:
      the text, what was not opened, how the file was read, and what the
      signal screen noticed (task 11). :func:`carry_door_reading` puts the
      last three onto the Stage 2 verdict, where the read path finds them.
    * :func:`screen_student_note` — the comment through the one text screen,
      for its flags (task 11, decision 10).
    * :func:`persist_door_refusal` — the one shape a refusal at the doors is
      stored in, for a refusal found after Stage 1's own ``try``.

    None but the last takes a session, touches the network, or knows which
    body called it. :func:`extract_document_text` is pure;
    :func:`assemble_submission_text` is pure apart from reading the ladder
    configuration and the model registry for the text budget
    (``submission_text_budget_chars``, cached per process).

Extending:
    A new shape coming out of Stage 1 is a branch in
    :func:`assemble_submission_text`; a new reason a file went unread is a
    member of the Stage 1 vocabulary and needs nothing here.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from course_supporter.security.schemas import NotOpenedEntry, ScreenFlag

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from course_supporter.homework.criteria_verdicts import WorkFile
    from course_supporter.security.exceptions import SecurityRejectedError
    from course_supporter.security.schemas import SafetyResult
    from course_supporter.security.stage1 import Stage1Result
    from course_supporter.storage.homework_repository import HomeworkRepository

STUDENT_NOTE_SOURCE = "student_note"
"""The ``source`` of a flag found in the student's comment."""


@dataclass(frozen=True, slots=True)
class DoorReading:
    """What the doors read out of one submission, for every stage after them.

    Attributes:
        text: What the models read -- the reviewer, the attempt classifier
            and Stage 2 alike. Never carries a flag.
        not_opened: What was named and not read, with the reason.
        recovered_encoding: How a single text file was read; ``None`` when the
            question does not apply (archive, document, project, test).
        flags: What the signal screen noticed -- in the files, their names and
            the comment. For Stage 2 and the trace only (task 11, decision 2).
        files: ``text`` file by file, for the quotes of the evaluation stage
            (task 09b); empty where nothing was read as files (a test, a
            project).
    """

    text: str
    not_opened: tuple[NotOpenedEntry, ...] = ()
    recovered_encoding: str | None = None
    flags: tuple[ScreenFlag, ...] = ()
    files: tuple[WorkFile, ...] = ()


def carry_door_reading(verdict: SafetyResult, reading: DoorReading) -> None:
    """Put what the doors know onto the Stage 2 verdict before it is stored.

    The verdict's column is what the read path reads, so a submission that
    PASSES must still carry which files were skipped, how its file was read,
    and -- for the author and support, never the student -- what the screen
    noticed, capped in a stable order.
    """
    from course_supporter.security.text_screen import trail_flags

    verdict.not_opened = list(reading.not_opened)
    verdict.recovered_encoding = reading.recovered_encoding
    verdict.flags, verdict.flags_omitted = trail_flags(reading.flags)


def screen_student_note(note: str | None) -> tuple[ScreenFlag, ...]:
    """The comment's flags, from the same screen and mode as the files.

    The door already screened the comment and stored it in NFC
    (``submission_core.check_student_note``), and stored nothing else: the door
    is synchronous in the API and the flags are wanted in the worker. The
    screen is deterministic and free, so it simply runs again here.

    Raises:
        SecurityRejectedError: ``SUSPICIOUS_UNICODE`` for a comment stored
            before the door screened comments -- the characters that refuse a
            file refuse the submission too.
    """
    from course_supporter.security.policies import HOMEWORK_POLICY
    from course_supporter.security.text_screen import screen_text

    if not note:
        return ()
    return screen_text(
        note, name=STUDENT_NOTE_SOURCE, mode=HOMEWORK_POLICY.text_screen_mode
    ).flags


async def persist_door_refusal(
    session: AsyncSession,
    hw_repo: HomeworkRepository,
    submission_id: uuid.UUID,
    exc: SecurityRejectedError,
) -> None:
    """Store a refusal at the doors the way Stage 1's own refusal is stored."""
    from course_supporter.security.schemas import Stage1RejectionResult

    rejection = Stage1RejectionResult(category=exc.category, detail=exc.detail)
    await hw_repo.store_safety_result(submission_id, rejection.model_dump(mode="json"))
    await hw_repo.update_status(submission_id, "rejected", error_message=exc.detail)
    await session.commit()


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
    text, not_opened, _files = _assemble(
        result, file_bytes=file_bytes, filename=filename
    )
    return text, not_opened


def assemble_submission_work(
    result: Stage1Result, *, file_bytes: bytes, filename: str
) -> tuple[str, tuple[NotOpenedEntry, ...], tuple[WorkFile, ...]]:
    """:func:`assemble_submission_text`, with the same text file by file.

    The new path's door (task 09b, ``PRE-FLIGHT.md`` 9.3): the text and what
    was not opened are exactly what :func:`assemble_submission_text` returns,
    and beside them is each file the text carries — its name and its own text,
    without the frame — so the evaluation stage looks for a quote inside one
    file and can say where it stands. A document (docx, pdf) has no lines a
    student would recognise, so its quotes are placed by the file alone.
    """
    from course_supporter.homework.criteria_verdicts import WorkFile

    text, not_opened, files = _assemble(
        result, file_bytes=file_bytes, filename=filename
    )
    return (
        text,
        not_opened,
        tuple(WorkFile(name, body, _has_lines(name)) for name, body in files),
    )


def _has_lines(filename: str) -> bool:
    """Whether a file's lines are the student's: not for an extracted document."""
    from course_supporter.security.file_type import extension_of
    from course_supporter.security.policies import HOMEWORK_CONVEYORS

    return HOMEWORK_CONVEYORS.get(extension_of(filename)) != "document"


def _assemble(
    result: Stage1Result, *, file_bytes: bytes, filename: str
) -> tuple[str, tuple[NotOpenedEntry, ...], tuple[tuple[str, str], ...]]:
    """The text, what was not opened, and each read file's name and text."""
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
        files = fitted.files
    else:
        body = (
            result.nfc_text
            if result.nfc_text is not None
            else file_bytes.decode("utf-8", errors="replace")
        )
        ensure_single_file_fits(body, filename=filename, budget_chars=budget)
        not_opened = result.not_opened
        files = ((filename, body),)

    return body + not_opened_block(not_opened), not_opened, files


def not_opened_block(entries: Sequence[NotOpenedEntry]) -> str:
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
