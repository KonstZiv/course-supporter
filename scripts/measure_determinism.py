"""Live measurement: how far the answers of an evaluation stage spread (2026-11-13).

The flag ``deterministic: true`` of a path stage now sends a fixed seed to the
providers that document one (``llm/reproducibility.py``). This script measures
what that buys: it asks the stage's FIRST rung the same recorded question N
times with the seed and N times without it, and counts how many different sets
of verdicts came back each way.

Usage::

    uv run python scripts/measure_determinism.py --input recorded.json --out out/
    uv run python scripts/measure_determinism.py --input recorded.json --out out/ \\
        --runs 5 --stage project_criteria_evaluation

``--input`` is a JSON object: the prompt's variables of one request of the
stage — what ``agents/criteria_evaluator.py::render_context`` returns
(``task_title``, ``task_description``, ``task_text``, ``criteria``, ``items``,
``repeat``, ``submission_text``, ``language``, ``max_quote_chars``,
``min_quote_chars``). ``items`` is also the list the answers are read against.

The stage and its first rung come from ``config/submission_paths.yaml``, the
prices from ``config/external_services.yaml``, the provider key from the app
settings (``.env``). Each request is built by the real ``StageRouter``:

* ``seed`` — the stage with ``deterministic: true``: temperature 0 and the seed;
* ``no_seed`` — the same stage with ``deterministic: false``: what every
  request of the stage was before 2026-11-13 (temperature 0, no seed).

No response validator runs, so no structural retry is paid for: an answer that
cannot be read is counted as such. Infrastructure retries of the router stay,
and each attempt is recorded.

Writes into ``--out``: ``calls.jsonl`` (one line per call: mode, run, seed,
temperature, input hash, tokens, price, finish reason, the answer, its verdict
set) and ``summary.json`` (per mode: calls, answers, how many different verdict
sets and texts, tokens, price). The summary is printed too.

Cost: ``2 x runs`` paid calls of the first rung (qwen3.7-max for both
evaluation stages): at the stage's largest realistic input about $0.17 a call
plus its reasoning (the stage's own arithmetic in submission_paths.yaml), so a
default run of 6 calls stays under about $1.40, and a typical input far under.

No register rows: the script runs outside any job, where a row has no job to
belong to (``service_logging._persist`` drops it); ``calls.jsonl`` is its
record of every call and its price.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from course_supporter.agents.criteria_evaluator import RESPONSE_SCHEMA
from course_supporter.homework.criteria_verdicts import read_answer
from course_supporter.homework.path_config import ModelStage
from course_supporter.homework.path_stages import router_stage_config
from course_supporter.llm.error_categories import (
    ErrorCategory,
    LadderExhaustedError,
    StructuralRetryError,
)
from course_supporter.llm.input_hash import hash_attempt_input
from course_supporter.llm.ladder_config import LadderConfig
from course_supporter.llm.pricing import TokenUsage, price_token_call
from course_supporter.llm.providers.base import LLMProvider
from course_supporter.llm.registry import ModelRegistryConfig
from course_supporter.llm.schemas import LLMRequest, LLMResponse
from course_supporter.llm.stage_router import StageRouter
from course_supporter.llm.token_budget import (
    estimate_request_input_tokens,
    estimate_tokens,
)

# The two evaluation stages share the answer's form (EvaluationAnswer).
STAGES = ("criteria_evaluation", "project_criteria_evaluation")
MODES = (("seed", True), ("no_seed", False))


@dataclass
class _Sent:
    """One call as the provider saw it."""

    request: LLMRequest
    started_at: datetime
    response: LLMResponse | None
    raised: Exception | None


class _Recorder(LLMProvider):
    """The real connector, with every call it makes written down."""

    def __init__(self, inner: LLMProvider) -> None:
        super().__init__()
        self._inner = inner
        self.provider_name = inner.provider_name
        self.default_max_output_tokens = inner.default_max_output_tokens
        self.sent: list[_Sent] = []

    def seed_supported(self, request: LLMRequest) -> bool:
        return self._inner.seed_supported(request)

    def check_request(self, request: LLMRequest) -> None:
        self._inner.check_request(request)

    def classify_error(self, exc: Exception) -> ErrorCategory:
        return self._inner.classify_error(exc)

    async def complete(self, request: LLMRequest) -> LLMResponse:
        started_at = datetime.now(UTC)
        try:
            response = await self._inner.complete(request)
        except Exception as exc:
            self.sent.append(_Sent(request, started_at, None, exc))
            raise
        self.sent.append(_Sent(request, started_at, response, None))
        return response


@dataclass(frozen=True)
class CallRecord:
    """One line of ``calls.jsonl``."""

    mode: str
    run: int
    provider: str
    model: str
    seed: int | None
    temperature: float
    input_hash: str
    tokens_in: int | None
    tokens_in_cached: int | None
    tokens_out: int | None
    tokens_reasoning: int | None
    finish_reason: str | None
    latency_ms: int | None
    cost_usd: str | None
    cost_kind: str
    content: str | None
    verdicts: list[list[str]] | None
    error: str | None


@dataclass
class ModeSummary:
    """What one mode's calls came to."""

    calls: int = 0
    answers: int = 0
    unreadable: int = 0
    failed_runs: int = 0
    distinct_verdict_sets: int = 0
    distinct_texts: int = 0
    verdict_set_counts: list[int] = field(default_factory=list)
    tokens_in: int = 0
    tokens_out: int = 0
    tokens_reasoning: int = 0
    cost_usd: str = "0"
    calls_without_price: int = 0


