"""The points where actions write their shadow estimates (BIL-D).

Each function is the one line a hook site adds; each goes through
:func:`~course_supporter.cost_estimate.shadow.record_estimate`, so each is
bound by its rule: it never raises, and it writes in its own transaction.

* :func:`shadow_review` — the new review path, once the submission is taken
  onto it and before its first stage (``homework/path_runner.py``).
* :func:`shadow_processing` — a material processor, once the material's cost
  drivers are known and before its first paid step (``ingestion/*``); the
  processing's charge starts right after it (BIL-E, ``billing.funds``).
* :func:`shadow_task_preparation` — before ``criteria_decomposition`` run for
  the new path, and before ``key_explanation``.
* :func:`shadow_node_summary_regeneration` — a ``node_summary_regeneration``
  job, once its scope is known and before its first node is visited
  (``api/tasks.py``).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import TYPE_CHECKING

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from course_supporter.cost_estimate.actions import (
    LadderStage,
    MaterialFactors,
    estimate_node_summary_regeneration,
    estimate_processing,
    estimate_review,
    estimate_task_preparation,
)
from course_supporter.cost_estimate.core import ActionEstimate
from course_supporter.cost_estimate.node_summary import read_node_summary_factors
from course_supporter.cost_estimate.settings import (
    EstimateSettings,
    get_estimate_settings,
)
from course_supporter.cost_estimate.shadow import (
    load_catalog,
    load_ladder_stages,
    preparation_key,
    processing_key,
    record_estimate,
    review_key,
)
from course_supporter.service_logging import get_current_job_id, get_current_tenant_id

if TYPE_CHECKING:
    from course_supporter.models.source import SourceDocument

logger = structlog.get_logger(__name__)


async def shadow_review(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    submission_id: uuid.UUID,
    tenant_id: uuid.UUID | None,
    job_id: uuid.UUID,
    path_key: str,
    stage_names: Sequence[str],
    path_stages: Mapping[str, LadderStage],
    submission_chars: int,
) -> None:
    """Estimate one submission on the new path (key: the submission)."""

    def compute(settings: EstimateSettings, at: datetime) -> ActionEstimate:
        return estimate_review(
            stage_names=stage_names,
            path_stages=path_stages,
            settings=settings,
            catalog=load_catalog(),
            submission_chars=submission_chars,
            at=at,
            inputs={"path_key": path_key},
        )

    await record_estimate(
        compute,
        action_key=review_key(submission_id),
        tenant_id=tenant_id,
        submission_id=submission_id,
        job_id=job_id,
        session_factory=session_factory,
    )


async def shadow_processing(
    *,
    kind: str,
    factors: Callable[[], MaterialFactors],
    source: object,
) -> None:
    """Estimate the running processing job (key: the job from the context).

    ``factors`` is called inside the estimate's guard, so a site's reading of
    its material (a text length, a slide count) cannot break the job either.
    ``source`` is the material's ``AuthoredDocument``; its id is read without
    raising.
    """
    job_id = get_current_job_id()
    if job_id is None:
        logger.warning("cost_estimate_skipped", reason="no job in context", kind=kind)
        return

    def compute(settings: EstimateSettings, at: datetime) -> ActionEstimate:
        return estimate_processing(
            kind=kind,
            factors=factors(),
            ladders=load_ladder_stages(),
            settings=settings,
            catalog=load_catalog(),
            at=at,
        )

    await record_estimate(
        compute,
        action_key=processing_key(job_id),
        tenant_id=get_current_tenant_id(),
        job_id=job_id,
        authored_document_id=_document_id(source),
    )
    await _charge_processing(kind, factors, job_id, source)


async def _charge_processing(
    kind: str,
    factors: Callable[[], MaterialFactors],
    job_id: uuid.UUID,
    source: object,
) -> None:
    """The processing's threshold and reserve (BIL-E), after its estimate.

    The estimate above never raises; this may: an author without credit who
    cannot cover the price fails the job here, before its first paid step
    (:class:`~course_supporter.billing.funds.InsufficientUnitsError`). A
    no-op while charging is off. Factors that cannot be read reserve nothing.
    """
    from course_supporter.billing.funds import processing_started

    try:
        known = factors()
    except Exception:
        logger.warning("billing_processing_factors_failed", kind=kind, exc_info=True)
        return
    await processing_started(
        kind=kind,
        factors=known,
        job_id=job_id,
        tenant_id=get_current_tenant_id(),
        authored_document_id=_document_id(source),
    )


def _document_id(source: object) -> uuid.UUID | None:
    value = getattr(source, "id", None)
    return value if isinstance(value, uuid.UUID) else None


async def shadow_task_preparation(
    *,
    kind: str,
    authored_document_id: uuid.UUID,
    version: str,
    task_chars: int,
    tenant_id: uuid.UUID | None = None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> None:
    """Estimate one preparation (key: task version + kind), whoever's job runs it."""

    def compute(settings: EstimateSettings, at: datetime) -> ActionEstimate:
        return estimate_task_preparation(
            kind=kind,
            ladders=load_ladder_stages(),
            settings=settings,
            catalog=load_catalog(),
            task_chars=task_chars,
            at=at,
            inputs={"version": version},
        )

    await record_estimate(
        compute,
        action_key=preparation_key(authored_document_id, version, kind),
        tenant_id=tenant_id if tenant_id is not None else get_current_tenant_id(),
        job_id=get_current_job_id(),
        authored_document_id=authored_document_id,
        session_factory=session_factory,
    )


async def shadow_parsed_document(
    doc: SourceDocument,
    *,
    kind: str,
    source: object,
    files: int | None = None,
) -> None:
    """:func:`shadow_processing` for a material whose text is parsed for free.

    Text, web and code: ``process_raw`` reads them without a model, and the
    safety check after it is the first paid step, so the parsed document's
    sizes are the cost drivers. ``files`` is the number of code files.
    """
    await shadow_processing(
        kind=kind,
        factors=lambda: MaterialFactors(
            files=files,
            text_chars=len(doc.assemble_text()),
            safety_chars=len(doc.safety_text()),
        ),
        source=source,
    )


async def shadow_node_summary_regeneration(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    vertex_node_id: uuid.UUID,
    job_id: uuid.UUID,
) -> None:
    """Estimate a regeneration job (key: the job, as for a processing).

    The factors are read from the database in a session of their own, read-only,
    inside the same guard as the write: a failed read is logged and the job goes
    on, like a failed write.
    """
    action_key = processing_key(job_id)
    try:
        async with session_factory() as session:
            factors = await read_node_summary_factors(
                session,
                vertex_node_id,
                child_summary_chars=get_estimate_settings().processing.node_summary.child_summary_chars,
            )
    except Exception:
        logger.exception("cost_estimate_failed", action_key=action_key)
        return

    def compute(settings: EstimateSettings, at: datetime) -> ActionEstimate:
        return estimate_node_summary_regeneration(
            factors=factors,
            ladders=load_ladder_stages(),
            settings=settings,
            catalog=load_catalog(),
            at=at,
            inputs={"vertex_node_id": str(vertex_node_id)},
        )

    await record_estimate(
        compute,
        action_key=action_key,
        tenant_id=get_current_tenant_id(),
        job_id=job_id,
        session_factory=session_factory,
    )
