"""What the author's routes share (mentor-rebuild tasks 06, 07b).

The answers several author routes give alike, from one place so they cannot
drift: a job collision, met by the routes of a test's answer key and by the
routes of a test written in the system; and the refusals of a test's draft —
the format's, with its place, and a Stage 1 screen's — given by the test
routes and by a YAML file uploaded as a test through the document route; and
the refusal of a draft that is not finished yet, with every unfinished place
(task 07c).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import TYPE_CHECKING

from fastapi import HTTPException

from course_supporter.homework.reference_service import GenerationInProgressError
from course_supporter.homework.test_completeness import DraftIncompleteError
from course_supporter.homework.test_yaml import (
    MAX_BODY_BYTES,
    DraftRefusalCode,
    DraftRefusedError,
    RefusalPlace,
)
from course_supporter.models.source import AssignmentType
from course_supporter.security.exceptions import SecurityRejectedError
from course_supporter.storage.project_base_repository import ProjectBaseRepository

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from course_supporter.storage.orm import AuthoredDocument
    from course_supporter.storage.s3 import S3Client

_PROJECT_TASK_TYPES = (
    AssignmentType.PROJECT,
    AssignmentType.PROJECT.value,
    "project",
)


async def collect_documents_s3_keys(
    documents: Sequence[AuthoredDocument],
    *,
    session: AsyncSession,
    s3: S3Client,
) -> list[str]:
    """Collect deduplicated S3 object keys for documents before cascade scrub.

    Gathers ``source_url`` keys, persisted ``slide_keys``, and ``project_bases``
    ``archive_key`` / ``snapshot_key`` entries for project tasks before
    cascade soft-delete scrubs ``source_url`` or removes ``project_bases`` rows.
    """
    file_keys: list[str] = []
    project_doc_ids: list[uuid.UUID] = []

    for doc in documents:
        s3_key = s3.extract_key(doc.source_url)
        if s3_key:
            file_keys.append(s3_key)
        if doc.slide_keys:
            file_keys.extend(key for key in doc.slide_keys if key)
        if doc.task_type in _PROJECT_TASK_TYPES:
            project_doc_ids.append(doc.id)

    if project_doc_ids:
        bases = await ProjectBaseRepository(session).list_for_documents(
            list(dict.fromkeys(project_doc_ids))
        )
        for base in bases:
            if base.archive_key:
                file_keys.append(base.archive_key)
            if base.snapshot_key:
                file_keys.append(base.snapshot_key)

    return list(dict.fromkeys(file_keys))


def generation_in_progress(exc: GenerationInProgressError) -> HTTPException:
    """Turn a job collision into the 409 the author reads (task 07, decision 9).

    409, not 422: nothing is wrong with the key, the draft or the task. Another
    job of this task holds the one slot the database allows; the request is not
    wrong, only early. Nothing the request wrote stands — not a key, not a
    version: the refused insert voided the transaction, and the route rolls the
    session back before answering (``GenerationInProgressError`` says why both).
    The answer says so.
    """
    return HTTPException(
        status_code=409,
        detail={
            "code": exc.code,
            "details": (
                "another job of this task is still running — its explanations "
                "being written, or the task itself being processed; nothing was "
                "saved, so send the request again once it finishes"
            ),
        },
    )


def too_large_a_test() -> DraftRefusedError:
    """A body or a file read past the size a test may have (section 6.2)."""
    return DraftRefusedError(
        DraftRefusalCode.TEST_TOO_LARGE,
        f"the test is over {MAX_BODY_BYTES} bytes; a test may have {MAX_BODY_BYTES}",
        RefusalPlace(),
    )


def draft_refused(exc: DraftRefusedError) -> HTTPException:
    """A draft the format refuses: its code, what is wrong, and where (section 6.3)."""
    return HTTPException(
        status_code=413 if exc.code is DraftRefusalCode.TEST_TOO_LARGE else 422,
        detail={
            "code": exc.code.value,
            "details": exc.details,
            "place": exc.place.to_json(),
        },
    )


def draft_incomplete(exc: DraftIncompleteError) -> HTTPException:
    """A draft not finished yet: every place it is unfinished at (task 07c).

    ``incomplete`` is the very list the draft's reading shows, so the author's
    interface reads the refusal and the reading with one parser.
    """
    return HTTPException(
        status_code=422,
        detail={
            "code": exc.code,
            "details": (
                "the draft is not finished: every place listed must be completed first"
            ),
            "incomplete": [place.to_json() for place in exc.places],
        },
    )


def security_rejected(exc: SecurityRejectedError) -> HTTPException:
    """A text a Stage 1 screen refused, answered as a refused upload is."""
    return HTTPException(
        status_code=400,
        detail={
            "code": "SECURITY_REJECTED",
            "category": exc.category.value,
            "details": exc.detail,
        },
    )
