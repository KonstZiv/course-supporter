"""``scripts/measure_determinism.py`` on a fake provider: no network (2026-11-13).

The fifth lock of the ``deterministic`` change: the summary counts the different
sets of verdicts per mode. The provider answers the seeded requests the same
way every time and the unseeded ones two ways, so the summary must say 1 and 2.
The stage, its prompt and the prices are the repository's own
(``config/submission_paths.yaml``, ``prompts/``, ``config/external_services.yaml``),
so the script is exercised on the request the router really builds.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from scripts import measure_determinism as script

from course_supporter.homework.path_config import load_path_config, model_stages
from course_supporter.llm.finish_reason import FinishReason
from course_supporter.llm.providers.base import LLMProvider
from course_supporter.llm.registry import load_registry
from course_supporter.llm.reproducibility import REPRODUCIBILITY_SEED
from course_supporter.llm.schemas import LLMRequest, LLMResponse

_ITEMS = ["c1", "c2"]
_VARIABLES: dict[str, Any] = {
    "task_title": "Sum of a list",
    "task_description": "Write a function.",
    "task_text": "Write total(xs) that returns the sum of xs.",
    "criteria": [
        {"id": "c1", "weight": "must", "text": "Defines total", "evidence": "code"},
        {"id": "c2", "weight": "should", "text": "Has a docstring", "evidence": ""},
    ],
    "items": _ITEMS,
    "repeat": [],
    "submission_text": "def total(xs):\n    return sum(xs)\n",
    "language": "English",
    "max_quote_chars": 300,
    "min_quote_chars": 10,
}


def _answer(c2: str) -> str:
    verdicts = [
        {"id": "c1", "verdict": "met", "quote": "def total(xs):", "missing": None},
        {"id": "c2", "verdict": c2, "quote": None, "missing": "No docstring."},
    ]
    return json.dumps({"verdicts": verdicts})


class _FakeQwen(LLMProvider):
    """Takes a seed; same answer with it, two answers in turn without it."""

    provider_name = "dashscope"

    def __init__(self, unseeded: list[str]) -> None:
        super().__init__()
        self.requests: list[LLMRequest] = []
        self._unseeded = unseeded

    def seed_supported(self, request: LLMRequest) -> bool:
        return True

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        if request.seed is not None:
            content = _answer("not_met")
        else:
            content = self._unseeded[len(self.requests) % len(self._unseeded)]
        return LLMResponse(
            content=content,
            provider="dashscope",
            model_id=request.model,
            tokens_in=1000,
            tokens_out=100,
            tokens_reasoning=40,
            finish_reason=FinishReason.STOP,
        )


async def _measure(
    tmp_path: Path, unseeded: list[str], runs: int = 3
) -> tuple[dict[str, script.ModeSummary], _FakeQwen]:
    stage = model_stages(load_path_config(Path("config/submission_paths.yaml")))[
        "criteria_evaluation"
    ]
    provider = _FakeQwen(unseeded)
    summaries = await script.measure(
        stage,
        provider,
        load_registry(Path("config/external_services.yaml")),
        _VARIABLES,
        runs=runs,
        out_dir=tmp_path,
    )
    return summaries, provider


class TestTheSummary:
    async def test_counts_the_different_verdict_sets_per_mode(
        self, tmp_path: Path
    ) -> None:
        summaries, _ = await _measure(tmp_path, [_answer("met"), _answer("not_met")])

        seeded, unseeded = summaries["seed"], summaries["no_seed"]
        assert (seeded.calls, seeded.answers) == (3, 3)
        assert seeded.distinct_verdict_sets == 1
        assert seeded.verdict_set_counts == [3]
        assert unseeded.distinct_verdict_sets == 2
        assert unseeded.verdict_set_counts == [2, 1]
        assert unseeded.distinct_texts == 2

    async def test_an_unreadable_answer_is_counted_apart(self, tmp_path: Path) -> None:
        summaries, _ = await _measure(tmp_path, ["not json", _answer("met")], runs=4)

        unseeded = summaries["no_seed"]
        assert unseeded.unreadable == 2
        assert unseeded.distinct_verdict_sets == 1
        assert unseeded.answers == 4

    async def test_tokens_and_price_add_up(self, tmp_path: Path) -> None:
        summaries, _ = await _measure(tmp_path, [_answer("met")])
        lines = (tmp_path / "calls.jsonl").read_text(encoding="utf-8").splitlines()
        records = [json.loads(line) for line in lines]

        seeded = summaries["seed"]
        assert (seeded.tokens_in, seeded.tokens_out) == (3000, 300)
        assert seeded.tokens_reasoning == 120
        assert seeded.calls_without_price == 0
        assert Decimal(seeded.cost_usd) == sum(
            Decimal(r["cost_usd"]) for r in records if r["mode"] == "seed"
        )
        assert Decimal(seeded.cost_usd) > 0


class TestTheRequests:
    async def test_only_the_seeded_mode_sends_the_seed(self, tmp_path: Path) -> None:
        _, provider = await _measure(tmp_path, [_answer("met")])

        seeds = [r.seed for r in provider.requests]
        assert seeds == [REPRODUCIBILITY_SEED] * 3 + [None] * 3
        assert {r.temperature for r in provider.requests} == {0.0}
        assert {r.model for r in provider.requests} == {"qwen3.7-max"}

    async def test_the_two_modes_are_the_same_input_but_the_seed(
        self, tmp_path: Path
    ) -> None:
        _, provider = await _measure(tmp_path, [_answer("met")], runs=1)

        seeded, plain = provider.requests
        assert seeded.model_copy(update={"seed": None}) == plain


class TestTheFiles:
    async def test_every_call_and_the_summary_are_written(self, tmp_path: Path) -> None:
        await _measure(tmp_path, [_answer("met"), _answer("not_met")])

        lines = (tmp_path / "calls.jsonl").read_text(encoding="utf-8").splitlines()
        records = [json.loads(line) for line in lines]
        assert [(r["mode"], r["run"]) for r in records] == [
            ("seed", 1),
            ("seed", 2),
            ("seed", 3),
            ("no_seed", 1),
            ("no_seed", 2),
            ("no_seed", 3),
        ]
        assert {r["seed"] for r in records if r["mode"] == "seed"} == {
            REPRODUCIBILITY_SEED
        }
        assert all(r["content"] and r["input_hash"] for r in records)
        summary = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
        assert summary["model"] == "qwen3.7-max"
        assert summary["modes"]["no_seed"]["distinct_verdict_sets"] == 2


def test_runs_below_one_are_refused() -> None:
    with pytest.raises(SystemExit):
        script._parse(["--input", "a.json", "--out", "o", "--runs", "0"])
