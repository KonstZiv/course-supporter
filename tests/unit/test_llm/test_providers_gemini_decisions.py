"""The decision-model connector: wire, errors, ladder behaviour (task 12a, K3).

No network: ``httpx.MockTransport`` answers in place of Google. The ladder
tests run the real :class:`StageRouter` over the real connector, with the
register's ``_persist`` replaced by a recorder and the sleep of the backoff
removed.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest
import structlog

from course_supporter.llm.error_categories import ErrorCategory, LadderExhaustedError
from course_supporter.llm.ladder_config import LadderConfig, LadderEntry, StageConfig
from course_supporter.llm.prompt_loader_md import StagePrompt
from course_supporter.llm.providers.base import RequestConfigError
from course_supporter.llm.providers.gemini_decisions import (
    DECISIONS_URL,
    REQUEST_TIMEOUT_S,
    GeminiDecisionsError,
    GeminiDecisionsProvider,
    GeminiDecisionsResponseError,
)
from course_supporter.llm.schemas import LLMRequest
from course_supporter.llm.stage_router import StageRouter
from tests._helpers.registry import empty_registry

_KEY = "AIza-test-key-0001"
_KEY_2 = "AIza-test-key-0002"
_QUESTIONS = {
    "topic": {
        "type": "choice",
        "instructions": "Is it about the work?",
        "criteria": {"on_topic": "yes", "off_topic": "no"},
    },
    "influence": {"type": "noul", "instructions": "An attempt on the grade?"},
}
_PROMPT = json.dumps({"input": "Завдання: «T». C", "questions": _QUESTIONS})
_ANSWERS = {
    "topic": {
        "type": "choice",
        "choice": "off_topic",
        "probabilities": {"on_topic": 0.08, "off_topic": 0.92},
        "confidence": 0.92,
    },
    "influence": {"type": "noul", "noul": 0.97, "confidence": 0.97},
}
_OK = {
    "answers": _ANSWERS,
    "usage": {"total_input_tokens": 812, "total_output_tokens": 0},
}


def _request(prompt: str = _PROMPT, **overrides: Any) -> LLMRequest:
    return LLMRequest(
        prompt=prompt, model="gemini-decision-1", action="reply_topic", **overrides
    )


class _Wire:
    """A scripted Google: answers in order, keeps every request it saw."""

    def __init__(self, *steps: httpx.Response | Exception) -> None:
        self.steps = list(steps)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        step = self.steps.pop(0) if len(self.steps) > 1 else self.steps[0]
        if isinstance(step, Exception):
            raise step
        return step

    def provider(self, *keys: str) -> GeminiDecisionsProvider:
        return GeminiDecisionsProvider(
            list(keys or (_KEY,)),
            "gemini-decision-1",
            transport=httpx.MockTransport(self),
        )


def _ok() -> httpx.Response:
    return httpx.Response(200, json=_OK)


def _refusal(status: int, code: str | None = None) -> httpx.Response:
    body = {"error": {"message": "the quoted input", "status": "X"}}
    if code is not None:
        body["error"]["code"] = code
    return httpx.Response(status, json=body)


# ── The wire ────────────────────────────────────────────────────────


class TestTheRequest:
    async def test_the_body_is_the_prompt_with_the_model_added(self) -> None:
        wire = _Wire(_ok())

        await wire.provider().complete(_request())

        (sent,) = wire.requests
        assert sent.method == "POST"
        assert str(sent.url) == DECISIONS_URL
        assert json.loads(sent.content) == {
            "model": "gemini-decision-1",
            "input": "Завдання: «T». C",
            "questions": _QUESTIONS,
        }

    async def test_the_key_and_the_content_type_are_headers(self) -> None:
        wire = _Wire(_ok())

        await wire.provider().complete(_request())

        (sent,) = wire.requests
        assert sent.headers["x-goog-api-key"] == _KEY
        assert sent.headers["content-type"] == "application/json"
        assert _KEY not in str(sent.url)

    async def test_the_call_times_out_after_fifteen_seconds(self) -> None:
        wire = _Wire(_ok())

        await wire.provider().complete(_request())

        assert REQUEST_TIMEOUT_S == 15.0
        assert wire.requests[0].extensions["timeout"] == {
            "connect": 15.0,
            "read": 15.0,
            "write": 15.0,
            "pool": 15.0,
        }

    async def test_a_pool_of_keys_is_used_round_robin(self) -> None:
        wire = _Wire(_ok())
        provider = wire.provider(_KEY, _KEY_2)

        for _ in range(3):
            await provider.complete(_request())

        assert [r.headers["x-goog-api-key"] for r in wire.requests] == [
            _KEY,
            _KEY_2,
            _KEY,
        ]

    async def test_the_model_of_the_request_is_the_one_sent(self) -> None:
        wire = _Wire(_ok())
        request = _request().model_copy(update={"model": ""})

        response = await wire.provider().complete(request)

        assert json.loads(wire.requests[0].content)["model"] == "gemini-decision-1"
        assert response.model_id == "gemini-decision-1"


class TestTheAnswer:
    async def test_content_is_the_answers_and_input_tokens_are_the_usage(self) -> None:
        response = await _Wire(_ok()).provider().complete(_request())

        assert json.loads(response.content) == _ANSWERS
        assert response.tokens_in == 812
        assert response.tokens_out == 0
        assert response.provider == "gemini_decisions"
        assert response.model_id == "gemini-decision-1"

    async def test_content_keeps_non_ascii_text_as_it_is(self) -> None:
        body = {"answers": {"topic": {"choice": "про роботу"}}, "usage": {}}

        response = (
            await _Wire(httpx.Response(200, json=body)).provider().complete(_request())
        )

        assert "про роботу" in response.content

    async def test_a_usage_without_input_tokens_is_not_reported(self) -> None:
        body = {"answers": _ANSWERS}

        response = (
            await _Wire(httpx.Response(200, json=body)).provider().complete(_request())
        )

        assert response.tokens_in is None

    @pytest.mark.parametrize(
        "body",
        [{"usage": {}}, {"answers": []}, {"answers": "x"}, ["answers"]],
    )
    async def test_an_answer_without_answers_is_an_error(self, body: Any) -> None:
        provider = _Wire(httpx.Response(200, json=body)).provider()

        with pytest.raises(GeminiDecisionsResponseError):
            await provider.complete(_request())

    async def test_a_body_that_is_not_json_is_an_error(self) -> None:
        provider = _Wire(httpx.Response(200, text="<html>")).provider()

        with pytest.raises(GeminiDecisionsResponseError):
            await provider.complete(_request())

    def test_such_an_error_is_semantic(self) -> None:
        provider = _Wire().provider()

        error = GeminiDecisionsResponseError("x")

        assert provider.classify_error(error) is ErrorCategory.SEMANTIC


class TestARequestTheWireCannotCarry:
    @pytest.mark.parametrize(
        "prompt",
        [
            "not json",
            "[]",
            json.dumps({"input": "x"}),
            json.dumps({"input": "x", "questions": {}, "extra": 1}),
            json.dumps({"model": "m", "input": "x", "questions": {}}),
        ],
    )
    def test_a_prompt_that_is_not_the_body_is_refused(self, prompt: str) -> None:
        with pytest.raises(RequestConfigError) as raised:
            _Wire().provider().check_request(_request(prompt))

        # The message names the fault, never the prompt: it holds a student's words.
        assert prompt not in str(raised.value)

    def test_a_system_prompt_is_refused(self) -> None:
        with pytest.raises(RequestConfigError, match="system prompt"):
            _Wire().provider().check_request(_request(system_prompt="be kind"))

    def test_contents_are_refused(self) -> None:
        with pytest.raises(RequestConfigError, match="text only"):
            _Wire().provider().check_request(_request(contents=[b"png"]))

    def test_a_seed_is_refused_as_on_every_connector_without_one(self) -> None:
        with pytest.raises(RequestConfigError, match="seed"):
            _Wire().provider().check_request(_request(seed=7))

    def test_the_body_the_prompt_holds_is_accepted(self) -> None:
        _Wire().provider().check_request(_request())


# ── Errors: categories, timeouts, secrets ───────────────────────────


def _error_of(response: httpx.Response) -> GeminiDecisionsError:
    provider = _Wire(response).provider()
    with pytest.raises(GeminiDecisionsError) as raised:
        import asyncio

        asyncio.run(provider.complete(_request()))
    return raised.value


class TestCategories:
    @pytest.mark.parametrize("status", [408, 429, 500, 502, 503, 504])
    def test_the_moments_errors_are_infrastructure(self, status: int) -> None:
        error = _error_of(_refusal(status))

        assert _Wire().provider().classify_error(error) is ErrorCategory.INFRASTRUCTURE

    @pytest.mark.parametrize(
        "code",
        [
            "safety",
            "prohibited_content",
            "blocklist",
            "image_safety",
            "content_blocked",
        ],
    )
    def test_a_refusal_by_googles_safety_is_semantic(self, code: str) -> None:
        error = _error_of(_refusal(400, code))

        assert error.error_code == code
        assert _Wire().provider().classify_error(error) is ErrorCategory.SEMANTIC

    @pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
    def test_any_other_4xx_is_semantic(self, status: int) -> None:
        error = _error_of(_refusal(status))

        assert _Wire().provider().classify_error(error) is ErrorCategory.SEMANTIC

    @pytest.mark.parametrize(
        "factory",
        [
            lambda: httpx.ReadTimeout("slow"),
            lambda: httpx.ConnectTimeout("slow"),
            lambda: httpx.ConnectError("down"),
            lambda: httpx.ReadError("cut"),
            lambda: TimeoutError(),
        ],
    )
    def test_network_errors_and_timeouts_are_infrastructure(
        self, factory: Callable[[], Exception]
    ) -> None:
        assert (
            _Wire().provider().classify_error(factory()) is ErrorCategory.INFRASTRUCTURE
        )

    def test_an_unknown_exception_is_semantic(self) -> None:
        assert _Wire().provider().classify_error(ValueError("?")) is (
            ErrorCategory.SEMANTIC
        )


class TestIsTimeout:
    @pytest.mark.parametrize(
        ("factory", "expected"),
        [
            (lambda: httpx.ReadTimeout("slow"), True),
            (lambda: httpx.ConnectTimeout("slow"), True),
            (lambda: httpx.PoolTimeout("slow"), True),
            (lambda: TimeoutError(), True),
            (lambda: httpx.ConnectError("down"), False),
            (lambda: _error_of(_refusal(429)), False),
            (lambda: _error_of(_refusal(503)), False),
            (lambda: _error_of(_refusal(408)), False),
            (lambda: _error_of(_refusal(504)), False),
        ],
    )
    def test_only_the_clients_own_timeout(
        self, factory: Callable[[], Exception], expected: bool
    ) -> None:
        assert _Wire().provider().is_timeout(factory()) is expected

    async def test_a_timeout_on_the_wire_is_what_complete_raises(self) -> None:
        provider = _Wire(httpx.ReadTimeout("slow")).provider()

        with pytest.raises(httpx.ReadTimeout) as raised:
            await provider.complete(_request())

        assert provider.is_timeout(raised.value) is True


class TestTheKeyStaysInTheHeader:
    @pytest.mark.parametrize("status", [400, 401, 429, 503])
    def test_an_error_text_names_the_status_and_the_code_alone(
        self, status: int
    ) -> None:
        error = _error_of(_refusal(status, "safety"))

        assert _KEY not in str(error)
        assert "the quoted input" not in str(error)
        assert str(error).startswith(f"gemini decisions: HTTP {status}")

    async def test_the_logs_of_a_call_do_not_hold_the_key(self) -> None:
        wire = _Wire(_refusal(429), _ok())
        with structlog.testing.capture_logs() as logs:
            with pytest.raises(GeminiDecisionsError):
                await wire.provider().complete(_request())
            await wire.provider().complete(_request())

        assert _KEY not in json.dumps(logs, default=str)

    async def test_a_transport_error_text_does_not_hold_the_key(self) -> None:
        provider = _Wire(
            httpx.ConnectError("All connection attempts failed")
        ).provider()

        with pytest.raises(httpx.ConnectError) as raised:
            await provider.complete(_request())

        assert _KEY not in str(raised.value)


# ── The ladder over the connector ───────────────────────────────────


@pytest.fixture(autouse=True)
def _canned_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "course_supporter.llm.stage_router.load_prompt",
        lambda prompt_ref, *, base_path=None: StagePrompt(user=_PROMPT),
    )


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr("asyncio.sleep", _sleep)


def _router(wire: _Wire) -> StageRouter:
    config = LadderConfig(
        stages={
            "reply_topic": StageConfig(
                prompt_ref="prompts/reply_topic/v1.md",
                ladder=[
                    LadderEntry(provider="gemini_decisions", model="gemini-decision-1")
                ],
            )
        }
    )
    return StageRouter(
        config,
        {"gemini_decisions": wire.provider()},
        registry=empty_registry(),
    )


class TestTheRouterRunsIt:
    @pytest.mark.parametrize("busy", [429, 503])
    async def test_a_busy_answer_is_repeated_on_the_rung(self, busy: int) -> None:
        wire = _Wire(_refusal(busy), _refusal(busy), _ok())

        result = await _router(wire).execute_for_stage("reply_topic")

        assert json.loads(result.content) == _ANSWERS
        assert len(wire.requests) == 3
        assert result.attempt_count == 3

    @pytest.mark.parametrize("code", ["safety", "prohibited_content", "blocklist"])
    async def test_a_safety_refusal_is_asked_once_and_ends_the_ladder(
        self, code: str
    ) -> None:
        wire = _Wire(_refusal(400, code), _ok())

        with pytest.raises(LadderExhaustedError) as raised:
            await _router(wire).execute_for_stage("reply_topic")

        assert len(wire.requests) == 1
        assert "HTTP 400" in str(raised.value)
        assert code in str(raised.value)

    async def test_a_client_timeout_is_repeated_once_not_three_times(self) -> None:
        wire = _Wire(httpx.ReadTimeout("slow"))

        with pytest.raises(LadderExhaustedError) as raised:
            await _router(wire).execute_for_stage("reply_topic")

        assert len(wire.requests) == 2
        assert "timeout" in str(raised.value)

    async def test_a_gateway_timeout_status_is_an_ordinary_busy_answer(self) -> None:
        wire = _Wire(_refusal(504))

        with pytest.raises(LadderExhaustedError):
            await _router(wire).execute_for_stage("reply_topic")

        # 1 + max_retries_infrastructure (3): not cut short as a client timeout.
        assert len(wire.requests) == 4
