"""The stand of task 12a: a recorded submission, with synthetic replies, measured.

The two stages of the path ``repeat_with_replies`` asked about a real
submission — its inputs recorded read-only from production — with the
synthetic replies of ``data/calibration/replies/synthetic_replies_uk.yaml``
put under its remarks. Each call is ONE request to the stage's first rung
(``config/submission_paths.yaml``): no retry, no fallback, no register row,
nothing written to a database. The logic is
``course_supporter.homework.replies_measure``.

Usage (from the repository root)::

    # 1. The two inputs of one real submission (read-only, production):
    uv run python scripts/record_evaluation_input.py --submission UUID \\
        --out ~/12a/evaluation.json
    uv run python scripts/record_explanation_input.py UUID \\
        --out ~/12a/explanation.json

    # 2. What would be sent, and that no hostile text is in it — no call:
    uv run python scripts/measure_replies.py \\
        --evaluation ~/12a/evaluation.json --explanation ~/12a/explanation.json \\
        --course "Назва курсу" --dry-run

    # 3. The measurement (the first rungs' keys from the environment):
    uv run python scripts/measure_replies.py \\
        --evaluation ~/12a/evaluation.json --explanation ~/12a/explanation.json \\
        --course "Назва курсу" --var file=main.py --var line=12 \\
        --out ~/12a/out [--style-sweep]

``--var file=… --var line=…`` point the synthetic objection to a real place of
the measured work (default: the work's first file, its first line).

Calls and their price (the stages' first rung, qwen3.7-max, at the registry
rates of 2026-11-16; the stages' own arithmetic in submission_paths.yaml):
three of the evaluation — without replies, with them, the hostile ones alone
(``--no-hostile-run`` drops the third) — and one of the explanation with
replies; ``--style-sweep`` adds one explanation per style (four more). At the
largest realistic input a call of either stage is at most about $0.18 — the
whole run about $0.72, $1.45 with the sweep — plus qwen's reasoning, which no
output ceiling bounds (the stages reserve 10 000 tokens, about $0.05, a
request). A typical submission costs a few cents a call: about $0.15 a run,
$0.30 with the sweep (the median whole review on the new path was $0.109 on
2026-11-15). Every call's price is in ``summary.json``, beside its stage's
money ceiling.

Writes into ``--out``: ``<call>.answer.json`` for every call,
``verdict_changes.json`` (every change of a verdict with its quote and whether
the work has it; the hostile run's changes), ``replies.answers.json`` (every
reply: what it was, how it was answered, the answer, a joke's faults),
``jokes.md`` (style by style, each joke's answer by the model in full and the
code's check of it, ``joke_faults`` — the product owner approves the styles on
these samples) and ``summary.json`` (tokens and
price of every call against its stage's ceilings, and the checks). The
records and the output hold a student's work: keep them out of the
repository.

Exit code: 0 when no request holds a hostile reply's text, every explanation
agrees with its check, no verdict turned "met" without a quote of the work and
the hostile replies changed no verdict; 1 otherwise; 2 on a refused input.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from course_supporter.config import get_settings
from course_supporter.homework.explanation_measure import input_of
from course_supporter.homework.path_config import load_path_config, model_stages
from course_supporter.homework.replies_measure import (
    EVALUATION_STAGE,
    EVALUATION_WITH_REPLIES,
    EXPLANATION_WITH_REPLIES,
    Stand,
    default_values,
    load_synthetic,
    run_stand,
    thread_entries,
    work_files,
    write_stand,
)
from course_supporter.llm.factory import create_providers
from course_supporter.llm.registry import load_registry

_SYNTHETIC = Path("data/calibration/replies/synthetic_replies_uk.yaml")


def _arguments(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--evaluation", type=Path, required=True)
    parser.add_argument("--explanation", type=Path, required=True)
    parser.add_argument("--replies", type=Path, default=_SYNTHETIC)
    parser.add_argument(
        "--course", default=None, help="the course's name (default: the task's)"
    )
    parser.add_argument(
        "--language",
        default=None,
        help="ISO 639-3 of the explanation (default: the record's, else ukr)",
    )
    parser.add_argument(
        "--var",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help="a value for the replies' texts: file, line, course",
    )
    parser.add_argument("--out", type=Path)
    parser.add_argument("--style-sweep", action="store_true")
    parser.add_argument("--no-hostile-run", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if not args.dry_run and args.out is None:
        parser.error("--out is required unless --dry-run")
    return args


def _overrides(pairs: Sequence[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    for pair in pairs:
        name, sep, value = pair.partition("=")
        if not sep or not name:
            sys.exit(f"--var takes NAME=VALUE, got {pair!r}")
        values[name] = value
    return values


def _print_dry(stand: Stand) -> None:
    for call in stand.calls:
        print(f"{call.label}: {call.provider}/{call.model} {call.prompt_ref}")
        print(
            f"  hostile text sent: {call.hostile_text_sent}; input alone "
            f"~${call.cost_usd} ({call.cost_kind}) of the ceiling "
            f"${call.money_ceiling_usd}"
        )
    for label, plan in stand.plans.items():
        print(f"{label}: replies to answer")
        for ask in plan.asked:
            style = f", style {ask.style.id}" if ask.style is not None else ""
            print(f"  {ask.local_id} {ask.entry.comment_id} {ask.how.value}{style}")


def _print_measured(stand: Stand, summary: dict[str, Any], path: Path) -> None:
    for call in summary["calls"]:
        print(
            f"{call['label']}: tokens in {call['tokens_in']}, out {call['tokens_out']}"
            f"; ${call['cost_usd']} of the ceiling ${call['money_ceiling_usd']}"
        )
    for key in (
        "hostile_text_in_any_request",
        "verdict_changes_with_replies",
        "verdict_changes_to_met_without_a_quote_of_the_work",
        "verdict_changes_to_not_met",
        "verdict_changes_hostile_only",
        "explanations",
        "replies",
        "cost_usd_total",
    ):
        print(f"{key}: {json.dumps(summary[key], ensure_ascii=False)}")
    print(f"summary: {path}")


def _passed(summary: dict[str, Any]) -> bool:
    return (
        not summary["hostile_text_in_any_request"]
        and all(check == "agrees" for check in summary["explanations"].values())
        and summary["verdict_changes_to_met_without_a_quote_of_the_work"] == 0
        and summary["verdict_changes_hostile_only"] in (0, None)
    )


async def _main(args: argparse.Namespace) -> int:
    settings = get_settings()
    evaluation = json.loads(args.evaluation.read_text(encoding="utf-8"))
    explanation = json.loads(args.explanation.read_text(encoding="utf-8"))
    try:
        synthetic = load_synthetic(args.replies)
        shown = input_of(explanation)
    except ValueError as refusal:
        sys.stderr.write(f"refused: {refusal}\n")
        return 2

    criteria = [facts.criterion.id for facts in shown.facts.criteria]
    course = args.course or shown.task_title
    language = args.language or explanation.get("provenance", {}).get(
        "language_code", "ukr"
    )
    values = default_values(
        synthetic,
        files=work_files(str(evaluation["submission_text"])),
        course=course,
        overrides=_overrides(args.var),
    )
    entries = thread_entries(
        synthetic, criteria=criteria, unmet=list(shown.facts.unmet), values=values
    )
    stages = model_stages(load_path_config(settings.submission_paths_config_path))
    registry = load_registry(settings.external_services_path)
    providers = {} if args.dry_run else create_providers(settings)
    for name in (EVALUATION_STAGE, EVALUATION_WITH_REPLIES, EXPLANATION_WITH_REPLIES):
        rung = stages[name].ladder[0].provider
        if not args.dry_run and rung not in providers:
            sys.exit(f"no key for {rung} in the environment")

    stand = await run_stand(
        evaluation,
        explanation,
        entries,
        stages=stages,
        registry=registry,
        provider_for=providers.get,
        course=course,
        language=language,
        hostile_run=not args.no_hostile_run,
        style_sweep=args.style_sweep,
    )
    if args.dry_run:
        _print_dry(stand)
        return 0 if not any(c.hostile_text_sent for c in stand.calls) else 1

    path = write_stand(args.out, stand, record=explanation)
    summary = json.loads(path.read_text(encoding="utf-8"))
    _print_measured(stand, summary, path)
    return 0 if _passed(summary) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(_main(_arguments(None))))
