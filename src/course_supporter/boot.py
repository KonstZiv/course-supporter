"""The startup every process shares: configuration loaded, checked, routed.

The API (``api/app.py::lifespan``) and the ARQ workers (``worker.py::startup``,
run by ``WorkerSettings`` and ``HomeworkWorkerSettings``) are separate processes
built from one code base. Each loads the model registry and the stage ladders,
refuses a broken configuration, and builds its StageRouter here, so a check
added or changed in this one place reaches every process at once.

What stays with each process -- logging setup, its database engine, its Redis
pool, S3, routes and middleware, worker functions -- is not configuration and
does not belong here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from course_supporter.homework.path_config import get_path_config, validate_path_config
from course_supporter.homework.path_stages import validate_stage_executors
from course_supporter.language import get_language_registry, validate_native_names
from course_supporter.llm.factory import create_stage_router
from course_supporter.llm.ladder_config import (
    load_ladder_config,
    validate_ladder_prompts,
    validate_ladders_against_registry,
)
from course_supporter.llm.registry import load_registry
from course_supporter.phrasebook import validate_phrasebook

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from course_supporter.config import Settings
    from course_supporter.llm.stage_router import StageRouter


def build_checked_stage_router(
    settings: Settings,
    *,
    session_factory: async_sessionmaker[AsyncSession],
) -> StageRouter:
    """Load registry + ladders, run the six boot checks, build the router.

    Any check that fails raises, and the raise stops the calling process
    before it serves a request or takes a job. ``session_factory`` is the
    caller's own: every attempt row is written through the process that made
    the call.
    """
    # Load the model registry once for the StageRouter: it consumes it for
    # ESC cost_usd + max_tokens fallback on unpinned ladder rungs.
    registry = load_registry(settings.external_services_path)

    ladder_config = load_ladder_config(settings.ladders_dir)
    # Fail-fast on a misconfigured ladder: unknown model, missing capability
    # or context window, untranslatable reasoning form, or a rung without a
    # named price (TASK-2.4.23 — DD-2.4-K + DD-2.4-Q-axis1; P6; mentor-rebuild 01).
    validate_ladders_against_registry(ladder_config, registry)
    # The rebuilt Mentor's submission paths are checked beside the ladders: a
    # hole or an inadmissible rung stops the boot instead of waiting for the
    # first submission routed through them (mentor-rebuild 02).
    path_config = get_path_config(settings.submission_paths_config_path)
    validate_path_config(
        path_config,
        registry,
        ladder_stage_names=ladder_config.stages.keys(),
    )
    # A described stage nobody can run is the same kind of hole as a missing
    # field, and stops the boot for the same reason (mentor-rebuild task 03).
    validate_stage_executors(path_config.stages)
    # Task 04's three checks, in the same place and for the same reason as the
    # two above: a hole in configuration stops the boot instead of surfacing in
    # a student's review. Every ladder prompt is read here (``DD-SP-AP``, half
    # two — the half for path stages already runs inside validate_path_config);
    # the phrasebook must carry every allowed language and every key of the
    # source, both sets checked both ways; the native-names file must cover the
    # same list. Reading the phrasebook at boot is also what lets the assembler
    # never open a file at review time.
    validate_ladder_prompts(ladder_config)
    allowed_languages = get_language_registry().languages
    validate_phrasebook(settings.phrasebook_dir, allowed_languages)
    validate_native_names(allowed_languages, settings.language_names_path)
    return create_stage_router(
        settings,
        ladder_config=ladder_config,
        registry=registry,
        session_factory=session_factory,
    )
