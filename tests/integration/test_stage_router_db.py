"""DB-backed integration tests for :class:`StageRouter` (KD16, commit (f)).

Verifies the end-to-end contract that 0.5 acceptance criterion #5
mandates: every ladder attempt (success or failure) writes an
ExternalServiceCall row through the real
:func:`service_logging._persist` path, and the rows survive the
router raising :class:`LadderExhaustedError`.

The provider layer is mocked (no external SDK calls in CI). Everything
below the provider boundary -- DB, session_factory, ContextVars,
``_persist`` -- is real.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import AsyncMock

import anthropic
import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from course_supporter.llm.error_categories import (
    ErrorCategory,
    LadderExhaustedError,
)
from course_supporter.llm.ladder_config import (
    LadderConfig,
    LadderEntry,
    StageConfig,
)
from course_supporter.llm.prompt_loader_md import StagePrompt
from course_supporter.llm.providers.base import LLMProvider
from course_supporter.llm.schemas import LLMResponse
from course_supporter.llm.stage_router import StageRouter
from course_supporter.service_logging import job_scope, tenant_scope
from course_supporter.storage.orm import ExternalServiceCall, Job
from tests._helpers.registry import empty_registry, registry_with

pytestmark = pytest.mark.requires_db


# ── Per-test Job fixture (depends on committed_seeds from conftest) ──


@pytest.fixture()
async def committed_job(
    session_factory: async_sessionmaker[AsyncSession],
    committed_seeds: dict[str, uuid.UUID],
) -> AsyncGenerator[dict[str, uuid.UUID]]:
    """Create a real Job row; clean up ESCs + Job after the test.

    Cleanup deletes ESC rows BEFORE the Job because
    ``external_service_calls.job_id`` has ``ondelete=NO ACTION``.
    """
    async with session_factory() as session:
        job = Job(
            tenant_id=committed_seeds["tenant_id"],
            course_node_id=committed_seeds["course_node_id"],
            job_type="document_processing",
        )
        session.add(job)
        await session.flush()
        await session.commit()
        job_id = job.id

    yield {
        "job_id": job_id,
        "tenant_id": committed_seeds["tenant_id"],
        "course_node_id": committed_seeds["course_node_id"],
    }

    async with session_factory() as session:
        await session.execute(
            ExternalServiceCall.__table__.delete().where(
                ExternalServiceCall.job_id == job_id
            )
        )
        await session.execute(Job.__table__.delete().where(Job.id == job_id))
        await session.commit()


# ── Helpers ────────────────────────────────────────────────────────


def _ladder(*entries: tuple[str, str]) -> list[LadderEntry]:
    return [LadderEntry(provider=p, model=m) for p, m in entries]


def _config(
    stage_name: str,
    *,
    entries: tuple[tuple[str, str], ...],
) -> LadderConfig:
    return LadderConfig(
        stages={
            stage_name: StageConfig(
                prompt_ref="prompts/example/v1.md",
                ladder=_ladder(*entries),
            ),
        }
    )


def _ok_response(
    content: str = "answer", tokens_reasoning: int | None = None
) -> LLMResponse:
    return LLMResponse(
        content=content,
        provider="x",
        model_id="x",
        tokens_in=12,
        tokens_out=34,
        tokens_reasoning=tokens_reasoning,
        latency_ms=42,
        cost_usd=0.0123,
    )


def _provider_with(
    *,
    side_effects: list[Any],
    classify_as: ErrorCategory = ErrorCategory.SEMANTIC,
) -> LLMProvider:
    p = AsyncMock(spec=LLMProvider)
    p.enabled = True
    p.complete = AsyncMock(side_effect=side_effects)
    # classify_error is sync on real providers; lambda matches that
    # contract and avoids Mock-as-ErrorCategory leaks.
    p.classify_error = lambda _exc, _cat=classify_as: _cat
    return p  # type: ignore[return-value]


def _anthropic_rate_limit() -> Exception:
    return anthropic.RateLimitError(
        "rate limited",
        response=httpx.Response(
            429, request=httpx.Request("POST", "https://api.anthropic.com")
        ),
        body=None,
    )


def _patch_load_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "course_supporter.llm.stage_router.load_prompt",
        lambda prompt_ref, *, base_path=None: StagePrompt(
            system="sys", user="user prompt body"
        ),
    )


def _patch_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip real backoff sleeps to keep the integration suite snappy."""

    async def _no_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr("asyncio.sleep", _no_sleep)


