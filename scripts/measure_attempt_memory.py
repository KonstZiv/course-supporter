"""Measure the evaluation of a repeated attempt with the memory (v2) and without (v1).

Task 13, K4. For each pair of recorded inputs — a student's first attempt and
their second at the same task — three requests to the first rung of
``criteria_evaluation`` in ``config/submission_paths.yaml``, with the stage's
seed: the first attempt with v1 (its verdicts), the second with v1, and the
second with v2 and the memory built from the first. No retry, no fallback, no
register row, nothing written to a database. The logic is
``course_supporter.homework.attempt_memory_measure``.

Usage (from the repository root)::

    # 1. The inputs of real attempts (read-only, production), two per pair:
    uv run python scripts/record_evaluation_input.py --submission FIRST_UUID \\
        --out ~/k4/p1.first.json
    uv run python scripts/record_evaluation_input.py --submission SECOND_UUID \\
        --out ~/k4/p1.second.json

    # 2. What would be sent, and the size the memory adds — no call, no key:
    uv run python scripts/measure_attempt_memory.py \\
        --pair ~/k4/p1.first.json ~/k4/p1.second.json --dry-run

    # 3. The measurement (the first rung's key from the environment —
    #    dashscope: ALIBABA_API_KEY, and DASHSCOPE_BASE_URL as in production):
    uv run python scripts/measure_attempt_memory.py \\
        --pair ~/k4/p1.first.json ~/k4/p1.second.json \\
        --pair ~/k4/p2.first.json ~/k4/p2.second.json --out ~/k4/out

Calls and their price: three a pair; about 20 calls are seven pairs. At the
registry rates (qwen3.7-max, read 2026-11-23) a typical call — ~8 000 tokens
in, ~2 000 out — is about $0.02-0.05, a call at the largest realistic input
about $0.26; ``--budget-usd`` (default 1.50) stops before a pair once the
calls so far cost that much. Every call's price is in ``summary.json``.

Writes into ``--out``: ``<pair>.<call>.answer.json`` for every answer and
``summary.json`` — per pair the memory (its verdicts, the size of its
difference, the share of changed lines), the growth of the second request,
every item's verdict three ways and where it stands against the change, the
tally of kept and changed verdicts on the items the change touched and those
it did not, the stale quotes, the calls' tokens and price. The records and
the output hold a student's work: keep them out of the repository.

Exit code: 0 when every call was answered (a dry run: captured); 1 otherwise;
2 on a refused input.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from decimal import Decimal
from pathlib import Path

from course_supporter.config import get_settings
from course_supporter.homework.attempt_memory_measure import (
    STAGE_NAME,
    PairResult,
    load_pair,
    measure_pair,
    summary_of,
    write_measurement,
)
from course_supporter.homework.path_config import ModelStage, load_path_config
from course_supporter.llm.factory import create_providers
from course_supporter.llm.registry import load_registry


def _arguments(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--pair",
        nargs=2,
        action="append",
        type=Path,
        required=True,
        metavar=("FIRST", "SECOND"),
        help="two recorded inputs: the first attempt's and the second's",
    )
    parser.add_argument("--out", type=Path, help="where to write the answers")
    parser.add_argument(
        "--budget-usd",
        type=Decimal,
        default=Decimal("1.50"),
        help="stop before a pair once the calls so far cost this much",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="render and capture the requests; call nothing",
    )
    args = parser.parse_args(argv)
    if not args.dry_run and args.out is None:
        parser.error("--out is required unless --dry-run")
    return args


async def _main(args: argparse.Namespace) -> int:
    settings = get_settings()
    config = load_path_config(settings.submission_paths_config_path)
    stage = config.stages[STAGE_NAME]
    if not isinstance(stage, ModelStage):
        print(f"{STAGE_NAME} is not a model stage", file=sys.stderr)
        return 2
    try:
        pairs = [
            load_pair(first, second, label=f"p{n}")
            for n, (first, second) in enumerate(args.pair, start=1)
        ]
    except (OSError, ValueError) as refused:
        print(f"refused: {refused}", file=sys.stderr)
        return 2
    registry = load_registry(settings.external_services_path)

    provider = None
    if not args.dry_run:
        provider = create_providers(settings).get(stage.ladder[0].provider)
        if provider is None:
            print(f"no key for {stage.ladder[0].provider}", file=sys.stderr)
            return 2

    results: list[PairResult] = []
    spent = Decimal(0)
    for pair in pairs:
        if spent >= args.budget_usd:
            print(f"{pair.label}: not sent — ${spent} spent of ${args.budget_usd}")
            continue
        result = await measure_pair(
            pair,
            stage=stage,
            registry=registry,
            provider=provider,
            rewritten_share=config.attempt_memory.rewritten_share,
            list_now=uuid.uuid4(),
            list_then=uuid.uuid4(),
        )
        results.append(result)
        spent += result.cost_usd
        _print(result, dry_run=args.dry_run)

    summary = summary_of(results, dry_run=args.dry_run)
    print(f"tally: {summary['tally']}")
    if args.out is not None:
        print(f"summary: {write_measurement(args.out, results, dry_run=args.dry_run)}")
    if args.dry_run:
        captured = all(
            call.sent.request is not None for r in results for call in r.calls
        )
        return 0 if captured else 1
    return 0 if results and all(r.answered for r in results) else 1


def _print(result: PairResult, *, dry_run: bool) -> None:
    memory = result.memory
    shown = (
        "no memory"
        if memory is None
        else (
            f"verdicts {None if memory.verdicts is None else len(memory.verdicts)}, "
            f"diff {None if memory.diff is None else len(memory.diff)} chars, "
            f"share {memory.changed_share}, rewritten {memory.rewritten}"
        )
    )
    print(f"{result.pair.label}: {shown}")
    for call in result.calls:
        entry = call.as_json()
        print(
            f"  {call.label} {call.prompt_ref}: request {entry['request_chars']} "
            f"chars, hash {entry['request_hash']}"
            + (
                ""
                if dry_run
                else f"; tokens in {entry['tokens_in']}, out {entry['tokens_out']}; "
                f"${entry['cost_usd']}"
            )
        )


if __name__ == "__main__":
    sys.exit(asyncio.run(_main(_arguments(None))))
