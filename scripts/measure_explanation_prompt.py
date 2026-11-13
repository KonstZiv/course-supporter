"""Measure the explanation prompts v2 and v3 on one recorded input.

Each prompt is rendered from the recorded input exactly as the stage renders
it, and sent ONCE to the first rung of ``review_explanation`` in
``config/submission_paths.yaml`` — no retry, no fallback, no register row.
The logic is ``course_supporter.homework.explanation_measure``.

Usage (from the repository root)::

    # what would be sent, and whether the user messages are the same: no call
    uv run python scripts/measure_explanation_prompt.py RECORD.json --dry-run

    # one paid call per prompt; the first rung's key comes from the environment
    uv run python scripts/measure_explanation_prompt.py RECORD.json --out DIR

``RECORD.json`` is written by ``scripts/record_explanation_input.py``. ``DIR``
receives ``v2.answer.json``, ``v3.answer.json`` and ``summary.json``: tokens,
price from the registry (``config/external_services.yaml``), the check of
each answer against the facts, and the recorder's provenance. The record and
the answers hold a student's work: keep them out of the repository.

Exit code: 0 when every prompt got an answer that agrees with the facts,
1 otherwise (the summary says which and why).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from course_supporter.config import get_settings
from course_supporter.homework.explanation_measure import (
    PROMPT_REFS,
    STAGE_NAME,
    input_of,
    measured,
    same_user_message,
    send_once,
    write_measurement,
)
from course_supporter.homework.path_config import ModelStage, load_path_config
from course_supporter.llm.factory import create_providers
from course_supporter.llm.registry import load_registry


def _arguments(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("record", type=Path, help="the recorded input (JSON)")
    parser.add_argument("--out", type=Path, help="where to write the answers")
    parser.add_argument(
        "--prompts",
        nargs="+",
        default=list(PROMPT_REFS),
        help="prompt files to compare (default: v2 and v3)",
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
    record = json.loads(args.record.read_text(encoding="utf-8"))
    shown = input_of(record)
    stage = load_path_config(settings.submission_paths_config_path).stages[STAGE_NAME]
    if not isinstance(stage, ModelStage):
        sys.exit(f"{STAGE_NAME} is not a model stage")
    registry = load_registry(settings.external_services_path)
    rung = stage.ladder[0]

    provider = None
    if not args.dry_run:
        provider = create_providers(settings).get(rung.provider)
        if provider is None:
            sys.exit(f"no key for {rung.provider} in the environment")

    sent = [
        await send_once(shown, ref, stage=stage, registry=registry, provider=provider)
        for ref in args.prompts
    ]
    same_user = same_user_message(sent)
    if args.dry_run:
        for item in sent:
            request = item.request
            assert request is not None  # send_once raises when nothing is captured
            print(
                f"{item.prompt_ref}: {item.provider}/{request.model}, "
                f"system {len(request.system_prompt or '')} chars, "
                f"user {len(request.prompt)} chars, hash {item.request_hash}"
            )
        print(f"user messages identical: {same_user}")
        return 0 if same_user else 1

    results = [measured(item, shown.facts, registry=registry) for item in sent]
    summary = write_measurement(args.out, results, record=record, same_user=same_user)
    for result in results:
        print(
            f"{result.label}: {result.check}; tokens in {result.tokens_in}, "
            f"out {result.tokens_out}; ${result.cost_usd} ({result.cost_kind})"
        )
    print(f"summary: {summary}")
    return 0 if all(r.check == "agrees" for r in results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(_main(_arguments(None))))
