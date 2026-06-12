"""Methodist live-smoke (Phase 3.2.3b, operator-gated).

This script drives the production methodist agent + orchestrator
against a dev course fixture for a SINGLE full two-pass run plus an
immediate repeated run that verifies memo-skip on both axes. It exists
as a one-shot diagnostic confirming the production wiring before the
Phase 3.2.4 endpoint lands.

History — encapsulation-break removal (Phase 3.2.3b)
====================================================

Phase 3.2.3a's predecessor of this tool called the orchestrator's
PRIVATE Pass 1 helpers in a loop because the public ``run()`` would
have triggered the deliberate no-op ``generate_topdown`` and
materialised ``enclosing_context_source_hash`` on every Raw — a
DD-3.2.2-A axis-2 hazard that would let a future real Pass 2
silently memo-skip every node. Phase 3.2.3b ships the real
``generate_topdown``, so the hazard is gone; the inline note in the
3.2.3a tool docstring promised this exact replacement. From now on
the tool calls the public ``orch.run(...)`` end-to-end.

Cost + plumbing (Phase 3.2.3b)
==============================

* ContextVar plumbing — Phase 3.2.3a observed that smoke-run ESC
  rows were skipped because the tool did not set ``_current_job_id``
  / ``_current_tenant_id`` before invoking the StageRouter. Fixed
  here: ``set_job_from_arq`` + ``set_tenant_from_job`` wrap the
  orchestrator call so every LLM attempt persists ESC.
* Cost envelope at spike rates (per SPIKE-3-2-3-results.md):
  python_start 4 nodes — Pass 1 ~$0.09 (deepseek-v4-pro-thinking
  bottomup median $0.022) + Pass 2 ~$0.04 (deepseek-v4-pro-thinking
  topdown median $0.013, run on 3 non-root nodes) = ~$0.13 worst
  case per smoke session.

Usage
=====

By default this script runs in ``--dry-run`` mode: it loads the
fixture, validates scope, prints what the run WOULD do, and exits
without touching the LLM or the DB beyond reads. Live execution
requires the explicit ``--live`` flag, which the operator passes
on the command line after merge review.

    # Dry-run (default — safe to run any time):
    uv run python tools/methodist_live_smoke.py --course python_start

    # Live (operator-gated; spends LLM tokens and writes DB rows):
    uv run python tools/methodist_live_smoke.py --course python_start --live

DB state contract (live mode)
=============================

* Pre: ``node_summaries_raw`` must be 0 rows (DD-3.2.2-A invariant
  closed by TRUNCATE 2026-06-12; restored before every smoke).
* After the FIRST run:
  - ``node_summaries_raw`` has exactly ``len(in_scope)`` rows.
  - Every row has ``source_content_hash`` written.
  - 3 non-root rows have non-NULL ``enclosing_context`` (Ukrainian
    framing for the python_start fixture per Phase 2.4.14).
  - 3 non-root rows have non-NULL ``enclosing_context_source_hash``.
  - The root row has ``enclosing_context = NULL`` (Pass 2 skips the
    course root with NOT_APPLICABLE) and
    ``enclosing_context_source_hash = NULL`` (set by 3.2.1 service
    only when Pass 2 actually fires).
* After the SECOND immediate run:
  - All ``pass1`` statuses = ``SKIPPED_MEMO`` (axis 1 fresh).
  - All non-root ``pass2`` statuses = ``SKIPPED_MEMO`` (axis 2
    fresh); root stays ``NOT_APPLICABLE``.
  - **Zero** ``ExternalServiceCall`` rows attributed to the second
    job (``count(ESC where job_id == job_id_second) == 0``). Per
    KD5 the ``job_id`` FK is the only mandatory scope on ESC; the
    second run's LLM-call count is therefore an exact integer
    rather than a delta against the first-run count (probe P5
    structural memo-skip guarantee).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
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
from course_supporter.service_logging import (
    set_job_from_arq,
    set_tenant_from_job,
)
from course_supporter.storage.node_summary_run_state import (
    NodeSummaryNodeStatus,
    NodeSummaryRunState,
)
from course_supporter.storage.orm import (
    CourseNode,
    ExternalServiceCall,
    Job,
    NodeSummaryRaw,
)

# Fixture map (ratified for 3.2.3a/b live-smoke).
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


async def _run_full_pipeline(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    stage_router: StageRouter,
    vertex_node_id: uuid.UUID,
    job_id: uuid.UUID,
) -> None:
    """Drive a full Pass 1 + Pass 2 run via the public orchestrator API.

    ContextVar plumbing closes the 3.2.3a ESC-skip observation: the
    StageRouter persists ``ExternalServiceCall`` rows only when
    ``_current_job_id`` and ``_current_tenant_id`` are set on the
    calling task's contextvars (per ``service_logging._persist``).
    """
    set_job_from_arq(job_id)
    await set_tenant_from_job(session_factory, job_id)

    async with session_factory() as session:
        orch = build_node_summary_orchestrator(session, stage_router)
        await orch.run(
            job_id=job_id,
            vertex_node_id=vertex_node_id,
            force=False,
        )


async def _verify_first_run(
    session: AsyncSession, vertex_node_id: uuid.UUID
) -> dict[str, Any]:
    """Read back Pass 1 + Pass 2 evidence for the operator after run 1."""
    vertex_row = await session.get(CourseNode, vertex_node_id)
    if vertex_row is None:
        msg = f"vertex CourseNode {vertex_node_id} vanished post-run"
        raise RuntimeError(msg)
    raws = (
        await session.execute(
            select(NodeSummaryRaw, CourseNode.parent_id)
            .join(CourseNode, CourseNode.id == NodeSummaryRaw.course_node_id)
            .where(CourseNode.tenant_id == vertex_row.tenant_id)
            .where(NodeSummaryRaw.deleted_at.is_(None))
        )
    ).all()
    non_root = [(r, pid) for r, pid in raws if pid is not None]
    root = [(r, pid) for r, pid in raws if pid is None]
    return {
        "raw_count": len(raws),
        "non_root_count": len(non_root),
        "root_count": len(root),
        "with_source_content_hash": sum(
            1 for r, _ in raws if r.source_content_hash is not None
        ),
        "non_root_with_enclosing_context": sum(
            1 for r, _ in non_root if r.enclosing_context is not None
        ),
        "non_root_with_enclosing_context_source_hash": sum(
            1 for r, _ in non_root if r.enclosing_context_source_hash is not None
        ),
        "root_enclosing_context_is_null": all(
            r.enclosing_context is None for r, _ in root
        ),
        "root_enclosing_context_source_hash_is_null": all(
            r.enclosing_context_source_hash is None for r, _ in root
        ),
    }


async def _read_run_state(
    session: AsyncSession, job_id: uuid.UUID
) -> NodeSummaryRunState | None:
    """Decode the orchestrator's run-state JSON off Job.stage_progress."""
    job = await session.get(Job, job_id)
    if job is None or job.stage_progress is None:
        return None
    return NodeSummaryRunState.model_validate(job.stage_progress)


