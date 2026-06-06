"""Tests for the KD16 :class:`StageRouter` (commit (e))."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock

import anthropic
import httpx
import openai
import pytest

from course_supporter.llm.error_categories import (
    ErrorCategory,
    LadderExhaustedError,
    StructuralRetryError,
)
from course_supporter.llm.ladder_config import (
    LadderConfig,
    LadderEntry,
    StageConfig,
)
from course_supporter.llm.prompt_loader_md import StagePrompt
from course_supporter.llm.providers.base import LLMProvider
from course_supporter.llm.schemas import LLMResponse
from course_supporter.llm.stage_router import StageResult, StageRouter
from tests._helpers.registry import empty_registry as _registry
from tests._helpers.registry import registry_with as _registry_with

if TYPE_CHECKING:
    from collections.abc import Iterable

# ── Helpers ────────────────────────────────────────────────────────


def _ladder(*entries: tuple[str, str]) -> list[LadderEntry]:
    return [LadderEntry(provider=p, model=m) for p, m in entries]


def _config(
    stage_name: str = "demo",
    *,
    entries: Iterable[tuple[str, str]] = (("anthropic", "claude-x"),),
    prompt_ref: str = "prompts/example/v1.md",
) -> LadderConfig:
    return LadderConfig(
        stages={
            stage_name: StageConfig(
                prompt_ref=prompt_ref,
                ladder=_ladder(*entries),
            ),
        }
    )


def _ok_response(
    content: str = "answer",
    provider: str = "anthropic",
    model: str = "claude-x",
) -> LLMResponse:
    return LLMResponse(
        content=content,
        provider=provider,
        model_id=model,
        tokens_in=10,
        tokens_out=20,
        latency_ms=42,
        cost_usd=0.001,
    )


def _ok_provider(content: str = "answer") -> LLMProvider:
    p = AsyncMock(spec=LLMProvider)
    p.enabled = True
    p.complete = AsyncMock(return_value=_ok_response(content))
    # classify_error is sync on real providers; not used on success path
    # but set explicitly to avoid Mock-as-ErrorCategory leaking elsewhere.
    p.classify_error = lambda _exc: ErrorCategory.SEMANTIC
    return p  # type: ignore[return-value]


def _failing_provider(
    *,
    side_effects: list[Any],
    classify_as: ErrorCategory = ErrorCategory.SEMANTIC,
) -> LLMProvider:
    p = AsyncMock(spec=LLMProvider)
    p.enabled = True
    p.complete = AsyncMock(side_effect=side_effects)
    # Default to SEMANTIC -- matches the base-class fallback. Tests
    # that need INFRASTRUCTURE / INPUT_OVERFLOW override after build.
    p.classify_error = lambda _exc, _cat=classify_as: _cat
    return p  # type: ignore[return-value]


def _mock_load_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace stage_router.load_prompt with a canned response."""
    monkeypatch.setattr(
        "course_supporter.llm.stage_router.load_prompt",
        lambda prompt_ref, *, base_path=None: StagePrompt(
            system="sys-template", user="user-template"
        ),
    )


def _capture_persist_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    async def _fake_persist(_session_factory: Any, **kwargs: Any) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(
        "course_supporter.llm.stage_router._persist",
        _fake_persist,
    )
    return calls


def _capture_sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    sleeps: list[float] = []

    async def _fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr("asyncio.sleep", _fake_sleep)
    return sleeps


def _anthropic_rate_limit() -> Exception:
    return anthropic.RateLimitError(
        "rate limited",
        response=httpx.Response(
            429, request=httpx.Request("POST", "https://api.anthropic.com")
        ),
        body=None,
    )


def _openai_overflow() -> Exception:
    return openai.BadRequestError(
        "context_length_exceeded",
        response=httpx.Response(
            400, request=httpx.Request("POST", "https://api.openai.com")
        ),
        body={"code": "context_length_exceeded"},
    )


def _semantic_exception() -> Exception:
    return RuntimeError("garbled response")


# ── Tests ──────────────────────────────────────────────────────────


