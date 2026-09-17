"""Translate the phrasebook source into every other allowed language.

Run by hand, never on a submission path::

    uv run python -m scripts.translate_phrasebook --dry-run
    uv run python -m scripts.translate_phrasebook

What it does, and what it deliberately does not:

* One call per language, the whole batch of keys in it, through the ladder
  stage ``dictionary_translation`` (one rung, ``gemini-2.5-pro``).
* It builds the router WITHOUT a session factory, so the run leaves no row in
  the call register. The register writes only inside a job, there is no job type
  for a script run, and inventing one would mean a migration (hotfix 3 probe).
  What a run costs is read from the provider's Usage page, before and after.
* It refuses to start when its own estimate is above the cost ceiling. The
  estimate prices the OUTPUT CEILING, not a guess at the answer's length, so it
  can only overstate: 60 x 8192 x $0.010 per 1k is $4.92, and the ceiling is $6.
* English goes first — it is the fallback language and the second language of
  the snapshot tests, so a broken prompt shows up on a language we can read
  rather than on the sixteenth in a row.
* Each language is written the moment its answer passes the placeholder check,
  so an interrupted run is finished by starting it again, and a mangled
  translation never reaches disk.
* A language that fails is NOT retried inside the run: a silent retry would make
  the number of calls disagree with the Usage difference, and that difference is
  the only measurement there is.
* Phrases a human has reviewed are never overwritten.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from course_supporter.config import get_settings
from course_supporter.language import display_name, get_language_registry
from course_supporter.llm.factory import create_providers
from course_supporter.llm.ladder_config import load_ladder_config
from course_supporter.llm.prompt_loader_md import load_prompt
from course_supporter.llm.registry import load_registry
from course_supporter.llm.stage_router import StageRouter
from course_supporter.llm.token_budget import estimate_tokens
from course_supporter.phrasebook import (
    FALLBACK_LANGUAGE,
    SOURCE_LANGUAGE,
    Phrase,
    load_language_file,
    placeholder_faults,
)

STAGE = "dictionary_translation"
COST_CEILING_USD = 6.0
"""Ratified 2026-09-17: one run, not one task. A repeat run translates only
what changed and costs cents."""

CONSECUTIVE_FAILURE_LIMIT = 5
"""Five languages failing in a row is a fault of the prompt, not of five
languages; the run stops instead of paying to the end."""

MACHINE_TRANSLATION_HEADER = (
    "# Machine translation, not reviewed.\n"
    "#\n"
    "# Written by scripts/translate_phrasebook.py from the Ukrainian source.\n"
    "# A phrase a human has gone over carries ``reviewed: true`` and the script\n"
    "# leaves it alone; this header stays until every phrase in the file does.\n"
)


@dataclass(frozen=True, slots=True)
class LanguagePlan:
    """One language, and the keys this run will ask for."""

    code: str
    name: str
    keys: tuple[str, ...]


@dataclass
class RunReport:
    """What the run did, in the numbers the operator checks it by."""

    planned: int = 0
    calls: int = 0
    attempts: int = 0
    written: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    lock_failures: int = 0
    stopped_early: str | None = None


def _target_path(directory: Path, code: str) -> Path:
    return directory / f"{code}.yaml"


def read_existing(directory: Path, code: str) -> dict[str, Phrase]:
    """What is already translated for this language; empty when nothing is."""
    path = _target_path(directory, code)
    return load_language_file(path) if path.exists() else {}


def plan_run(
    source: dict[str, Phrase],
    allowed: list[str],
    directory: Path,
    *,
    only_keys: tuple[str, ...] = (),
) -> list[LanguagePlan]:
    """Which languages to call, in which order, for which keys.

    English first (the fallback language), then the rest alphabetically. A
    language whose file already carries every key is skipped entirely — that is
    what makes an interrupted run finishable and a repeat run cheap. Reviewed
    phrases are never asked for again.
    """
    targets = [code for code in allowed if code not in {SOURCE_LANGUAGE}]
    ordered = ([FALLBACK_LANGUAGE] if FALLBACK_LANGUAGE in targets else []) + sorted(
        code for code in targets if code != FALLBACK_LANGUAGE
    )

    plans: list[LanguagePlan] = []
    for code in ordered:
        existing = read_existing(directory, code)
        wanted = only_keys or tuple(source)
        keys = tuple(
            key
            for key in source
            if key in wanted
            and not (key in existing and (existing[key].reviewed or not only_keys))
        )
        if keys:
            plans.append(LanguagePlan(code=code, name=display_name(code), keys=keys))
    return plans


def estimate_run_usd(
    plans: list[LanguagePlan],
    source: dict[str, Phrase],
    *,
    output_ceiling: int,
    price_in_per_1k: float,
    price_out_per_1k: float,
) -> tuple[float, int]:
    """What the run cannot cost more than, and the input tokens it is based on.

    The output side is the ceiling the rung is allowed to produce, not a guess
    at the answer: the same number the request puts on the wire as
    ``max_tokens``, so the estimate overstates rather than hopes. The input side
    is the rendered prompt of the largest batch, measured with the router's own
    estimator.
    """
    if not plans:
        return 0.0, 0
    prompt = load_prompt(f"prompts/{STAGE}/v1.md")
    widest = max(plans, key=lambda plan: len(plan.keys))
    rendered = prompt.render(
        language_name=widest.name,
        language_code=widest.code,
        source_json=_payload(source, widest.keys),
    )
    tokens_in = estimate_tokens(rendered.user or "", rendered.system)
    per_call = tokens_in / 1000 * price_in_per_1k + output_ceiling / 1000 * (
        price_out_per_1k
    )
    return per_call * len(plans), tokens_in


def _payload(source: dict[str, Phrase], keys: tuple[str, ...]) -> str:
    return json.dumps(
        {key: source[key].text for key in keys}, ensure_ascii=False, indent=2
    )


def parse_answer(content: str, keys: tuple[str, ...]) -> dict[str, str]:
    """The model's answer as phrases, or a ValueError naming what is wrong.

    The key set is checked here as well as in the prompt: a batch that comes
    back short is the failure this stage has, and it has to be caught before the
    file is written, not at the next boot.
    """
    try:
        raw = json.loads(content)
    except json.JSONDecodeError as exc:
        msg = f"answer is not JSON: {exc}"
        raise ValueError(msg) from exc
    if not isinstance(raw, dict):
        msg = f"answer is {type(raw).__name__}, not an object"
        raise ValueError(msg)
    missing = sorted(set(keys) - set(raw))
    extra = sorted(set(raw) - set(keys))
    if missing or extra:
        msg = f"answer misses {missing or 'nothing'} and invents {extra or 'nothing'}"
        raise ValueError(msg)
    bad = sorted(key for key, value in raw.items() if not isinstance(value, str))
    if bad:
        msg = f"answer has non-string values for {bad}"
        raise ValueError(msg)
    return {str(key): str(value) for key, value in raw.items()}


def write_language(
    directory: Path,
    code: str,
    phrases: dict[str, Phrase],
) -> None:
    """Write one language file, header first when anything in it is machine-made."""
    body: dict[str, Any] = {}
    for key, phrase in phrases.items():
        body[key] = (
            {"text": phrase.text, "reviewed": True} if phrase.reviewed else phrase.text
        )
    header = (
        MACHINE_TRANSLATION_HEADER
        if any(not phrase.reviewed for phrase in phrases.values())
        else ""
    )
    dumped = yaml.safe_dump(body, allow_unicode=True, sort_keys=False, width=10_000)
    _target_path(directory, code).write_text(header + dumped, encoding="utf-8")


async def translate_language(
    router: StageRouter,
    plan: LanguagePlan,
    source: dict[str, Phrase],
) -> tuple[dict[str, str], int]:
    """One call for one language: the translated phrases and the attempts spent."""
    result = await router.execute_for_stage(
        STAGE,
        expects_json=True,
        language_name=plan.name,
        language_code=plan.code,
        source_json=_payload(source, plan.keys),
    )
    return parse_answer(result.content, plan.keys), result.attempt_count


async def run(
    router: StageRouter,
    plans: list[LanguagePlan],
    source: dict[str, Phrase],
    directory: Path,
) -> RunReport:
    """Walk the plan, writing each language as it lands. No language is retried."""
    report = RunReport(planned=len(plans))
    consecutive = 0
    for plan in plans:
        try:
            translated, attempts = await translate_language(router, plan, source)
            report.calls += 1
            report.attempts += attempts
        except Exception as exc:
            report.calls += 1
            report.failed.append((plan.code, f"{type(exc).__name__}: {exc}"))
            consecutive += 1
            if report.calls == 1:
                report.stopped_early = f"the first call failed: {exc}"
                break
            if consecutive >= CONSECUTIVE_FAILURE_LIMIT:
                report.stopped_early = (
                    f"{consecutive} languages in a row failed — a fault of the "
                    f"prompt, not of {consecutive} languages"
                )
                break
            continue

        phrases = {
            key: Phrase(text=text, reviewed=False) for key, text in translated.items()
        }
        faults = placeholder_faults(
            {key: source[key] for key in plan.keys}, phrases, code=plan.code
        )
        if faults:
            report.lock_failures += 1
            report.failed.append((plan.code, "; ".join(faults)))
            consecutive += 1
            if consecutive >= CONSECUTIVE_FAILURE_LIMIT:
                report.stopped_early = (
                    f"{consecutive} languages in a row failed the placeholder "
                    f"check — a fault of the prompt, not of {consecutive} languages"
                )
                break
            continue

        merged = dict(read_existing(directory, plan.code))
        merged.update(phrases)
        write_language(directory, plan.code, merged)
        report.written.append(plan.code)
        consecutive = 0
    return report


def build_router() -> tuple[StageRouter, int, float, float]:
    """The router this script calls through, and the numbers its estimate needs.

    No session factory: the run writes no register rows (see the module
    docstring). The rung's ceiling and the model's prices come from the same
    files the boot checks read.
    """
    settings = get_settings()
    registry = load_registry(settings.external_services_path)
    ladders = load_ladder_config(settings.ladders_dir)
    stage = ladders.get_stage(STAGE)
    rung = stage.ladder[0]
    model = registry.models[rung.model]
    ceiling = rung.max_output_tokens or model.max_output_tokens or 0
    prices = model.cost_per_1k
    if prices is None:
        msg = f"Rung '{rung.model}' has no price in the registry"
        raise ValueError(msg)
    router = StageRouter(
        ladder_config=ladders,
        providers=create_providers(settings),
        registry=registry,
        session_factory=None,
    )
    return router, ceiling, prices.input, prices.output


def _report(report: RunReport, *, rtl: tuple[str, ...] = ("fas", "ara", "heb")) -> None:
    print("\n--- run report")
    print(f"  languages planned:  {report.planned}")
    print(f"  calls made:         {report.calls}")
    print(f"  ladder attempts:    {report.attempts}")
    print(f"  written:            {len(report.written)}")
    print(f"  failed:             {len(report.failed)}")
    print(f"  placeholder check:  {report.lock_failures} language(s) refused")
    for code, why in report.failed:
        print(f"    - {code}: {why}")
    if report.stopped_early:
        print(f"  STOPPED EARLY: {report.stopped_early}")
    print(
        "  right-to-left languages (nobody here reads them — the check is the proof):"
    )
    for code in rtl:
        state = "written" if code in report.written else "NOT written"
        print(f"    - {code}: {state}")


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the plan and the estimate, call nothing",
    )
    parser.add_argument(
        "--keys",
        default="",
        help="comma-separated phrase keys to translate instead of the missing ones",
    )
    args = parser.parse_args()

    settings = get_settings()
    directory = settings.phrasebook_dir
    source = load_language_file(_target_path(directory, SOURCE_LANGUAGE))
    allowed = list(get_language_registry().languages)
    only_keys = tuple(key.strip() for key in args.keys.split(",") if key.strip())
    plans = plan_run(source, allowed, directory, only_keys=only_keys)

    router, ceiling, price_in, price_out = build_router()
    estimate, tokens_in = estimate_run_usd(
        plans,
        source,
        output_ceiling=ceiling,
        price_in_per_1k=price_in,
        price_out_per_1k=price_out,
    )

    print("--- plan")
    print(f"  languages allowed:  {len(allowed)} (source '{SOURCE_LANGUAGE}' excluded)")
    print(f"  languages to call:  {len(plans)}")
    print(f"  keys per call:      {len(plans[0].keys) if plans else 0}")
    print(f"  input tokens/call:  ~{tokens_in} (estimator: characters / 3.5)")
    print(f"  output ceiling:     {ceiling} tokens/call")
    print(f"  price per 1k:       ${price_in} in / ${price_out} out")
    print(f"  ESTIMATE:           ${estimate:.2f}")
    print(f"  CEILING:            ${COST_CEILING_USD:.2f}")

    if estimate > COST_CEILING_USD:
        print(
            f"\nSTOP: the estimate ${estimate:.2f} is above the ceiling "
            f"${COST_CEILING_USD:.2f}. Nothing was called."
        )
        return 1
    if args.dry_run:
        print("\ndry run: nothing called")
        return 0
    if not plans:
        print("\nnothing to translate")
        return 0

    report = await run(router, plans, source, directory)
    _report(report)
    return 2 if report.stopped_early else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
