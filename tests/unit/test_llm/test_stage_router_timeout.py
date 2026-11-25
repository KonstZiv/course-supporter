"""A client timeout is repeated on a rung once; other infrastructure errors thrice.

2026-11-24 (DD-SP-DJ): a provider's own client timeout (``is_timeout``) is
already a full wait, so the router repeats it at most ``max_retries_timeout``
times (default 1) on a rung, counted apart from the general limit. The general
limit (``1 + max_retries_infrastructure`` attempts per rung) is unchanged and
is not bypassed by alternating timeouts with 429/503.

No network: a scripted provider that raises or answers in order, with the two
error shapes the router needs told apart (a "busy" answer from the server and
a client timeout), and the register's ``_persist`` replaced by a recorder.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock

import pytest

from course_supporter.call_outcome import CallOutcome, RequestKind
from course_supporter.llm.error_categories import ErrorCategory, LadderExhaustedError
from course_supporter.llm.ladder_config import LadderConfig, LadderEntry, StageConfig
from course_supporter.llm.prompt_loader_md import StagePrompt
from course_supporter.llm.providers.base import LLMProvider
from course_supporter.llm.providers.dashscope import (
    DashScopeProvider,
    DashScopeResponseError,
)
from course_supporter.llm.schemas import LLMRequest, LLMResponse
from course_supporter.llm.stage_router import StageRouter
from tests._helpers.registry import empty_registry


class _ServerBusyError(Exception):
    """A fast answer of the server (the 429 / 503 shape): infrastructure."""


class _ClientTimeoutError(Exception):
    """The client gave up waiting: infrastructure, and a timeout."""


class _Scripted(LLMProvider):
    """Raises or answers as scripted, one step per call."""

    provider_name = "scripted"

    def __init__(self, *script: Exception | LLMResponse) -> None:
        super().__init__()
        self._script = list(script)
        self.calls = 0

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.calls += 1
        step = self._script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step

    def classify_error(self, exc: Exception) -> ErrorCategory:
        if isinstance(exc, (_ServerBusyError, _ClientTimeoutError)):
            return ErrorCategory.INFRASTRUCTURE
        return super().classify_error(exc)

    def is_timeout(self, exc: Exception) -> bool:
        return isinstance(exc, _ClientTimeoutError)


@pytest.fixture(autouse=True)
def _canned_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "course_supporter.llm.stage_router.load_prompt",
        lambda prompt_ref, *, base_path=None: StagePrompt(system="sys", user="user"),
    )


@pytest.fixture()
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    delays: list[float] = []

    async def _sleep(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr("asyncio.sleep", _sleep)
    return delays


@pytest.fixture(autouse=True)
def rows(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """The register rows the router would write, in order."""
    captured: list[dict[str, Any]] = []

    async def _fake_persist(_session_factory: Any, **kwargs: Any) -> uuid.UUID:
        row_id = uuid.uuid4()
        captured.append({**kwargs, "id": row_id})
        return row_id

    monkeypatch.setattr("course_supporter.llm.stage_router._persist", _fake_persist)
    return captured


def _answer(content: str = "answer") -> LLMResponse:
    return LLMResponse(
        content=content, provider="x", model_id="x", tokens_in=10, tokens_out=20
    )


def _stage(*providers: str) -> StageConfig:
    return StageConfig(
        prompt_ref="prompts/example/v1.md",
        ladder=[LadderEntry(provider=p, model=f"m-{p}") for p in providers],
    )


def _router(providers: dict[str, LLMProvider], **limits: int) -> StageRouter:
    return StageRouter(
        LadderConfig(),
        providers,
        registry=empty_registry(),
        session_factory=AsyncMock(),
        **limits,
    )


def _attempt_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rows of calls that were made (trace rows have no request kind)."""
    return [r for r in rows if r.get("request_kind") is not None]


