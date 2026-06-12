"""Methodist Pass 1 live-smoke (Phase 3.2.3a, operator-gated).

This script drives the production methodist agent + orchestrator
helpers against a dev course fixture for a SINGLE Pass 1 prowalk —
nothing else. It exists as a one-shot diagnostic confirming the
production wiring before the Phase 3.2.4 endpoint lands.

WARNING — DELIBERATE ENCAPSULATION BREAK
========================================

This tool calls private methods on
:class:`NodeSummaryGenerationOrchestrator`
(``validate_scope`` is public; ``_collect_course_nodes``,
``_compute_leaf_to_root_order`` and ``_visit_pass1`` are private).
Reasoning: the orchestrator's public ``run()`` runs Pass 1 + Pass 2
back-to-back with no opt-out — calling it from this tool would let
the no-op :meth:`MethodistAgent.generate_topdown` materialise
``enclosing_context_source_hash`` on every visited Raw, breaking
the DD-3.2.2-A discipline at the axis-2 boundary (task 3.2.3a §
Архітектурні інваріанти процесне правило). Pass 1 alone is what
3.2.3a needs to validate; private-method invocation is the cleanest
way to honor that constraint without touching the orchestrator's
public surface.

The encapsulation break is **deliberate** and **scoped to this
tool**. Replace with a proper public API in Phase 3.2.4 when the
production endpoint materialises — the endpoint can either:

* expose a ``pass_filter`` parameter on ``run()`` (orchestrator
  refactor — Phase 3.2.4 ratify),
* or call a public ``run_pass1_only()`` method added at that time.

Usage
=====

By default this script runs in ``--dry-run`` mode: it loads the
fixture, validates scope, prints what Pass 1 WOULD do, and exits
without touching the LLM or the DB beyond reads. Live execution
requires the explicit ``--live`` flag, which the operator passes
on the command line after merge review.

    # Dry-run (default — safe to run any time):
    uv run python tools/methodist_live_smoke.py --course python_start

    # Live (operator-gated; spends LLM tokens and writes DB rows):
    uv run python tools/methodist_live_smoke.py --course python_start --live

Cost envelope at spike rates (per SPIKE-3-2-3-results.md): python_start
4 nodes x deepseek-v4-pro thinking ≈ $0.09 worst case.

DB state contract (live mode):
* Pre: ``node_summaries_raw`` must be 0 rows (DD-3.2.2-A invariant).
* Post: ``node_summaries_raw`` has exactly len(in_scope) rows; every
  row has ``source_content_hash`` written by the orchestrator
  (canonical-fields-via-agent + source-hash-via-orchestrator
  contract). ``enclosing_context`` and
  ``enclosing_context_source_hash`` MUST be NULL on every row
  (Pass 2 never ran — verify with psql after the script returns).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# Tool runs from the backend repo root; ``src/`` is on the path via
# the standard uv-managed environment.
from course_supporter.agents.methodist_factory import (
    build_node_summary_orchestrator,
)
from course_supporter.config import get_settings
from course_supporter.jobs import JobType
from course_supporter.llm import create_model_router
from course_supporter.llm.factory import create_providers
from course_supporter.llm.ladder_config import (
    load_ladder_config,
    validate_ladders_against_registry,
)
from course_supporter.llm.registry import load_registry
from course_supporter.llm.stage_router import StageRouter
from course_supporter.storage.node_summary_run_state import (
    NodeSummaryRunState,
)
from course_supporter.storage.orm import CourseNode, Job, NodeSummaryRaw

# Fixture map (ratified for 3.2.3a live-smoke). Other roots from the
# dev DB are commented out for clarity; uncomment to extend.
FIXTURES: dict[str, uuid.UUID] = {
    "python_start": uuid.UUID("019e5a58-5e31-7d21-9345-b9e8950c0831"),
    # "making_science": uuid.UUID("019e9210-27d2-7bc3-8dc4-b722920bb46e"),
    # "vvedennia": uuid.UUID("019e9ca5-f592-7852-8ac2-39535e5fe978"),
}


def _load_env() -> None:
    """Load backend ``.env`` into ``os.environ`` (idempotent)."""
    env_path = Path(__file__).resolve().parents[1] / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if key not in os.environ:
            os.environ[key] = value


async def _build_stage_router(
    session_factory: async_sessionmaker[AsyncSession],
) -> StageRouter:
    """Mirror the production startup wiring from ``worker.py``."""
    s = get_settings()
    registry = load_registry(s.external_services_path)
    # We don't actually use the ModelRouter in this tool — it's
    # constructed as a side effect of mirror-wiring; this tool is
    # one-shot, so spinning up the providers cheaply is fine.
    _ = create_model_router(s, session_factory, registry=registry)
    ladder_config = load_ladder_config(s.ladders_dir)
    validate_ladders_against_registry(ladder_config, registry)
    providers = create_providers(s)
    return StageRouter(
        ladder_config=ladder_config,
        providers=providers,
        registry=registry,
        session_factory=session_factory,
    )


async def _ensure_pre_state_clean(session: AsyncSession) -> None:
    raw_count = await session.scalar(select(NodeSummaryRaw.id).limit(1))
    if raw_count is not None:
        msg = (
            "pre-state check failed: node_summaries_raw is NOT empty. "
            "DD-3.2.2-A invariant requires 0 rows before live-smoke. "
            "Inspect the DB and clear or rerun this tool on a clean dev DB."
        )
        raise RuntimeError(msg)


async def _create_smoke_job(
    session: AsyncSession, vertex_node_id: uuid.UUID
) -> uuid.UUID:
    """Create a node_summary_regeneration Job for this run."""
    vertex = await session.get(CourseNode, vertex_node_id)
    if vertex is None:
        msg = f"vertex CourseNode {vertex_node_id} not found"
        raise RuntimeError(msg)
    job = Job(
        tenant_id=vertex.tenant_id,
        course_node_id=vertex_node_id,
        job_type=JobType.NODE_SUMMARY_REGENERATION,
        status="active",
        input_params={
            "vertex_node_id": str(vertex_node_id),
            "force": False,
            "tool": "methodist_live_smoke",
        },
    )
    session.add(job)
    await session.flush()
    await session.commit()
    return job.id


async def _run_pass1_only(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    stage_router: StageRouter,
    vertex_node_id: uuid.UUID,
    job_id: uuid.UUID,
) -> None:
    """Drive Pass 1 by inlining the orchestrator's bottom-up leg.

    DELIBERATE ENCAPSULATION BREAK (see module docstring). The
    sequence below mirrors :meth:`NodeSummaryGenerationOrchestrator.run`
    lines that drive Pass 1, but stops BEFORE
    ``Job.current_stage='topdown'`` so axis-2 source-hash is never
    materialised on these rows.
    """
    # Step 1: validate_scope + seed pass1 PENDING using the public
    # validate_scope, then manually persist via the orchestrator's
    # private helper. We construct one orchestrator and drive it.
    async with session_factory() as session:
        orch = build_node_summary_orchestrator(session, stage_router)
        from course_supporter.storage.job_repository import JobRepository
        from course_supporter.storage.node_summary_run_state import (
            NodeSummaryNodeStatus,
        )

        # Mirror run() lines 343-370 (Pass 1 leg only).
        vertex = await session.get(CourseNode, vertex_node_id)
        if vertex is None or vertex.deleted_at is not None:
            msg = f"vertex CourseNode {vertex_node_id} not found or soft-deleted"
            raise RuntimeError(msg)

        preserved_errors = await orch._read_back_preserved_errors(job_id)
        job_repo = JobRepository(session)
        await job_repo.update_stage(job_id, "bottomup")
        scope = await orch.validate_scope(vertex_node_id, force=False)
        run_state = NodeSummaryRunState(
            vertex_node_id=vertex_node_id,
            force=False,
            scope=scope,
            pass1={
                nid: NodeSummaryNodeStatus.PENDING for nid in scope.in_scope_node_ids
            },
            errors=preserved_errors,
        )
        await orch._persist_run_state(job_id, run_state)
        await session.commit()

        course_nodes = await orch._collect_course_nodes(vertex)
        in_scope_set = set(scope.in_scope_node_ids)
        pass1_order = orch._compute_leaf_to_root_order(
            vertex_node_id, in_scope_set, course_nodes
        )
        print(
            f"[live-smoke] Pass 1 walk order ({len(pass1_order)} nodes): "
            + " → ".join(course_nodes[nid].title[:30] for nid in pass1_order)
        )
        for nid in pass1_order:
            # K3 ratify: strictly sequential.
            print(
                f"[live-smoke]   visiting node {course_nodes[nid].title!r} (id={nid})"
            )
            await orch._visit_pass1(course_nodes[nid], run_state, job_id)
        print(
            "[live-smoke] Pass 1 complete; NOT triggering Pass 2 leg per "
            "task 3.2.3a §Архітектурні інваріанти процесне правило."
        )


async def _verify_post_state(
    session: AsyncSession, vertex_node_id: uuid.UUID
) -> dict[str, Any]:
    """Read back Pass 1 evidence + axis-2 invariant for the operator."""
    vertex_row = await session.get(CourseNode, vertex_node_id)
    if vertex_row is None:
        msg = f"vertex CourseNode {vertex_node_id} vanished post-run"
        raise RuntimeError(msg)
    raws = (
        (
            await session.execute(
                select(NodeSummaryRaw)
                .join(CourseNode, CourseNode.id == NodeSummaryRaw.course_node_id)
                .where(CourseNode.tenant_id == vertex_row.tenant_id)
                .where(NodeSummaryRaw.deleted_at.is_(None))
            )
        )
        .scalars()
        .all()
    )
    return {
        "raw_count": len(raws),
        "with_source_content_hash": sum(
            1 for r in raws if r.source_content_hash is not None
        ),
        "axis_2_invariant_holds": all(
            r.enclosing_context is None and r.enclosing_context_source_hash is None
            for r in raws
        ),
        "axis_2_violators": [
            str(r.course_node_id)
            for r in raws
            if r.enclosing_context_source_hash is not None
            or r.enclosing_context is not None
        ],
    }


async def main(args: argparse.Namespace) -> int:
    _load_env()
    s = get_settings()
    engine = create_async_engine(s.database_url, pool_size=2)
    session_factory = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )

    fixture_root = FIXTURES.get(args.course)
    if fixture_root is None:
        print(f"unknown fixture: {args.course!r}", file=sys.stderr)
        return 2

    print(f"[live-smoke] fixture: {args.course} (root {fixture_root})")

    async with session_factory() as session:
        await _ensure_pre_state_clean(session)
    print("[live-smoke] pre-state OK: node_summaries_raw is empty")

    if args.dry_run:
        async with session_factory() as session:
            orch = build_node_summary_orchestrator(
                session, await _build_stage_router(session_factory)
            )
            scope = await orch.validate_scope(fixture_root, force=False)
            print(
                f"[live-smoke] dry-run: scope has "
                f"{len(scope.in_scope_node_ids)} in-scope nodes and "
                f"{len(scope.uncovered_stale_node_ids)} uncovered-stale outside; "
                "no LLM calls, no DB writes."
            )
        await engine.dispose()
        return 0

    print("[live-smoke] LIVE MODE — spending tokens and writing DB rows.")
    stage_router = await _build_stage_router(session_factory)

    async with session_factory() as session:
        job_id = await _create_smoke_job(session, fixture_root)
    print(f"[live-smoke] created job_id={job_id}")

    await _run_pass1_only(
        session_factory=session_factory,
        stage_router=stage_router,
        vertex_node_id=fixture_root,
        job_id=job_id,
    )

    async with session_factory() as session:
        post = await _verify_post_state(session, fixture_root)
    print(f"[live-smoke] post-state: {post}")
    if not post["axis_2_invariant_holds"]:
        print(
            f"[live-smoke] FAIL — axis-2 invariant violated on "
            f"{post['axis_2_violators']}. Investigate before merging 3.2.3b.",
            file=sys.stderr,
        )
        return 3

    await engine.dispose()
    print("[live-smoke] OK")
    return 0


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument(
        "--course",
        default="python_start",
        choices=list(FIXTURES.keys()),
        help="dev DB fixture root key (default: python_start)",
    )
    g = p.add_mutually_exclusive_group()
    g.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="DEFAULT: validate scope, print plan, no LLM and no DB writes.",
    )
    g.add_argument(
        "--live",
        dest="dry_run",
        action="store_false",
        help=(
            "Operator-gated live run: spends LLM tokens, writes "
            "Pass 1 canonical fields on NodeSummaryRaw. Use after "
            "task 3.2.3a is merge-ready."
        ),
    )
    return p


if __name__ == "__main__":
    sys.exit(asyncio.run(main(_build_arg_parser().parse_args())))
