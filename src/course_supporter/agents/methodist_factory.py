"""Methodist factory — wires :class:`MethodistAgent` into the orchestrator.

Phase 3.2.3a is the first task that builds
:class:`NodeSummaryGenerationOrchestrator` in production scope. There
is no HTTP endpoint, no ARQ task wiring, and no background-job
enqueue yet — those land in Phase 3.2.4. This module ships the
minimum: a helper that constructs the orchestrator with the
methodist agent injected via the DI seam established in Phase 3.2.2.

Consumers in the 3.2.3a delta:

* ``tools/methodist_live_smoke.py`` — operator-gated diagnostic that
  imports :func:`build_node_summary_orchestrator` and drives Pass 1
  on a dev fixture.

Future consumers in Phase 3.2.4:

* ARQ worker task ``arq_regenerate_node_summary`` (Phase 3.2.4).
* FastAPI POST endpoint for the methodist-regenerate trigger.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from course_supporter.agents.methodist import MethodistAgent
from course_supporter.storage.node_summary_orchestrator import (
    NodeSummaryGenerationOrchestrator,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from course_supporter.llm.stage_router import StageRouter


def build_methodist_agent(
    session: AsyncSession, stage_router: StageRouter
) -> MethodistAgent:
    """Construct a :class:`MethodistAgent` bound to the caller's session.

    Sessions are shared with the orchestrator (S5 ratify): reads in
    the agent see the same view as the orchestrator's transaction;
    writes from the agent flush onto the orchestrator's session.
    """
    return MethodistAgent(session=session, stage_router=stage_router)


def build_node_summary_orchestrator(
    session: AsyncSession, stage_router: StageRouter
) -> NodeSummaryGenerationOrchestrator:
    """Construct the orchestrator with the methodist agent wired in.

    Single source of truth for the DI shape; both the Phase 3.2.3a
    live-smoke tool and the Phase 3.2.4 production endpoint will
    consume this helper rather than re-instantiating the orchestrator
    manually.
    """
    agent = build_methodist_agent(session, stage_router)
    return NodeSummaryGenerationOrchestrator(session=session, methodist=agent)