def verdict_set(content: str, expected: Sequence[str]) -> list[list[str]] | None:
    """The answer's verdicts as sorted ``[id, verdict]`` pairs; ``None`` if unread."""
    try:
        answer = read_answer(content, expected)
    except StructuralRetryError:
        return None
    return sorted([v.id, v.verdict.value] for v in answer.verdicts)


def _record(
    sent: _Sent,
    *,
    mode: str,
    run: int,
    provider: str,
    registry: ModelRegistryConfig,
    expected: Sequence[str],
) -> CallRecord:
    request, response = sent.request, sent.response
    usage = (
        TokenUsage(
            tokens_in=response.tokens_in,
            tokens_in_cached=response.tokens_in_cached,
            tokens_out=response.tokens_out,
        )
        if response is not None
        else None
    )
    priced = price_token_call(
        registry.models.get(request.model),
        catalog_version=registry.catalog_version,
        started_at=sent.started_at,
        usage=usage,
        failure=sent.raised,
        input_estimate=estimate_request_input_tokens(request),
        output_estimate=(
            estimate_tokens(response.content) if response is not None else None
        ),
    )
    content = response.content if response is not None else None
    return CallRecord(
        mode=mode,
        run=run,
        provider=provider,
        model=request.model,
        seed=request.seed,
        temperature=request.temperature,
        input_hash=hash_attempt_input(request, provider=provider),
        tokens_in=response.tokens_in if response else None,
        tokens_in_cached=response.tokens_in_cached if response else None,
        tokens_out=response.tokens_out if response else None,
        tokens_reasoning=response.tokens_reasoning if response else None,
        finish_reason=response.finish_reason.value if response else None,
        latency_ms=response.latency_ms if response else None,
        cost_usd=str(priced.cost_usd) if priced.cost_usd is not None else None,
        cost_kind=priced.cost_kind.value,
        content=content,
        verdicts=verdict_set(content, expected) if content else None,
        error=(str(sent.raised) or type(sent.raised).__name__)
        if sent.raised is not None
        else None,
    )


def summarize(records: Sequence[CallRecord], failed_runs: int = 0) -> ModeSummary:
    """Count one mode's calls: answers, distinct verdict sets, tokens, price."""
    summary = ModeSummary(calls=len(records), failed_runs=failed_runs)
    sets: Counter[str] = Counter()
    texts: set[str] = set()
    cost = Decimal(0)
    for record in records:
        summary.tokens_in += record.tokens_in or 0
        summary.tokens_out += record.tokens_out or 0
        summary.tokens_reasoning += record.tokens_reasoning or 0
        if record.cost_usd is None:
            summary.calls_without_price += 1
        else:
            cost += Decimal(record.cost_usd)
        if not record.content:
            continue
        summary.answers += 1
        texts.add(record.content)
        if record.verdicts is None:
            summary.unreadable += 1
        else:
            sets[json.dumps(record.verdicts)] += 1
    summary.distinct_verdict_sets = len(sets)
    summary.distinct_texts = len(texts)
    summary.verdict_set_counts = sorted(sets.values(), reverse=True)
    summary.cost_usd = str(cost)
    return summary


