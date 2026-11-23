"""The stand of task 13, K4: a repeated attempt with the memory and without, offline.

The pair is ``tests/fixtures/attempt_memory/first.json`` and ``second.json`` —
synthetic, in the format ``scripts/record_evaluation_input.py`` writes; the
second attempt adds a docstring and a type check to the first. Here:

* the memory is production's own (``earlier_review.attempt_memory``) over
  the two works, and the request with it is the stage's — v2 with
  ``previous_attempt`` — beside v1's, both sent with the stage's seed;
* three calls a pair over a provider double, read as the stage reads them,
  and the comparison: the items the change touched and those it did not;
* a stale quote — the first work's line, not the second's — is counted;
* the script's dry run calls nothing, builds no provider and exits 0.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import pytest
from scripts import measure_attempt_memory

from course_supporter.homework import attempt_memory_measure as stand
from course_supporter.homework.path_config import ModelStage, load_path_config
from course_supporter.llm.error_categories import ErrorCategory
from course_supporter.llm.prompt_loader_md import load_prompt
from course_supporter.llm.providers.base import LLMProvider
from course_supporter.llm.registry import ModelRegistryConfig, load_registry
from course_supporter.llm.reproducibility import REPRODUCIBILITY_SEED
from course_supporter.llm.schemas import LLMRequest, LLMResponse

_REPO_ROOT = Path(__file__).resolve().parents[3]
_FIXTURES = _REPO_ROOT / "tests" / "fixtures" / "attempt_memory"
_FIRST = _FIXTURES / "first.json"
_SECOND = _FIXTURES / "second.json"
_NOW = uuid.UUID("00000000-0000-7000-8000-00000000000a")
_THEN = uuid.UUID("00000000-0000-7000-8000-00000000000b")


def _answer(*verdicts: tuple[str, str, str | None]) -> str:
    return json.dumps(
        {
            "verdicts": [
                {
                    "id": item,
                    "verdict": verdict,
                    "quote": text if verdict == "met" else None,
                    "missing": None if verdict == "met" else text,
                }
                for item, verdict, text in verdicts
            ]
        },
        ensure_ascii=False,
    )


_RAISE_NEGATIVE = 'raise ValueError("n must not be negative")'
_RAISE_TYPE = 'raise TypeError("n must be an int")'
_DOCSTRING = '"""Повертає n-те число Фібоначчі."""'

_FIRST_ANSWER = _answer(
    ("c1", "met", "def fibonacci(n):"),
    ("c2.p1", "met", _RAISE_NEGATIVE),
    ("c2.p2", "not_met", "Тип n не перевіряється."),
    ("c3", "not_met", "Докстрінга немає."),
)
_WITHOUT_ANSWER = _answer(
    ("c1", "met", "def fibonacci(n):"),
    # Without the memory the model changes its mind on an untouched item.
    ("c2.p1", "not_met", "Від'ємне n не перевіряється."),
    ("c2.p2", "met", _RAISE_TYPE),
    ("c3", "met", _DOCSTRING),
)
_WITH_ANSWER = _answer(
    ("c1", "met", "def fibonacci(n):"),
    ("c2.p1", "met", _RAISE_NEGATIVE),
    ("c2.p2", "met", _RAISE_TYPE),
    ("c3", "met", _DOCSTRING),
)


class _Provider(LLMProvider):
    """Answers in turn; keeps every request."""

    def __init__(self, *contents: str) -> None:
        super().__init__()
        self.provider_name = "dashscope"
        self._contents = list(contents)
        self.requests: list[LLMRequest] = []

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        return LLMResponse(
            content=self._contents.pop(0),
            provider="dashscope",
            model_id=request.model,
            tokens_in=len(request.prompt) // 4,
            tokens_out=200,
        )

    def classify_error(self, exc: Exception) -> ErrorCategory:
        return ErrorCategory.INFRASTRUCTURE

    def seed_supported(self, request: LLMRequest) -> bool:
        return True  # as DashScope, the first rung's


@pytest.fixture(scope="module")
def stage() -> ModelStage:
    config = load_path_config(_REPO_ROOT / "config" / "submission_paths.yaml")
    found = config.stages[stand.STAGE_NAME]
    assert isinstance(found, ModelStage)
    return found


@pytest.fixture(scope="module")
def registry() -> ModelRegistryConfig:
    return load_registry(_REPO_ROOT / "config" / "external_services.yaml")


@pytest.fixture()
def pair() -> stand.Pair:
    return stand.load_pair(_FIRST, _SECOND, label="p1")


async def _measure(
    pair: stand.Pair,
    stage: ModelStage,
    registry: ModelRegistryConfig,
    provider: LLMProvider | None,
) -> stand.PairResult:
    return await stand.measure_pair(
        pair,
        stage=stage,
        registry=registry,
        provider=provider,
        rewritten_share=0.5,
        list_now=_NOW,
        list_then=_THEN,
    )


class TestThePair:
    def test_the_records_are_one_task_and_one_list(self, pair: stand.Pair) -> None:
        assert not pair.task_changed
        assert not pair.criteria_changed

    def test_a_record_without_its_variables_is_refused(self, tmp_path: Path) -> None:
        broken = tmp_path / "broken.json"
        broken.write_text('{"items": []}', encoding="utf-8")

        with pytest.raises(ValueError, match="not a recorded evaluation input"):
            stand.load_pair(_FIRST, broken, label="p1")


class TestTheCalls:
    async def test_the_requests_are_the_stages_with_the_same_seed(
        self,
        pair: stand.Pair,
        stage: ModelStage,
        registry: ModelRegistryConfig,
    ) -> None:
        provider = _Provider(_FIRST_ANSWER, _WITHOUT_ANSWER, _WITH_ANSWER)

        result = await _measure(pair, stage, registry, provider)

        first, without, with_ = provider.requests
        assert {r.seed for r in provider.requests} == {REPRODUCIBILITY_SEED}
        assert "<previous_attempt_diff>" not in without.prompt
        assert "<previous_attempt_diff>" in with_.prompt
        assert "<previous_attempt_verdicts>" in with_.prompt
        # The request with the memory is the stage's: v2 from the record's
        # variables and ``previous_attempt``.
        assert result.memory is not None
        expected = load_prompt(stand.WITH, base_path=_REPO_ROOT).render(
            **stand.with_memory(pair.second, result.memory)
        )
        assert with_.prompt == expected.user
        assert with_.system_prompt == expected.system
        # Without the memory, v1 — byte for byte the second attempt's request
        # as production sent it before K4.
        calibrated = load_prompt(stand.WITHOUT, base_path=_REPO_ROOT).render(
            **pair.second
        )
        assert without.prompt == calibrated.user
        assert first.prompt != without.prompt

    async def test_the_memory_is_the_first_answers_verdicts_and_the_difference(
        self,
        pair: stand.Pair,
        stage: ModelStage,
        registry: ModelRegistryConfig,
    ) -> None:
        provider = _Provider(_FIRST_ANSWER, _WITHOUT_ANSWER, _WITH_ANSWER)

        result = await _measure(pair, stage, registry, provider)

        memory = result.memory
        assert memory is not None
        assert memory.verdicts is not None
        assert [(v.id, v.verdict.value) for v in memory.verdicts] == [
            ("c1", "met"),
            ("c2.p1", "met"),
            ("c2.p2", "not_met"),
            ("c3", "not_met"),
        ]
        assert memory.diff is not None
        assert f"+    {_DOCSTRING}" in memory.diff
        assert not memory.rewritten

    async def test_the_comparison_tells_the_touched_items_from_the_rest(
        self,
        pair: stand.Pair,
        stage: ModelStage,
        registry: ModelRegistryConfig,
    ) -> None:
        provider = _Provider(_FIRST_ANSWER, _WITHOUT_ANSWER, _WITH_ANSWER)

        result = await _measure(pair, stage, registry, provider)

        assert [
            (i.item, i.place, i.first, i.without, i.with_memory)
            for i in result.compared
        ] == [
            ("c1", "untouched", "met", "met", "met"),
            ("c2.p1", "untouched", "met", "not_met", "met"),
            ("c2.p2", "changed", "not_met", "met", "met"),
            ("c3", "changed", "not_met", "met", "met"),
        ]
        assert stand.tally(result.compared) == {
            "changed": {
                "items": 2,
                "kept_with_memory": 0,
                "kept_without": 0,
                "with_equals_without": 2,
                "stale_quotes": 0,
            },
            "untouched": {
                "items": 2,
                "kept_with_memory": 2,
                "kept_without": 1,
                "with_equals_without": 1,
                "stale_quotes": 0,
            },
        }

    async def test_a_stale_quote_is_counted(
        self,
        pair: stand.Pair,
        stage: ModelStage,
        registry: ModelRegistryConfig,
        tmp_path: Path,
    ) -> None:
        # The second work without the negative check: the first's line is gone,
        # and the answer with the memory quotes it all the same.
        second = dict(pair.second)
        second["submission_text"] = second["submission_text"].replace(
            f"    if n < 0:\n        {_RAISE_NEGATIVE}\n", ""
        )
        changed = stand.Pair("p2", pair.first, second)
        provider = _Provider(_FIRST_ANSWER, _WITHOUT_ANSWER, _WITH_ANSWER)

        result = await _measure(changed, stage, registry, provider)

        [p1] = [i for i in result.compared if i.item == "c2.p1"]
        assert (p1.place, p1.with_memory, p1.stale_quote) == (
            "changed",
            "unconfirmed",
            True,
        )
        summary = stand.write_measurement(tmp_path, [result], dry_run=False)
        written = json.loads(summary.read_text(encoding="utf-8"))
        assert written["tally"]["changed"]["stale_quotes"] == 1
        assert written["pairs"][0]["input_growth"]["chars"] > 0
        assert (tmp_path / "p2.second.v2.answer.json").exists()


class TestTheScriptsDryRun:
    async def test_it_calls_nothing_and_exits_0(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def no_providers(*_args: Any) -> None:
            raise AssertionError("a dry run builds no provider")

        monkeypatch.setattr(measure_attempt_memory, "create_providers", no_providers)
        monkeypatch.chdir(_REPO_ROOT)
        args = measure_attempt_memory._arguments(
            ["--pair", str(_FIRST), str(_SECOND), "--dry-run"]
        )

        code = await measure_attempt_memory._main(args)

        out = capsys.readouterr().out
        assert code == 0
        assert "p1: verdicts 4, diff " in out
        for label in ("first.v1", "second.v1", "second.v2"):
            assert f"  {label} prompts/criteria_evaluation/" in out

    async def test_a_missing_record_is_refused_with_2(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.chdir(_REPO_ROOT)
        args = measure_attempt_memory._arguments(
            ["--pair", str(_FIRST), str(tmp_path / "none.json"), "--dry-run"]
        )

        assert await measure_attempt_memory._main(args) == 2
