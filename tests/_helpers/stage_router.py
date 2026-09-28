"""The StageRouter the API builds, for the RUN_SMOKE tests that drive a real one.

Mirrors the steps in ``course_supporter.api.app.lifespan`` — the registry,
the ladders, the ladder checks the boot runs, then the same
:func:`~course_supporter.llm.factory.create_stage_router` with the shared
session factory — so a smoke test exercises the router production serves
rather than a lookalike. The boot checks that are not about the router
(submission paths, phrasebook, native language names) stay with the boot.

Importing this module builds no router and calls no provider: that happens
only when :func:`build_stage_router` is called, inside a test the RUN_SMOKE
gate let through.
"""

from __future__ import annotations

from course_supporter.config import settings
from course_supporter.llm.factory import create_stage_router
from course_supporter.llm.ladder_config import (
    load_ladder_config,
    validate_ladder_prompts,
    validate_ladders_against_registry,
)
from course_supporter.llm.registry import load_registry
from course_supporter.llm.stage_router import StageRouter
from course_supporter.storage.database import async_session


def build_stage_router() -> StageRouter:
    """Build the router exactly as the API lifespan does."""
    registry = load_registry(settings.external_services_path)
    ladder_config = load_ladder_config(settings.ladders_dir)
    validate_ladders_against_registry(ladder_config, registry)
    validate_ladder_prompts(ladder_config)
    return create_stage_router(
        settings,
        ladder_config=ladder_config,
        registry=registry,
        session_factory=async_session,
    )