async def measure(
    stage: ModelStage,
    provider: LLMProvider,
    registry: ModelRegistryConfig,
    variables: dict[str, Any],
    *,
    runs: int,
    out_dir: Path,
    stage_name: str = "criteria_evaluation",
    prompt_base_path: Path | None = None,
) -> dict[str, ModeSummary]:
    """Ask the stage's first rung ``runs`` times per mode; write and summarise.

    ``provider`` is the connector of the first rung. Returns the summary per
    mode, as written to ``summary.json``.
    """
    config = router_stage_config(stage)
    first = config.ladder[0]
    expected = list(variables["items"])
    out_dir.mkdir(parents=True, exist_ok=True)
    summaries: dict[str, ModeSummary] = {}
    with (out_dir / "calls.jsonl").open("w", encoding="utf-8") as calls_file:
        for mode, deterministic in MODES:
            one_rung = config.model_copy(
                update={"ladder": [first], "deterministic": deterministic}
            )
            records: list[CallRecord] = []
            failed_runs = 0
            for run in range(1, runs + 1):
                recorder = _Recorder(provider)
                router = StageRouter(
                    LadderConfig(),
                    {first.provider: recorder},
                    registry=registry,
                    prompt_base_path=prompt_base_path,
                )
                try:
                    await router.execute_stage(
                        one_rung,
                        stage_name,
                        expects_json=True,
                        response_schema=RESPONSE_SCHEMA,
                        stop_on_output_ceiling=True,
                        **variables,
                    )
                except LadderExhaustedError:
                    failed_runs += 1
                for sent in recorder.sent:
                    record = _record(
                        sent,
                        mode=mode,
                        run=run,
                        provider=first.provider,
                        registry=registry,
                        expected=expected,
                    )
                    records.append(record)
                    calls_file.write(json.dumps(asdict(record), ensure_ascii=False))
                    calls_file.write("\n")
            summaries[mode] = summarize(records, failed_runs)
    document = {
        "stage": stage_name,
        "provider": first.provider,
        "model": first.model,
        "runs_per_mode": runs,
        "modes": {mode: asdict(s) for mode, s in summaries.items()},
    }
    (out_dir / "summary.json").write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return summaries


def _parse(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--stage", choices=STAGES, default=STAGES[0])
    args = parser.parse_args(argv)
    if args.runs < 1:
        parser.error("--runs must be at least 1")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    """Run the measurement against the configured provider (paid calls)."""
    from course_supporter.config import get_settings
    from course_supporter.homework.path_config import load_path_config, model_stages
    from course_supporter.llm.factory import create_providers
    from course_supporter.llm.registry import load_registry

    args = _parse(argv)
    variables = json.loads(args.input.read_text(encoding="utf-8"))
    settings = get_settings()
    registry = load_registry(settings.external_services_path)
    stage = model_stages(load_path_config(settings.submission_paths_config_path))[
        args.stage
    ]
    rung = stage.ladder[0]
    provider = create_providers(settings).get(rung.provider)
    if provider is None:
        sys.stderr.write(f"no key configured for provider '{rung.provider}'\n")
        return 2
    summaries = asyncio.run(
        measure(
            stage,
            provider,
            registry,
            variables,
            runs=args.runs,
            out_dir=args.out,
            stage_name=args.stage,
        )
    )
    sys.stdout.write(f"{args.stage} on {rung.provider}/{rung.model}\n")
    for mode, summary in summaries.items():
        sys.stdout.write(
            f"  {mode:8} answers {summary.answers}/{summary.calls}, "
            f"different verdict sets {summary.distinct_verdict_sets} "
            f"{summary.verdict_set_counts}, unreadable {summary.unreadable}, "
            f"different texts {summary.distinct_texts}, "
            f"${summary.cost_usd}\n"
        )
    sys.stdout.write(f"written to {args.out}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
