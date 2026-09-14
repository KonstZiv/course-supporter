"""What StageRouter writes to the call register (mentor-rebuild task 01).

Every test captures the ``_persist`` keyword arguments — the exact row the
router would write — and, where the ladder's path matters, also asserts the
router's own result, so a register change can never hide a routing change.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any
from unittest.mock import AsyncMock

import anthropic
import httpx
import openai
import pytest

from course_supporter.call_outcome import CallOutcome, SkipReason
from course_supporter.llm.error_categories import (
    ErrorCategory,
    LadderExhaustedError,
    StructuralRetryError,
)
from course_supporter.llm.finish_reason import FinishReason
from course_supporter.llm.input_hash import hash_attempt_input
from course_supporter.llm.ladder_config import LadderConfig, LadderEntry, StageConfig
from course_supporter.llm.prompt_loader_md import StagePrompt
from course_supporter.llm.providers.base import LLMProvider
from course_supporter.llm.schemas import LLMRequest, LLMResponse
from course_supporter.llm.stage_router import StageResult, StageRouter
from tests._helpers.registry import empty_registry, registry_with

_TEMPLATE = StagePrompt(system="sys-template", user="user-template")


@pytest.fixture(autouse=True)
def _canned_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "course_supporter.llm.stage_router.load_prompt",
        lambda prompt_ref, *, base_path=None: _TEMPLATE,
    )


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr("asyncio.sleep", _sleep)


@pytest.fixture()
def rows(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    captured: list[dict[str, Any]] = []

    async def _fake_persist(_session_factory: Any, **kwargs: Any) -> None:
        captured.append(kwargs)

    monkeypatch.setattr("course_supporter.llm.stage_router._persist", _fake_persist)
    return captured


def _config(
    *entries: tuple[str, str],
    record_output: bool = False,
    input_budget_ratio: float | None = None,
) -> LadderConfig:
    return LadderConfig(
        stages={
            "demo": StageConfig(
                prompt_ref="prompts/example/v1.md",
                ladder=[LadderEntry(provider=p, model=m) for p, m in entries],
                record_output=record_output,
                input_budget_ratio=input_budget_ratio,
            )
        }
    )


def _response(
    content: str = "answer", finish_reason: FinishReason = FinishReason.STOP
) -> LLMResponse:
    return LLMResponse(
        content=content,
        provider="x",
        model_id="x",
        tokens_in=10,
        tokens_out=8192 if finish_reason is FinishReason.OUTPUT_CEILING else 20,
        finish_reason=finish_reason,
    )


def _provider(
    *side_effects: Any, classify_as: ErrorCategory = ErrorCategory.SEMANTIC
) -> LLMProvider:
    provider = AsyncMock(spec=LLMProvider)
    provider.enabled = True
    provider.complete = AsyncMock(side_effect=list(side_effects))
    provider.classify_error = lambda _exc, _cat=classify_as: _cat
    return provider  # type: ignore[return-value]


def _router(
    config: LadderConfig,
    providers: dict[str, LLMProvider],
    *,
    record_full_input: bool = False,
    with_register: bool = True,
    **kwargs: Any,
) -> StageRouter:
    return StageRouter(
        config,
        providers,
        registry=kwargs.pop("registry", empty_registry()),
        session_factory=AsyncMock() if with_register else None,
        record_full_input=record_full_input,
        **kwargs,
    )


def _outcomes(rows: list[dict[str, Any]]) -> list[CallOutcome]:
    return [row["outcome"] for row in rows]


# ── Attempt rows ────────────────────────────────────────────────────────


class TestAttemptRows:
    async def test_success_row_carries_prompt_version_and_input_hash(
        self, rows: list[dict[str, Any]]
    ) -> None:
        router = _router(
            _config(("anthropic", "a")), {"anthropic": _provider(_response())}
        )

        await router.execute_for_stage("demo")

        (row,) = rows
        assert row["outcome"] is CallOutcome.SUCCESS
        assert row["finish_reason"] is FinishReason.STOP
        assert row["prompt_ref"] == "prompts/example/v1.md"
        # The TEMPLATE's hash, not the rendered text's.
        assert row["prompt_hash"] == _TEMPLATE.content_hash()
        expected_request = LLMRequest(
            prompt="user-template",
            system_prompt="sys-template",
            model="a",
            action="demo",
        )
        assert row["input_hash"] == hash_attempt_input(
            expected_request, provider="anthropic"
        )
        assert row["output_text"] is None
        assert row["input_text"] is None

    @pytest.mark.parametrize(
        ("finish_reason", "expected"),
        [
            (FinishReason.OUTPUT_CEILING, CallOutcome.EMPTY_AT_OUTPUT_CEILING),
            (FinishReason.STOP, CallOutcome.EMPTY),
            (FinishReason.UNKNOWN, CallOutcome.EMPTY),
        ],
    )
    async def test_empty_body_is_told_apart_by_the_finish_reason(
        self,
        rows: list[dict[str, Any]],
        finish_reason: FinishReason,
        expected: CallOutcome,
    ) -> None:
        router = _router(
            _config(("anthropic", "a")),
            {"anthropic": _provider(_response("", finish_reason))},
        )

        with pytest.raises(LadderExhaustedError):
            await router.execute_for_stage("demo")

        assert rows[0]["outcome"] is expected
        assert rows[0]["finish_reason"] is finish_reason
        # Transport still succeeded: the flag keeps meaning transport only.
        assert rows[0]["success"] is True

    async def test_validator_rejection_is_invalid_content_on_both_tries(
        self, rows: list[dict[str, Any]]
    ) -> None:
        def _reject(_content: str) -> None:
            raise StructuralRetryError("off-schema")

        router = _router(
            _config(("anthropic", "a")),
            {"anthropic": _provider(_response("{bad"), _response("{still bad"))},
        )

        with pytest.raises(LadderExhaustedError):
            await router.execute_for_stage("demo", response_validator=_reject)

        assert _outcomes(rows) == [
            CallOutcome.INVALID_CONTENT,
            CallOutcome.INVALID_CONTENT,
            CallOutcome.ABANDONED,
        ]
        # The structural retry appends feedback to the prompt: another input.
        assert rows[0]["input_hash"] != rows[1]["input_hash"]

    @pytest.mark.parametrize(
        ("exception", "category", "expected"),
        [
            (
                anthropic.RateLimitError(
                    "rate limited",
                    response=httpx.Response(
                        429, request=httpx.Request("POST", "https://x")
                    ),
                    body=None,
                ),
                ErrorCategory.INFRASTRUCTURE,
                CallOutcome.TRANSPORT_ERROR,
            ),
            (
                openai.BadRequestError(
                    "context_length_exceeded",
                    response=httpx.Response(
                        400, request=httpx.Request("POST", "https://x")
                    ),
                    body={"code": "context_length_exceeded"},
                ),
                ErrorCategory.INPUT_OVERFLOW,
                CallOutcome.INPUT_OVERFLOW,
            ),
            (
                RuntimeError("auth rejected"),
                ErrorCategory.SEMANTIC,
                CallOutcome.PROVIDER_REFUSAL,
            ),
        ],
        ids=["infrastructure", "input-overflow", "refusal"],
    )
    async def test_raised_call_outcome_follows_the_router_category(
        self,
        rows: list[dict[str, Any]],
        exception: Exception,
        category: ErrorCategory,
        expected: CallOutcome,
    ) -> None:
        router = _router(
            _config(("anthropic", "a")),
            {"anthropic": _provider(exception, classify_as=category)},
            max_retries_infrastructure=0,
        )

        with pytest.raises(LadderExhaustedError):
            await router.execute_for_stage("demo")

        assert _outcomes(rows) == [expected, CallOutcome.ABANDONED]
        assert rows[0]["success"] is False
        assert rows[0]["finish_reason"] is None

    async def test_every_infrastructure_retry_is_its_own_transport_error_row(
        self, rows: list[dict[str, Any]]
    ) -> None:
        router = _router(
            _config(("anthropic", "a")),
            {
                "anthropic": _provider(
                    RuntimeError("503"),
                    RuntimeError("503"),
                    classify_as=ErrorCategory.INFRASTRUCTURE,
                )
            },
            max_retries_infrastructure=1,
        )

        with pytest.raises(LadderExhaustedError):
            await router.execute_for_stage("demo")

        assert _outcomes(rows) == [
            CallOutcome.TRANSPORT_ERROR,
            CallOutcome.TRANSPORT_ERROR,
            CallOutcome.ABANDONED,
        ]


# ── Trace rows ──────────────────────────────────────────────────────────


class TestTraceRows:
    async def test_skipped_rungs_leave_a_no_call_row_with_their_reason(
        self, rows: list[dict[str, Any]]
    ) -> None:
        disabled = _provider()
        disabled.enabled = False  # type: ignore[misc]
        answering = _provider(_response())
        router = _router(
            _config(("missing", "m"), ("disabled", "d"), ("anthropic", "a")),
            {"disabled": disabled, "anthropic": answering},
        )

        await router.execute_for_stage("demo")

        assert _outcomes(rows) == [
            CallOutcome.SKIPPED,
            CallOutcome.SKIPPED,
            CallOutcome.SUCCESS,
        ]
        assert [row.get("skip_reason") for row in rows[:2]] == [
            SkipReason.PROVIDER_NOT_CONFIGURED,
            SkipReason.PROVIDER_DISABLED,
        ]
        for trace in rows[:2]:
            assert trace["success"] is None
            assert trace.get("error_message") is None
            assert trace.get("input_hash") is None
            assert trace["prompt_hash"] == _TEMPLATE.content_hash()

    async def test_input_budget_skip_names_its_reason_not_its_numbers(
        self, rows: list[dict[str, Any]]
    ) -> None:
        registry = registry_with(model_id="tiny", cost_per_1k_in=0.0)
        registry.models["tiny"].max_context = 2  # any real prompt exceeds it
        router = _router(
            _config(("anthropic", "tiny"), input_budget_ratio=0.5),
            {"anthropic": _provider()},
            registry=registry,
        )

        with pytest.raises(LadderExhaustedError) as exc_info:
            await router.execute_for_stage("demo")

        (trace,) = rows
        assert trace["outcome"] is CallOutcome.SKIPPED
        assert trace["skip_reason"] is SkipReason.INPUT_BUDGET_EXCEEDED
        assert trace.get("error_message") is None
        # The numbers stay where they were: the exception text (Methodist
        # parses its prefix — methodist.py), not the register.
        (_, _, reason) = exc_info.value.attempts[0]
        assert reason.startswith("input budget exceeded: ~")

    async def test_ladder_reason_texts_are_unchanged(
        self, rows: list[dict[str, Any]]
    ) -> None:
        disabled = _provider()
        disabled.enabled = False  # type: ignore[misc]
        router = _router(
            _config(("missing", "m"), ("disabled", "d")), {"disabled": disabled}
        )

        with pytest.raises(LadderExhaustedError) as exc_info:
            await router.execute_for_stage("demo")

        assert [a[2] for a in exc_info.value.attempts] == [
            "provider not configured",
            "provider disabled",
        ]

    async def test_no_trace_is_written_without_a_register(self) -> None:
        router = _router(
            _config(("missing", "m"), ("anthropic", "a")),
            {"anthropic": _provider(_response())},
            with_register=False,
        )

        result = await router.execute_for_stage("demo")

        assert result.content == "answer"


# ── The ladder itself does not move ────────────────────────────────────


class TestDescentUnchanged:
    """Recognising an empty body at the output ceiling changes what the register
    says, not what the router does: the rule "no descent on the ceiling" belongs
    to task 03. Pinned on the step-E shape, with and without a register."""

    async def _run(
        self, *, with_register: bool
    ) -> tuple[StageResult, LLMProvider, LLMProvider]:
        ceiling = _provider(_response("", FinishReason.OUTPUT_CEILING))
        fallback = _provider(_response("from the dearer rung"))
        router = _router(
            _config(("deepseek_thinking", "pro"), ("dashscope", "max")),
            {"deepseek_thinking": ceiling, "dashscope": fallback},
            with_register=with_register,
        )
        return await router.execute_for_stage("demo"), ceiling, fallback

    async def test_empty_at_ceiling_still_descends_to_the_next_rung(
        self, rows: list[dict[str, Any]]
    ) -> None:
        result, ceiling, fallback = await self._run(with_register=True)

        assert result == StageResult(
            content="from the dearer rung",
            provider_used="dashscope",
            model_used="max",
            attempt_count=2,
        )
        ceiling.complete.assert_awaited_once()  # type: ignore[attr-defined]
        fallback.complete.assert_awaited_once()  # type: ignore[attr-defined]
        assert _outcomes(rows) == [
            CallOutcome.EMPTY_AT_OUTPUT_CEILING,
            CallOutcome.ABANDONED,
            CallOutcome.SUCCESS,
        ]

    async def test_register_writes_do_not_change_the_result(
        self, rows: list[dict[str, Any]]
    ) -> None:
        with_register, _, _ = await self._run(with_register=True)
        without_register, _, _ = await self._run(with_register=False)

        assert with_register == without_register


# ── Output and full-input recording ─────────────────────────────────────


class TestRecording:
    async def test_output_is_recorded_only_on_stages_that_enable_it(
        self, rows: list[dict[str, Any]]
    ) -> None:
        for record_output in (False, True):
            router = _router(
                _config(("anthropic", "a"), record_output=record_output),
                {"anthropic": _provider(_response('{"ok": true}'))},
            )
            await router.execute_for_stage("demo")

        assert [row["output_text"] for row in rows] == [None, '{"ok": true}']

    async def test_full_input_only_when_raised_and_it_hashes_to_the_row_hash(
        self, rows: list[dict[str, Any]]
    ) -> None:
        for record_full_input in (False, True):
            router = _router(
                _config(("anthropic", "a")),
                {"anthropic": _provider(_response())},
                record_full_input=record_full_input,
            )
            await router.execute_for_stage("demo")

        lowered, raised = rows
        assert lowered["input_text"] is None
        assert lowered["input_hash"] == raised["input_hash"]  # hash: always
        document = json.loads(raised["input_text"])
        assert document["user"] == "user-template"
        assert document["system"] == "sys-template"
        assert (
            hashlib.sha256(raised["input_text"].encode("utf-8")).hexdigest()
            == raised["input_hash"]
        )
