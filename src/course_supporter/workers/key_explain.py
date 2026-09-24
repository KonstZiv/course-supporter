"""The work that writes a test key's explanations (mentor-rebuild tasks 06, 07).

Runs once per (task version, answers, language), outside any student
submission — ``TASK.md`` invariant 3. The service decides that a version is
needed and asks for this work; the work decides nothing about versions and
everything about money and models.

Order of the body, and why each step is where it is:

1. **Read** the version, the author's layer and the task.
2. **Check the key again.** Between the request and the run, the author can
   replace the key or re-author the test. The version's axes are what it was
   asked to explain, and a set of explanations written for answers nobody holds
   any more would be paid for and then never read. So the two hashes are
   compared once more here, inside the job, and a mismatch fails the version
   without calling a model.
3. **Ask the funds port**, before the first paid call, with this stage's
   ceiling. A refusal is an answer: the version fails with it as its reason,
   and the author reads why nothing was written.
4. **Generate**, from the test's source text — the text its questions are
   parsed from, not the stitched one (task 07, decision 25). The agent's
   validator is what guarantees one non-empty explanation and one doubt flag
   per question. ANY failure here — the ladder running out, or a defect nobody
   foresaw — fails the version with a reason and re-raises, so the author
   never sees "still writing" for work that has already stopped. A success
   stores the explanations, the doubted questions and the prompt that wrote
   them — the prompt read from this job's register rows, like the cost below.
5. **Account the actual cost** — after success AND after a failure that already
   spent something, because a failed generation is not a free one. The number
   is summed from the register rows of THIS job, so it is what was really
   charged rather than what was estimated.
"""

from __future__ import annotations

import uuid
from typing import Any

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.agents.key_explainer import (
    KEY_EXPLANATION_CEILING_USD,
    STAGE_NAME,
    KeyExplainerAgent,
)
from course_supporter.call_outcome import FundsDecision
from course_supporter.funds_port import (
    AlwaysEnoughFundsPort,
    FundsPort,
    VersionWorkContext,
    VersionWorkKind,
)
from course_supporter.homework.reference_key import answers_digest
from course_supporter.jobs.execution_seam import through_seam
from course_supporter.language import display_name
from course_supporter.llm.error_categories import LadderExhaustedError
from course_supporter.reference_kinds import ReferenceKind
from course_supporter.storage.orm import (
    AuthoredDocument,
    ExternalServiceCall,
    TaskReference,
    TaskReferenceOverride,
)
from course_supporter.storage.task_reference_repository import TaskReferenceRepository

logger = structlog.get_logger(__name__)

_KEY_MOVED = "the key or the task text changed before the explanations were written"
"""Failure reason when the version's axes no longer describe anything current."""


@through_seam()
async def arq_explain_key(
    ctx: dict[str, Any],
    job_id: str,  # UUID as string (ARQ JSON serialization)
    reference_id: str,  # UUID as string
) -> dict[str, Any]:
    """Write the explanations of one reference version, or fail it with a reason.

    Wrapped by the L2 execution seam: the seam owns the ``Job.status``
    lifecycle, so this body never touches it. What the body owns is the
    version's own state — ``pending → ready | failed(reason)`` — because that
    is what the author reads, and it outlives the job row.

    Returns the dict the seam persists to ``Job.result_data``.
    """
    log = logger.bind(job_id=job_id, reference_id=reference_id)
    session_factory = ctx["session_factory"]
    stage_router = ctx["stage_router"]
    port: FundsPort = ctx.get("funds_port") or AlwaysEnoughFundsPort(session_factory)

    async with session_factory() as session:
        repo = TaskReferenceRepository(session)
        version = await repo.get_by_id(uuid.UUID(reference_id))
        if version is None:
            msg = "the reference version vanished after the job was enqueued"
            raise RuntimeError(msg)

        document = await session.get(AuthoredDocument, version.authored_document_id)
        override = await repo.get_override(
            version.authored_document_id, ReferenceKind(version.kind)
        )

        # (2) The key may have moved while this job waited in the queue. The
        # two None checks are part of the same question — a task or a key that
        # is gone is a key that moved — but they are spelled here rather than
        # inside the predicate so the types narrow for everything below.
        if (
            document is None
            or document.deleted_at is not None
            or override is None
            or not _still_current(version, document, override)
        ):
            await repo.mark_failed(version.id, _KEY_MOVED)
            await session.commit()
            log.info("key_explanation.superseded", version=version.version)
            return {"state": "failed", "reason": _KEY_MOVED, "calls": 0}

        context = VersionWorkContext(
            tenant_id=await _tenant_of(session, document),
            authored_document_id=document.id,
            source_content_hash=version.source_content_hash,
            work_kind=VersionWorkKind.KEY_EXPLANATION,
        )

        # (3) Money before models.
        answer = await port.check_and_reserve_for_version(
            context, KEY_EXPLANATION_CEILING_USD
        )
        if answer.decision is FundsDecision.REFUSED:
            reason = f"funds refused: {answer.refusal_reason}"
            await repo.mark_failed(version.id, reason)
            await session.commit()
            log.info("key_explanation.refused", reason=reason)
            return {"state": "failed", "reason": reason, "calls": 0}

        # (4) The only paid step.
        task_text = await _task_text(session, document.id)
        try:
            written = await KeyExplainerAgent(stage_router).explain(
                task_text=task_text,
                answers=override.answers,
                language=display_name(version.language) if version.language else None,
            )
        except Exception as exc:
            # Every way this can end badly leaves the version FAILED with a
            # reason, and then re-raises so the seam fails the Job too. The
            # catch is deliberately broad: a version left `pending` would tell
            # the author "still writing" forever, and the one thing worse than
            # a failure is a failure that looks like progress. LadderExhausted
            # is the expected member; anything else is a defect, and a defect
            # must not be quieter than a bad model.
            reason = (
                f"generation failed: {exc}"
                if isinstance(exc, LadderExhaustedError)
                else f"generation failed unexpectedly: {type(exc).__name__}: {exc}"
            )
            await repo.mark_failed(version.id, reason)
            await session.commit()
            # (5) A failed generation is not a free one.
            await port.account_version_work_cost(
                context, await _spent_on(session, uuid.UUID(job_id))
            )
            log.warning(
                "key_explanation.failed",
                version=version.version,
                error_type=type(exc).__name__,
            )
            raise

        prompt_ref = await _prompt_of(session, uuid.UUID(job_id))
        if prompt_ref is None:
            log.warning("key_explanation.prompt_unrecorded", version=version.version)
        await repo.mark_ready(
            version.id,
            written.explanations,
            doubts=written.doubts,
            prompt_ref=prompt_ref,
        )
        await session.commit()

        spent = await _spent_on(session, uuid.UUID(job_id))
        await port.account_version_work_cost(context, spent)
        log.info(
            "key_explanation.ready",
            version=version.version,
            questions=len(written.explanations),
            doubted=len(written.doubts),
            spent_usd=spent,
        )
        return {
            "state": "ready",
            "questions": len(written.explanations),
            "doubted": len(written.doubts),
            "cost_usd": spent,
        }


