"""Measure the evaluation of a repeated attempt with the memory (v2) and without (v1).

Task 13, K4. For each pair of recorded inputs — a student's first attempt and
their second at the same task — three requests to the first rung of
``criteria_evaluation`` in ``config/submission_paths.yaml``, with the stage's
seed: the first attempt with v1 (its verdicts), the second with v1, and the
second with v2 and the memory built from the first — under the ceiling of the
request's growth, as the stage puts it. No retry, no fallback, no
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

A false history (``--flip ITEM``, repeatable, on every pair): the first
attempt's ``met`` on ITEM is shown in the memory of the third call as
``not_met`` — no quote, the sentence "(flipped for the measurement)"; the
first two calls are what they were. ``summary.json`` then lists, per pair,
each flipped item — its true first verdict, what the request showed, the
verdict without the memory and with it, and whether the model judged it by
the work or by the history — and their count; an item first not met, or one
the pair has not, is noted as ``flip_skipped`` (``not_met``, ``absent``)::

    uv run python scripts/measure_attempt_memory.py \\
        --pair ~/k4/p1.first.json ~/k4/p1.second.json --flip c1 --out ~/k4/out

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
    flip_entries,
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
        "--flip",
        action="append",
        default=[],
        metavar="ITEM",
        help=(
            "show the first attempt's 'met' on ITEM as 'not_met' in the memory "
            "of every pair (a false history); repeatable"
        ),
    )
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
            context_lines=config.attempt_memory.diff_context_lines,
            max_growth=config.attempt_memory.max_growth,
            list_now=uuid.uuid4(),
            list_then=uuid.uuid4(),
            flip=args.flip,
        )
        results.append(result)
        spent += result.cost_usd
        _print(result, dry_run=args.dry_run)

    summary = summary_of(results, dry_run=args.dry_run)
    print(f"tally: {summary['tally']}")
    if args.flip:
        print(f"flips: {summary['flips']}")
    if args.out is not None:
        print(f"summary: {write_measurement(args.out, results, dry_run=args.dry_run)}")
    if args.dry_run:
        captured = all(
            call.sent.request is not None for r in results for call in r.calls
        )
        return 0 if captured else 1
    return 0 if results and all(r.answered for r in results) else 1


def _print(result: PairResult, *, dry_run: bool) -> None:
    memory, fitted = result.memory, result.fitted
    shown = (
        "no memory"
        if memory is None
        else (
            f"verdicts {None if memory.verdicts is None else len(memory.verdicts)}, "
            f"diff {None if memory.diff is None else len(memory.diff)} chars, "
            f"share {memory.changed_share}, rewritten {memory.rewritten}"
        )
    )
    ceiling = (
        ""
        if fitted is None
        else (
            f"; ceiling: reason {fitted.reason}, base {fitted.base_chars} chars, "
            f"growth {fitted.growth:.3f}, sent {fitted.sent_growth:.3f}"
        )
    )
    print(f"{result.pair.label}: {shown}{ceiling}")
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
    for flip in flip_entries(result):
        if "flip_skipped" in flip:
            print(f"  flip {flip['item']}: skipped ({flip['flip_skipped']})")
            continue
        first = "a dry run's placeholder" if flip["dry_run"] else flip["first"]
        print(
            f"  flip {flip['item']}: first {first}, in the request "
            f"{flip['shown_in_memory']} ({flip['shown_missing']!r}); judged by "
            f"{flip['judged_by']}"
        )


if __name__ == "__main__":
    sys.exit(asyncio.run(_main(_arguments(None))))
