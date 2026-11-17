"""What the router records about each request, and the call it answers from.

2026-11-16: every attempt row says which request of the stage it is
(``request_kind``), where its rung stands on the stage's ladder
(``rung_index``), and what seed and temperature went out; a trace row names
its rung and no request kind. :class:`StageResult` names the attempt row its
answer came from (``call_id``). None of it changes what is sent or which rung
answers — the tests assert the router's own result beside each row.

Every test captures the ``_persist`` keyword arguments — the row the router
would write — with a fake that hands back an id, as the real one does for a
row it wrote.
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from course_supporter.call_outcome import CallOutcome, RequestKind
from course_supporter.llm.error_categories import (
    ErrorCategory,
    LadderExhaustedError,
    StructuralRetryError,
)
from course_supporter.llm.ladder_config import LadderConfig, LadderEntry, StageConfig
from course_supporter.llm.prompt_loader_md import StagePrompt
from course_supporter.llm.providers.base import LLMProvider
from course_supporter.llm.schemas import LLMResponse
from course_supporter.llm.stage_router import StageExecution, StageRouter
from tests._helpers.registry import empty_registry

_TEMPLATE = StagePrompt(system="sys", user="user")


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
    """The rows written, each with the id the fake gave it under ``"id"``."""
    captured: list[dict[str, Any]] = []

    async def _fake_persist(_session_factory: Any, **kwargs: Any) -> uuid.UUID:
        row_id = uuid.uuid4()
        captured.append({**kwargs, "id": row_id})
        return row_id

    monkeypatch.setattr("course_supporter.llm.stage_router._persist", _fake_persist)
    return captured


def _stage(*entries: tuple[str, str]) -> StageConfig:
    return StageConfig(
        prompt_ref="prompts/example/v1.md",
        ladder=[LadderEntry(provider=p, model=m) for p, m in entries],
    )


def _response(content: str = "answer") -> LLMResponse:
    return LLMResponse(
        content=content, provider="x", model_id="x", tokens_in=10, tokens_out=20
    )


def _provider(
    *side_effects: Any, classify_as: ErrorCategory = ErrorCategory.SEMANTIC
) -> LLMProvider:
    provider = AsyncMock(spec=LLMProvider)
    provider.enabled = True
    provider.complete = AsyncMock(side_effect=list(side_effects))
    provider.classify_error = lambda _exc, _cat=classify_as: _cat
    provider.check_request = lambda _request: None
    return provider  # type: ignore[return-value]


def _router(
    providers: dict[str, LLMProvider], *, with_register: bool = True
) -> StageRouter:
    return StageRouter(
        LadderConfig(),
        providers,
        registry=empty_registry(),
        session_factory=AsyncMock() if with_register else None,
    )


def _kinds(rows: list[dict[str, Any]]) -> list[tuple[Any, Any, Any]]:
    return [(r["outcome"], r.get("request_kind"), r["rung_index"]) for r in rows]


class TestRequestKindAndRung:
    async def test_a_first_request_answered(self, rows: list[dict[str, Any]]) -> None:
        router = _router({"a": _provider(_response())})

        result = await router.execute_stage(_stage(("a", "m1")), "demo")

        (row,) = rows
        assert (row["request_kind"], row["rung_index"]) == (RequestKind.FIRST, 1)
        assert result.provider_used == "a"

    async def test_an_infrastructure_retry_is_told_from_the_first(
        self, rows: list[dict[str, Any]]
    ) -> None:
        provider = _provider(
            TimeoutError("slow"),
            _response(),
            classify_as=ErrorCategory.INFRASTRUCTURE,
        )

        result = await _router({"a": provider}).execute_stage(
            _stage(("a", "m1")), "demo"
        )

        assert _kinds(rows) == [
            (CallOutcome.TRANSPORT_ERROR, RequestKind.FIRST, 1),
            (CallOutcome.SUCCESS, RequestKind.INFRA_RETRY, 1),
        ]
        assert result.attempt_count == 2

    async def test_a_structural_retry_is_told_from_the_first(
        self, rows: list[dict[str, Any]]
    ) -> None:
        seen: list[str] = []

        def _validator(content: str) -> None:
            seen.append(content)
            if len(seen) == 1:
                raise StructuralRetryError("not JSON")

        provider = _provider(_response("bad"), _response("good"))

        result = await _router({"a": provider}).execute_stage(
            _stage(("a", "m1")), "demo", response_validator=_validator
        )

        assert _kinds(rows) == [
            (CallOutcome.INVALID_CONTENT, RequestKind.FIRST, 1),
            (CallOutcome.SUCCESS, RequestKind.STRUCTURAL_RETRY, 1),
        ]
        assert result.content == "good"

    async def test_each_rung_has_its_position_and_a_trace_no_kind(
        self, rows: list[dict[str, Any]]
    ) -> None:
        router = _router({"a": _provider(_response("")), "b": _provider(_response())})

        result = await router.execute_stage(
            _stage(("missing", "m0"), ("a", "m1"), ("b", "m2")), "demo"
        )

        assert _kinds(rows) == [
            (CallOutcome.SKIPPED, None, 1),
            (CallOutcome.EMPTY, RequestKind.FIRST, 2),
            (CallOutcome.ABANDONED, None, 2),
            (CallOutcome.SUCCESS, RequestKind.FIRST, 3),
        ]
        assert result.provider_used == "b"

    async def test_a_trace_carries_no_seed_and_no_temperature(
        self, rows: list[dict[str, Any]]
    ) -> None:
        await _router({"a": _provider(_response())}).execute_stage(
            _stage(("missing", "m0"), ("a", "m1")), "demo"
        )

        trace = rows[0]
        assert trace["outcome"] is CallOutcome.SKIPPED
        assert "seed" not in trace and "temperature" not in trace


class TestTheItemRepeat:
    async def test_its_rows_keep_the_rung_it_was_sent_to(
        self, rows: list[dict[str, Any]]
    ) -> None:
        provider = _provider(
            TimeoutError("slow"),
            _response(),
            classify_as=ErrorCategory.INFRASTRUCTURE,
        )

        await _router({"b": provider}).execute_stage(
            _stage(("b", "m2")), "demo", repeat_on_rung=2
        )

        assert _kinds(rows) == [
            (CallOutcome.TRANSPORT_ERROR, RequestKind.ITEM_REPEAT, 2),
            (CallOutcome.SUCCESS, RequestKind.INFRA_RETRY, 2),
        ]

    async def test_an_abandoned_repeat_keeps_the_position_on_its_trace(
        self, rows: list[dict[str, Any]]
    ) -> None:
        with pytest.raises(LadderExhaustedError):
            await _router({"b": _provider(_response(""))}).execute_stage(
                _stage(("b", "m2")), "demo", repeat_on_rung=3
            )

        assert _kinds(rows) == [
            (CallOutcome.EMPTY, RequestKind.ITEM_REPEAT, 3),
            (CallOutcome.ABANDONED, None, 3),
        ]

    @pytest.mark.parametrize(
        ("ladder", "position"),
        [((("a", "m1"), ("b", "m2")), 1), ((("a", "m1"),), 0)],
        ids=["two-rungs", "position-0"],
    )
    async def test_a_repeat_not_on_one_rung_is_refused_before_any_call(
        self,
        rows: list[dict[str, Any]],
        ladder: tuple[tuple[str, str], ...],
        position: int,
    ) -> None:
        provider = _provider(_response())

        with pytest.raises(ValueError, match="one rung"):
            await _router({"a": provider, "b": provider}).execute_stage(
                _stage(*ladder), "demo", repeat_on_rung=position
            )

        assert rows == []

    async def test_an_execution_hands_its_repeat_to_the_router(
        self, rows: list[dict[str, Any]]
    ) -> None:
        execution = StageExecution(stage=_stage(("a", "m1")), stage_name="demo")
        router = _router({"a": _provider(_response(), _response())})

        await execution.run(router)
        await replace(execution, repeat_on_rung=4).run(router)

        assert [r["request_kind"] for r in rows] == [
            RequestKind.FIRST,
            RequestKind.ITEM_REPEAT,
        ]
        assert [r["rung_index"] for r in rows] == [1, 4]


class TestTheCallAnAnswerCameFrom:
    async def test_the_answering_attempts_row(self, rows: list[dict[str, Any]]) -> None:
        router = _router({"a": _provider(_response("")), "b": _provider(_response())})

        result = await router.execute_stage(_stage(("a", "m1"), ("b", "m2")), "demo")

        answered = [r for r in rows if r["outcome"] is CallOutcome.SUCCESS]
        assert result.call_id == answered[0]["id"]

    async def test_after_a_structural_retry_it_is_the_retrys_row(
        self, rows: list[dict[str, Any]]
    ) -> None:
        calls: list[str] = []

        def _validator(content: str) -> None:
            calls.append(content)
            if len(calls) == 1:
                raise StructuralRetryError("not JSON")

        result = await _router(
            {"a": _provider(_response("bad"), _response("good"))}
        ).execute_stage(_stage(("a", "m1")), "demo", response_validator=_validator)

        assert result.call_id == rows[1]["id"]
        assert rows[1]["request_kind"] is RequestKind.STRUCTURAL_RETRY

    async def test_none_without_a_register(self) -> None:
        router = _router({"a": _provider(_response())}, with_register=False)

        result = await router.execute_stage(_stage(("a", "m1")), "demo")

        assert result.call_id is None

    async def test_none_when_the_row_was_not_written(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _dropped(_session_factory: Any, **_kwargs: Any) -> None:
            return None

        monkeypatch.setattr("course_supporter.llm.stage_router._persist", _dropped)

        result = await _router({"a": _provider(_response())}).execute_stage(
            _stage(("a", "m1")), "demo"
        )

        assert result.call_id is None
        assert result.content == "answer"
