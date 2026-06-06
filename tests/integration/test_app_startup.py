"""Integration tests for Phase 1.2.1 — StageRouter foundation wiring.

The FastAPI lifespan in :mod:`course_supporter.api.app` instantiates
``StageRouter`` at startup via ``load_ladder_config(settings.ladders_dir)``
and ``create_providers(settings)``. These tests pin the same wiring
contract independently — assembling ``StageRouter`` via the same call
shapes the lifespan uses — so the wiring shape is regression-guarded
even when the full lifespan's other dependencies (ARQ Redis pool,
S3 bucket bootstrap) are not available in the test environment.

Acceptance gates covered (PHASE.md §2.1 step 3 + §4 gate 5):

* ``settings.ladders_dir`` resolves to a directory containing the
  registered ``safety_check`` stage (ladders_mentor.yaml).
* ``StageRouter(ladder_config=..., providers=...)`` constructor
  accepts the loaded config + providers (option-a two-build wiring
  per §6.2 ratify).
* ``get_stage_router`` FastAPI dependency reads from
  ``request.app.state.stage_router`` and returns the configured router.
"""

from __future__ import annotations

from typing import cast

import pytest
from fastapi import Request

from course_supporter.api.deps import get_stage_router
from course_supporter.config import settings
from course_supporter.llm.factory import create_providers
from course_supporter.llm.ladder_config import load_ladder_config
from course_supporter.llm.stage_router import StageRouter
from tests._helpers.registry import empty_registry


@pytest.mark.requires_db
class TestStageRouterFoundation:
    """Phase 1.2.1 — instantiate StageRouter at production startup."""

    def test_ladder_config_loads_safety_check_stage(self) -> None:
        """``load_ladder_config(settings.ladders_dir)`` yields the safety_check stage.

        Mirrors the lifespan call shape: ladder directory from Settings,
        ``safety_check`` registered in ``config/ladders_mentor.yaml`` per
        Phase 0.6 KD16 deliverable.
        """
        ladder_config = load_ladder_config(settings.ladders_dir)
        stage = ladder_config.get_stage("safety_check")
        assert stage.prompt_ref == "prompts/safety_check/v1.md"
        assert len(stage.ladder) >= 1

    def test_stage_router_instantiates_with_loaded_config(self) -> None:
        """``StageRouter`` constructor accepts ``ladder_config`` + ``providers``.

        Option-a two-build wiring per §6.2 ratify: providers built once
        for StageRouter independently of ModelRouter's internal providers.
        """
        ladder_config = load_ladder_config(settings.ladders_dir)
        providers = create_providers(settings)
        stage_router = StageRouter(
            ladder_config=ladder_config,
            providers=providers,
            registry=empty_registry(),
        )
        assert isinstance(stage_router, StageRouter)

    async def test_get_stage_router_dependency_returns_app_state_router(self) -> None:
        """``get_stage_router`` reads ``request.app.state.stage_router``.

        Mirrors the existing ``get_model_router`` resolution pattern
        (api/deps.py:90). The dependency is the integration point for
        future StageRouter consumers (homework migration in Phase 1.2.2).
        """
        ladder_config = load_ladder_config(settings.ladders_dir)
        providers = create_providers(settings)
        sentinel_router = StageRouter(
            ladder_config=ladder_config,
            providers=providers,
            registry=empty_registry(),
        )

        class _State:
            stage_router: StageRouter | None = None

        class _App:
            def __init__(self) -> None:
                self.state = _State()
                self.state.stage_router = sentinel_router

        class _Request:
            def __init__(self, app: _App) -> None:
                self.app = app

        request = cast(Request, _Request(_App()))
        resolved = await get_stage_router(request)
        assert resolved is sentinel_router