async def _count_esc_rows_for_job(session: AsyncSession, job_id: uuid.UUID) -> int:
    """Count ExternalServiceCall rows attributed to a single Job.

    Per KD5 (``storage/orm.py`` ``ExternalServiceCall``), the table's
    only mandatory FK is ``job_id``; tenant_id and course_node_id
    resolve through the Job and its input_params and are NOT columns
    on ESC. The memo-skip evidence after the second run is therefore
    expressed as ``count(ESC where job_id == job_id_second)`` — exactly
    the rows the second run was responsible for, which MUST be zero
    when every visit short-circuited via SKIPPED_MEMO / NOT_APPLICABLE
    BEFORE invoking the LLM hook.
    """
    result = await session.execute(
        select(func.count(ExternalServiceCall.id)).where(
            ExternalServiceCall.job_id == job_id
        )
    )
    return int(result.scalar_one())


async def _verify_memo_skip(
    *,
    session: AsyncSession,
    job_id_second: uuid.UUID,
) -> dict[str, Any]:
    """After the second run: pin SKIPPED_MEMO statuses + zero ESC rows.

    The memo-skip check no longer needs an esc_before / esc_after
    delta. Because ESC rows carry ``job_id`` directly, the second run's
    LLM-call count is simply the count of ESC rows belonging to the
    second job — zero when the structural memo-skip held.
    """
    run_state = await _read_run_state(session, job_id_second)
    if run_state is None:
        msg = "run-state missing after second run — orchestrator did not persist"
        raise RuntimeError(msg)
    pass1_statuses = list(run_state.pass1.values())
    pass2_statuses = list(run_state.pass2.values())
    esc_from_second = await _count_esc_rows_for_job(session, job_id_second)
    return {
        "pass1_all_skipped_memo": all(
            s == NodeSummaryNodeStatus.SKIPPED_MEMO for s in pass1_statuses
        ),
        "pass1_status_counts": _count_statuses(pass1_statuses),
        "pass2_non_root_all_skipped_memo": all(
            s == NodeSummaryNodeStatus.SKIPPED_MEMO
            for s in pass2_statuses
            if s != NodeSummaryNodeStatus.NOT_APPLICABLE
        ),
        "pass2_status_counts": _count_statuses(pass2_statuses),
        "esc_rows_from_second_run": esc_from_second,
    }