class TestTwoTimeoutsEndTheRung:
    async def test_second_timeout_ends_the_rung_and_the_ladder_descends(
        self, sleeps: list[float], rows: list[dict[str, Any]]
    ) -> None:
        slow = _Scripted(_ClientTimeoutError("t1"), _ClientTimeoutError("t2"))
        spare = _Scripted(_answer("from the next rung"))

        result = await _router({"slow": slow, "spare": spare}).execute_stage(
            _stage("slow", "spare"), "demo"
        )

        assert slow.calls == 2  # not four
        assert sleeps == [1]  # one backoff, before the one repeat
        assert result.provider_used == "spare"
        assert result.content == "from the next rung"
        assert result.attempt_count == 3

    async def test_a_single_rung_fails_for_the_timeout_and_says_so(
        self, sleeps: list[float]
    ) -> None:
        slow = _Scripted(_ClientTimeoutError("t1"), _ClientTimeoutError("t2"))

        with pytest.raises(LadderExhaustedError) as caught:
            await _router({"slow": slow}).execute_stage(_stage("slow"), "demo")

        assert slow.calls == 2
        ((provider, _model, reason),) = caught.value.attempts
        assert provider == "slow"
        assert reason.startswith("INFRASTRUCTURE: timeout - ")
        assert "2 client timeout(s) in 2 attempt(s)" in reason
        assert "t2" in reason  # the last error is carried, as before

    async def test_a_timeout_then_success_answers_from_the_second_attempt(
        self, sleeps: list[float]
    ) -> None:
        provider = _Scripted(_ClientTimeoutError("t1"), _answer("recovered"))

        result = await _router({"p": provider}).execute_stage(_stage("p"), "demo")

        assert result.content == "recovered"
        assert result.attempt_count == 2
        assert provider.calls == 2
        assert sleeps == [1]


class TestOtherInfrastructureErrorsAreUnchanged:
    async def test_three_busy_answers_then_success_is_the_fourth_attempt(
        self, sleeps: list[float]
    ) -> None:
        provider = _Scripted(
            _ServerBusyError("1"),
            _ServerBusyError("2"),
            _ServerBusyError("3"),
            _answer("late"),
        )

        result = await _router({"p": provider}).execute_stage(_stage("p"), "demo")

        assert result.content == "late"
        assert result.attempt_count == 4
        assert provider.calls == 4
        assert sleeps == [1, 2, 4]

    async def test_four_busy_answers_exhaust_the_rung_with_the_old_reason(
        self, sleeps: list[float]
    ) -> None:
        provider = _Scripted(*(_ServerBusyError(str(i)) for i in range(4)))

        with pytest.raises(LadderExhaustedError) as caught:
            await _router({"p": provider}).execute_stage(_stage("p"), "demo")

        assert provider.calls == 4
        assert sleeps == [1, 2, 4]
        ((_, _, reason),) = caught.value.attempts
        assert reason == "INFRASTRUCTURE: 4 attempts exhausted - 3"

    async def test_a_dashscope_server_504_is_still_retried_thrice(
        self, sleeps: list[float]
    ) -> None:
        # HTTP 504 is the server's answer, not the client's timeout.
        dashscope = DashScopeProvider(api_keys=("k",), default_model="qwen-plus")
        dashscope.complete = AsyncMock(  # type: ignore[method-assign]
            side_effect=[
                *(
                    DashScopeResponseError(
                        status_code=504, code="Timeout", message="gateway"
                    )
                    for _ in range(3)
                ),
                _answer("fourth"),
            ]
        )

        result = await _router({"dashscope": dashscope}).execute_stage(
            _stage("dashscope"), "demo"
        )

        assert result.content == "fourth"
        assert dashscope.complete.await_count == 4
        assert sleeps == [1, 2, 4]

    async def test_dashscope_client_timeouts_stop_after_two(
        self, sleeps: list[float]
    ) -> None:
        dashscope = DashScopeProvider(api_keys=("k",), default_model="qwen-plus")
        dashscope.complete = AsyncMock(  # type: ignore[method-assign]
            side_effect=[TimeoutError(), TimeoutError(), _answer("never")]
        )
        spare = _Scripted(_answer("next rung"))

        result = await _router({"dashscope": dashscope, "spare": spare}).execute_stage(
            _stage("dashscope", "spare"), "demo"
        )

        assert dashscope.complete.await_count == 2
        assert result.provider_used == "spare"