async def _fetch_escs(
    session_factory: async_sessionmaker[AsyncSession],
    job_id: uuid.UUID,
) -> list[ExternalServiceCall]:
    async with session_factory() as session:
        result = await session.execute(
            select(ExternalServiceCall)
            .where(ExternalServiceCall.job_id == job_id)
            .order_by(ExternalServiceCall.created_at)
        )
        return list(result.scalars())


# ── Tests ──────────────────────────────────────────────────────────


class TestStageRouterDB:
    async def test_two_attempt_success(
        self,
        monkeypatch: pytest.MonkeyPatch,
        session_factory: async_sessionmaker[AsyncSession],
        committed_job: dict[str, uuid.UUID],
    ) -> None:
        """INFRASTRUCTURE retry: first attempt fails, second succeeds.

        Acceptance #5 contract:
        * Two ESC rows persisted.
        * First: ``success=False`` with ``error_message``.
        * Second: ``success=True``, no error_message.
        * Both: ``strategy="default"`` (KD16).
        * ``StageResult.attempt_count == 2``.
        """
        _patch_load_prompt(monkeypatch)
        _patch_sleep(monkeypatch)

        provider = _provider_with(
            side_effects=[_anthropic_rate_limit(), _ok_response("recovered")],
            classify_as=ErrorCategory.INFRASTRUCTURE,
        )

        config = _config("demo_stage", entries=(("anthropic", "claude-x"),))
        router = StageRouter(
            config,
            {"anthropic": provider},
            session_factory=session_factory,
            # TASK-2.4.22: F(a) computes cost_usd from registry pricing
            # (providers no longer carry it). The mocked response has
            # tokens_in=12, tokens_out=34; with the rates below the
            # expected cost is 12*0.1/1000 + 34*0.2/1000 = 0.008.
            registry=registry_with(
                model_id="claude-x",
                cost_per_1k_in=0.1,
                cost_per_1k_out=0.2,
            ),
        )

        with (
            tenant_scope(committed_job["tenant_id"]),
            job_scope(committed_job["job_id"]),
        ):
            result = await router.execute_for_stage("demo_stage")

        assert result.content == "recovered"
        assert result.provider_used == "anthropic"
        assert result.model_used == "claude-x"
        assert result.attempt_count == 2

        escs = await _fetch_escs(session_factory, committed_job["job_id"])
        assert len(escs) == 2

        first, second = escs
        assert first.action == "demo_stage"
        assert first.strategy == "default"
        assert first.provider == "anthropic"
        assert first.model_id == "claude-x"
        assert first.success is False
        assert first.error_message is not None
        assert "rate limited" in first.error_message

        assert second.action == "demo_stage"
        assert second.strategy == "default"
        assert second.success is True
        assert second.error_message is None
        # Successful call carries token / cost telemetry. Cost is computed
        # from the registry pricing above (F(a)), not from the response.
        assert second.unit_in == 12
        assert second.unit_out == 34
        assert second.cost_usd == pytest.approx(0.008)
        # The mocked response reports no reasoning tokens → new column is NULL.
        assert second.unit_out_reasoning is None

    async def test_reasoning_tokens_persisted_via_live_path(
        self,
        monkeypatch: pytest.MonkeyPatch,
        session_factory: async_sessionmaker[AsyncSession],
        committed_job: dict[str, uuid.UUID],
    ) -> None:
        """C: ``LLMResponse.tokens_reasoning`` is written to
        ``external_service_calls.unit_out_reasoning`` through the real
        ``_persist`` accounting path (no direct column write)."""
        _patch_load_prompt(monkeypatch)

        provider = _provider_with(side_effects=[_ok_response(tokens_reasoning=1160)])
        config = _config("demo_stage", entries=(("anthropic", "claude-x"),))
        router = StageRouter(
            config,
            {"anthropic": provider},
            session_factory=session_factory,
            registry=registry_with(
                model_id="claude-x", cost_per_1k_in=0.1, cost_per_1k_out=0.2
            ),
        )

        with (
            tenant_scope(committed_job["tenant_id"]),
            job_scope(committed_job["job_id"]),
        ):
            await router.execute_for_stage("demo_stage")

        escs = await _fetch_escs(session_factory, committed_job["job_id"])
        assert len(escs) == 1
        assert escs[0].unit_out == 34
        assert escs[0].unit_out_reasoning == 1160

    async def test_full_ladder_exhaustion(
        self,
        monkeypatch: pytest.MonkeyPatch,
        session_factory: async_sessionmaker[AsyncSession],
        committed_job: dict[str, uuid.UUID],
    ) -> None:
        """All entries fail -> LadderExhaustedError, but every attempt is logged.

        Each LLM call is independently committed to ESC by ``_persist``;
        the router's terminal ``raise`` does not roll those rows back.
        """
        _patch_load_prompt(monkeypatch)
        _patch_sleep(monkeypatch)

        # Two entries, each fails with a SEMANTIC exception (single
        # attempt each, no retries on SEMANTIC).
        bad_a = _provider_with(
            side_effects=[RuntimeError("first entry failed")],
        )
        bad_b = _provider_with(
            side_effects=[RuntimeError("second entry failed")],
        )

        config = _config(
            "demo_stage",
            entries=(("anthropic", "a-1"), ("gemini", "g-1")),
        )
        router = StageRouter(
            config,
            {"anthropic": bad_a, "gemini": bad_b},
            session_factory=session_factory,
            registry=empty_registry(),
        )

        with (
            tenant_scope(committed_job["tenant_id"]),
            job_scope(committed_job["job_id"]),
            pytest.raises(LadderExhaustedError) as exc_info,
        ):
            await router.execute_for_stage("demo_stage")

        assert exc_info.value.stage_name == "demo_stage"
        assert len(exc_info.value.attempts) == 2

        # Two ESC rows survive the raise -- one per attempt.
        escs = await _fetch_escs(session_factory, committed_job["job_id"])
        assert len(escs) == 2

        providers_logged = [esc.provider for esc in escs]
        assert providers_logged == ["anthropic", "gemini"]

        for esc in escs:
            assert esc.success is False
            assert esc.error_message is not None
            assert esc.action == "demo_stage"
            assert esc.strategy == "default"

        assert "first entry failed" in (escs[0].error_message or "")
        assert "second entry failed" in (escs[1].error_message or "")

    async def test_empty_content_treated_as_semantic(
        self,
        monkeypatch: pytest.MonkeyPatch,
        session_factory: async_sessionmaker[AsyncSession],
        committed_job: dict[str, uuid.UUID],
    ) -> None:
        """Empty content -> immediate fallback; ESC still success=True.

        Documented in StageRouter module docstring: the call succeeded
        in transport terms (charge incurred), so ``success=True`` is
        accurate for billing audit. The router's policy decision to
        fall through is independent of that flag.
        """
        _patch_load_prompt(monkeypatch)
        _patch_sleep(monkeypatch)

        empty_provider = _provider_with(
            side_effects=[_ok_response(content="")],
        )
        good_provider = _provider_with(
            side_effects=[_ok_response("from second entry")],
        )

        config = _config(
            "demo_stage",
            entries=(("anthropic", "a-1"), ("gemini", "g-1")),
        )
        router = StageRouter(
            config,
            {"anthropic": empty_provider, "gemini": good_provider},
            session_factory=session_factory,
            registry=empty_registry(),
        )

        with (
            tenant_scope(committed_job["tenant_id"]),
            job_scope(committed_job["job_id"]),
        ):
            result = await router.execute_for_stage("demo_stage")

        assert result.content == "from second entry"
        assert result.provider_used == "gemini"
        assert result.attempt_count == 2

        escs = await _fetch_escs(session_factory, committed_job["job_id"])
        assert len(escs) == 2

        # Both ESCs record success=True -- both LLM calls completed in
        # transport terms. The first is just empty-content, which the
        # router's fallback policy treats as SEMANTIC (no error_message
        # because no exception was raised inside _call_with_log).
        assert escs[0].provider == "anthropic"
        assert escs[0].success is True
        assert escs[0].error_message is None

        assert escs[1].provider == "gemini"
        assert escs[1].success is True
        assert escs[1].error_message is None