class TestHappyPath:
    async def test_success_first_attempt(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _mock_load_prompt(monkeypatch)
        provider = _ok_provider("hello world")
        router = StageRouter(_config(), {"anthropic": provider}, registry=_registry())

        result = await router.execute_for_stage("demo")

        assert isinstance(result, StageResult)
        assert result.content == "hello world"
        assert result.provider_used == "anthropic"
        assert result.model_used == "claude-x"
        assert result.attempt_count == 1
        provider.complete.assert_awaited_once()

    async def test_falls_back_to_second_entry_on_first_failure(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _mock_load_prompt(monkeypatch)
        _capture_sleeps(monkeypatch)

        bad = _failing_provider(side_effects=[_semantic_exception()])
        good = _ok_provider("from second entry")
        config = _config(
            entries=(("anthropic", "claude-x"), ("gemini", "gemini-x")),
        )

        router = StageRouter(
            config,
            {"anthropic": bad, "gemini": good},
            registry=_registry(),
        )
        result = await router.execute_for_stage("demo")

        assert result.content == "from second entry"
        assert result.provider_used == "gemini"
        assert result.attempt_count == 2  # 1 fail + 1 success


class TestInfrastructureFallback:
    async def test_retry_succeeds_on_second_attempt(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _mock_load_prompt(monkeypatch)
        sleeps = _capture_sleeps(monkeypatch)

        infra_exc = _anthropic_rate_limit()
        provider = _failing_provider(
            side_effects=[infra_exc, _ok_response("recovered")],
        )
        # classify_error returns INFRASTRUCTURE for the rate-limit exc.

        provider.classify_error = lambda _exc: ErrorCategory.INFRASTRUCTURE  # type: ignore[method-assign]

        router = StageRouter(_config(), {"anthropic": provider}, registry=_registry())
        result = await router.execute_for_stage("demo")

        assert result.content == "recovered"
        assert result.attempt_count == 2
        assert sleeps == [1]  # one backoff before the successful retry

    @pytest.mark.parametrize(
        ("max_retries", "expected_attempts", "expected_sleeps"),
        [
            (0, 1, []),
            (1, 2, [1]),
            (3, 4, [1, 2, 4]),
        ],
    )
    async def test_max_retries_boundary(
        self,
        monkeypatch: pytest.MonkeyPatch,
        max_retries: int,
        expected_attempts: int,
        expected_sleeps: list[float],
    ) -> None:
        _mock_load_prompt(monkeypatch)
        sleeps = _capture_sleeps(monkeypatch)

        # All calls fail with infrastructure errors -> exhaust this entry
        # and surface LadderExhaustedError (single-entry ladder).
        provider = _failing_provider(
            side_effects=[_anthropic_rate_limit() for _ in range(max_retries + 1)],
        )
        provider.classify_error = lambda _exc: ErrorCategory.INFRASTRUCTURE  # type: ignore[method-assign]

        router = StageRouter(
            _config(),
            {"anthropic": provider},
            max_retries_infrastructure=max_retries,
            registry=_registry(),
        )

        with pytest.raises(LadderExhaustedError) as exc_info:
            await router.execute_for_stage("demo")

        assert provider.complete.await_count == expected_attempts
        assert sleeps == expected_sleeps
        assert exc_info.value.attempts[0][0] == "anthropic"
        assert "INFRASTRUCTURE" in exc_info.value.attempts[0][2]

    async def test_exponential_schedule_three_retries(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _mock_load_prompt(monkeypatch)
        sleeps = _capture_sleeps(monkeypatch)

        provider = _failing_provider(
            side_effects=[_anthropic_rate_limit() for _ in range(4)],
        )
        provider.classify_error = lambda _exc: ErrorCategory.INFRASTRUCTURE  # type: ignore[method-assign]

        router = StageRouter(
            _config(),
            {"anthropic": provider},
            max_retries_infrastructure=3,
            registry=_registry(),
        )

        with pytest.raises(LadderExhaustedError):
            await router.execute_for_stage("demo")

        # Default: 4 attempts with delays 1, 2, 4 (capped at 30).
        assert sleeps == [1, 2, 4]


class TestStructuralRetry:
    async def test_retry_succeeds_on_second_attempt(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _mock_load_prompt(monkeypatch)
        _capture_sleeps(monkeypatch)

        provider = _failing_provider(
            side_effects=[
                StructuralRetryError("missing required field 'topic'"),
                _ok_response("retry succeeded"),
            ],
        )
        router = StageRouter(_config(), {"anthropic": provider}, registry=_registry())

        result = await router.execute_for_stage("demo")

        assert result.content == "retry succeeded"
        assert result.attempt_count == 2

        # Verify the feedback message landed in the retry prompt.
        first_request = provider.complete.await_args_list[0].args[0]
        retry_request = provider.complete.await_args_list[1].args[0]
        assert first_request.prompt == "user-template"
        assert "failed validation" in retry_request.prompt
        assert "missing required field 'topic'" in retry_request.prompt

    async def test_retry_failure_falls_back_to_next_entry(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _mock_load_prompt(monkeypatch)
        _capture_sleeps(monkeypatch)

        bad = _failing_provider(
            side_effects=[
                StructuralRetryError("first failure"),
                StructuralRetryError("retry also bad"),
            ],
        )
        good = _ok_provider("from fallback")
        router = StageRouter(
            _config(entries=(("anthropic", "claude-x"), ("gemini", "g-x"))),
            {"anthropic": bad, "gemini": good},
            registry=_registry(),
        )

        result = await router.execute_for_stage("demo")

        assert result.content == "from fallback"
        assert result.provider_used == "gemini"
        # Initial + structural retry on bad provider, then 1 success on good.
        assert result.attempt_count == 3

    async def test_attempt_count_includes_structural_retry(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _mock_load_prompt(monkeypatch)
        _capture_sleeps(monkeypatch)

        provider = _failing_provider(
            side_effects=[
                StructuralRetryError("validation error"),
                _ok_response("ok"),
            ],
        )
        router = StageRouter(_config(), {"anthropic": provider}, registry=_registry())

        result = await router.execute_for_stage("demo")
        assert result.attempt_count == 2  # original + structural retry


class TestSemanticFallback:
    async def test_semantic_exception_immediate_fallback(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _mock_load_prompt(monkeypatch)
        sleeps = _capture_sleeps(monkeypatch)

        bad = _failing_provider(side_effects=[_semantic_exception()])
        good = _ok_provider("from fallback")
        router = StageRouter(
            _config(entries=(("anthropic", "claude-x"), ("gemini", "g-x"))),
            {"anthropic": bad, "gemini": good},
            registry=_registry(),
        )

        result = await router.execute_for_stage("demo")

        assert result.provider_used == "gemini"
        assert result.attempt_count == 2  # 1 fail + 1 success, no retry
        assert sleeps == []

    async def test_empty_content_treated_as_semantic(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _mock_load_prompt(monkeypatch)
        _capture_sleeps(monkeypatch)

        # First provider returns successfully but with empty content.
        empty_provider = AsyncMock(spec=LLMProvider)
        empty_provider.enabled = True
        empty_provider.complete = AsyncMock(return_value=_ok_response(content=""))
        good = _ok_provider("non-empty answer")

        router = StageRouter(
            _config(entries=(("anthropic", "claude-x"), ("gemini", "g-x"))),
            {"anthropic": empty_provider, "gemini": good},
            registry=_registry(),
        )

        result = await router.execute_for_stage("demo")
        assert result.provider_used == "gemini"
        assert result.content == "non-empty answer"
        assert result.attempt_count == 2  # empty + good


class TestInputOverflowFallback:
    async def test_overflow_immediate_fallback(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _mock_load_prompt(monkeypatch)
        sleeps = _capture_sleeps(monkeypatch)

        bad = _failing_provider(side_effects=[_openai_overflow()])
        bad.classify_error = lambda _exc: ErrorCategory.INPUT_OVERFLOW  # type: ignore[method-assign]
        good = _ok_provider("fits")

        router = StageRouter(
            _config(entries=(("openai", "gpt-4"), ("gemini", "g-x"))),
            {"openai": bad, "gemini": good},
            registry=_registry(),
        )

        result = await router.execute_for_stage("demo")

        assert result.provider_used == "gemini"
        assert sleeps == []  # no retry on input overflow


class TestLadderExhaustion:
    async def test_all_entries_fail_raises_with_attempt_tuples(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _mock_load_prompt(monkeypatch)
        _capture_sleeps(monkeypatch)

        bad_a = _failing_provider(side_effects=[_semantic_exception()])
        bad_b = _failing_provider(side_effects=[_semantic_exception()])

        router = StageRouter(
            _config(entries=(("anthropic", "a-1"), ("gemini", "g-1"))),
            {"anthropic": bad_a, "gemini": bad_b},
            registry=_registry(),
        )

        with pytest.raises(LadderExhaustedError) as exc_info:
            await router.execute_for_stage("demo")

        attempts = exc_info.value.attempts
        assert len(attempts) == 2
        assert attempts[0][:2] == ("anthropic", "a-1")
        assert attempts[1][:2] == ("gemini", "g-1")
        assert exc_info.value.stage_name == "demo"

    async def test_mixed_categories_preserved_in_attempts(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _mock_load_prompt(monkeypatch)
        _capture_sleeps(monkeypatch)

        overflow = _failing_provider(side_effects=[_openai_overflow()])
        overflow.classify_error = lambda _exc: ErrorCategory.INPUT_OVERFLOW  # type: ignore[method-assign]

        semantic = _failing_provider(side_effects=[_semantic_exception()])

        router = StageRouter(
            _config(entries=(("openai", "gpt-4"), ("anthropic", "claude-x"))),
            {"openai": overflow, "anthropic": semantic},
            registry=_registry(),
        )

        with pytest.raises(LadderExhaustedError) as exc_info:
            await router.execute_for_stage("demo")

        reasons = [a[2] for a in exc_info.value.attempts]
        assert "INPUT_OVERFLOW" in reasons[0]
        assert "SEMANTIC" in reasons[1]


class TestESCPersistence:
    async def test_persists_per_attempt_with_correct_kwargs(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _mock_load_prompt(monkeypatch)
        calls = _capture_persist_calls(monkeypatch)
        _capture_sleeps(monkeypatch)

        bad = _failing_provider(side_effects=[_semantic_exception()])
        good = _ok_provider("ok")
        router = StageRouter(
            _config(entries=(("anthropic", "a-x"), ("gemini", "g-x"))),
            {"anthropic": bad, "gemini": good},
            session_factory=AsyncMock(),  # truthy stand-in
            registry=_registry(),
        )

        await router.execute_for_stage("demo")

        assert len(calls) == 2
        # First (failed) attempt
        assert calls[0]["action"] == "demo"
        assert calls[0]["strategy"] == "default"
        assert calls[0]["provider"] == "anthropic"
        assert calls[0]["model_id"] == "a-x"
        assert calls[0]["success"] is False
        assert "garbled response" in (calls[0]["error_message"] or "")
        # Second (successful) attempt
        assert calls[1]["provider"] == "gemini"
        assert calls[1]["success"] is True
        assert calls[1]["error_message"] is None

    async def test_no_persist_when_session_factory_absent(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _mock_load_prompt(monkeypatch)
        calls = _capture_persist_calls(monkeypatch)

        provider = _ok_provider("ok")
        router = StageRouter(_config(), {"anthropic": provider}, registry=_registry())

        await router.execute_for_stage("demo")

        # Without session_factory, ESC writes are skipped.
        assert calls == []


class TestProviderUnavailable:
    async def test_missing_provider_falls_through(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _mock_load_prompt(monkeypatch)
        _capture_sleeps(monkeypatch)

        good = _ok_provider("from second")
        router = StageRouter(
            _config(entries=(("absent", "x"), ("gemini", "g-x"))),
            {"gemini": good},  # "absent" not registered
            registry=_registry(),
        )

        result = await router.execute_for_stage("demo")
        assert result.provider_used == "gemini"

    async def test_disabled_provider_falls_through(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _mock_load_prompt(monkeypatch)
        _capture_sleeps(monkeypatch)

        disabled = AsyncMock(spec=LLMProvider)
        disabled.enabled = False
        good = _ok_provider("from second")

        router = StageRouter(
            _config(entries=(("anthropic", "a-x"), ("gemini", "g-x"))),
            {"anthropic": disabled, "gemini": good},
            registry=_registry(),
        )

        result = await router.execute_for_stage("demo")
        assert result.provider_used == "gemini"
        disabled.complete.assert_not_called()


class TestRenderContextFlow:
    async def test_render_context_reaches_llm_request(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Use a real load_prompt-shaped substitute that respects render args.
        def _stub_load(prompt_ref: str, *, base_path: Any = None) -> StagePrompt:
            return StagePrompt(
                system="lang={{ lang }}",
                user="topic={{ topic }}",
            )

        monkeypatch.setattr(
            "course_supporter.llm.stage_router.load_prompt",
            _stub_load,
        )

        provider = _ok_provider("ok")
        router = StageRouter(_config(), {"anthropic": provider}, registry=_registry())

        await router.execute_for_stage("demo", lang="ua", topic="recursion")

        request = provider.complete.await_args.args[0]
        assert request.system_prompt == "lang=ua"
        assert request.prompt == "topic=recursion"
        assert request.action == "demo"
        assert request.strategy == "default"
        assert request.model == "claude-x"


class TestStageNameUnknown:
    async def test_unknown_stage_raises_keyerror(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _mock_load_prompt(monkeypatch)
        provider = _ok_provider()
        router = StageRouter(_config(), {"anthropic": provider}, registry=_registry())

        with pytest.raises(KeyError, match="Unknown stage"):
            await router.execute_for_stage("nope")


class TestPersistFailureDoesNotMask:
    """Commit (g) review fix: an exception inside ``_persist`` must
    not mask the original LLM exception that triggered the
    finally-block. Telemetry never interrupts control flow.

    The proof setup wires an INFRASTRUCTURE failure on attempt 1 plus
    a successful retry on attempt 2 against a single-entry ladder.
    Without the swallow, the persist-side ``RuntimeError`` would
    propagate from ``finally`` and replace the rate-limit exception;
    classify_error would receive the ``RuntimeError`` -> SEMANTIC
    -> immediate fallback -> single-entry ladder exhausted ->
    ``LadderExhaustedError``. The fact that the retry runs and the
    test gets ``"recovered"`` proves the rate-limit exception
    reached the classifier intact.
    """

    async def test_persist_failure_does_not_mask_llm_exception(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _mock_load_prompt(monkeypatch)
        _capture_sleeps(monkeypatch)

        async def _failing_persist(_session_factory: Any, **_kwargs: Any) -> None:
            raise RuntimeError("DB write blew up")

        monkeypatch.setattr(
            "course_supporter.llm.stage_router._persist",
            _failing_persist,
        )

        provider = _failing_provider(
            side_effects=[_anthropic_rate_limit(), _ok_response("recovered")],
            classify_as=ErrorCategory.INFRASTRUCTURE,
        )

        router = StageRouter(
            _config(),  # single-entry ladder -- no fallback safety net
            {"anthropic": provider},
            session_factory=AsyncMock(),  # truthy; persist will be invoked
            registry=_registry(),
        )

        result = await router.execute_for_stage("demo")

        assert result.content == "recovered"
        assert result.attempt_count == 2  # initial + INFRASTRUCTURE retry
        assert provider.complete.await_count == 2


class TestResponseValidator:
    """``response_validator`` hook drives ladder retry on schema failure."""

    async def test_validator_none_preserves_legacy_behaviour(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Without a validator, the first non-empty response wins (regression
        guard against the new keyword-only parameter changing existing
        behaviour for current callers).
        """
        _mock_load_prompt(monkeypatch)
        _capture_sleeps(monkeypatch)

        provider = _ok_provider("legacy answer")
        router = StageRouter(_config(), {"anthropic": provider}, registry=_registry())

        result = await router.execute_for_stage("demo")

        assert result.content == "legacy answer"
        assert result.attempt_count == 1
        assert provider.complete.await_count == 1

    async def test_validator_passes_returns_response(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Validator returning normally → response surfaces unchanged."""
        _mock_load_prompt(monkeypatch)
        _capture_sleeps(monkeypatch)

        provider = _ok_provider("good content")
        router = StageRouter(_config(), {"anthropic": provider}, registry=_registry())

        seen: list[str] = []

        def _validator(content: str) -> None:
            seen.append(content)

        result = await router.execute_for_stage("demo", response_validator=_validator)

        assert result.content == "good content"
        assert seen == ["good content"]

    async def test_validator_raises_structural_retry_succeeds_on_retry(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """First call's content fails validator → structural retry on same
        rung with feedback → retry content passes → success.
        """
        _mock_load_prompt(monkeypatch)
        _capture_sleeps(monkeypatch)

        provider = AsyncMock(spec=LLMProvider)
        provider.enabled = True
        provider.complete = AsyncMock(
            side_effect=[
                _ok_response("bad-output"),
                _ok_response("good-output"),
            ]
        )
        provider.classify_error = lambda _exc: ErrorCategory.SEMANTIC

        router = StageRouter(_config(), {"anthropic": provider}, registry=_registry())

        call_count = {"n": 0}

        def _validator(content: str) -> None:
            call_count["n"] += 1
            if content == "bad-output":
                raise StructuralRetryError("coverage mismatch: 100 vs 90")

        result = await router.execute_for_stage("demo", response_validator=_validator)

        assert result.content == "good-output"
        assert result.attempt_count == 2  # initial + structural retry
        assert call_count["n"] == 2

        # Confirm feedback landed in the retry prompt.
        retry_request = provider.complete.await_args_list[1].args[0]
        assert "failed validation" in retry_request.prompt
        assert "coverage mismatch: 100 vs 90" in retry_request.prompt

    async def test_validator_fails_on_retry_falls_through_to_next_rung(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Validator rejects both initial and retry on rung 0 → next rung
        runs and (with no validator failure there) succeeds.
        """
        _mock_load_prompt(monkeypatch)
        _capture_sleeps(monkeypatch)

        bad = AsyncMock(spec=LLMProvider)
        bad.enabled = True
        bad.complete = AsyncMock(
            side_effect=[
                _ok_response("bad-1"),
                _ok_response("bad-2"),
            ]
        )
        bad.classify_error = lambda _exc: ErrorCategory.SEMANTIC

        good = _ok_provider("acceptable")

        router = StageRouter(
            _config(entries=(("anthropic", "claude-x"), ("gemini", "g-x"))),
            {"anthropic": bad, "gemini": good},
            registry=_registry(),
        )

        bad_contents = {"bad-1", "bad-2"}

        def _validator(content: str) -> None:
            if content in bad_contents:
                raise StructuralRetryError(f"schema mismatch: {content}")

        result = await router.execute_for_stage("demo", response_validator=_validator)

        assert result.content == "acceptable"
        assert result.provider_used == "gemini"
        # 1 initial + 1 structural retry on bad rung, then 1 success on good.
        assert result.attempt_count == 3

    async def test_all_rungs_fail_validator_raises_ladder_exhausted(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Validator rejects every rung → LadderExhaustedError surfaces
        with per-attempt feedback in the attempts list.
        """
        _mock_load_prompt(monkeypatch)
        _capture_sleeps(monkeypatch)

        bad_a = AsyncMock(spec=LLMProvider)
        bad_a.enabled = True
        bad_a.complete = AsyncMock(
            side_effect=[_ok_response("a-1"), _ok_response("a-2")]
        )
        bad_a.classify_error = lambda _exc: ErrorCategory.SEMANTIC

        bad_b = AsyncMock(spec=LLMProvider)
        bad_b.enabled = True
        bad_b.complete = AsyncMock(
            side_effect=[_ok_response("b-1"), _ok_response("b-2")]
        )
        bad_b.classify_error = lambda _exc: ErrorCategory.SEMANTIC

        router = StageRouter(
            _config(entries=(("anthropic", "claude-x"), ("gemini", "g-x"))),
            {"anthropic": bad_a, "gemini": bad_b},
            registry=_registry(),
        )

        def _always_fail(content: str) -> None:
            raise StructuralRetryError(f"reject {content}")

        with pytest.raises(LadderExhaustedError) as exc_info:
            await router.execute_for_stage("demo", response_validator=_always_fail)

        attempts = exc_info.value.attempts
        assert len(attempts) == 2
        # Each rung's "reason" comes from _structural_retry exhaustion.
        for _provider, _model, reason in attempts:
            assert "STRUCTURAL" in reason

    async def test_validator_skipped_on_empty_content(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Empty content path is handled by the existing SEMANTIC fallback
        in ``_attempt_entry``; the validator must NOT be invoked on an
        empty string (otherwise schema-parsing the empty string would
        produce a misleading StructuralRetryError instead of the
        established SEMANTIC fallback).
        """
        _mock_load_prompt(monkeypatch)
        _capture_sleeps(monkeypatch)

        empty_provider = AsyncMock(spec=LLMProvider)
        empty_provider.enabled = True
        empty_provider.complete = AsyncMock(return_value=_ok_response(content=""))
        empty_provider.classify_error = lambda _exc: ErrorCategory.SEMANTIC
        good = _ok_provider("answer")

        router = StageRouter(
            _config(entries=(("anthropic", "claude-x"), ("gemini", "g-x"))),
            {"anthropic": empty_provider, "gemini": good},
            registry=_registry(),
        )

        observed: list[str] = []

        def _validator(content: str) -> None:
            observed.append(content)

        result = await router.execute_for_stage("demo", response_validator=_validator)

        assert result.provider_used == "gemini"
        # Validator saw the good content but NOT the empty string.
        assert observed == ["answer"]


# ── Sub-area #0 — multimodal contents= + LadderEntry per-rung overrides ──
# Phase 2.3 KD-2.3-R + KD-2.3-S + v0.3 N1 propagation chain coverage.


class TestExecuteForStageContents:
    """``execute_for_stage(..., contents=[...])`` reaches ``LLMRequest.contents``."""

    async def test_contents_kwarg_propagates_to_llm_request(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _mock_load_prompt(monkeypatch)
        provider = _ok_provider("ok")
        router = StageRouter(_config(), {"anthropic": provider}, registry=_registry())

        png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8
        await router.execute_for_stage("demo", contents=[png])

        request = provider.complete.await_args.args[0]
        assert request.contents == [png]

    async def test_omitting_contents_leaves_request_contents_none(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Backward-compat: text-only callers (audio/text/web/security)
        # must continue producing requests with ``contents is None``.
        _mock_load_prompt(monkeypatch)
        provider = _ok_provider("ok")
        router = StageRouter(_config(), {"anthropic": provider}, registry=_registry())

        await router.execute_for_stage("demo")

        request = provider.complete.await_args.args[0]
        assert request.contents is None


class TestLadderEntryOverridesPropagation:
    """Per-rung ``reasoning`` / ``max_output_tokens`` map into the request."""

    async def test_reasoning_override_propagates_to_request(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _mock_load_prompt(monkeypatch)
        provider = _ok_provider("ok")

        config = LadderConfig(
            stages={
                "demo": StageConfig(
                    prompt_ref="prompts/example/v1.md",
                    ladder=[
                        LadderEntry(
                            provider="anthropic",
                            model="claude-x",
                            reasoning={"exclude": True},
                        )
                    ],
                ),
            }
        )
        router = StageRouter(config, {"anthropic": provider}, registry=_registry())

        await router.execute_for_stage("demo")

        request = provider.complete.await_args.args[0]
        assert request.reasoning == {"exclude": True}

    async def test_max_output_tokens_override_propagates_to_request(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _mock_load_prompt(monkeypatch)
        provider = _ok_provider("ok")

        config = LadderConfig(
            stages={
                "demo": StageConfig(
                    prompt_ref="prompts/example/v1.md",
                    ladder=[
                        LadderEntry(
                            provider="anthropic",
                            model="claude-x",
                            max_output_tokens=4096,
                        )
                    ],
                ),
            }
        )
        router = StageRouter(config, {"anthropic": provider}, registry=_registry())

        await router.execute_for_stage("demo")

        request = provider.complete.await_args.args[0]
        assert request.max_tokens == 4096

    async def test_no_overrides_leave_request_fields_none(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Default ``LadderEntry`` (no overrides) preserves provider-side
        # fallbacks: request.reasoning is None and request.max_tokens
        # is None so the provider uses its class-level default.
        _mock_load_prompt(monkeypatch)
        provider = _ok_provider("ok")
        router = StageRouter(_config(), {"anthropic": provider}, registry=_registry())

        await router.execute_for_stage("demo")

        request = provider.complete.await_args.args[0]
        assert request.reasoning is None
        assert request.max_tokens is None


# ── TASK-2.4.22: registry-aware StageRouter ────────────────────────


class TestRegistryAwareCost:
    """F(a) — StageRouter computes cost_usd from registry pricing."""

    async def test_cost_populated_from_registry_on_success(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _mock_load_prompt(monkeypatch)
        calls = _capture_persist_calls(monkeypatch)

        provider = _ok_provider("ok")
        # _ok_response sets tokens_in=10, tokens_out=20 →
        # 10 * 0.001 / 1000 + 20 * 0.002 / 1000 = 0.00001 + 0.00004 = 0.00005
        router = StageRouter(
            _config(),
            {"anthropic": provider},
            registry=_registry_with(cost_per_1k_in=0.001, cost_per_1k_out=0.002),
            session_factory=AsyncMock(),
        )

        await router.execute_for_stage("demo")

        assert len(calls) == 1
        assert calls[0]["success"] is True
        assert calls[0]["cost_usd"] == pytest.approx(0.00005)

    async def test_cost_null_when_model_not_in_registry(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _mock_load_prompt(monkeypatch)
        calls = _capture_persist_calls(monkeypatch)

        provider = _ok_provider("ok")
        router = StageRouter(
            _config(),
            {"anthropic": provider},
            registry=_registry(),  # empty registry — model unknown
            session_factory=AsyncMock(),
        )

        await router.execute_for_stage("demo")

        assert len(calls) == 1
        assert calls[0]["success"] is True
        assert calls[0]["cost_usd"] is None

    async def test_cost_null_when_zero_tokens(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _mock_load_prompt(monkeypatch)
        calls = _capture_persist_calls(monkeypatch)

        provider = AsyncMock(spec=LLMProvider)
        provider.enabled = True
        provider.complete = AsyncMock(
            return_value=LLMResponse(
                content="ok",
                provider="anthropic",
                model_id="claude-x",
                tokens_in=0,
                tokens_out=0,
                latency_ms=10,
                cost_usd=None,
            )
        )
        provider.classify_error = lambda _exc: ErrorCategory.SEMANTIC

        router = StageRouter(
            _config(),
            {"anthropic": provider},  # type: ignore[dict-item]
            registry=_registry_with(cost_per_1k_in=0.001, cost_per_1k_out=0.002),
            session_factory=AsyncMock(),
        )

        await router.execute_for_stage("demo")

        assert len(calls) == 1
        assert calls[0]["success"] is True
        assert calls[0]["cost_usd"] is None

    async def test_cost_null_on_failed_call(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _mock_load_prompt(monkeypatch)
        _capture_sleeps(monkeypatch)
        calls = _capture_persist_calls(monkeypatch)

        provider = _failing_provider(side_effects=[_semantic_exception()])
        router = StageRouter(
            _config(),
            {"anthropic": provider},
            registry=_registry_with(cost_per_1k_in=0.001, cost_per_1k_out=0.002),
            session_factory=AsyncMock(),
        )

        with pytest.raises(LadderExhaustedError):
            await router.execute_for_stage("demo")

        assert len(calls) == 1
        assert calls[0]["success"] is False
        assert calls[0]["cost_usd"] is None

    async def test_cost_zero_for_unpriced_model(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Model in registry with 0.0 pricing → cost_usd=0.0 (not NULL).

        Mirrors legacy ModelRouter behaviour; semantically the model is
        either free or pricing is unset in YAML — caller's responsibility
        to keep registry accurate (DD-2.4-F audit script territory).
        """
        _mock_load_prompt(monkeypatch)
        calls = _capture_persist_calls(monkeypatch)

        provider = _ok_provider("ok")
        router = StageRouter(
            _config(),
            {"anthropic": provider},
            registry=_registry_with(cost_per_1k_in=0.0, cost_per_1k_out=0.0),
            session_factory=AsyncMock(),
        )

        await router.execute_for_stage("demo")

        assert len(calls) == 1
        assert calls[0]["cost_usd"] == 0.0


class TestRegistryAwareMaxTokens:
    """H — _build_request resolves max_tokens from registry on unpinned rungs."""

    async def test_rung_pin_wins_over_registry(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _mock_load_prompt(monkeypatch)

        provider = _ok_provider("ok")
        config = LadderConfig(
            stages={
                "demo": StageConfig(
                    prompt_ref="prompts/example/v1.md",
                    ladder=[
                        LadderEntry(
                            provider="anthropic",
                            model="claude-x",
                            max_output_tokens=999,  # rung pin
                        )
                    ],
                )
            }
        )
        router = StageRouter(
            config,
            {"anthropic": provider},
            registry=_registry_with(max_output_tokens=4096),
        )

        await router.execute_for_stage("demo")

        request = provider.complete.await_args.args[0]
        assert request.max_tokens == 999  # rung wins

    async def test_unpinned_rung_resolves_registry_max_tokens(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _mock_load_prompt(monkeypatch)

        provider = _ok_provider("ok")
        router = StageRouter(
            _config(),  # rung has no max_output_tokens
            {"anthropic": provider},
            registry=_registry_with(max_output_tokens=8192),
        )

        await router.execute_for_stage("demo")

        request = provider.complete.await_args.args[0]
        assert request.max_tokens == 8192

    async def test_unpinned_rung_with_unknown_model_returns_none(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Status-quo provider-default path when model not in registry."""
        _mock_load_prompt(monkeypatch)

        provider = _ok_provider("ok")
        router = StageRouter(
            _config(),
            {"anthropic": provider},
            registry=_registry(),  # empty registry — model unknown
        )

        await router.execute_for_stage("demo")

        request = provider.complete.await_args.args[0]
        assert request.max_tokens is None

    async def test_unpinned_rung_with_registry_model_without_cap_returns_none(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Registry has the model but its max_output_tokens is None."""
        _mock_load_prompt(monkeypatch)

        provider = _ok_provider("ok")
        router = StageRouter(
            _config(),
            {"anthropic": provider},
            registry=_registry_with(max_output_tokens=None),
        )

        await router.execute_for_stage("demo")

        request = provider.complete.await_args.args[0]
        assert request.max_tokens is None
