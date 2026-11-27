"""The work that composes a task's criteria list at the author's request (DD-SP-DR).

Until now the list was composed only by the first student's submission, inside
its review. The author may ask for it earlier — a button on the criteria screen,
``POST /documents/{id}/criteria/compose`` — and this is the work that request
queues. It decides nothing about composing and nothing about money: both belong
to :class:`~course_supporter.homework.criteria_list_service.CriteriaListService`,
the very service the evaluation stage calls, so a list composed here is claimed,
paid for (``task_preparation``), recorded and failed exactly as one composed
there. What this body adds is only the road around it:

1. **Compose** — ``get_or_compose`` with the shadow estimate on, as the review
   path asks for it. A list already in force is returned as it is, without a
   model call: a student's submission may have got there first.
2. **Continue the waiting** — the same rule the author's edit follows
   (:func:`~course_supporter.homework.path_continuation.resume_awaiting_criteria`):
   students' works held for this list are put back in the queue. Usually there
   are none. Best-effort: the list is stored, and the worker's start and the
   author's edit are the other entries.

A model's refusal — funds or a ceiling — is not an error of this work: the
service records it on the list row, the author reads it from the same reading
as for a submission's refusal (``not_composed`` and its ``reason_code``), and
the job ends ``complete`` with the outcome in its result. A defect raises, and
the seam fails the job.
"""

from __future__ import annotations

import uuid
from typing import Any

import structlog

from course_supporter.homework.criteria_list_service import (
    CriteriaInForce,
    build_criteria_list_service,
)
from course_supporter.homework.path_continuation import resume_awaiting_criteria
from course_supporter.jobs.execution_seam import through_seam
from course_supporter.storage.job_repository import JobRepository

logger = structlog.get_logger(__name__)


@through_seam()
async def arq_compose_criteria(
    ctx: dict[str, Any],
    job_id: str,  # UUID as string (ARQ JSON serialization)
    authored_document_id: str,  # UUID as string
) -> dict[str, Any]:
    """Compose the criteria list of one task's current version, or record why not.

    Wrapped by the L2 execution seam: the seam owns the ``Job.status``
    lifecycle and sets the register's job and tenant, so every model call this
    body makes leaves its row and is charged to this job (KD5, BIL-E).

    Returns the dict the seam persists to ``Job.result_data``.
    """
    log = logger.bind(job_id=job_id, authored_document_id=authored_document_id)
    session_factory = ctx["session_factory"]
    document_id = uuid.UUID(authored_document_id)

    service = build_criteria_list_service(
        session_factory, ctx["stage_router"], shadow_estimate=True
    )
    outcome = await service.get_or_compose(document_id)
    if not isinstance(outcome, CriteriaInForce):
        log.info("criteria_composition.unavailable", reason=outcome.reason.value)
        return {"state": "unavailable", "reason": outcome.reason.value}

    await _continue_the_waiting(ctx, job_id=uuid.UUID(job_id), document_id=document_id)
    log.info("criteria_composition.ready", layer=outcome.layer.value)
    return {
        "state": "ready",
        "layer": outcome.layer.value,
        "criteria": len(outcome.criteria),
    }


async def _continue_the_waiting(
    ctx: dict[str, Any], *, job_id: uuid.UUID, document_id: uuid.UUID
) -> None:
    """Put back the works that wait for the list now in force; never raises.

    The same rule as the author's edit: one entry among three
    (``PRE-FLIGHT.md`` 9.2). A queue that refuses must not cost the author a
    list that is already stored.
    """
    arq = ctx.get("redis")
    if arq is None:
        return
    try:
        async with ctx["session_factory"]() as session:
            job = await JobRepository(session).get_by_id(job_id)
            if job is None or job.tenant_id is None:
                return
            await resume_awaiting_criteria(
                session,
                arq,
                tenant_id=job.tenant_id,
                authored_document_id=document_id,
            )
    except Exception:
        logger.warning(
            "criteria_composition_continuation_failed",
            authored_document_id=str(document_id),
            exc_info=True,
        )