def _still_current(
    version: TaskReference,
    document: AuthoredDocument,
    override: TaskReferenceOverride,
) -> bool:
    """Do the version's axes still describe the task and the key as they are?

    Both axes, not one. The author can re-author the text without touching the
    key, and can replace the key without touching the text; either one alone
    makes these explanations answer a question nobody is asking.
    """
    if document.content_hash != version.source_content_hash:
        return False
    return bool(answers_digest(override.answers) == version.answers_hash)


async def _tenant_of(session: AsyncSession, document: AuthoredDocument) -> uuid.UUID:
    """The tenant that owns the task, through its course node."""
    from course_supporter.storage.orm import CourseNode

    node = await session.get(CourseNode, document.course_node_id)
    if node is None:
        msg = "the task's course node vanished"
        raise RuntimeError(msg)
    return node.tenant_id


async def _task_text(session: AsyncSession, document_id: uuid.UUID) -> str:
    """The test's source text: its segments joined without a separator.

    The text its questions are parsed from and the author's key is checked
    against (task 07, decision 25). The text the mentor pipeline stitches puts a
    blank line at every segment boundary, and a boundary can fall inside a
    question — a model reading it would be reading a different test.
    """
    from course_supporter.homework.task_context import load_task_source_text

    return await load_task_source_text(session, document_id)


async def _prompt_of(session: AsyncSession, job_id: uuid.UUID) -> str | None:
    """The prompt that wrote the explanations, as the register recorded it.

    The router writes the stage's ``prompt_ref`` on every call it makes, so the
    register names the prompt that actually answered this job — not whichever
    one the ladder names by the time anyone reads it. ``None`` when the register
    has no such row: its writes are best-effort, and a lost row loses this too.
    """
    stmt = (
        select(ExternalServiceCall.prompt_ref)
        .where(
            ExternalServiceCall.job_id == job_id,
            ExternalServiceCall.action == STAGE_NAME,
            ExternalServiceCall.success.is_(True),
        )
        .order_by(ExternalServiceCall.created_at.desc())
        .limit(1)
    )
    prompt_ref: str | None = (await session.execute(stmt)).scalar_one_or_none()
    return prompt_ref


async def _spent_on(session: AsyncSession, job_id: uuid.UUID) -> float:
    """What this job actually paid, summed from the register rows it wrote.

    The register is the authority on cost, not the estimate and not the agent:
    a rung that failed and a rung that succeeded both left a row, and the port
    is owed the sum of them. Rows of this job that record no call — the
    funds-port row itself — carry NULL and are skipped by the sum.
    """
    stmt = select(func.coalesce(func.sum(ExternalServiceCall.cost_usd), 0.0)).where(
        ExternalServiceCall.job_id == job_id,
        ExternalServiceCall.action == STAGE_NAME,
    )
    spent: float | None = (await session.execute(stmt)).scalar_one()
    return float(spent or 0.0)
