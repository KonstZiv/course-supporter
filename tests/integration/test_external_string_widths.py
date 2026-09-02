"""Real external strings fit the columns that store them.

These are the values the system does not author: a MIME type declared by the
browser, a prompt reference that grows with the ladder file names. Unit tests
cannot catch a width problem — the column width lives in the database — so the
guard has to write the real value into the real schema.

The trigger was live: the first ``.docx`` submission after the gates pass
returned 500, because the browser sends
``application/vnd.openxmlformats-officedocument.wordprocessingml.document``
(71 characters) into what was then a ``VARCHAR(50)``.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import String
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.storage.orm import (
    AuthoredDocument,
    Base,
    CourseNode,
    ExternalServiceCall,
    HomeworkSubmission,
    Job,
    Student,
    Tenant,
)

pytestmark = pytest.mark.requires_db

# The real value a browser sends for a Word document — 71 characters. Written
# out rather than computed so the test states what it is defending against.
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

# The longest prompt reference in config/ladders_mentor.yaml — 51 characters,
# already past the old VARCHAR(50). Nothing writes prompt_ref yet (DD-CQ-C),
# which is the only reason this never surfaced in production.
LONGEST_PROMPT_REF = "prompts/mentor_layered_evaluation_node_course/v1.md"


async def test_docx_mime_fits_file_type(
    db_session: AsyncSession,
    seed_tenant: Tenant,
    seed_root_node: CourseNode,
    seed_material_entry: AuthoredDocument,
) -> None:
    assert len(DOCX_MIME) == 71

    student = Student(tenant_id=seed_tenant.id, external_id=f"w-{uuid.uuid4().hex[:8]}")
    db_session.add(student)
    await db_session.flush()

    submission = HomeworkSubmission(
        tenant_id=seed_tenant.id,
        student_id=student.id,
        course_node_id=seed_root_node.id,
        node_id=seed_root_node.id,
        authored_document_id=seed_material_entry.id,
        file_url="https://example.invalid/homework/work.docx",
        file_type=DOCX_MIME,
        original_filename="work.docx",
    )
    db_session.add(submission)
    await db_session.flush()
    await db_session.refresh(submission)

    # Round-trips whole: a width that truncated instead of raising would be
    # worse than the 500 this replaces.
    assert submission.file_type == DOCX_MIME


async def test_longest_prompt_ref_fits(
    db_session: AsyncSession, seed_tenant: Tenant, seed_root_node: CourseNode
) -> None:
    assert len(LONGEST_PROMPT_REF) == 51

    job = Job(
        tenant_id=seed_tenant.id,
        course_node_id=seed_root_node.id,
        job_type="homework_processing",
    )
    db_session.add(job)
    await db_session.flush()

    call = ExternalServiceCall(
        job_id=job.id,
        action="mentor_layered_evaluation_node_course",
        provider="deepseek_thinking",
        model_id="deepseek-v4-pro",
        prompt_ref=LONGEST_PROMPT_REF,
    )
    db_session.add(call)
    await db_session.flush()
    await db_session.refresh(call)

    assert call.prompt_ref == LONGEST_PROMPT_REF


async def test_model_written_description_beyond_the_old_bound_fits(
    db_session: AsyncSession,
    seed_root_node: CourseNode,
    seed_material_entry: AuthoredDocument,
) -> None:
    # A model asked for "≤512 characters" is not a model bound by 512: the
    # limit is an instruction in the prompt, and only some draft schemas carry
    # a max_length. Where they do not, the column was the last thing standing
    # between an over-long description and a 500 on authored ingestion.
    from course_supporter.storage.orm import DocumentSummary

    long_description = "Опис, довший за стару межу. " * 25
    assert len(long_description) > 512

    summary = DocumentSummary(
        authored_document_id=seed_material_entry.id,
        course_root_id=seed_root_node.id,
        title="Widths",
        description=long_description,
    )
    db_session.add(summary)
    await db_session.flush()
    await db_session.refresh(summary)

    assert summary.description == long_description


# ── Structural guard ───────────────────────────────────────────────

# Columns whose value is authored by somebody other than this system: a client,
# an author, a model, an external identity provider. The list is the explicit
# source, transcribed from gates/PROBE-VARCHAR.md — deliberately NOT derived
# from the models, because deriving it from the thing under test would make the
# guard agree with any change to it.
#
# Bounded columns absent from this list are internal vocabularies (statuses,
# categories, provider keys), fixed-shape values (sha256 hex, canonical
# ISO 639-3 language codes) or generous conventional caps that the standard,
# not this system, tops out (e-mail 254 < 320, filesystem names 255 < 500,
# browser URLs ~2000).
EXTERNALLY_AUTHORED: frozenset[tuple[str, str]] = frozenset(
    {
        ("homework_submissions", "file_type"),  # MIME declared by the client
        ("external_service_calls", "prompt_ref"),  # grows with ladder filenames
        ("document_segments", "description"),  # written by a model
        ("document_summaries", "description"),  # written by a model
    }
)


def test_externally_authored_columns_are_not_width_bounded() -> None:
    """No column that stores somebody else's string may cap its width.

    A width is a promise about a value we control. On a value we do not, it can
    only fail — as a truncation, which loses data silently, or as a 500 on an
    ordinary upload, which is what actually happened. The bound belongs in
    validation at the door, where it can answer with a reason.
    """
    bounded = {
        (table.name, col.name)
        for table in Base.metadata.sorted_tables
        for col in table.columns
        if isinstance(col.type, String) and col.type.length
    }
    offenders = sorted(EXTERNALLY_AUTHORED & bounded)
    assert offenders == [], (
        f"{offenders!r} store externally-authored strings in a width-bounded "
        f"column; use Text and bound the value in validation instead"
    )


def test_the_guard_list_still_matches_real_columns() -> None:
    # A guard that names a column no longer in the schema passes for the wrong
    # reason. Pin that every entry still exists.
    known = {
        (table.name, col.name)
        for table in Base.metadata.sorted_tables
        for col in table.columns
    }
    missing = sorted(EXTERNALLY_AUTHORED - known)
    assert missing == [], f"guard names columns that no longer exist: {missing!r}"