def _count_statuses(
    statuses: list[NodeSummaryNodeStatus],
) -> dict[str, int]:
    out: dict[str, int] = {}
    for s in statuses:
        out[s.value] = out.get(s.value, 0) + 1
    return out


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

    # ── First run: full two-pass against a clean fixture ──────────
    async with session_factory() as session:
        job_id_first = await _create_smoke_job(session, fixture_root)
    print(f"[live-smoke] run 1: created job_id={job_id_first}")

    await _run_full_pipeline(
        session_factory=session_factory,
        stage_router=stage_router,
        vertex_node_id=fixture_root,
        job_id=job_id_first,
    )

    async with session_factory() as session:
        first = await _verify_first_run(session, fixture_root)
        esc_from_first = await _count_esc_rows_for_job(session, job_id_first)
    print(f"[live-smoke] run 1 evidence: {first}")
    print(f"[live-smoke] run 1 ESC rows: {esc_from_first}")

    first_ok = (
        first["with_source_content_hash"] == first["raw_count"]
        and first["non_root_with_enclosing_context"] == first["non_root_count"]
        and first["non_root_with_enclosing_context_source_hash"]
        == first["non_root_count"]
        and first["root_enclosing_context_is_null"]
        and first["root_enclosing_context_source_hash_is_null"]
    )
    if not first_ok:
        print(
            "[live-smoke] FAIL — first run evidence missed an invariant.",
            file=sys.stderr,
        )
        await engine.dispose()
        return 3

    # ── Second run: memo-skip on both axes, zero new LLM calls ────
    async with session_factory() as session:
        job_id_second = await _create_smoke_job(session, fixture_root)
    print(f"[live-smoke] run 2: created job_id={job_id_second}")

    await _run_full_pipeline(
        session_factory=session_factory,
        stage_router=stage_router,
        vertex_node_id=fixture_root,
        job_id=job_id_second,
    )

    async with session_factory() as session:
        memo = await _verify_memo_skip(
            session=session,
            job_id_second=job_id_second,
        )
    print(f"[live-smoke] run 2 memo-skip evidence: {memo}")

    memo_ok = (
        memo["pass1_all_skipped_memo"]
        and memo["pass2_non_root_all_skipped_memo"]
        and memo["esc_rows_from_second_run"] == 0
    )
    if not memo_ok:
        print(
            "[live-smoke] FAIL — second run did NOT achieve full memo-skip "
            "or wrote new ESC rows (memoization regression).",
            file=sys.stderr,
        )
        await engine.dispose()
        return 4

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
            "Operator-gated live run: spends LLM tokens, writes Pass 1 "
            "canonical + Pass 2 enclosing_context fields on NodeSummaryRaw, "
            "then immediately re-runs to verify memo-skip on both axes. "
            "Use after task 3.2.3b is merge-ready."
        ),
    )
    return p


if __name__ == "__main__":
    sys.exit(asyncio.run(main(_build_arg_parser().parse_args())))
