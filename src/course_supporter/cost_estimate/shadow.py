"""Write a shadow estimate beside an action — and never let it touch the action.

Purpose:
    Every hook (the new review path, the material processors, the task
    preparations) calls one function here, :func:`record_estimate`, with a
    closure that computes the estimate. This is the single place of the rule
    of BIL-D: an estimate never changes or breaks the action it describes.

The rule, in code:
    * the computation and the write run inside one ``try``; any exception —
      a missing setting, an unknown model, a database error — is logged with
      the action's key and swallowed, and the hook goes on as before;
    * the write uses its own session from the factory, so its transaction
      neither commits nor rolls back the action's, nor goes with it;
    * ``INSERT ... ON CONFLICT DO NOTHING`` on ``(action_kind, action_key)``:
      a second estimate of the same action (a re-run job, a retried stage)
      writes nothing;
    * nothing here calls a model or the network.

Interface:
    :func:`configure` — the worker gives its session factory at startup; hooks
    that hold none (the processors) write through it.
    :func:`record_estimate` — compute and write; True when a row was written.
    :func:`review_key`, :func:`processing_key`, :func:`preparation_key` — the
    text identity of an action.
    :func:`load_catalog`, :func:`load_ladder_stages` — the catalog and ladders
    as the estimate reads them, read once per process.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from functools import lru_cache

import structlog
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from course_supporter.cost_estimate.actions import STT_ACTION, Catalog, LadderStage
from course_supporter.cost_estimate.core import (
    ActionEstimate,
    price_units,
    reserve_units,
)
from course_supporter.cost_estimate.settings import (
    EstimateSettings,
    get_estimate_settings,
)
from course_supporter.storage.orm import ActionCostEstimate

logger = structlog.get_logger(__name__)

_session_factory: async_sessionmaker[AsyncSession] | None = None


def configure(session_factory: async_sessionmaker[AsyncSession] | None) -> None:
    """Give the process's session factory to the hooks that hold none."""
    global _session_factory
    _session_factory = session_factory


def _utc_now() -> datetime:
    return datetime.now(UTC)


# ── Keys ──


def review_key(submission_id: uuid.UUID) -> str:
    """A review is one submission, with all its jobs."""
    return str(submission_id)


def processing_key(job_id: uuid.UUID) -> str:
    """A processing is one job."""
    return str(job_id)


def preparation_key(authored_document_id: uuid.UUID, version: str, kind: str) -> str:
    """A preparation is one (task version, kind of preparation) pair."""
    return f"{authored_document_id}:{version}:{kind}"


# ── What the estimate reads ──


@lru_cache(maxsize=1)
def load_catalog() -> Catalog:
    """The registry's models, STT chain and version (read once per process)."""
    from course_supporter.config import get_settings
    from course_supporter.llm.registry import load_registry

    registry = load_registry(get_settings().external_services_path)
    return Catalog(
        models=dict(registry.models),
        stt_chain=tuple(registry.get_chain(STT_ACTION)),
        version=registry.catalog_version,
    )


@lru_cache(maxsize=1)
def load_ladder_stages() -> Mapping[str, LadderStage]:
    """Every ladder stage of ``config/ladders_*.yaml`` (read once per process)."""
    from course_supporter.config import get_settings
    from course_supporter.llm.ladder_config import load_ladder_config

    return dict(load_ladder_config(get_settings().ladders_dir).stages)


# ── The write ──


def _row(estimate: ActionEstimate, settings: EstimateSettings) -> dict[str, object]:
    k = settings.units_per_usd
    return {
        "action_kind": estimate.kind.value,
        "e_usd": estimate.e_usd,
        "p_units": price_units(estimate.e_usd, margin=settings.margin, units_per_usd=k),
        "reserve_units": reserve_units(estimate.e_usd, units_per_usd=k),
        "margin": settings.margin,
        "units_per_usd": k,
        "catalog_version": estimate.catalog_version,
        "breakdown": estimate.breakdown(),
        "inputs": estimate.inputs,
        "uncalibrated": estimate.uncalibrated,
    }


async def record_estimate(
    compute: Callable[[EstimateSettings, datetime], ActionEstimate],
    *,
    action_key: str,
    tenant_id: uuid.UUID | None = None,
    submission_id: uuid.UUID | None = None,
    job_id: uuid.UUID | None = None,
    authored_document_id: uuid.UUID | None = None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    now: Callable[[], datetime] = _utc_now,
) -> bool:
    """Compute an action's estimate and write it; never raise.

    ``compute`` gets the settings and the moment of the estimate. Returns
    True when a row was written, False when none was: the action already had
    one, no session factory is known, or something failed — logged as
    ``cost_estimate_failed`` with the key, and swallowed.
    """
    log = logger.bind(action_key=action_key)
    try:
        factory = session_factory if session_factory is not None else _session_factory
        if factory is None:
            log.warning("cost_estimate_skipped", reason="no session factory")
            return False
        settings = get_estimate_settings()
        estimate = compute(settings, now())
        values = _row(estimate, settings) | {
            "action_key": action_key,
            "tenant_id": tenant_id,
            "submission_id": submission_id,
            "job_id": job_id,
            "authored_document_id": authored_document_id,
        }
        statement = (
            insert(ActionCostEstimate)
            .values(**values)
            .on_conflict_do_nothing(constraint="uq_action_cost_estimates_action")
            .returning(ActionCostEstimate.id)
        )
        async with factory() as session:
            written = (await session.execute(statement)).scalar_one_or_none()
            await session.commit()
        log.info(
            "cost_estimate_recorded",
            action_kind=estimate.kind.value,
            e_usd=str(estimate.e_usd),
            written=written is not None,
        )
        return written is not None
    except Exception:
        log.exception("cost_estimate_failed")
        return False