class TestTimeoutsAreCountedApart:
    async def test_busy_timeout_busy_timeout_ends_on_the_fourth_attempt(
        self, sleeps: list[float]
    ) -> None:
        provider = _Scripted(
            _ServerBusyError("b1"),
            _ClientTimeoutError("t1"),
            _ServerBusyError("b2"),
            _ClientTimeoutError("t2"),
        )

        with pytest.raises(LadderExhaustedError) as caught:
            await _router({"p": provider}).execute_stage(_stage("p"), "demo")

        # Neither earlier (the first timeout is repeated) nor later (a fifth
        # attempt would break the general limit).
        assert provider.calls == 4
        assert sleeps == [1, 2, 4]
        ((_, _, reason),) = caught.value.attempts
        assert reason.startswith("INFRASTRUCTURE: timeout - ")
        assert "2 client timeout(s) in 4 attempt(s)" in reason

    async def test_a_busy_answer_between_two_timeouts_does_not_reset_the_count(
        self, sleeps: list[float]
    ) -> None:
        provider = _Scripted(
            _ClientTimeoutError("t1"),
            _ServerBusyError("b1"),
            _ClientTimeoutError("t2"),
            _answer(),
        )

        with pytest.raises(LadderExhaustedError):
            await _router({"p": provider}).execute_stage(_stage("p"), "demo")

        # The second timeout ends the rung on the third attempt: the general
        # limit (four) is not what stopped it.
        assert provider.calls == 3

    async def test_the_general_limit_still_caps_a_larger_timeout_limit(
        self, sleeps: list[float]
    ) -> None:
        provider = _Scripted(*(_ClientTimeoutError(str(i)) for i in range(4)))

        with pytest.raises(LadderExhaustedError) as caught:
            await _router({"p": provider}, max_retries_timeout=5).execute_stage(
                _stage("p"), "demo"
            )

        assert provider.calls == 4  # 1 + max_retries_infrastructure, not 6
        ((_, _, reason),) = caught.value.attempts
        assert "timeout" in reason

    async def test_a_timeout_limit_of_zero_ends_the_rung_at_the_first_timeout(
        self, sleeps: list[float]
    ) -> None:
        provider = _Scripted(_ClientTimeoutError("t1"), _answer())

        with pytest.raises(LadderExhaustedError):
            await _router({"p": provider}, max_retries_timeout=0).execute_stage(
                _stage("p"), "demo"
            )

        assert provider.calls == 1
        assert sleeps == []

    async def test_no_infrastructure_retries_means_no_timeout_retry_either(
        self, sleeps: list[float]
    ) -> None:
        # The explanation measure sets ``max_retries_infrastructure=0``.
        provider = _Scripted(_ClientTimeoutError("t1"), _answer())

        with pytest.raises(LadderExhaustedError) as caught:
            await _router({"p": provider}, max_retries_infrastructure=0).execute_stage(
                _stage("p"), "demo"
            )

        assert provider.calls == 1
        ((_, _, reason),) = caught.value.attempts
        assert reason.startswith("INFRASTRUCTURE: timeout - ")

    async def test_the_count_is_per_rung(self, sleeps: list[float]) -> None:
        first = _Scripted(_ClientTimeoutError("a1"), _ClientTimeoutError("a2"))
        second = _Scripted(_ClientTimeoutError("b1"), _answer("second try"))

        result = await _router({"a": first, "b": second}).execute_stage(
            _stage("a", "b"), "demo"
        )

        assert (first.calls, second.calls) == (2, 2)
        assert result.provider_used == "b"
        assert result.content == "second try"


class TestTheRegisterHasOneRowPerCall:
    async def test_two_timeouts_are_two_rows_of_the_same_kinds_as_before(
        self, sleeps: list[float], rows: list[dict[str, Any]]
    ) -> None:
        provider = _Scripted(_ClientTimeoutError("t1"), _ClientTimeoutError("t2"))

        with pytest.raises(LadderExhaustedError):
            await _router({"p": provider}).execute_stage(_stage("p"), "demo")

        attempts = _attempt_rows(rows)
        assert len(attempts) == provider.calls == 2
        assert [r["request_kind"] for r in attempts] == [
            RequestKind.FIRST,
            RequestKind.INFRA_RETRY,
        ]
        assert [r["outcome"] for r in attempts] == [CallOutcome.TRANSPORT_ERROR] * 2
        assert all(r["success"] is False for r in attempts)

    async def test_a_timeout_then_success_writes_a_failed_and_an_answered_row(
        self, sleeps: list[float], rows: list[dict[str, Any]]
    ) -> None:
        provider = _Scripted(_ClientTimeoutError("t1"), _answer())

        await _router({"p": provider}).execute_stage(_stage("p"), "demo")

        assert [(r["request_kind"], r["outcome"]) for r in _attempt_rows(rows)] == [
            (RequestKind.FIRST, CallOutcome.TRANSPORT_ERROR),
            (RequestKind.INFRA_RETRY, CallOutcome.SUCCESS),
        ]

    async def test_mixed_errors_write_as_many_rows_as_calls(
        self, sleeps: list[float], rows: list[dict[str, Any]]
    ) -> None:
        provider = _Scripted(
            _ServerBusyError("b1"),
            _ClientTimeoutError("t1"),
            _ServerBusyError("b2"),
            _ClientTimeoutError("t2"),
        )

        with pytest.raises(LadderExhaustedError):
            await _router({"p": provider}).execute_stage(_stage("p"), "demo")

        attempts = _attempt_rows(rows)
        assert len(attempts) == provider.calls == 4
        assert [r["request_kind"] for r in attempts] == [
            RequestKind.FIRST,
            RequestKind.INFRA_RETRY,
            RequestKind.INFRA_RETRY,
            RequestKind.INFRA_RETRY,
        ]
